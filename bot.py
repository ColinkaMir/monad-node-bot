import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database import Database
from monitor import check_all_nodes
from validator_liveness import check_all_validators, resolve_validator
from vdp_client import get_vdp_stats, get_vdp_validators
from vdp_dispatcher import check_vdp_events
from vdp_topics import (
    TOPIC_BY_KEY,
    TOPICS_BY_CATEGORY,
    TOP_LEVEL_BUTTONS,
    VDP_CATEGORIES,
)


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log_dir = os.path.dirname(config.LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


logger = logging.getLogger(__name__)
DEFAULT_WAVE2_START = "2026-02-02"
WATCHLIST_SKIP_TEXT = "Skip"


STATUS_ICON = {
    "ok": "OK",
    "unreachable": "DOWN",
    "stuck": "STUCK",
    "lagging": "LAG",
    "unknown": "PENDING",
}

STATUS_LABEL = {
    "ok": "OK",
    "unreachable": "Unreachable",
    "stuck": "Block stuck",
    "lagging": "Lagging",
    "unknown": "Pending first check",
}

TOP_LEVEL_MENU_TEXTS = {"VDP Tracker", "Node Monitoring", "My Alerts", "Help"}
CATEGORY_EXPLAINERS = {
    "approvals_funding": [
        "`New Foundation funding transfer` = new qualifying 100,000-100,100 MON send from tracked Foundation wallets, including broader Foundation-funded cases",
        "`New VDP approval` = a newly observed address entering the narrower VDP approval flow used by the tracker; not every Foundation transfer becomes a VDP row",
    ],
    "active_set": [
        "`Entered active set` / `Left active set` = participation-state changes in the live validator set",
        "`Consensus stake added` / `removed` = a meaningful change in current live consensus stake, which is the strongest near-term rotation signal",
        "`Possible rotation pair` = the bot saw a matching stake removal and stake addition in the same poll cycle; this is an inferred pair, not a direct transaction decode",
    ],
}


def normalize_url(url: str) -> str:
    return url.rstrip("/")


def is_valid_rpc_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def format_node(node: dict) -> str:
    status = node.get("status", "unknown")
    icon = STATUS_ICON.get(status, "ERR")
    label = STATUS_LABEL.get(status, status)
    block = node.get("last_block")
    block_str = f"{block:,}" if block is not None else "-"
    return f"{icon} `{node['rpc_url']}`\n   Status: {label} | Block: {block_str}"


def is_vdp2_row(validator: dict, wave2_start: str) -> bool:
    approval_date = validator.get("approval_date") or ""
    registration_date = validator.get("registration_date") or ""
    if validator.get("funding_source") != "VDP":
        return False
    if approval_date < wave2_start:
        return False
    if registration_date and registration_date < wave2_start:
        return False
    return True


def _parse_ymd(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def count_recent_rows(validators: list[dict], field_name: str, days: int) -> int:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    total = 0
    for validator in validators:
        raw_value = validator.get(field_name) or ""
        date_value = _parse_ymd(raw_value)
        if date_value and date_value >= cutoff:
            total += 1
    return total


def main_menu_markup() -> ReplyKeyboardMarkup:
    keyboard = [[left, right] for left, right in TOP_LEVEL_BUTTONS]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def vdp_category_menu_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(title, callback_data=f"vdp:cat:{key}")]
        for key, title in VDP_CATEGORIES.items()
    ]
    rows.append([InlineKeyboardButton("My Watchlist", callback_data="vdp:watchlist")])
    rows.append([InlineKeyboardButton("Back", callback_data="vdp:home")])
    return InlineKeyboardMarkup(rows)


def vdp_topic_menu_markup(category_key: str, subscription_map: dict[str, bool]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Subscribe All", callback_data=f"vdp:suball:{category_key}"),
            InlineKeyboardButton("Unsubscribe All", callback_data=f"vdp:unsuball:{category_key}"),
        ],
    ]
    for topic in TOPICS_BY_CATEGORY.get(category_key, []):
        icon = "ON" if subscription_map.get(topic.key, False) else "OFF"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {topic.title}",
                    callback_data=f"vdp:topic:{topic.key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back", callback_data="vdp:tracker")])
    return InlineKeyboardMarkup(rows)


