import json
import sqlite3
from typing import Optional


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id               INTEGER NOT NULL,
                    rpc_url               TEXT    NOT NULL,
                    added_at              TEXT    DEFAULT (datetime('now')),
                    last_block            INTEGER,
                    last_block_changed_at TEXT,
                    status_since          TEXT,
                    status                TEXT    DEFAULT 'unknown',
                    alerted               INTEGER DEFAULT 0,
                    UNIQUE(user_id, rpc_url)
                )
            """)
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
            if "status_since" not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN status_since TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL UNIQUE,
                    username         TEXT,
                    first_name       TEXT,
                    last_name        TEXT,
                    is_paused        INTEGER NOT NULL DEFAULT 0,
                    created_at       TEXT DEFAULT (datetime('now')),
                    updated_at       TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vdp_subscriptions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    topic_key  TEXT NOT NULL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, topic_key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vdp_watchlists (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    watch_type TEXT NOT NULL,
                    watch_value TEXT NOT NULL,
                    label      TEXT,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, watch_type, watch_value)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vdp_seen_state (
                    entity_type TEXT NOT NULL,
                    entity_key  TEXT NOT NULL,
                    state_json  TEXT NOT NULL,
                    updated_at  TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY(entity_type, entity_key)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_vdp_subscriptions_topic_enabled
                ON vdp_subscriptions (topic_key, is_enabled)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_vdp_watchlists_type_value_enabled
                ON vdp_watchlists (watch_type, watch_value, is_enabled)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS validator_watches (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id            INTEGER NOT NULL,
                    validator_id       INTEGER NOT NULL,
                    label              TEXT,
                    status             TEXT    DEFAULT 'unknown',
                    alerted            INTEGER DEFAULT 0,
                    status_since       TEXT,
                    last_in_set_at     TEXT,
                    last_consensus_mon REAL,
                    flags              INTEGER,
                    last_rewards       REAL,
                    last_produced_at   TEXT,
                    added_at           TEXT    DEFAULT (datetime('now')),
                    UNIQUE(user_id, validator_id)
                )
            """)
            vw_cols = {row["name"] for row in conn.execute("PRAGMA table_info(validator_watches)").fetchall()}
            if "last_rewards" not in vw_cols:
                conn.execute("ALTER TABLE validator_watches ADD COLUMN last_rewards REAL")
            if "last_produced_at" not in vw_cols:
                conn.execute("ALTER TABLE validator_watches ADD COLUMN last_produced_at TEXT")

    def count_user_nodes(self, user_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE user_id = ?", (user_id,)
            ).fetchone()[0]

    def _get_user_row_id(self, conn: sqlite3.Connection, telegram_user_id: int) -> Optional[int]:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def ensure_user(
        self,
        telegram_user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        first_name = ?,
                        last_name = ?,
                        updated_at = datetime('now')
                    WHERE telegram_user_id = ?
                    """,
                    (username, first_name, last_name, telegram_user_id),
                )
                return existing["id"]

            cursor = conn.execute(
                """
                INSERT INTO users (telegram_user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_user_id, username, first_name, last_name),
            )
            return int(cursor.lastrowid)

    def get_user_by_telegram_id(self, telegram_user_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_user_paused(self, telegram_user_id: int, paused: bool):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_paused = ?, updated_at = datetime('now')
                WHERE telegram_user_id = ?
                """,
                (1 if paused else 0, telegram_user_id),
            )

    def set_vdp_subscription(self, telegram_user_id: int, topic_key: str, enabled: bool):
        with self._connect() as conn:
            user_row_id = self._get_user_row_id(conn, telegram_user_id)
            if user_row_id is None:
                raise ValueError(f"User {telegram_user_id} must exist before setting subscriptions")

            conn.execute(
                """
                INSERT INTO vdp_subscriptions (user_id, topic_key, is_enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, topic_key)
                DO UPDATE SET
                    is_enabled = excluded.is_enabled,
                    updated_at = datetime('now')
                """,
                (user_row_id, topic_key, 1 if enabled else 0),
            )

    def set_vdp_subscriptions_for_topics(
        self,
        telegram_user_id: int,
        topic_keys: list[str],
        enabled: bool,
    ):
        if not topic_keys:
            return

        for topic_key in topic_keys:
            self.set_vdp_subscription(telegram_user_id, topic_key, enabled)

    def get_vdp_subscription_map(self, telegram_user_id: int) -> dict[str, bool]:
        with self._connect() as conn:
            user_row_id = self._get_user_row_id(conn, telegram_user_id)
            if user_row_id is None:
                return {}

            rows = conn.execute(
                "SELECT topic_key, is_enabled FROM vdp_subscriptions WHERE user_id = ?",
                (user_row_id,),
            ).fetchall()
            return {row["topic_key"]: bool(row["is_enabled"]) for row in rows}

    def add_vdp_watchlist(
        self,
        telegram_user_id: int,
        watch_type: str,
        watch_value: str,
        label: Optional[str] = None,
    ) -> tuple[bool, str]:
        with self._connect() as conn:
            user_row_id = self._get_user_row_id(conn, telegram_user_id)
            if user_row_id is None:
                raise ValueError(f"User {telegram_user_id} must exist before adding a watchlist entry")
            try:
                conn.execute(
                    """
                    INSERT INTO vdp_watchlists (user_id, watch_type, watch_value, label)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_row_id, watch_type, watch_value, label),
                )
                return True, ""
            except sqlite3.IntegrityError:
                return False, "duplicate"

    def get_vdp_watchlists(self, telegram_user_id: int) -> list[dict]:
        with self._connect() as conn:
            user_row_id = self._get_user_row_id(conn, telegram_user_id)
            if user_row_id is None:
                return []
            rows = conn.execute(
                """
                SELECT watch_type, watch_value, label, is_enabled, created_at
                FROM vdp_watchlists
                WHERE user_id = ?
                ORDER BY created_at, watch_type, watch_value
                """,
                (user_row_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def remove_vdp_watchlist(self, telegram_user_id: int, watch_type: str, watch_value: str) -> bool:
        with self._connect() as conn:
            user_row_id = self._get_user_row_id(conn, telegram_user_id)
            if user_row_id is None:
                return False
            cursor = conn.execute(
                """
                DELETE FROM vdp_watchlists
                WHERE user_id = ? AND watch_type = ? AND watch_value = ?
                """,
                (user_row_id, watch_type, watch_value),
            )
            return cursor.rowcount > 0

    def get_active_vdp_subscribers(self, topic_key: str) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.telegram_user_id
                FROM vdp_subscriptions s
                JOIN users u ON u.id = s.user_id
                WHERE s.topic_key = ?
                  AND s.is_enabled = 1
                  AND u.is_paused = 0
                """,
                (topic_key,),
            ).fetchall()
            return [int(row["telegram_user_id"]) for row in rows]

    def get_matching_watchlist_entries(self, watch_type: str, watch_value: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.telegram_user_id,
                    w.watch_type,
                    w.watch_value,
                    w.label
                FROM vdp_watchlists w
                JOIN users u ON u.id = w.user_id
                WHERE w.watch_type = ?
                  AND w.watch_value = ?
                  AND w.is_enabled = 1
                  AND u.is_paused = 0
                """,
                (watch_type, watch_value),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_seen_state(self, entity_type: str, entity_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state_json
                FROM vdp_seen_state
                WHERE entity_type = ? AND entity_key = ?
                """,
                (entity_type, entity_key),
            ).fetchone()
            if not row:
                return None
            return json.loads(row["state_json"])

    def set_seen_state(self, entity_type: str, entity_key: str, state: dict):
        payload = json.dumps(state, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vdp_seen_state (entity_type, entity_key, state_json)
                VALUES (?, ?, ?)
                ON CONFLICT(entity_type, entity_key)
                DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = datetime('now')
                """,
                (entity_type, entity_key, payload),
            )

    def add_node(self, user_id: int, rpc_url: str) -> tuple[bool, str]:
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO nodes (user_id, rpc_url) VALUES (?, ?)",
                    (user_id, rpc_url),
                )
                return True, ""
            except sqlite3.IntegrityError:
                return False, "duplicate"

    def remove_node(self, user_id: int, rpc_url: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM nodes WHERE user_id = ? AND rpc_url = ?",
                (user_id, rpc_url),
            )
            return cursor.rowcount > 0

    def get_user_nodes(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE user_id = ? ORDER BY added_at", (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_nodes(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM nodes").fetchall()]

    def update_node(
        self,
        node_id: int,
        last_block: Optional[int],
        last_block_changed_at: Optional[str],
        status_since: Optional[str],
        status: str,
        alerted: int,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE nodes
                SET last_block = ?,
                    last_block_changed_at = ?,
                    status_since = ?,
                    status = ?,
                    alerted = ?
                WHERE id = ?
                """,
                (last_block, last_block_changed_at, status_since, status, alerted, node_id),
            )

    # ------------------------------------------------------------------
    # On-chain validator-liveness watches (Phase 1)
    # user_id is the Telegram user id (used directly as chat_id), mirroring `nodes`.
    # ------------------------------------------------------------------
    def count_user_validator_watches(self, user_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM validator_watches WHERE user_id = ?", (user_id,)
            ).fetchone()[0]

    def add_validator_watch(
        self, user_id: int, validator_id: int, label: Optional[str] = None
    ) -> tuple[bool, str]:
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO validator_watches (user_id, validator_id, label) VALUES (?, ?, ?)",
                    (user_id, int(validator_id), label),
                )
                return True, ""
            except sqlite3.IntegrityError:
                return False, "duplicate"

    def remove_validator_watch(self, user_id: int, validator_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM validator_watches WHERE user_id = ? AND validator_id = ?",
                (user_id, int(validator_id)),
            )
            return cursor.rowcount > 0

    def get_user_validator_watches(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM validator_watches WHERE user_id = ? ORDER BY added_at",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_validator_watches(self) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM validator_watches").fetchall()
            ]

    def update_validator_watch(
        self,
        watch_id: int,
        status: str,
        alerted: int,
        status_since: Optional[str],
        last_in_set_at: Optional[str],
        last_consensus_mon: Optional[float],
        flags: Optional[int],
        last_rewards: Optional[float] = None,
        last_produced_at: Optional[str] = None,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE validator_watches
                SET status = ?,
                    alerted = ?,
                    status_since = ?,
                    last_in_set_at = ?,
                    last_consensus_mon = ?,
                    flags = ?,
                    last_rewards = ?,
                    last_produced_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    alerted,
                    status_since,
                    last_in_set_at,
                    last_consensus_mon,
                    flags,
                    last_rewards,
                    last_produced_at,
                    watch_id,
                ),
            )
