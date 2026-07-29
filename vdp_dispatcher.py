import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import CallbackContext

import config
from database import Database
from vdp_client import get_funding_activity, get_vdp_validators, rpc_eth_call


logger = logging.getLogger(__name__)

BOOTSTRAP_ENTITY_TYPE = "__meta__"
VALIDATOR_BOOTSTRAP_KEY = "vdp_validators_bootstrap_complete"
FUNDING_BOOTSTRAP_KEY = "vdp_funding_bootstrap_complete"
FOUNDATION_DELEGATION_BOOTSTRAP_KEY = "vdp_foundation_delegations_bootstrap_complete"
FOUNDATION_DELEGATION_LAST_SCAN_KEY = "vdp_foundation_delegations_last_scan"
MEANINGFUL_STAKE_DELTA_MON = 1_000_000.0
ROTATION_MATCH_TOLERANCE_MON = 250_000.0
FOUNDATION_DELEGATION_TOLERANCE_MON = 250_000.0
ROTATION_DELTA_TARGET_MON = 2_000_000.0
ROTATION_DELTA_TOLERANCE_MON = 400_000.0
ROTATION_REMAINING_TARGET_MON = 9_000_000.0
ROTATION_REMAINING_TOLERANCE_MON = 750_000.0
ROTATION_ACTIVE_TARGET_MON = 11_000_000.0
ROTATION_ACTIVE_TOLERANCE_MON = 750_000.0

STAKE_METRICS = (
    ("consensus_stake_mon", "Consensus"),
    ("snapshot_stake_mon", "Snapshot"),
)

FOUNDATION_DELEGATOR_WALLETS = (
    ("0xfa735cca8424e4ef30980653bf9015331d9929db", "Foundation wallet 1"),
    ("0xf235ab9b2f80a9569079c0d62aab91024f4dd61e", "Foundation wallet 2"),
)

SELECTOR_GET_DELEGATIONS = "0x4fd66050"
SELECTOR_GET_DELEGATOR = "0x573c1ce0"


def _bootstrap_done(db: Database, entity_key: str) -> bool:
    return bool(db.get_seen_state(BOOTSTRAP_ENTITY_TYPE, entity_key))


def _mark_bootstrap_done(db: Database, entity_key: str):
    db.set_seen_state(BOOTSTRAP_ENTITY_TYPE, entity_key, {"done": True})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _mark_meta_timestamp(db: Database, entity_key: str):
    db.set_seen_state(BOOTSTRAP_ENTITY_TYPE, entity_key, {"ts": _utc_now().isoformat()})


def _meta_timestamp(db: Database, entity_key: str) -> datetime | None:
    state = db.get_seen_state(BOOTSTRAP_ENTITY_TYPE, entity_key)
    if not state:
        return None
    raw = state.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _should_scan_foundation_delegations(db: Database) -> bool:
    last_scan = _meta_timestamp(db, FOUNDATION_DELEGATION_LAST_SCAN_KEY)
    if last_scan is None:
        return True
    return (_utc_now() - last_scan) >= timedelta(seconds=config.FOUNDATION_DELEGATION_SCAN_SECONDS)


def _as_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _validator_state(row: dict) -> dict:
    return {
        "address": (row.get("address") or "").lower(),
        "lifecycle_status": row.get("lifecycle_status"),
        "participation_state": row.get("participation_state"),
        "validator_id": row.get("validator_id"),
        "approved": row.get("approval_date"),
        "registered": row.get("registration_date"),
        "funding_source": row.get("funding_source"),
        "stake_mon": _as_float(row.get("stake_mon")),
        "consensus_stake_mon": _as_float(row.get("consensus_stake_mon")),
        "snapshot_stake_mon": _as_float(row.get("snapshot_stake_mon")),
    }


def _format_amount_mon(value: float) -> str:
    absolute = abs(float(value or 0.0))
    if absolute >= 1_000_000:
        return f"{absolute / 1_000_000:.2f}M MON"
    if absolute >= 1_000:
        return f"{absolute:,.0f} MON"
    return f"{absolute:.2f} MON"


def _format_delta_mon(delta: float) -> str:
    sign = "+" if delta >= 0 else "-"
    return f"{sign}{_format_amount_mon(delta)}"


def _normalize_address(value: str | None) -> str:
    return (value or "").strip().lower()


def _strip_0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


def _encode_uint_word(value: int) -> str:
    return f"{int(value):064x}"


def _encode_address_word(address: str) -> str:
    return _strip_0x(_normalize_address(address)).rjust(64, "0")


def _decode_u256_word(words: list[int], index: int) -> int:
    return int.from_bytes(words[index], byteorder="big")


def _hex_to_words(raw_result: str) -> list[bytes]:
    payload = _strip_0x(raw_result or "")
    if len(payload) % 64 != 0:
        raise ValueError("eth_call payload is not word-aligned")
    return [bytes.fromhex(payload[i : i + 64]) for i in range(0, len(payload), 64)]


async def _staking_eth_call(selector: str, encoded_args: str) -> list[bytes] | None:
    raw_result = await rpc_eth_call(
        config.STAKING_PRECOMPILE_ADDRESS,
        f"{selector}{encoded_args}",
    )
    if not raw_result:
        return None
    return _hex_to_words(raw_result)


