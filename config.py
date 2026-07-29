import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
REFERENCE_RPC: str = os.getenv("REFERENCE_RPC", "https://rpc-testnet.monadinfra.com")
# Heavy VDP-event polling rotates over this pool (public monad RPC first so it does not burst
# rpc-testnet.monadinfra.com, which was 429-throttling the VDP checker). Liveness stays on
# REFERENCE_RPC. Comma-separated; first that answers wins, 429 -> fail over to next.
VDP_RPC_POOL: list = [u.strip() for u in os.getenv(
    "VDP_RPC_POOL",
    "https://testnet-rpc.monad.xyz,https://rpc-testnet.monadinfra.com",
).split(",") if u.strip()]
VDP_API_BASE: str = os.getenv("VDP_API_BASE", "https://prooflines.org/monad/vdp-api/api")
VDP_CHECK_INTERVAL: int = int(os.getenv("VDP_CHECK_INTERVAL", "60"))
FOUNDATION_DELEGATION_SCAN_SECONDS: int = int(os.getenv("FOUNDATION_DELEGATION_SCAN_SECONDS", "300"))
STAKING_PRECOMPILE_ADDRESS: str = os.getenv(
    "STAKING_PRECOMPILE_ADDRESS",
    "0x0000000000000000000000000000000000001000",
)
MAX_NODES_PER_USER: int = int(os.getenv("MAX_NODES_PER_USER", "5"))
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))
UNREACHABLE_ALERT_MINUTES: int = int(os.getenv("UNREACHABLE_ALERT_MINUTES", "3"))
BLOCK_STUCK_MINUTES: int = int(os.getenv("BLOCK_STUCK_MINUTES", "3"))
LAG_THRESHOLD: int = int(os.getenv("LAG_THRESHOLD", "10"))
ALERT_REPEAT_MINUTES: int = int(os.getenv("ALERT_REPEAT_MINUTES", "5"))
# On-chain validator-liveness (validator_liveness.py)
LEFT_SET_ALERT_MINUTES: int = int(os.getenv("LEFT_SET_ALERT_MINUTES", "3"))
PROPOSER_STALL_MINUTES: int = int(os.getenv("PROPOSER_STALL_MINUTES", "20"))
DB_PATH: str = os.getenv("DB_PATH", os.path.join(BASE_DIR, "nodes.db"))
LOG_FILE: str = os.getenv("LOG_FILE", os.path.join(BASE_DIR, "logs", "monad-node-bot.log"))
