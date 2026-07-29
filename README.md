# Monad Node Monitor Bot

A Telegram bot for monitoring Monad testnet nodes. Get instant alerts when your node goes down, stops producing blocks, or falls behind the network.

## 🤖 Use the live bot

The bot is already running — no setup needed:

**[@MonadBeaconBot](https://t.me/MonadBeaconBot)**

### Add your node in 3 steps

1. Open [@MonadBeaconBot](https://t.me/MonadBeaconBot) in Telegram and tap **Start**
2. Send your node's RPC URL:
   ```
   /add http://YOUR_SERVER_IP:8080
   ```
3. Done. The bot checks your node every minute and alerts you if anything goes wrong.

Run `/status` to see the current health of your node at any time.

---

## Features

- **Reachability check** — detects when your node's RPC endpoint stops responding
- **Block progress check** — alerts if block height doesn't change for 3+ minutes
- **Lag detection** — compares your node's block with the public network RPC; alerts if you're more than 10 blocks behind
- **Recovery notifications** — notifies you when a node comes back online
- **No spam** — short RPC blips are ignored; by default unreachable alerts fire only after 3 continuous minutes of failure, and recovery is sent only if an actual outage alert was already sent
- **Multi-user** — any operator can use the bot; each user manages their own nodes independently (up to 5 per user)

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Introduction and usage instructions |
| `/add <rpc_url>` | Add a node to monitor (e.g. `/add http://1.2.3.4:8080`) |
| `/remove <rpc_url>` | Stop monitoring a node |
| `/status` | Show current status of all your nodes |
| `/list` | List your monitored node URLs |
| `/help` | Show help message |

## Alert types

| Alert | Meaning |
|-------|---------|
| 🔴 Node unreachable | RPC endpoint did not respond |
| 🟡 Block not progressing | Block height unchanged for >3 minutes |
| 🟡 Node is lagging | Node block is 10+ blocks behind the network |
| ✅ Node recovered | Node is healthy again after an alert |

## Self-hosting

### Prerequisites

- Python 3.11+
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))

### Installation

```bash
git clone https://github.com/your-username/monad-node-bot
cd monad-node-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # set your BOT_TOKEN
```

### Running manually

```bash
source venv/bin/activate
python bot.py
```

### Running as a systemd service

```bash
# Copy service file
sudo cp monad-node-bot.service /etc/systemd/system/

# Optional: create local logs directory ahead of time
mkdir -p logs

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable monad-node-bot
sudo systemctl start monad-node-bot

# Check status
sudo systemctl status monad-node-bot
sudo journalctl -u monad-node-bot -f
```

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | — | **Required.** Telegram bot token |
| `REFERENCE_RPC` | `https://rpc-testnet.monadinfra.com` | Public RPC used for lag comparison |
| `VDP_API_BASE` | `https://prooflines.org/monad/vdp-api/api` | Public VDP tracker API used for validator-program alerts |
| `VDP_CHECK_INTERVAL` | `60` | Seconds between VDP / validator-program polling cycles |
| `FOUNDATION_DELEGATION_SCAN_SECONDS` | `300` | Seconds between deeper Foundation delegation scans through the staking precompile |
| `STAKING_PRECOMPILE_ADDRESS` | `0x0000000000000000000000000000000000001000` | Monad staking precompile used for direct delegation reads |
| `CHECK_INTERVAL` | `60` | Seconds between checks |
| `UNREACHABLE_ALERT_MINUTES` | `3` | Minutes an RPC endpoint must remain unreachable before sending an outage alert |
| `BLOCK_STUCK_MINUTES` | `3` | Minutes before reporting a stuck block |
| `LAG_THRESHOLD` | `10` | Max allowed block difference before alerting |
| `MAX_NODES_PER_USER` | `5` | Maximum nodes per Telegram user |
| `DB_PATH` | `nodes.db` | Path to SQLite database |
| `LOG_FILE` | `./logs/monad-node-bot.log` | Log file path; defaults to a user-writable logs directory inside the bot workspace |

## License

MIT