async def _get_delegations_page(delegator: str, start_val_id: int = 0) -> tuple[bool, int, list[int]] | None:
    encoded_args = f"{_encode_address_word(delegator)}{_encode_uint_word(start_val_id)}"
    words = await _staking_eth_call(SELECTOR_GET_DELEGATIONS, encoded_args)
    if not words or len(words) < 4:
        return None

    is_done = _decode_u256_word(words, 0) != 0
    next_val_id = _decode_u256_word(words, 1)
    offset_bytes = _decode_u256_word(words, 2)
    array_index = offset_bytes // 32
    if array_index >= len(words):
        return None
    array_len = _decode_u256_word(words, array_index)
    validator_ids = [
        _decode_u256_word(words, array_index + 1 + index)
        for index in range(array_len)
        if array_index + 1 + index < len(words)
    ]
    return is_done, next_val_id, validator_ids


async def _get_all_delegation_ids(delegator: str) -> list[int] | None:
    all_ids: list[int] = []
    next_val_id = 0
    while True:
        page = await _get_delegations_page(delegator, next_val_id)
        if page is None:
            return None
        is_done, next_val_id, validator_ids = page
        all_ids.extend(int(item) for item in validator_ids)
        if is_done:
            return sorted(set(all_ids))


async def _get_delegator_state(delegator: str, validator_id: int) -> dict | None:
    encoded_args = f"{_encode_uint_word(validator_id)}{_encode_address_word(delegator)}"
    words = await _staking_eth_call(SELECTOR_GET_DELEGATOR, encoded_args)
    if not words or len(words) < 7:
        return None

    stake = _decode_u256_word(words, 0)
    delta_stake = _decode_u256_word(words, 3)
    next_delta_stake = _decode_u256_word(words, 4)
    total_stake_wei = stake + delta_stake + next_delta_stake
    return {
        "validator_id": int(validator_id),
        "stake_wei": stake,
        "delta_stake_wei": delta_stake,
        "next_delta_stake_wei": next_delta_stake,
        "total_stake_wei": total_stake_wei,
        "total_stake_mon": total_stake_wei / 1_000_000_000_000_000_000,
        "delta_epoch": _decode_u256_word(words, 5),
        "next_delta_epoch": _decode_u256_word(words, 6),
    }


async def _get_wallet_delegation_state(wallet_address: str, wallet_label: str) -> dict | None:
    validator_ids = await _get_all_delegation_ids(wallet_address)
    if validator_ids is None:
        return None

    delegations: dict[str, dict] = {}
    for validator_id in validator_ids:
        state = await _get_delegator_state(wallet_address, validator_id)
        if state is None:
            return None
        if state["total_stake_wei"] <= 0:
            continue
        delegations[str(int(validator_id))] = state

    return {
        "wallet_address": wallet_address,
        "wallet_label": wallet_label,
        "delegations": delegations,
    }


def _build_watchlist_text(base_text: str, watch_matches: list[dict]) -> str:
    if not watch_matches:
        return base_text

    descriptors: list[str] = []
    for match in watch_matches:
        if match.get("label"):
            descriptors.append(str(match["label"]))
        else:
            descriptors.append(f"{match['watch_type']}: {match['watch_value']}")
    watch_line = ", ".join(sorted(set(descriptors)))
    return f"*Watchlist match*\n{watch_line}\n\n{base_text}"


def _collect_watchlist_matches(db: Database, address: str | None, validator_id) -> dict[int, list[dict]]:
    match_map: dict[int, list[dict]] = {}
    if address:
        for match in db.get_matching_watchlist_entries("address", address.lower()):
            match_map.setdefault(int(match["telegram_user_id"]), []).append(match)
    if validator_id is not None:
        for match in db.get_matching_watchlist_entries("validator_id", str(validator_id)):
            match_map.setdefault(int(match["telegram_user_id"]), []).append(match)
    return match_map


def _collect_watchlist_matches_for_entities(
    db: Database,
    entities: list[tuple[str | None, object | None]],
) -> dict[int, list[dict]]:
    merged: dict[int, list[dict]] = {}
    for address, validator_id in entities:
        partial = _collect_watchlist_matches(db, address, validator_id)
        for telegram_user_id, entries in partial.items():
            merged.setdefault(telegram_user_id, []).extend(entries)
    return merged


