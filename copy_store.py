"""复制状态独立建表；与现有消息表共用同一原子消息认领。"""
from contextlib import closing
from datetime import datetime, timezone
from utils import MessageStore

class CopyStore(MessageStore):
    def __init__(self, path):
        super().__init__(path)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS copy_operations (
                message_id TEXT PRIMARY KEY, requester TEXT NOT NULL, source_token TEXT,
                parent_token TEXT, status TEXT NOT NULL, stage TEXT NOT NULL,
                copy_attempts INTEGER NOT NULL DEFAULT 0, document_id TEXT,
                requester_edit INTEGER NOT NULL DEFAULT 0, url TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(copy_operations)")}
            if "checkpoint" not in columns:
                connection.execute("ALTER TABLE copy_operations ADD COLUMN checkpoint TEXT")
            if "checkpoint_at" not in columns:
                connection.execute("ALTER TABLE copy_operations ADD COLUMN checkpoint_at TEXT")
            if "owner_transfer_attempts" not in columns:
                connection.execute("ALTER TABLE copy_operations ADD COLUMN owner_transfer_attempts INTEGER NOT NULL DEFAULT 0")
            if "owner_transfer_status" not in columns:
                connection.execute("ALTER TABLE copy_operations ADD COLUMN owner_transfer_status TEXT DEFAULT ''")

    def claim(self, message_id, requester):
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("INSERT OR IGNORE INTO messages VALUES (?,?,'claimed',NULL,NULL)", (message_id, now))
            if cursor.rowcount != 1:
                return False
            connection.execute("""INSERT INTO copy_operations
                (message_id,requester,status,stage,created_at,updated_at)
                VALUES (?,?,'RECEIVED','INPUT',?,?)""", (message_id, requester, now, now))
            connection.execute("""UPDATE copy_operations SET checkpoint='PREPARED',
                checkpoint_at=? WHERE message_id=?""", (now, message_id))
            return True

    def get(self, message_id):
        import sqlite3
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM copy_operations WHERE message_id=?", (message_id,)).fetchone()
            return dict(row) if row else None

    def save(self, message_id, status, stage, **fields):
        allowed = {"source_token", "parent_token", "document_id", "requester_edit", "url", "checkpoint", "owner_transfer_attempts", "owner_transfer_status"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid copy field")
        updates = dict(status=status, stage=stage, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
        if "checkpoint" in fields:
            updates["checkpoint_at"] = updates["updated_at"]
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("UPDATE copy_operations SET " + ",".join(key+"=?" for key in updates)
                                        + " WHERE message_id=?", (*updates.values(), message_id))
            if cursor.rowcount != 1:
                raise ValueError("Missing copy operation")

    def begin_copy(self, message_id):
        with closing(self.connect()) as connection, connection:
            now = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute("""UPDATE copy_operations SET status='UNKNOWN',stage='COPY',copy_attempts=1,
                checkpoint='BEFORE_EXTERNAL_WRITE', checkpoint_at=?,
                updated_at=? WHERE message_id=? AND status='RECEIVED' AND copy_attempts=0""",
                (now, now, message_id))
            return cursor.rowcount == 1

    def list_recoverable(self):
        import sqlite3
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("""SELECT * FROM copy_operations
                WHERE status IN ('RECEIVED','UNKNOWN','AMBIGUOUS','PARTIAL')
                ORDER BY created_at""")]
