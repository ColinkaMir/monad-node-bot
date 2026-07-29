"""On-chain validator-liveness monitoring (Phase 1).

Resilient alternative to RPC-polling: reads validator health from the public
reference RPC's staking precompile, so it keeps working after operators close
their RPC ports per the Foundation 2026-06-17 advisory.

Phase 1 signal (deterministic, low false-positive): consensus (active) set
membership. A watched validator dropping out of the consensus set fires a
`left active set` alert; re-entry fires recovery. Consensus stake / flags are
read for context only (no stake-threshold alerting yet — that is Phase 2).

Selectors and decode layout ported from the production Japan scanner
(`~/monad-vdp/scanner.py`). ABI helpers are reused from vdp_dispatcher.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from telegram.ext import CallbackContext

import config
from database import Database
from vdp_client import get_vdp_validators
from vdp_dispatcher import (
    _decode_u256_word,
    _encode_uint_word,
    _staking_eth_call,
)

logger = logging.getLogger(__name__)

# Staking precompile selectors (same as ~/monad-vdp/scanner.py).
SELECTOR_GET_CONSENSUS_SET = "0xfb29b729"
SELECTOR_GET_VALIDATOR = "0x2b6d639a"

# Minutes a validator must remain out of the consensus set before we alert,
# to absorb epoch-boundary / RPC transients. Mirrors UNREACHABLE_ALERT_MINUTES.
LEFT_SET_ALERT_MINUTES = int(getattr(config, "LEFT_SET_ALERT_MINUTES", 3))

# Minutes an in-active-set validator can go without producing a block (its w5
# reward counter not advancing) before we treat it as not-producing / down.
# Conservative default: with ~200 roughly-equal-stake validators on ~0.4s blocks,
# a healthy validator proposes about once every ~80s, so 20 min of zero production
# is ~15x the expected interval -> effectively no false positives. Tune in soak.
PROPOSER_STALL_MINUTES = int(getattr(config, "PROPOSER_STALL_MINUTES", 20))

# Safety bound on consensus-set pagination pages.
_MAX_SET_PAGES = 64

_WEI = 1_000_000_000_000_000_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_consensus_set() -> Optional[set[int]]:
    """Return the current consensus (active) validator-id set, or None on read failure.

    Returning None (never an empty/partial set) on any read problem is critical:
    callers MUST skip the cycle rather than treat a failed read as "everyone left".
    """
    ids: set[int] = set()
    next_index = 0
    for _ in range(_MAX_SET_PAGES):
        try:
            words = await _staking_eth_call(
                SELECTOR_GET_CONSENSUS_SET, _encode_uint_word(next_index)
            )
        except Exception as exc:  # malformed payload, etc.
            logger.warning("consensus-set eth_call failed: %s", exc)
            return None
        if not words or len(words) < 4:
            return None
        is_done = _decode_u256_word(words, 0)
        next_index = _decode_u256_word(words, 1)
        arr_offset_words = _decode_u256_word(words, 2) // 32
        if arr_offset_words >= len(words):
            return None
        arr_len = _decode_u256_word(words, arr_offset_words)
        start = arr_offset_words + 1
        for i in range(arr_len):
            if start + i < len(words):
                ids.add(_decode_u256_word(words, start + i))
        if is_done:
            return ids
    # Hit the page bound without an explicit done flag; return what we have.
    return ids


async def fetch_validator_view(validator_id: int) -> Optional[dict]:
    """Read getValidator(validatorId) -> {auth, flags, consensus_mon, snapshot_mon}, or None."""
    if validator_id is None or int(validator_id) <= 0:
        return None
    try:
        words = await _staking_eth_call(
            SELECTOR_GET_VALIDATOR, _encode_uint_word(int(validator_id))
        )
    except Exception as exc:
        logger.warning("getValidator eth_call failed for %s: %s", validator_id, exc)
        return None
    if not words or len(words) < 10:
        return None
    auth_address = "0x" + words[0].hex()[24:]
    flags = _decode_u256_word(words, 1)
    rewards_wei = _decode_u256_word(words, 5)
    consensus_wei = _decode_u256_word(words, 6)
    snapshot_wei = _decode_u256_word(words, 8)
    return {
        "auth": auth_address.lower(),
        "flags": flags,
        # w5 is the validator's accumulated block-reward counter: it advances
        # (by the block reward) every time the validator proposes a block. A
        # frozen w5 while in the active set means the node is not producing.
        "rewards_mon": rewards_wei / _WEI,
        "consensus_mon": consensus_wei / _WEI,
        "snapshot_mon": snapshot_wei / _WEI,
    }


async def resolve_validator(token: str) -> Optional[dict]:
    """Resolve a /watchval argument to {validator_id, label}.

    Accepts a numeric validator id directly, or a 0x authority address resolved
    via the VDP validator directory (best-effort; VDP-tracked validators only).
    Returns None if it can't be resolved.
    """
    token = (token or "").strip()
    if not token:
        return None

    if token.isdigit():
        vid = int(token)
        label = await _label_for_validator_id(vid)
        return {"validator_id": vid, "label": label}

    if token.lower().startswith("0x") and len(token) == 42:
        addr = token.lower()
        for row in await get_vdp_validators():
            if (row.get("address") or "").lower() == addr:
                vid = row.get("validator_id")
                if vid is not None:
                    name = row.get("validator_name") or row.get("name")
                    return {"validator_id": int(vid), "label": name or f"#{int(vid)}"}
        return None

    return None


async def _label_for_validator_id(validator_id: int) -> str:
    try:
        for row in await get_vdp_validators():
            if str(row.get("validator_id")) == str(validator_id):
                name = row.get("validator_name") or row.get("name")
                if name:
                    return str(name)
    except Exception:
        pass
    return f"#{int(validator_id)}"


def _ref(label: Optional[str], validator_id: int) -> str:
    if label and not label.startswith("#"):
        return f"Validator #{validator_id} ({label})"
    return f"Validator #{validator_id}"


def _stake_line(view: Optional[dict]) -> str:
    if not view:
        return ""
    return f"\nConsensus stake: {view['consensus_mon']:,.0f} MON"


async def _send(context: CallbackContext, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Failed to send validator alert to %s: %s", user_id, exc)


def _aware(ts_str: str, now: datetime) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _process_watch(
    context: CallbackContext,
    db: Database,
    watch: dict,
    consensus_set: set[int],
    now: datetime,
):
    watch_id = watch["id"]
    user_id = watch["user_id"]
    validator_id = int(watch["validator_id"])
    label = watch.get("label")
    prev_status = watch.get("status") or "unknown"
    prev_alerted = int(watch.get("alerted") or 0)
    prev_status_since = watch.get("status_since")
    prev_last_in_set_at = watch.get("last_in_set_at")
    now_str = now.isoformat()

    in_set = validator_id in consensus_set
    # One getValidator read per watched validator per cycle: stake, flags, and the
    # w5 reward counter used for block-production liveness.
    view = await fetch_validator_view(validator_id)

    # --- block-production tracking via the w5 reward counter ---
    last_rewards = watch.get("last_rewards")
    last_produced_at = watch.get("last_produced_at")
    if not in_set:
        # Production is only meaningful inside the set; reset the baseline so a
        # later re-entry recalibrates cleanly.
        last_rewards = None
        last_produced_at = None
    elif view is not None:
        rewards = view["rewards_mon"]
        if last_rewards is None:
            # First in-set observation: assume producing from now.
            last_rewards = rewards
            last_produced_at = now_str
        elif rewards > last_rewards + 1e-6:
            # Reward counter advanced -> the validator proposed a block.
            last_rewards = rewards
            last_produced_at = now_str
        elif rewards < last_rewards - 1e-6:
            # Counter went down (claim/reset): update baseline, not a production event.
            last_rewards = rewards
    else:
        # In set but the getValidator read FAILED (RPC 403/429/timeout on all pool RPCs).
        # We cannot confirm non-production, so PAUSE the stall timer: bump last_produced_at
        # to now so an RPC-outage window never accumulates toward a false "not producing".
        # 2026-07-08: monadinfra RPC flakiness produced exactly this false positive on #267.
        if last_produced_at is not None:
            last_produced_at = now_str

    stalled = (
        in_set
        and view is not None            # never declare a stall on a failed read
        and last_produced_at is not None
        and (now - _aware(last_produced_at, now)).total_seconds()
        >= PROPOSER_STALL_MINUTES * 60
    )

    if not in_set:
        new_status = "out_of_set"
    elif stalled:
        new_status = "not_producing"
    else:
        new_status = "ok"

    last_in_set_at = now_str if in_set else prev_last_in_set_at
    seen_in_set_before = bool(prev_last_in_set_at)
    new_status_since = now_str if new_status != prev_status else (prev_status_since or now_str)
    new_alerted = prev_alerted

    if prev_status in ("unknown", "new"):
        # Bootstrap: record baseline, never alert on first classification.
        new_alerted = 0
    elif new_status != prev_status:
        if new_status == "ok":
            if prev_alerted:
                msg = (
                    "✅ *Validator producing blocks again*\n"
                    if prev_status == "not_producing"
                    else "✅ *Validator back in active set*\n"
                )
                await _send(context, user_id, msg + f"`{_ref(label, validator_id)}`" + _stake_line(view))
            new_alerted = 0
        elif new_status == "not_producing":
            # In the set but the reward counter has been frozen past the stall
            # window: in consensus yet not proposing -> genuinely down / stuck.
            if prev_status == "ok":
                await _send(
                    context,
                    user_id,
                    "🔴 *Validator not producing blocks*\n"
                    f"`{_ref(label, validator_id)}`" + _stake_line(view) +
                    f"\nIn the active set but no block produced for over {PROPOSER_STALL_MINUTES} min. "
                    "Node is likely down or stuck.",
                )
                new_alerted = 1
        elif new_status == "out_of_set":
            if prev_status == "ok":
                new_alerted = 0  # arm the debounce below
            # From not_producing we keep prev_alerted (already alerted) -> no second alert.

    # Debounced "left active set" alert. Neutral wording: on testnet this is often
    # routine Foundation rotation, not a failure (see testnet-validator-rotation).
    if (
        new_status == "out_of_set"
        and not new_alerted
        and prev_status not in ("unknown", "new")
        and seen_in_set_before
        and (now - _aware(new_status_since, now)).total_seconds() >= LEFT_SET_ALERT_MINUTES * 60
    ):
        await _send(
            context,
            user_id,
            "🟡 *Validator left active set*\n"
            f"`{_ref(label, validator_id)}`" + _stake_line(view) +
            "\nNo longer in the consensus set. On testnet this is often routine rotation, "
            "but it can also be an issue. Worth a check.",
        )
        new_alerted = 1

    new_consensus_mon = view["consensus_mon"] if view else watch.get("last_consensus_mon")
    new_flags = view["flags"] if view else watch.get("flags")

    db.update_validator_watch(
        watch_id,
        new_status,
        new_alerted,
        new_status_since,
        last_in_set_at,
        new_consensus_mon,
        new_flags,
        last_rewards,
        last_produced_at,
    )
    logger.debug(
        "validator_watch vid=%s user=%s status=%s in_set=%s stalled=%s",
        validator_id, user_id, new_status, in_set, stalled,
    )


async def check_all_validators(context: CallbackContext):
    """Job: poll on-chain liveness for all watched validators.

    One shared consensus-set read per cycle (O(1) RPC regardless of #watches),
    then per-validator membership classification. Skips the cycle entirely if
    the set read fails, so RPC blips never produce false `left active set` alerts.
    """
    db: Database = context.bot_data["db"]
    watches = db.get_all_validator_watches()
    if not watches:
        return

    consensus_set = await fetch_consensus_set()
    if consensus_set is None:
        logger.warning("consensus-set read failed; skipping validator-liveness cycle")
        return

    now = _utc_now()
    for watch in watches:
        try:
            await _process_watch(context, db, watch, consensus_set, now)
        except Exception as exc:
            logger.error("validator watch %s failed: %s", watch.get("id"), exc)