async def _send_to_topic(
    context: CallbackContext,
    db: Database,
    topic_key: str,
    text: str,
    *,
    address: str | None = None,
    validator_id=None,
    related_entities: list[tuple[str | None, object | None]] | None = None,
):
    entities: list[tuple[str | None, object | None]] = []
    if address is not None or validator_id is not None:
        entities.append((address, validator_id))
    if related_entities:
        entities.extend(related_entities)

    watchlist_matches = _collect_watchlist_matches_for_entities(db, entities)
    recipients = set(db.get_active_vdp_subscribers(topic_key)) | set(watchlist_matches.keys())
    if not recipients:
        return

    for telegram_user_id in recipients:
        message_text = text
        if telegram_user_id in watchlist_matches:
            message_text = _build_watchlist_text(text, watchlist_matches[telegram_user_id])
        try:
            await context.bot.send_message(
                chat_id=telegram_user_id,
                text=message_text,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Failed to deliver %s to %s: %s", topic_key, telegram_user_id, exc)


def _format_validator_ref(state: dict) -> str:
    address = state.get("address") or "-"
    validator_id = state.get("validator_id")
    if validator_id is None:
        return f"Address: `{address}`"
    return f"Address: `{address}`\nValidator ID: `#{validator_id}`"


def _format_stake_band(state: dict) -> str:
    return (
        f"Consensus: `{_format_amount_mon(state.get('consensus_stake_mon', 0.0))}`\n"
        f"Snapshot: `{_format_amount_mon(state.get('snapshot_stake_mon', 0.0))}`\n"
        f"Current total: `{_format_amount_mon(state.get('stake_mon', 0.0))}`"
    )


def _format_before_after(previous_value: float, current_value: float) -> str:
    return f"`{_format_amount_mon(previous_value)}` -> `{_format_amount_mon(current_value)}`"


def _format_participation_transition(previous: dict, state: dict) -> str:
    prev_participation = previous.get("participation_state") or "unknown"
    curr_participation = state.get("participation_state") or "unknown"
    return f"Transition: `{prev_participation}` -> `{curr_participation}`"


def _format_transition_stake_lines(previous: dict, state: dict) -> str:
    previous_consensus = _as_float(previous.get("consensus_stake_mon"))
    current_consensus = _as_float(state.get("consensus_stake_mon"))
    previous_snapshot = _as_float(previous.get("snapshot_stake_mon"))
    current_snapshot = _as_float(state.get("snapshot_stake_mon"))
    previous_total = _as_float(previous.get("stake_mon"))
    current_total = _as_float(state.get("stake_mon"))

    return (
        f"Consensus: {_format_before_after(previous_consensus, current_consensus)}\n"
        f"Snapshot: {_format_before_after(previous_snapshot, current_snapshot)}\n"
        f"Total: {_format_before_after(previous_total, current_total)}"
    )


def _within_band(value: float, target: float, tolerance: float) -> bool:
    return abs(float(value or 0.0) - target) <= tolerance


def _is_rotation_like_removal(previous: dict, state: dict, previous_value: float, current_value: float, delta: float) -> bool:
    return (
        delta <= -MEANINGFUL_STAKE_DELTA_MON
        and _within_band(abs(delta), ROTATION_DELTA_TARGET_MON, ROTATION_DELTA_TOLERANCE_MON)
        and _within_band(current_value, ROTATION_REMAINING_TARGET_MON, ROTATION_REMAINING_TOLERANCE_MON)
        and previous.get("participation_state") == "active-set"
        and state.get("participation_state") != "active-set"
        and _within_band(previous_value, ROTATION_ACTIVE_TARGET_MON, ROTATION_ACTIVE_TOLERANCE_MON)
    )


def _is_rotation_like_addition(previous: dict, state: dict, previous_value: float, current_value: float, delta: float) -> bool:
    return (
        delta >= MEANINGFUL_STAKE_DELTA_MON
        and _within_band(abs(delta), ROTATION_DELTA_TARGET_MON, ROTATION_DELTA_TOLERANCE_MON)
        and _within_band(current_value, ROTATION_ACTIVE_TARGET_MON, ROTATION_ACTIVE_TOLERANCE_MON)
        and previous.get("participation_state") != "active-set"
        and state.get("participation_state") == "active-set"
        and _within_band(previous_value, ROTATION_REMAINING_TARGET_MON, ROTATION_REMAINING_TOLERANCE_MON)
    )


def _rotation_pattern_note(previous: dict, state: dict, previous_value: float, current_value: float, delta: float) -> str:
    if _is_rotation_like_removal(previous, state, previous_value, current_value, delta):
        return (
            "*Pattern*\n"
            "- High-confidence rotation pattern\n"
            "- Consensus stake moved roughly `11M -> 9M`\n"
            "- Validator moved from `active-set` to outside the active set\n\n"
        )
    if _is_rotation_like_addition(previous, state, previous_value, current_value, delta):
        return (
            "*Pattern*\n"
            "- High-confidence rotation pattern\n"
            "- Consensus stake moved roughly `9M -> 11M`\n"
            "- Validator moved into `active-set`\n\n"
        )
    return ""


def _pair_rotation_confidence(removed_previous: dict, removed: dict, added_previous: dict, added: dict) -> str:
    removal_confident = _is_rotation_like_removal(
        removed_previous,
        removed,
        _as_float(removed_previous.get("consensus_stake_mon")),
        _as_float(removed.get("consensus_stake_mon")),
        _as_float(removed.get("consensus_stake_mon")) - _as_float(removed_previous.get("consensus_stake_mon")),
    )
    addition_confident = _is_rotation_like_addition(
        added_previous,
        added,
        _as_float(added_previous.get("consensus_stake_mon")),
        _as_float(added.get("consensus_stake_mon")),
        _as_float(added.get("consensus_stake_mon")) - _as_float(added_previous.get("consensus_stake_mon")),
    )

    if removal_confident and addition_confident:
        return (
            "*Pattern*\n"
            "- High-confidence rotation pattern\n"
            "- Removed side matches `~11M -> ~9M`\n"
            "- Added side matches `~9M -> ~11M`\n\n"
        )
    return ""


def _build_stake_change_event(
    state: dict,
    previous: dict,
    metric_key: str,
    metric_label: str,
    delta: float,
) -> dict:
    return {
        "state": state,
        "previous": previous,
        "metric_key": metric_key,
        "metric_label": metric_label,
        "delta": delta,
        "previous_value": _as_float(previous.get(metric_key)),
        "current_value": _as_float(state.get(metric_key)),
    }


def _event_signature_from_parts(address: str | None, validator_id, metric_key: str) -> tuple[str, object, str]:
    return ((address or ""), validator_id, metric_key)


def _build_validator_index(validators: list[dict]) -> dict[int, dict]:
    index: dict[int, dict] = {}
    for row in validators:
        validator_id = row.get("validator_id")
        if validator_id is None:
            continue
        index[int(validator_id)] = _validator_state(row)
    return index


def _validator_context(validator_index: dict[int, dict], validator_id: int) -> dict:
    state = validator_index.get(int(validator_id))
    if state:
        return state
    return {
        "address": None,
        "validator_id": int(validator_id),
        "funding_source": "Unknown",
        "lifecycle_status": "registered",
        "participation_state": "unknown",
        "stake_mon": 0.0,
        "consensus_stake_mon": 0.0,
        "snapshot_stake_mon": 0.0,
    }


def _pair_stake_changes(removals: list[dict], additions: list[dict]) -> list[dict]:
    remaining_additions = additions.copy()
    pairs: list[dict] = []

    for removal in sorted(removals, key=lambda event: abs(event["delta"]), reverse=True):
        best_index = None
        best_gap = None
        removal_size = abs(removal["delta"])
        for index, addition in enumerate(remaining_additions):
            gap = abs(removal_size - abs(addition["delta"]))
            if gap > ROTATION_MATCH_TOLERANCE_MON:
                continue
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_index = index

        if best_index is None:
            continue

        addition = remaining_additions.pop(best_index)
        pairs.append({"removed": removal, "added": addition, "gap": best_gap or 0.0})

    return pairs


def _paired_signatures(pairs: list[dict]) -> set[tuple[str, object, str]]:
    signatures: set[tuple[str, object, str]] = set()
    for pair in pairs:
        for key in ("removed", "added"):
            event = pair[key]
            state = event["state"]
            signatures.add(_event_signature_from_parts(state.get("address"), state.get("validator_id"), event["metric_key"]))
    return signatures


def _should_suppress_low_signal_stake_event(event: dict) -> bool:
    if event["metric_key"] != "snapshot_stake_mon":
        return False

    previous = event["previous"]
    state = event["state"]
    prev_participation = previous.get("participation_state")
    curr_participation = state.get("participation_state")
    previous_consensus = _as_float(previous.get("consensus_stake_mon"))
    current_consensus = _as_float(state.get("consensus_stake_mon"))

    return (
        prev_participation != "active-set"
        and curr_participation != "active-set"
        and previous_consensus < 1.0
        and current_consensus < 1.0
    )


async def _dispatch_stake_change_events(
    context: CallbackContext,
    db: Database,
    stake_change_events: list[dict],
    suppressed_signatures: set[tuple[str, object, str]] | None = None,
):
    if not stake_change_events:
        return

    suppressed_signatures = suppressed_signatures or set()

    consensus_removals = [
        event
        for event in stake_change_events
        if _event_signature_from_parts(
            event["state"].get("address"),
            event["state"].get("validator_id"),
            event["metric_key"],
        )
        not in suppressed_signatures
        if event["metric_key"] == "consensus_stake_mon" and event["delta"] <= -MEANINGFUL_STAKE_DELTA_MON
    ]
    consensus_additions = [
        event
        for event in stake_change_events
        if _event_signature_from_parts(
            event["state"].get("address"),
            event["state"].get("validator_id"),
            event["metric_key"],
        )
        not in suppressed_signatures
        if event["metric_key"] == "consensus_stake_mon" and event["delta"] >= MEANINGFUL_STAKE_DELTA_MON
    ]

    paired_consensus = _pair_stake_changes(consensus_removals, consensus_additions)
    paired_consensus = [
        pair
        for pair in paired_consensus
        if pair["removed"]["state"].get("funding_source") == "VDP"
        or pair["added"]["state"].get("funding_source") == "VDP"
    ]
    paired_event_signatures = _paired_signatures(paired_consensus)

    for pair in paired_consensus:
        removed = pair["removed"]
        added = pair["added"]
        removed_state = removed["state"]
        added_state = added["state"]
        pattern_note = _pair_rotation_confidence(
            removed["previous"],
            removed_state,
            added["previous"],
            added_state,
        )
        text = (
            f"*{'High-confidence rotation pair detected' if pattern_note else 'Possible rotation pair detected'}*\n\n"
            f"{pattern_note}"
            "*Stake removed from*\n"
            f"{_format_validator_ref(removed_state)}\n"
            f"Funding source: `{removed_state.get('funding_source') or '-'}`\n"
            f"Consensus change: `{_format_delta_mon(removed['delta'])}`\n"
            f"Before: `{_format_amount_mon(removed['previous_value'])}`\n"
            f"After: `{_format_amount_mon(removed['current_value'])}`\n\n"
            "*Stake added to*\n"
            f"{_format_validator_ref(added_state)}\n"
            f"Funding source: `{added_state.get('funding_source') or '-'}`\n"
            f"Consensus change: `{_format_delta_mon(added['delta'])}`\n"
            f"Before: `{_format_amount_mon(added['previous_value'])}`\n"
            f"After: `{_format_amount_mon(added['current_value'])}`\n\n"
            "This is an inferred pair from the same polling cycle and matching stake size, not a direct transaction decode."
        )
        await _send_to_topic(
            context,
            db,
            "vdp.rotation_pair_detected",
            text,
            related_entities=[
                (removed_state.get("address"), removed_state.get("validator_id")),
                (added_state.get("address"), added_state.get("validator_id")),
            ],
        )

    for event in stake_change_events:
        if event["state"].get("funding_source") != "VDP":
            continue
        signature = _event_signature_from_parts(
            event["state"].get("address"),
            event["state"].get("validator_id"),
            event["metric_key"],
        )
        if signature in paired_event_signatures or signature in suppressed_signatures:
            continue
        if _should_suppress_low_signal_stake_event(event):
            continue

        metric_label = event["metric_label"]
        previous_value = event["previous_value"]
        current_value = event["current_value"]
        state = event["state"]
        if event["delta"] >= MEANINGFUL_STAKE_DELTA_MON:
            title = f"{metric_label} stake added"
            topic_key = f"vdp.{event['metric_key'].replace('_mon', '')}_added"
        elif event["delta"] <= -MEANINGFUL_STAKE_DELTA_MON:
            title = f"{metric_label} stake removed"
            topic_key = f"vdp.{event['metric_key'].replace('_mon', '')}_removed"
        else:
            continue

        active_set_note = ""
        previous_participation = event["previous"].get("participation_state")
        current_participation = state.get("participation_state")
        if previous_participation == "active-set" and current_participation == "active-set":
            active_set_note = "Validator remains in `active-set` after this change.\n"
        elif previous_participation != "active-set" and current_participation != "active-set":
            active_set_note = "Validator remains outside `active-set` after this change.\n"

        text = (
            f"*{title}*\n\n"
            f"{_rotation_pattern_note(event['previous'], state, previous_value, current_value, event['delta'])}"
            f"{_format_validator_ref(state)}\n"
            f"Funding source: `{state.get('funding_source') or '-'}`\n"
            f"Participation: `{state.get('participation_state') or '-'}`\n"
            f"{_format_participation_transition(event['previous'], state)}\n"
            f"{active_set_note}"
            f"{'Added' if event['delta'] >= 0 else 'Removed'}: `{_format_amount_mon(event['delta'])}`\n"
            f"{metric_label} band: {_format_before_after(previous_value, current_value)}\n"
            f"Current {metric_label.lower()}: `{_format_amount_mon(current_value)}`\n\n"
            f"{_format_stake_band(state)}"
        )
        await _send_to_topic(
            context,
            db,
            topic_key,
            text,
            address=state.get("address"),
            validator_id=state.get("validator_id"),
        )


async def _process_foundation_delegations(
    context: CallbackContext,
    db: Database,
    bootstrap: bool,
    validator_index: dict[int, dict],
) -> bool:
    if not bootstrap and not _should_scan_foundation_delegations(db):
        return True

    increases: list[dict] = []
    decreases: list[dict] = []

    for wallet_address, wallet_label in FOUNDATION_DELEGATOR_WALLETS:
        current_state = await _get_wallet_delegation_state(wallet_address, wallet_label)
        if current_state is None:
            return False

        previous_state = db.get_seen_state("foundation_delegations", wallet_address)
        db.set_seen_state("foundation_delegations", wallet_address, current_state)

        if bootstrap or previous_state is None:
            continue

        previous_map = previous_state.get("delegations", {}) if isinstance(previous_state, dict) else {}
        current_map = current_state.get("delegations", {})
        validator_ids = sorted(set(previous_map) | set(current_map), key=lambda item: int(item))

        for validator_id_raw in validator_ids:
            previous_item = previous_map.get(validator_id_raw) or {}
            current_item = current_map.get(validator_id_raw) or {}
            previous_total = _as_float(previous_item.get("total_stake_mon"))
            current_total = _as_float(current_item.get("total_stake_mon"))
            delta = current_total - previous_total
            if abs(delta) < MEANINGFUL_STAKE_DELTA_MON:
                continue

            validator_id = int(validator_id_raw)
            state = _validator_context(validator_index, validator_id)
            event = {
                "wallet_address": wallet_address,
                "wallet_label": wallet_label,
                "validator_id": validator_id,
                "state": state,
                "previous_total": previous_total,
                "current_total": current_total,
                "delta": delta,
            }
            if delta > 0:
                increases.append(event)
            else:
                decreases.append(event)

    pairs = _pair_stake_changes(decreases, increases)
    for pair in pairs:
        removed = pair["removed"]
        added = pair["added"]
        removed_state = removed["state"]
        added_state = added["state"]
        if removed_state.get("funding_source") != "VDP" and added_state.get("funding_source") != "VDP":
            continue
        if abs(abs(removed["delta"]) - abs(added["delta"])) > FOUNDATION_DELEGATION_TOLERANCE_MON:
            continue

        removal_rotation_like = (
            _within_band(abs(removed["delta"]), ROTATION_DELTA_TARGET_MON, ROTATION_DELTA_TOLERANCE_MON)
            and _within_band(removed["current_total"], ROTATION_REMAINING_TARGET_MON, ROTATION_REMAINING_TOLERANCE_MON)
        )
        addition_rotation_like = (
            _within_band(abs(added["delta"]), ROTATION_DELTA_TARGET_MON, ROTATION_DELTA_TOLERANCE_MON)
            and _within_band(added["current_total"], ROTATION_ACTIVE_TARGET_MON, ROTATION_ACTIVE_TOLERANCE_MON)
        )
        pattern_note = ""
        if removal_rotation_like and addition_rotation_like:
            pattern_note = (
                "*Pattern*\n"
                "- High-confidence rotation pattern\n"
                "- Foundation delegation moved roughly `~2M`\n"
                "- Removed side now sits near `~9M`\n"
                "- Added side now sits near `~11M`\n\n"
            )

        text = (
            f"*{'High-confidence Foundation rotation detected' if pattern_note else 'Foundation delegation shift detected'}*\n\n"
            f"{pattern_note}"
            f"Removed via *{removed['wallet_label']}*\n"
            f"{_format_validator_ref(removed_state)}\n"
            f"Funding source: `{removed_state.get('funding_source') or '-'}`\n"
            f"Delegation change: `{_format_delta_mon(removed['delta'])}`\n"
            f"Before: `{_format_amount_mon(removed['previous_total'])}`\n"
            f"After: `{_format_amount_mon(removed['current_total'])}`\n\n"
            f"Added via *{added['wallet_label']}*\n"
            f"{_format_validator_ref(added_state)}\n"
            f"Funding source: `{added_state.get('funding_source') or '-'}`\n"
            f"Delegation change: `{_format_delta_mon(added['delta'])}`\n"
            f"Before: `{_format_amount_mon(added['previous_total'])}`\n"
            f"After: `{_format_amount_mon(added['current_total'])}`\n\n"
            "This comes from Foundation wallet delegation state read directly from the staking precompile. It is stronger than a validator-state inference, but still not a direct transaction decode."
        )
        await _send_to_topic(
            context,
            db,
            "vdp.rotation_pair_detected",
            text,
            related_entities=[
                (removed_state.get("address"), removed_state.get("validator_id")),
                (added_state.get("address"), added_state.get("validator_id")),
            ],
        )

    _mark_meta_timestamp(db, FOUNDATION_DELEGATION_LAST_SCAN_KEY)
    return True


async def _process_vdp_validators(
    context: CallbackContext,
    db: Database,
    bootstrap: bool,
    skip_approval_addresses: set[str] | None = None,
) -> tuple[bool, dict[int, dict]]:
    """
    `skip_approval_addresses` is a lower-cased set of recipient addresses for
    which a Foundation funding alert already fired in this dispatch cycle with
    state=approved. For those addresses we suppress the duplicate
    `vdp.new_vdp_approval` send (the funding alert already carries the
    "New VDP approval (Foundation funding)" header).
    """
    skip_approval_addresses = skip_approval_addresses or set()
    validators = await get_vdp_validators()
    if not validators:
        return False, {}

    stake_change_events: list[dict] = []
    transition_suppressed_signatures: set[tuple[str, object, str]] = set()
    validator_index = _build_validator_index(validators)

    for row in validators:
        is_vdp_row = row.get("funding_source") == "VDP"
        state = _validator_state(row)
        address = state["address"]
        if not address:
            continue

        previous = db.get_seen_state("vdp_validator", address)
        db.set_seen_state("vdp_validator", address, state)

        if not bootstrap and previous is None:
            if is_vdp_row and state["lifecycle_status"] == "approved":
                address_lower = str(state.get("address") or "").lower()
                if address_lower and address_lower in skip_approval_addresses:
                    logger.debug(
                        "Skipping duplicate vdp.new_vdp_approval for %s "
                        "(funding alert already carried the approval header)",
                        address_lower,
                    )
                else:
                    text = (
                        "*New VDP approval*\n\n"
                        f"{_format_validator_ref(state)}\n"
                        f"Approved: `{state.get('approved') or '-'}`"
                    )
                    await _send_to_topic(
                        context,
                        db,
                        "vdp.new_vdp_approval",
                        text,
                        address=state.get("address"),
                        validator_id=state.get("validator_id"),
                    )
            elif is_vdp_row and state["lifecycle_status"] == "registered":
                text = (
                    "*New validator registration*\n\n"
                    f"{_format_validator_ref(state)}\n"
                    f"Registered: `{state.get('registered') or '-'}`"
                )
                await _send_to_topic(
                    context,
                    db,
                    "vdp.new_validator_registration",
                    text,
                    address=state.get("address"),
                    validator_id=state.get("validator_id"),
                )
            if is_vdp_row and state["participation_state"] == "active-set" and state["lifecycle_status"] == "registered":
                text = (
                    "*Entered active set*\n\n"
                    f"{_format_validator_ref(state)}\n"
                    "Participation: `active-set`\n"
                    f"{_format_stake_band(state)}"
                )
                await _send_to_topic(
                    context,
                    db,
                    "vdp.entered_active_set",
                    text,
                    address=state.get("address"),
                    validator_id=state.get("validator_id"),
                )
            continue

        if previous is None:
            continue

        prev_lifecycle = previous.get("lifecycle_status")
        curr_lifecycle = state.get("lifecycle_status")
        prev_participation = previous.get("participation_state")
        curr_participation = state.get("participation_state")

        if is_vdp_row and prev_lifecycle != "approved" and curr_lifecycle == "approved":
            address_lower = str(state.get("address") or "").lower()
            if address_lower and address_lower in skip_approval_addresses:
                logger.debug(
                    "Skipping duplicate vdp.new_vdp_approval (lifecycle transition) "
                    "for %s (funding alert already carried the approval header)",
                    address_lower,
                )
            else:
                text = (
                    "*New VDP approval*\n\n"
                    f"{_format_validator_ref(state)}\n"
                    f"Approved: `{state.get('approved') or '-'}`"
                )
                await _send_to_topic(
                    context,
                    db,
                    "vdp.new_vdp_approval",
                    text,
                    address=state.get("address"),
                    validator_id=state.get("validator_id"),
                )

        if is_vdp_row and prev_lifecycle == "approved" and curr_lifecycle == "registered":
            text = (
                "*VDP validator progressed to registration*\n\n"
                f"{_format_validator_ref(state)}\n"
                f"Approved: `{state.get('approved') or '-'}`\n"
                f"Registered: `{state.get('registered') or '-'}`"
            )
            await _send_to_topic(
                context,
                db,
                "vdp.approved_to_registered",
                text,
                address=state.get("address"),
                validator_id=state.get("validator_id"),
            )
        elif is_vdp_row and prev_lifecycle != "registered" and curr_lifecycle == "registered":
            text = (
                "*New validator registration*\n\n"
                f"{_format_validator_ref(state)}\n"
                f"Registered: `{state.get('registered') or '-'}`"
            )
            await _send_to_topic(
                context,
                db,
                "vdp.new_validator_registration",
                text,
                address=state.get("address"),
                validator_id=state.get("validator_id"),
            )

        if is_vdp_row and prev_participation != "active-set" and curr_participation == "active-set":
            transition_suppressed_signatures.update(
                {
                    _event_signature_from_parts(state.get("address"), state.get("validator_id"), "consensus_stake_mon"),
                    _event_signature_from_parts(state.get("address"), state.get("validator_id"), "snapshot_stake_mon"),
                }
            )
            text = (
                "*Entered active set*\n\n"
                f"{_format_validator_ref(state)}\n"
                f"Was: `{prev_participation or 'unknown'}`\n"
                "Now: `active-set`\n"
                f"{_format_participation_transition(previous, state)}\n"
                f"{_format_transition_stake_lines(previous, state)}\n\n"
                f"{_format_stake_band(state)}"
            )
            await _send_to_topic(
                context,
                db,
                "vdp.entered_active_set",
                text,
                address=state.get("address"),
                validator_id=state.get("validator_id"),
            )
        elif is_vdp_row and prev_participation == "active-set" and curr_participation != "active-set":
            transition_suppressed_signatures.update(
                {
                    _event_signature_from_parts(state.get("address"), state.get("validator_id"), "consensus_stake_mon"),
                    _event_signature_from_parts(state.get("address"), state.get("validator_id"), "snapshot_stake_mon"),
                }
            )
            text = (
                "*Left active set*\n\n"
                f"{_format_validator_ref(state)}\n"
                "Was: `active-set`\n"
                f"Now: `{curr_participation or 'unknown'}`\n"
                f"{_format_participation_transition(previous, state)}\n"
                f"{_format_transition_stake_lines(previous, state)}\n\n"
                f"{_format_stake_band(state)}"
            )
            await _send_to_topic(
                context,
                db,
                "vdp.left_active_set",
                text,
                address=state.get("address"),
                validator_id=state.get("validator_id"),
            )

        if curr_lifecycle == "registered":
            for metric_key, metric_label in STAKE_METRICS:
                if metric_key not in previous:
                    continue
                previous_value = _as_float(previous.get(metric_key))
                current_value = _as_float(state.get(metric_key))
                delta = current_value - previous_value
                if abs(delta) < MEANINGFUL_STAKE_DELTA_MON:
                    continue
                stake_change_events.append(
                    _build_stake_change_event(
                        state=state,
                        previous=previous,
                        metric_key=metric_key,
                        metric_label=metric_label,
                        delta=delta,
                    )
                )

    if not bootstrap:
        await _dispatch_stake_change_events(
            context,
            db,
            stake_change_events,
            suppressed_signatures=transition_suppressed_signatures,
        )

    return True, validator_index


async def _process_funding_activity(context: CallbackContext, db: Database, bootstrap: bool) -> tuple[bool, set[str]]:
    """Returns (ready, set of lower-cased recipient addresses that just fired
    a fresh funding alert in state=approved). The caller passes that set into
    _process_vdp_validators so the duplicate `vdp.new_vdp_approval` alert is
    suppressed when both topics would otherwise fire for the same on-chain event.
    """
    just_funded_approved: set[str] = set()
    funding_rows = await get_funding_activity(limit=25)
    if not funding_rows:
        return False, just_funded_approved

    for row in funding_rows:
        tx_hash = (row.get("tx_hash") or row.get("tx") or "").lower()
        if not tx_hash:
            continue

        current_state = {
            "wallet_label": row.get("wallet_label") or row.get("source_label") or row.get("wallet") or "Foundation wallet",
            "recipient": row.get("recipient") or row.get("recipient_address") or row.get("address") or "-",
            "amount_mon": row.get("amount_mon") or row.get("amount") or "-",
            "state": row.get("state") or "-",
            "validator_id": row.get("validator_id"),
            "tx_hash": tx_hash,
        }
        previous = db.get_seen_state("funding_transfer", tx_hash)
        db.set_seen_state("funding_transfer", tx_hash, current_state)

        if bootstrap or previous is not None:
            continue

        recipient_str = str(current_state.get("recipient") or "")
        state_str = str(current_state.get("state") or "").lower()
        is_vdp_approval_funding = (
            state_str == "approved"
            and recipient_str
            and recipient_str != "-"
        )
        if is_vdp_approval_funding:
            just_funded_approved.add(recipient_str.lower())

        validator_id = current_state.get("validator_id")
        validator_ref = f"`#{validator_id}`" if validator_id is not None else "`-`"
        def _amount_is_non_onboarding(raw) -> bool:
            try:
                return abs(float(str(raw).replace(",", "")) - 100_000.0) <= 0.5
            except (TypeError, ValueError):
                return False

        non_onboarding_tranche = _amount_is_non_onboarding(
            current_state.get("amount_mon")
        )
        if non_onboarding_tranche:
            header = "*Foundation transfer 100k (non-onboarding tranche)*"
        else:
            header = (
                "*New VDP approval (Foundation funding)*"
                if is_vdp_approval_funding
                else "*New Foundation funding transfer*"
            )
        text = (
            f"{header}\n\n"
            f"Wallet: *{current_state['wallet_label']}*\n"
            f"Recipient: `{current_state['recipient']}`\n"
            f"Amount: *{current_state['amount_mon']}*\n"
            f"State: `{current_state['state']}`\n"
            f"Validator ID: {validator_ref}"
        )
        if non_onboarding_tranche:
            text += (
                "\n\n_Note: exactly-100k transfers have never been followed by a "
                "validator registration (0/24 since Dec 2025) - likely not a VDP "
                "onboarding tranche._"
            )
        await _send_to_topic(
            context,
            db,
            "vdp.new_foundation_funding_transfer",
            text,
            address=current_state.get("recipient"),
            validator_id=current_state.get("validator_id"),
        )

    return True, just_funded_approved


async def check_vdp_events(context: CallbackContext):
    db: Database = context.bot_data["db"]
    validator_bootstrap = not _bootstrap_done(db, VALIDATOR_BOOTSTRAP_KEY)
    funding_bootstrap = not _bootstrap_done(db, FUNDING_BOOTSTRAP_KEY)
    foundation_bootstrap = not _bootstrap_done(db, FOUNDATION_DELEGATION_BOOTSTRAP_KEY)

    # Funding first so that fresh state=approved funding alerts seed the
    # skip_approval_addresses set passed into the validator scan. Suppresses
    # the duplicate `vdp.new_vdp_approval` when a single on-chain event would
    # otherwise fire both topics for the same recipient.
    funding_ready, just_funded_approved = await _process_funding_activity(
        context, db, funding_bootstrap
    )
    validators_ready, validator_index = await _process_vdp_validators(
        context, db, validator_bootstrap,
        skip_approval_addresses=just_funded_approved,
    )
    foundation_ready = await _process_foundation_delegations(
        context,
        db,
        foundation_bootstrap,
        validator_index,
    )

    if validator_bootstrap and validators_ready:
        _mark_bootstrap_done(db, VALIDATOR_BOOTSTRAP_KEY)
        logger.info("VDP validator bootstrap completed without sending historical alerts")
    if funding_bootstrap and funding_ready:
        _mark_bootstrap_done(db, FUNDING_BOOTSTRAP_KEY)
        logger.info("VDP funding bootstrap completed without sending historical alerts")
    if foundation_bootstrap and foundation_ready:
        _mark_bootstrap_done(db, FOUNDATION_DELEGATION_BOOTSTRAP_KEY)
        _mark_meta_timestamp(db, FOUNDATION_DELEGATION_LAST_SCAN_KEY)
        logger.info("Foundation delegation bootstrap completed without sending historical alerts")
