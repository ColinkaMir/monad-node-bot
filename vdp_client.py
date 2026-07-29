import asyncio
import logging
from typing import Any

import aiohttp

import config


logger = logging.getLogger(__name__)


async def _get_json(path: str) -> Any:
    url = f"{config.VDP_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json()
    except Exception as exc:
        logger.warning("VDP API request failed for %s: %s", url, exc)
        return None


async def get_vdp_stats() -> dict:
    payload = await _get_json("stats")
    return payload if isinstance(payload, dict) else {}


async def get_vdp_validators() -> list[dict]:
    payload = await _get_json("validators")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def get_funding_activity(limit: int = 5) -> list[dict]:
    payload = await _get_json(f"funding-activity?limit={limit}")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _vdp_rpc_pool(explicit):
    """RPC endpoints to try in order. Explicit arg wins; else the VDP pool; else REFERENCE_RPC."""
    if explicit:
        raw = [explicit]
    else:
        raw = list(getattr(config, "VDP_RPC_POOL", None) or [config.REFERENCE_RPC])
    seen, out = set(), []
    for u in raw:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def rpc_eth_call(to_address: str, data: str, *, rpc_url: str | None = None) -> str | None:
    timeout = aiohttp.ClientTimeout(total=20)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to_address, "data": data}, "latest"],
    }
    pool = _vdp_rpc_pool(rpc_url)
    last_err = None
    for url in pool:
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as response:
                        if response.status == 429:
                            # throttled: brief honor of Retry-After, then fail over to next RPC
                            ra = response.headers.get("Retry-After", "")
                            delay = float(ra) if ra.isdigit() else 0.5 * (2 ** attempt)
                            last_err = "HTTP 429 (rate-limited)"
                            await asyncio.sleep(min(delay, 5))
                            break
                        response.raise_for_status()
                        body = await response.json(content_type=None)
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(0.3 * (2 ** attempt))
                continue
            if not isinstance(body, dict):
                logger.warning("RPC eth_call returned unexpected payload type: %r", body)
                return None
            if body.get("error"):
                logger.warning("RPC eth_call returned error for %s: %s", url, body["error"])
                return None
            result = body.get("result")
            return result if isinstance(result, str) else None
    logger.warning("RPC eth_call failed on all RPCs [%s]: %s", ", ".join(pool), last_err)
    return None