def vdp_topic_toggle_markup(topic_key: str, category_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Subscribe", callback_data=f"vdp:set:{topic_key}:1"),
                InlineKeyboardButton("Unsubscribe", callback_data=f"vdp:set:{topic_key}:0"),
            ],
            [InlineKeyboardButton("Back", callback_data=f"vdp:cat:{category_key}")],
        ]
    )


def my_alerts_markup(paused: bool) -> InlineKeyboardMarkup:
    pause_button = (
        InlineKeyboardButton("Resume All", callback_data="vdp:resume")
        if paused
        else InlineKeyboardButton("Pause All", callback_data="vdp:pause")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Manage VDP Alerts", callback_data="vdp:tracker")],
            [InlineKeyboardButton("Manage Watchlist", callback_data="vdp:watchlist")],
            [pause_button],
        ]
    )


def watchlist_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Watch Address", callback_data="vdp:watch:add:address"),
                InlineKeyboardButton("Watch Validator ID", callback_data="vdp:watch:add:validator_id"),
            ],
            [
                InlineKeyboardButton("View Watchlist", callback_data="vdp:watch:view"),
                InlineKeyboardButton("Remove Entry", callback_data="vdp:watch:remove"),
            ],
            [InlineKeyboardButton("Back", callback_data="vdp:tracker")],
        ]
    )


