# Built with the help of Claude (Anthropic) — https://claude.ai
import logging
import logging.handlers
import sys
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
from database import Database
from monitor import check_all_nodes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    return url.rstrip("/")


def is_valid_rpc_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


STATUS_ICON = {
    "ok":          "✅",
    "unreachable": "🔴",
    "stuck":       "🟡",
    "lagging":     "🟡",
    "unknown":     "⏳",
}

STATUS_LABEL = {
    "ok":          "OK",
    "unreachable": "Unreachable",
    "stuck":       "Block stuck",
    "lagging":     "Lagging",
    "unknown":     "Pending first check",
}


def format_node(node: dict) -> str:
    status = node.get("status", "unknown")
    icon = STATUS_ICON.get(status, "❓")
    label = STATUS_LABEL.get(status, status)
    block = node.get("last_block")
    block_str = f"{block:,}" if block is not None else "—"
    return f"{icon} `{node['rpc_url']}`\n   Status: {label} | Block: {block_str}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Monad Node Monitor*\n\n"
        "Monitor your Monad testnet nodes and get instant alerts when something goes wrong.\n\n"
        "*Commands:*\n"
        "`/add <rpc_url>` — add a node to monitor\n"
        "`/remove <rpc_url>` — stop monitoring a node\n"
        "`/status` — show status of all your nodes\n"
        "`/list` — list your monitored nodes\n"
        "`/help` — show this message\n\n"
        "*Example:*\n"
        "`/add http://1.2.3.4:8080`\n\n"
        f"You can monitor up to {config.MAX_NODES_PER_USER} nodes.\n"
        "Checks run every minute."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: `/add <rpc_url>`\nExample: `/add http://1.2.3.4:8080`",
            parse_mode="Markdown",
        )
        return

    rpc_url = normalize_url(context.args[0])

    if not is_valid_rpc_url(rpc_url):
        await update.message.reply_text(
            "❌ Invalid URL. Must start with `http://` or `https://`.",
            parse_mode="Markdown",
        )
        return

    if db.count_user_nodes(user_id) >= config.MAX_NODES_PER_USER:
        await update.message.reply_text(
            f"❌ You have reached the limit of {config.MAX_NODES_PER_USER} nodes.\n"
            "Remove one with `/remove <rpc_url>` before adding a new one.",
            parse_mode="Markdown",
        )
        return

    ok, reason = db.add_node(user_id, rpc_url)
    if not ok and reason == "duplicate":
        await update.message.reply_text(
            f"⚠️ `{rpc_url}` is already in your list.", parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"✅ Added `{rpc_url}`\nFirst check will run within a minute.",
        parse_mode="Markdown",
    )
    logger.info("User %s added node %s", user_id, rpc_url)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: `/remove <rpc_url>`", parse_mode="Markdown"
        )
        return

    rpc_url = normalize_url(context.args[0])
    removed = db.remove_node(user_id, rpc_url)

    if removed:
        await update.message.reply_text(
            f"🗑 Removed `{rpc_url}`", parse_mode="Markdown"
        )
        logger.info("User %s removed node %s", user_id, rpc_url)
    else:
        await update.message.reply_text(
            f"❌ Node `{rpc_url}` not found in your list.", parse_mode="Markdown"
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
    for i, node in enumerate(nodes, 1):
        lines.append(f"{i}. `{node['rpc_url']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not config.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in .env")
        sys.exit(1)

    setup_logging()
    logger.info("Starting Monad Node Monitor bot")

    db = Database(config.DB_PATH)

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))

    app.job_queue.run_repeating(
        check_all_nodes,
        interval=config.CHECK_INTERVAL,
        first=10,
    )

    logger.info(
        "Bot started. Check interval: %ds, Reference RPC: %s",
        config.CHECK_INTERVAL,
        config.REFERENCE_RPC,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
