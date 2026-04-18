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
                    status                TEXT    DEFAULT 'unknown',
                    alerted               INTEGER DEFAULT 0,
                    UNIQUE(user_id, rpc_url)
                )
            """)

    def count_user_nodes(self, user_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE user_id = ?", (user_id,)
            ).fetchone()[0]

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
        status: str,
        alerted: int,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE nodes
                SET last_block = ?,
                    last_block_changed_at = ?,
                    status = ?,
                    alerted = ?
                WHERE id = ?
                """,
                (last_block, last_block_changed_at, status, alerted, node_id),
            )