def watchlist_remove_markup(entries: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries[:20]:
        label_prefix = f"{entry['label']} - " if entry.get("label") else ""
        label = f"{label_prefix}{entry['watch_type']}: {entry['watch_value']}"
        rows.append(
            [
                InlineKeyboardButton(
                    f"Remove {label}",
                    callback_data=f"vdp:watch:del:{entry['watch_type']}:{entry['watch_value']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back", callback_data="vdp:watchlist")])
    return InlineKeyboardMarkup(rows)


def format_vdp_summary(stats: dict, validators: list[dict]) -> str:
    if not stats and not validators:
        return (
            "*VDP Tracker*\n\n"
            "The tracker API is temporarily unavailable, but you can still manage VDP subscriptions."
        )

    wave2_start = stats.get("wave2_public_start", DEFAULT_WAVE2_START) if stats else DEFAULT_WAVE2_START
    vdp_rows = [validator for validator in validators if validator.get("funding_source") == "VDP"]
    vdp2_rows = [validator for validator in validators if is_vdp2_row(validator, wave2_start)]
    vdp2_registered = [validator for validator in vdp2_rows if validator.get("lifecycle_status") == "registered"]
    vdp2_pending = [validator for validator in vdp2_rows if validator.get("lifecycle_status") == "approved"]
    vdp2_active_now = [
        validator
        for validator in vdp2_registered
        if validator.get("participation_state") == "active-set"
    ]
    vdp2_rotated_out_now = [
        validator
        for validator in vdp2_registered
        if validator.get("participation_state") != "active-set"
    ]
    vdp2_approvals_7d = count_recent_rows(vdp2_rows, "approval_date", 7)
    vdp2_registrations_7d = count_recent_rows(vdp2_rows, "registration_date", 7)
    vdp2_approvals_30d = count_recent_rows(vdp2_rows, "approval_date", 30)
    vdp2_registrations_30d = count_recent_rows(vdp2_rows, "registration_date", 30)
    vdp2_registered_total = len(vdp2_registered)

    return (
        "*VDP Tracker*\n\n"
        "*Warning*\n"
        "- VDP process under revision\n"
        "- Current applications should not be treated as fully finalized yet\n\n"
        "*Current*\n"
        f"- Registered validators: *{stats.get('registered_validators', '-') if stats else '-'}*\n"
        f"- VDP-2 cohort: *{len(vdp2_rows)}*\n"
        f"- VDP-2 registered: *{len(vdp2_registered)}*\n"
        f"- VDP-2 in active set: *{len(vdp2_active_now)} / {vdp2_registered_total}*\n"
        f"- VDP-2 rotated out now: *{len(vdp2_rotated_out_now)} / {vdp2_registered_total}*\n"
        f"- VDP-2 pending: *{len(vdp2_pending)}*\n"
        f"- VDP all-history rows: *{len(vdp_rows)}*\n"
        f"- Network active set: *{stats.get('active_set_registrations', '-') if stats else '-'}*\n\n"
        "*Momentum*\n"
        f"- 7d: *+{vdp2_approvals_7d} approvals* / *+{vdp2_registrations_7d} registrations*\n"
        f"- 30d: *+{vdp2_approvals_30d} approvals* / *+{vdp2_registrations_30d} registrations*\n\n"
        "*Definitions*\n"
        f"- `VDP-2` = current public approval-era slice since *{wave2_start}*\n"
        "- `VDP-2 in active set` = registered VDP-2 validators currently in the live active set\n"
        "- `VDP-2 rotated out now` = registered VDP-2 validators currently outside the live active set\n"
        "- `VDP all-history` = full approval-matched VDP history\n\n"
        "*Process caveat*\n"
        "- Fresh operator notes suggest the broader VDP process is being revised, so treat this as current queue intelligence rather than a guaranteed final Foundation workflow.\n\n"
        "Choose the part of validator-program activity you want to manage."
    )


def format_category_summary(category_key: str, subscription_map: dict[str, bool]) -> str:
    title = VDP_CATEGORIES.get(category_key, category_key)
    topics = TOPICS_BY_CATEGORY.get(category_key, [])
    enabled = sum(1 for topic in topics if subscription_map.get(topic.key, False))
    total = len(topics)
    explainer_lines = CATEGORY_EXPLAINERS.get(category_key, [])
    explainers = ""
    if explainer_lines:
        explainers = "\n\n*What these mean*\n" + "\n".join(f"- {line}" for line in explainer_lines)
    return (
        f"*{title}*\n\n"
        f"{enabled}/{total} alerts enabled"
        f"{explainers}\n\n"
        "Use the buttons below to subscribe broadly or manage individual alerts."
    )


def format_topic_detail(topic_key: str, enabled: bool) -> str:
    topic = TOPIC_BY_KEY[topic_key]
    status = "enabled" if enabled else "disabled"
    return (
        f"*{topic.title}*\n\n"
        f"{topic.description}\n\n"
        f"Current status: *{status}*"
    )


def format_my_alerts(subscription_map: dict[str, bool], paused: bool) -> str:
    lines = ["*Your alerts*\n", f"Status: {'Paused' if paused else 'Active'}"]
    for category_key, title in VDP_CATEGORIES.items():
        topics = TOPICS_BY_CATEGORY.get(category_key, [])
        enabled = sum(1 for topic in topics if subscription_map.get(topic.key, False))
        lines.append(f"- {title}: {enabled}/{len(topics)} enabled")
    return "\n".join(lines)


def format_watchlist_summary(entries: list[dict]) -> str:
    lines = [
        "*My Watchlist*\n",
        "Track the specific validator addresses or validator IDs you care about.",
    ]
    if not entries:
        lines.append("\nNo watchlist entries yet.")
        return "\n".join(lines)

    lines.append(f"\nCurrent entries: {len(entries)}")
    for entry in entries[:10]:
        prefix = f"*{entry['label']}* - " if entry.get("label") else ""
        lines.append(f"- {prefix}`{entry['watch_type']}`: `{entry['watch_value']}`")
    if len(entries) > 10:
        lines.append(f"- ...and {len(entries) - 10} more")
    return "\n".join(lines)


def normalize_watchlist_value(watch_type: str, raw_value: str) -> str | None:
    value = (raw_value or "").strip()
    if watch_type == "address":
        if len(value) == 42 and value.lower().startswith("0x"):
            return value.lower()
        return None
    if watch_type == "validator_id":
        if value.isdigit():
            return str(int(value))
        return None
    return None


def parse_start_watch_payload(payload: str | None) -> dict | None:
    raw = (payload or "").strip()
    if not raw.startswith("vwa_"):
        return None

    parts = raw.split("_", 2)
    if len(parts) != 3:
        return None

    _, validator_id_raw, address_raw = parts
    address = normalize_watchlist_value("address", address_raw)
    if address is None:
        return None

    validator_id = None
    if validator_id_raw != "na":
        validator_id = normalize_watchlist_value("validator_id", validator_id_raw)
        if validator_id is None:
            return None

    label = f"Validator #{validator_id}" if validator_id is not None else None
    return {
        "watch_type": "address",
        "watch_value": address,
        "label": label,
        "validator_id": validator_id,
    }


async def ensure_user(db: Database, update: Update) -> int:
    user = update.effective_user
    return db.ensure_user(
        telegram_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def send_vdp_home(message, db: Database, telegram_user_id: int):
    stats = await get_vdp_stats()
    validators = await get_vdp_validators()
    await message.reply_text(
        format_vdp_summary(stats, validators),
        parse_mode="Markdown",
        reply_markup=vdp_category_menu_markup(),
    )


async def send_watchlist_home(message, db: Database, telegram_user_id: int):
    entries = db.get_vdp_watchlists(telegram_user_id)
    await message.reply_text(
        format_watchlist_summary(entries),
        parse_mode="Markdown",
        reply_markup=watchlist_menu_markup(),
    )


async def maybe_handle_start_watch_payload(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database) -> bool:
    if not context.args:
        return False

    payload = parse_start_watch_payload(context.args[0])
    if payload is None:
        return False

    watch_value = payload["watch_value"]
    label = payload.get("label")
    ok, reason = db.add_vdp_watchlist(
        update.effective_user.id,
        payload["watch_type"],
        watch_value,
        label=label,
    )
    label_text = f" with label *{label}*" if label else ""
    if ok:
        confirmation = (
            "*MonadBeaconBot*\n\n"
            f"Added this validator to your watchlist: `{watch_value}`{label_text}\n\n"
            "You will now receive watchlist-based VDP alerts for this validator.\n\n"
            "*This includes:*\n"
            "- approvals and registration progress\n"
            "- active-set entry / exit\n"
            "- stake-change and rotation-related alerts"
        )
    elif reason == "duplicate":
        confirmation = (
            "*MonadBeaconBot*\n\n"
            f"`{watch_value}` is already in your watchlist.\n\n"
            "You will continue receiving watchlist-based VDP alerts for it.\n\n"
            "*This includes:*\n"
            "- approvals and registration progress\n"
            "- active-set entry / exit\n"
            "- stake-change and rotation-related alerts"
        )
    else:
        confirmation = (
            "*MonadBeaconBot*\n\n"
            "I could not add that validator to your watchlist automatically."
        )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Manage Watchlist", callback_data="vdp:watchlist")],
            [InlineKeyboardButton("Open VDP Tracker", callback_data="vdp:tracker")],
        ]
    )
    await update.message.reply_text(
        confirmation,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    await ensure_user(db, update)
    if await maybe_handle_start_watch_payload(update, context, db):
        return
    text = (
        "*MonadBeaconBot*\n\n"
        "Choose what you want to manage:\n"
        "- `VDP Tracker` for validator-program activity\n"
        "- `Node Monitoring` for RPC and sync alerts\n"
        "- `My Alerts` to review your current setup\n"
        "- `Help` for a quick guide\n\n"
        f"You can monitor up to {config.MAX_NODES_PER_USER} node RPC endpoints."
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_markup(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*MonadBeaconBot help*\n\n"
        "`VDP Tracker` lets you manage approval, registration, and active-set notifications.\n"
        "Treat the VDP layer as live queue intelligence. Current operator notes suggest the broader Foundation process is being revised, so the visible queue should not be read as a final guarantee of the next workflow.\n"
        "`Node Monitoring` keeps the classic RPC and sync alerts.\n"
        "`My Alerts` shows what is currently enabled.\n\n"
        "*Classic commands still work:*\n"
        "`/add <rpc_url>`\n"
        "`/remove <rpc_url>`\n"
        "`/status`\n"
        "`/list`\n"
        "`/start`\n\n"
        "*On-chain validator liveness* (no RPC access needed, survives closed RPC ports):\n"
        "`/watchval <validator_id>`\n"
        "`/unwatchval <validator_id>`\n"
        "`/myvalidators`"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_markup(),
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    await ensure_user(db, update)

    if not context.args:
        await update.message.reply_text(
            "Usage: `/add <rpc_url>`\nExample: `/add http://1.2.3.4:8080`",
            parse_mode="Markdown",
        )
        return

    rpc_url = normalize_url(context.args[0])

    if not is_valid_rpc_url(rpc_url):
        await update.message.reply_text(
            "Invalid URL. It must start with `http://` or `https://`.",
            parse_mode="Markdown",
        )
        return

    if db.count_user_nodes(user_id) >= config.MAX_NODES_PER_USER:
        await update.message.reply_text(
            f"You have reached the limit of {config.MAX_NODES_PER_USER} nodes.\n"
            "Remove one with `/remove <rpc_url>` before adding a new one.",
            parse_mode="Markdown",
        )
        return

    ok, reason = db.add_node(user_id, rpc_url)
    if not ok and reason == "duplicate":
        await update.message.reply_text(
            f"`{rpc_url}` is already in your list.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"Added `{rpc_url}`.\nFirst check will run within a minute.",
        parse_mode="Markdown",
    )
    logger.info("User %s added node %s", user_id, rpc_url)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: `/remove <rpc_url>`",
            parse_mode="Markdown",
        )
        return

    rpc_url = normalize_url(context.args[0])
    removed = db.remove_node(user_id, rpc_url)

    if removed:
        await update.message.reply_text(f"Removed `{rpc_url}`", parse_mode="Markdown")
        logger.info("User %s removed node %s", user_id, rpc_url)
    else:
        await update.message.reply_text(
            f"Node `{rpc_url}` was not found in your list.",
            parse_mode="Markdown",
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    nodes = db.get_user_nodes(user_id)

    if not nodes:
        await update.message.reply_text(
            "You have no nodes. Use `/add <rpc_url>` to start monitoring.",
            parse_mode="Markdown",
        )
        return

    lines = ["*Your nodes:*\n"]
    for node in nodes:
        lines.append(format_node(node))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    nodes = db.get_user_nodes(user_id)

    if not nodes:
        await update.message.reply_text(
            "You have no monitored nodes. Use `/add <rpc_url>`.",
            parse_mode="Markdown",
        )
        return

    lines = [f"*Your nodes ({len(nodes)}/{config.MAX_NODES_PER_USER}):*\n"]
    for idx, node in enumerate(nodes, 1):
        lines.append(f"{idx}. `{node['rpc_url']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


_VALIDATOR_STATUS_LABEL = {
    "ok": "🟢 in active set, producing",
    "not_producing": "🔴 in set but NOT producing",
    "out_of_set": "🟡 out of active set",
    "unknown": "… checking",
    "new": "… checking",
}


async def cmd_watchval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    await ensure_user(db, update)

    if not context.args:
        await update.message.reply_text(
            "Usage: `/watchval <validator_id|address> [label]`\n"
            "Example: `/watchval 267`\n\n"
            "Monitors your validator on-chain (active-set membership) using public "
            "data — no access to your node's RPC port is needed, so it keeps working "
            "after you close your RPC ports.",
            parse_mode="Markdown",
        )
        return

    if db.count_user_validator_watches(user_id) >= config.MAX_NODES_PER_USER:
        await update.message.reply_text(
            f"You have reached the limit of {config.MAX_NODES_PER_USER} watched validators.\n"
            "Remove one with `/unwatchval <validator_id>` first.",
            parse_mode="Markdown",
        )
        return

    resolved = await resolve_validator(context.args[0])
    if not resolved:
        await update.message.reply_text(
            "Could not resolve that validator. Pass a numeric validator id "
            "(e.g. `/watchval 267`), or a `0x` authority address known to the VDP directory.",
            parse_mode="Markdown",
        )
        return

    validator_id = resolved["validator_id"]
    label = " ".join(context.args[1:]) if len(context.args) > 1 else resolved.get("label")

    ok, reason = db.add_validator_watch(user_id, validator_id, label)
    if not ok and reason == "duplicate":
        await update.message.reply_text(
            f"You are already watching validator #{validator_id}.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"Now watching *Validator #{validator_id}* on-chain.\n"
        "You'll get an alert if it stops producing blocks while in the active set "
        "(node likely down), or if it leaves the active set, and when it recovers. "
        "First check runs within a minute.",
        parse_mode="Markdown",
    )
    logger.info("User %s now watching validator %s", user_id, validator_id)


async def cmd_unwatchval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: `/unwatchval <validator_id>`", parse_mode="Markdown"
        )
        return

    token = context.args[0].strip()
    if token.isdigit():
        validator_id = int(token)
    else:
        resolved = await resolve_validator(token)
        if not resolved:
            await update.message.reply_text(
                "Pass the numeric validator id shown in `/myvalidators`.",
                parse_mode="Markdown",
            )
            return
        validator_id = resolved["validator_id"]

    removed = db.remove_validator_watch(user_id, validator_id)
    if removed:
        await update.message.reply_text(
            f"Stopped watching validator #{validator_id}.", parse_mode="Markdown"
        )
        logger.info("User %s stopped watching validator %s", user_id, validator_id)
    else:
        await update.message.reply_text(
            f"Validator #{validator_id} was not in your watch list.",
            parse_mode="Markdown",
        )


async def cmd_myvalidators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    watches = db.get_user_validator_watches(user_id)

    if not watches:
        await update.message.reply_text(
            "You are not watching any validators on-chain. "
            "Use `/watchval <validator_id>` to start.",
            parse_mode="Markdown",
        )
        return

    lines = [f"*Watched validators ({len(watches)}/{config.MAX_NODES_PER_USER}):*\n"]
    for w in watches:
        status = _VALIDATOR_STATUS_LABEL.get(w.get("status"), w.get("status") or "?")
        label = w.get("label")
        suffix = f" ({label})" if label and not str(label).startswith("#") else ""
        lines.append(f"• `#{w['validator_id']}`{suffix} — {status}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    await ensure_user(db, update)
    text = (update.message.text or "").strip()
    pending_watch = context.user_data.get("awaiting_watchlist")

    if pending_watch:
        if text in TOP_LEVEL_MENU_TEXTS:
            context.user_data.pop("awaiting_watchlist", None)
        else:
            stage = pending_watch.get("stage", "value")
            watch_type = pending_watch["watch_type"]
            if stage == "value":
                normalized = normalize_watchlist_value(watch_type, text)
                if normalized is None:
                    expected = "a 0x-address" if watch_type == "address" else "a numeric validator ID"
                    await update.message.reply_text(
                        f"That does not look like {expected}. Try again or tap a top-level menu button to cancel.",
                        parse_mode="Markdown",
                    )
                    return

                context.user_data["awaiting_watchlist"] = {
                    "stage": "label",
                    "watch_type": watch_type,
                    "watch_value": normalized,
                }
                await update.message.reply_text(
                    "Now send a short label so you can recognize this watch later, or type `Skip`.",
                    parse_mode="Markdown",
                )
                return

            if stage == "label":
                watch_value = pending_watch["watch_value"]
                label = None if text == WATCHLIST_SKIP_TEXT else text.strip()
                if label == "":
                    label = None
                if label and len(label) > 60:
                    await update.message.reply_text(
                        "Keep the label short, up to 60 characters, or type `Skip`.",
                        parse_mode="Markdown",
                    )
                    return

                ok, reason = db.add_vdp_watchlist(
                    update.effective_user.id,
                    watch_type,
                    watch_value,
                    label=label,
                )
                context.user_data.pop("awaiting_watchlist", None)
                entries = db.get_vdp_watchlists(update.effective_user.id)
                label_text = f" with label *{label}*" if label else ""
                if ok:
                    await update.message.reply_text(
                        f"Added watchlist entry: `{watch_type}` = `{watch_value}`{label_text}",
                        parse_mode="Markdown",
                    )
                elif reason == "duplicate":
                    await update.message.reply_text(
                        f"`{watch_type}` = `{watch_value}` is already in your watchlist.",
                        parse_mode="Markdown",
                    )
                await update.message.reply_text(
                    format_watchlist_summary(entries),
                    parse_mode="Markdown",
                    reply_markup=watchlist_menu_markup(),
                )
                return

    if text == "VDP Tracker":
        await send_vdp_home(update.message, db, update.effective_user.id)
        return

    if text == "Node Monitoring":
        msg = (
            "*Node Monitoring*\n\n"
            "Use the classic commands to manage node RPC monitoring:\n"
            "`/add <rpc_url>`\n"
            "`/remove <rpc_url>`\n"
            "`/status`\n"
            "`/list`\n\n"
            "The VDP layer will live alongside this monitoring surface."
        )
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=main_menu_markup(),
        )
        return

    if text == "My Alerts":
        user_row = db.get_user_by_telegram_id(update.effective_user.id)
        sub_map = db.get_vdp_subscription_map(update.effective_user.id)
        paused = bool(user_row["is_paused"]) if user_row else False
        await update.message.reply_text(
            format_my_alerts(sub_map, paused),
            parse_mode="Markdown",
            reply_markup=my_alerts_markup(paused),
        )
        return

    if text == "Help":
        await cmd_help(update, context)
        return


async def handle_vdp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    query = update.callback_query
    await ensure_user(db, update)
    user_id = update.effective_user.id
    data = query.data or ""

    if data in {"vdp:home", "vdp:tracker"}:
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        stats = await get_vdp_stats()
        validators = await get_vdp_validators()
        await query.edit_message_text(
            format_vdp_summary(stats, validators),
            parse_mode="Markdown",
            reply_markup=vdp_category_menu_markup(),
        )
        return

    if data.startswith("vdp:cat:"):
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        category_key = data.split(":", 2)[2]
        subscription_map = db.get_vdp_subscription_map(user_id)
        await query.edit_message_text(
            format_category_summary(category_key, subscription_map),
            parse_mode="Markdown",
            reply_markup=vdp_topic_menu_markup(category_key, subscription_map),
        )
        return

    if data.startswith("vdp:suball:") or data.startswith("vdp:unsuball:"):
        context.user_data.pop("awaiting_watchlist", None)
        _, action, category_key = data.split(":", 2)
        enabled = action == "suball"
        topic_keys = [topic.key for topic in TOPICS_BY_CATEGORY.get(category_key, [])]
        db.set_vdp_subscriptions_for_topics(user_id, topic_keys, enabled)
        subscription_map = db.get_vdp_subscription_map(user_id)
        await query.answer("Section subscribed" if enabled else "Section unsubscribed")
        await query.edit_message_text(
            format_category_summary(category_key, subscription_map),
            parse_mode="Markdown",
            reply_markup=vdp_topic_menu_markup(category_key, subscription_map),
        )
        return

    if data.startswith("vdp:topic:"):
        topic_key = data.split(":", 2)[2]
        topic = TOPIC_BY_KEY.get(topic_key)
        if not topic:
            await query.answer()
            return
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        subscription_map = db.get_vdp_subscription_map(user_id)
        enabled = subscription_map.get(topic_key, False)
        await query.edit_message_text(
            format_topic_detail(topic_key, enabled),
            parse_mode="Markdown",
            reply_markup=vdp_topic_toggle_markup(topic_key, topic.category),
        )
        return

    if data.startswith("vdp:set:"):
        _, _, topic_key, raw_enabled = data.split(":", 3)
        topic = TOPIC_BY_KEY.get(topic_key)
        if not topic:
            await query.answer()
            return
        context.user_data.pop("awaiting_watchlist", None)
        enabled = raw_enabled == "1"
        db.set_vdp_subscription(user_id, topic_key, enabled)
        subscription_map = db.get_vdp_subscription_map(user_id)
        await query.answer("Subscribed" if enabled else "Unsubscribed")
        await query.edit_message_text(
            format_category_summary(topic.category, subscription_map),
            parse_mode="Markdown",
            reply_markup=vdp_topic_menu_markup(topic.category, subscription_map),
        )
        return

    if data == "vdp:watchlist":
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        entries = db.get_vdp_watchlists(user_id)
        await query.edit_message_text(
            format_watchlist_summary(entries),
            parse_mode="Markdown",
            reply_markup=watchlist_menu_markup(),
        )
        return

    if data in {"vdp:watch:view", "vdp:watch:home"}:
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        entries = db.get_vdp_watchlists(user_id)
        await query.edit_message_text(
            format_watchlist_summary(entries),
            parse_mode="Markdown",
            reply_markup=watchlist_menu_markup(),
        )
        return

    if data.startswith("vdp:watch:add:"):
        watch_type = data.split(":", 3)[3]
        context.user_data["awaiting_watchlist"] = {"stage": "value", "watch_type": watch_type}
        prompt = (
            "Send the validator registration address you want to watch."
            if watch_type == "address"
            else "Send the validator ID you want to watch."
        )
        await query.answer("Send it in chat")
        await query.edit_message_text(
            f"*Add to watchlist*\n\n{prompt}\n\nSend it as a normal chat message. Tap a top-level menu button to cancel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data="vdp:watchlist")]]
            ),
        )
        return

    if data == "vdp:watch:remove":
        await query.answer()
        context.user_data.pop("awaiting_watchlist", None)
        entries = db.get_vdp_watchlists(user_id)
        if not entries:
            await query.edit_message_text(
                "*My Watchlist*\n\nNo watchlist entries to remove yet.",
                parse_mode="Markdown",
                reply_markup=watchlist_menu_markup(),
            )
            return
        await query.edit_message_text(
            "*Remove watchlist entry*\n\nChoose an entry to remove.",
            parse_mode="Markdown",
            reply_markup=watchlist_remove_markup(entries),
        )
        return

    if data.startswith("vdp:watch:del:"):
        _, _, _, watch_type, watch_value = data.split(":", 4)
        removed = db.remove_vdp_watchlist(user_id, watch_type, watch_value)
        entries = db.get_vdp_watchlists(user_id)
        await query.answer("Removed" if removed else "Already removed")
        await query.edit_message_text(
            format_watchlist_summary(entries),
            parse_mode="Markdown",
            reply_markup=watchlist_menu_markup(),
        )
        return

    if data == "vdp:pause":
        context.user_data.pop("awaiting_watchlist", None)
        db.set_user_paused(user_id, True)
        subscription_map = db.get_vdp_subscription_map(user_id)
        await query.answer("Alerts paused")
        await query.edit_message_text(
            format_my_alerts(subscription_map, True),
            parse_mode="Markdown",
            reply_markup=my_alerts_markup(True),
        )
        return

    if data == "vdp:resume":
        context.user_data.pop("awaiting_watchlist", None)
        db.set_user_paused(user_id, False)
        subscription_map = db.get_vdp_subscription_map(user_id)
        await query.answer("Alerts resumed")
        await query.edit_message_text(
            format_my_alerts(subscription_map, False),
            parse_mode="Markdown",
            reply_markup=my_alerts_markup(False),
        )


def main():
    if not config.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in .env")
        sys.exit(1)

    setup_logging()
    logger.info("Starting MonadBeaconBot")

    db = Database(config.DB_PATH)

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("watchval", cmd_watchval))
    app.add_handler(CommandHandler("unwatchval", cmd_unwatchval))
    app.add_handler(CommandHandler("myvalidators", cmd_myvalidators))
    app.add_handler(CallbackQueryHandler(handle_vdp_callback, pattern=r"^vdp:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_input,
        )
    )

    app.job_queue.run_repeating(
        check_all_nodes,
        interval=config.CHECK_INTERVAL,
        first=10,
    )
    app.job_queue.run_repeating(
        check_vdp_events,
        interval=config.VDP_CHECK_INTERVAL,
        first=15,
    )
    app.job_queue.run_repeating(
        check_all_validators,
        interval=config.CHECK_INTERVAL,
        first=20,
    )

    logger.info(
        "Bot started. Node check interval: %ds, VDP check interval: %ds, Reference RPC: %s, VDP API: %s",
        config.CHECK_INTERVAL,
        config.VDP_CHECK_INTERVAL,
        config.REFERENCE_RPC,
        config.VDP_API_BASE,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
