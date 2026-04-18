import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from telegram.ext import CallbackContext

import config
from database import Database

logger = logging.getLogger(__name__)

_RPC_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def get_block_number(
    rpc_url: str, session: aiohttp.ClientSession
) -> Optional[int]:
    payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
    try:
        async with session.post(rpc_url, json=payload, timeout=_RPC_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
            return int(data["result"], 16)
    except Exception as e:
        logger.debug("RPC call failed for %s: %s", rpc_url, e)
        return None


def _determine_status(
    block: Optional[int],
    prev_block: Optional[int],
    last_block_changed_at: Optional[str],
    ref_block: Optional[int],
    now: datetime,
) -> tuple[str, Optional[int], Optional[str]]:
    """Return (status, new_last_block, new_last_block_changed_at)."""
    now_str = now.isoformat()

    if block is None:
        return "unreachable", prev_block, last_block_changed_at

    # Update changed_at whenever block advances or on first check
    if prev_block is None or block != prev_block:
        new_changed_at = now_str
    else:
        new_changed_at = last_block_changed_at or now_str

    # Stuck check: block hasn't moved for BLOCK_STUCK_MINUTES
    if block == prev_block and last_block_changed_at:
        changed_at = datetime.fromisoformat(last_block_changed_at)
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=timezone.utc)
        stuck_seconds = (now - changed_at).total_seconds()
        if stuck_seconds > config.BLOCK_STUCK_MINUTES * 60:
            return "stuck", block, new_changed_at

    # Lag check: node block is behind the reference
    if ref_block is not None and ref_block - block > config.LAG_THRESHOLD:
        return "lagging", block, new_changed_at

    return "ok", block, new_changed_at


async def _send_alert(
    context: CallbackContext,
    user_id: int,
    rpc_url: str,
    status: str,
    block: Optional[int],
    ref_block: Optional[int],
):
    block_str = f"{block:,}" if block is not None else "N/A"

    if status == "unreachable":
        text = (
            f"🔴 *Node unreachable*\n"
            f"`{rpc_url}`\n"
            f"Last known block: {block_str}"
        )
    elif status == "stuck":
        text = (
            f"🟡 *Block not progressing*\n"
            f"`{rpc_url}`\n"
            f"Stuck at block {block_str} for >{config.BLOCK_STUCK_MINUTES} minutes"
        )
    elif status == "lagging":
        diff = (ref_block - block) if ref_block and block else "?"
        text = (
            f"🟡 *Node is lagging behind*\n"
            f"`{rpc_url}`\n"
            f"Node: {block_str} | Network: {ref_block:,} | Behind: {diff} blocks"
        )
    else:
        return

    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Failed to send alert to %s: %s", user_id, e)


async def _send_recovery(
    context: CallbackContext,
    user_id: int,
    rpc_url: str,
    block: Optional[int],
):
    block_str = f"{block:,}" if block is not None else "N/A"
    text = (
        f"✅ *Node recovered*\n"
        f"`{rpc_url}`\n"
        f"Current block: {block_str}"
    )
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Failed to send recovery to %s: %s", user_id, e)


async def check_all_nodes(context: CallbackContext):
    """Job that runs every CHECK_INTERVAL seconds."""
    db: Database = context.bot_data["db"]
    nodes = db.get_all_nodes()
    if not nodes:
        return

    now = datetime.now(timezone.utc)

    async with aiohttp.ClientSession() as session:
        ref_block = await get_block_number(config.REFERENCE_RPC, session)
        if ref_block is None:
            logger.warning("Reference RPC unreachable, skipping lag checks")

        for node in nodes:
            await _process_node(context, db, session, node, ref_block, now)


async def _process_node(
    context: CallbackContext,
    db: Database,
    session: aiohttp.ClientSession,
    node: dict,
    ref_block: Optional[int],
    now: datetime,
):
    node_id = node["id"]
    user_id = node["user_id"]
    rpc_url = node["rpc_url"]
    prev_status = node["status"]
    prev_alerted = node["alerted"]

    block = await get_block_number(rpc_url, session)

    new_status, new_block, new_changed_at = _determine_status(
        block=block,
        prev_block=node["last_block"],
        last_block_changed_at=node["last_block_changed_at"],
        ref_block=ref_block,
        now=now,
    )

    new_alerted = prev_alerted

    # Status changed — decide whether to alert or send recovery
    if new_status != prev_status:
        if new_status == "ok" and prev_status not in ("ok", "unknown"):
            await _send_recovery(context, user_id, rpc_url, new_block)
            new_alerted = 0
        elif new_status != "ok":
            # New type of problem — reset so alert fires below
            new_alerted = 0

    # Fire alert once per problem
    if new_status != "ok" and not new_alerted:
        await _send_alert(context, user_id, rpc_url, new_status, new_block, ref_block)
        new_alerted = 1

    db.update_node(node_id, new_block, new_changed_at, new_status, new_alerted)
    logger.debug(
        "Node %s user=%s status=%s block=%s", rpc_url, user_id, new_status, new_block
    )
