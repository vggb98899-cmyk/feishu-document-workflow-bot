"""与 Phase 1 共用 SQLite，独立保存创建操作和结果。"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from utils import MessageStore

class OperationStore(MessageStore):
    def __init__(self, path):
        super().__init__(path)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS create_operations (
                message_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL,
                status TEXT NOT NULL, create_attempts INTEGER NOT NULL DEFAULT 0,
                document_id TEXT, revision_id INTEGER, url TEXT,
                collaborator_granted INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

        self.migrate_folder_columns()
        self.migrate_recovery_columns()
        self.migrate_owner_columns()
        self.retire_global_create_guard()

    def migrate_owner_columns(self):
        with closing(self.connect()) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(create_operations)")}
            if "owner_transfer_attempts" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN owner_transfer_attempts INTEGER NOT NULL DEFAULT 0")
            if "owner_transfer_status" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN owner_transfer_status TEXT DEFAULT ''")

    def migrate_recovery_columns(self):
        with closing(self.connect()) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(create_operations)")}
            if "checkpoint" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN checkpoint TEXT")
            if "checkpoint_at" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN checkpoint_at TEXT")

    def migrate_folder_columns(self):
        with closing(self.connect()) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(create_operations)")}
            if "folder_token" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN folder_token TEXT")
            if "child_public_view" not in columns:
                connection.execute("ALTER TABLE create_operations ADD COLUMN child_public_view TEXT DEFAULT 'UNKNOWN'")

    def retire_global_create_guard(self):
        # 正式规则：只按 message_id 去重。移除旧 PoC 触发器，不删历史操作。
        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER IF EXISTS poc_one_create_only")

    def claim_operation(self, message_id, sender_id):
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, 'claimed', NULL, NULL)",
                (message_id, now))
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """INSERT INTO create_operations
                (message_id, sender_id, status, stage, created_at, updated_at)
                VALUES (?, ?, 'RECEIVED', 'GATE', ?, ?)""", (message_id, sender_id, now, now))
            connection.execute("""UPDATE create_operations SET checkpoint='PREPARED',
                checkpoint_at=? WHERE message_id=?""", (now, message_id))
            return True

    def begin_create(self, message_id):
        # UNKNOWN 在外部请求前提交；即使进程突然退出也不会自动重发 create。
        with closing(self.connect()) as connection, connection:
            now = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """UPDATE create_operations SET status='UNKNOWN', stage='CREATE',
                checkpoint='BEFORE_EXTERNAL_WRITE', checkpoint_at=?,
                create_attempts=1, updated_at=? WHERE message_id=?
                AND status='RECEIVED' AND create_attempts=0""",
                (now, now, message_id))
            return cursor.rowcount == 1

    def save_operation(self, message_id, status, stage, **fields):
        allowed = {"document_id", "revision_id", "url", "collaborator_granted", "folder_token", "child_public_view", "checkpoint", "owner_transfer_attempts", "owner_transfer_status"}
        if not set(fields).issubset(allowed):
            raise ValueError("Invalid operation field")
        updates = {"status": status, "stage": stage,
                   "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
        if "checkpoint" in fields:
            updates["checkpoint_at"] = updates["updated_at"]
        assignments = ", ".join(name + "=?" for name in updates)
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE create_operations SET " + assignments + " WHERE message_id=?",
                (*updates.values(), message_id))
            if cursor.rowcount != 1:
                raise ValueError("Operation not found")

    def get_operation(self, message_id):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM create_operations WHERE message_id=?", (message_id,)).fetchone()
            return dict(row) if row else None

    def list_recoverable(self):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("""SELECT * FROM create_operations
                WHERE status IN ('RECEIVED','UNKNOWN','AMBIGUOUS','PARTIAL')
                ORDER BY created_at""")]
