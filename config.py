import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
REFERENCE_RPC: str = os.getenv("REFERENCE_RPC", "https://rpc-testnet.monadinfra.com")
MAX_NODES_PER_USER: int = int(os.getenv("MAX_NODES_PER_USER", "5"))
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))
BLOCK_STUCK_MINUTES: int = int(os.getenv("BLOCK_STUCK_MINUTES", "3"))
LAG_THRESHOLD: int = int(os.getenv("LAG_THRESHOLD", "10"))
DB_PATH: str = os.getenv("DB_PATH", "/home/admin/monad-node-bot/nodes.db")
LOG_FILE: str = os.getenv("LOG_FILE", "/var/log/monad-node-bot.log")
