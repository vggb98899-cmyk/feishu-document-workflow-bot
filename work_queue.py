"""FEISHU_TEMPLATE_BOT 持久事件队列；消息先落库，worker 才执行外部写操作。"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from utils import MessageStore


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def operation_identity(message_id, operation_type, source_token=""):
    value = f"FEISHU_TEMPLATE_BOT:{message_id}:{operation_type}:{source_token}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


class WorkQueue(MessageStore):
    def __init__(self, path):
        super().__init__(path)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS work_queue (
                operation_id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                operation_type TEXT NOT NULL, requester TEXT NOT NULL,
                source_token TEXT NOT NULL DEFAULT '', command_text TEXT NOT NULL,
                status TEXT NOT NULL, enqueued_at TEXT NOT NULL,
                started_at TEXT, updated_at TEXT NOT NULL, worker_id TEXT,
                recovery_result TEXT, notification_status TEXT NOT NULL DEFAULT 'PENDING')""")
            connection.execute("""CREATE TABLE IF NOT EXISTS worker_health (
                worker_id TEXT PRIMARY KEY, heartbeat_at TEXT NOT NULL,
                last_progress_at TEXT, active_operation_id TEXT,
                fatal_exception TEXT)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(work_queue)")}
            if "notification_status" not in columns:
                connection.execute("ALTER TABLE work_queue ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'PENDING'")

    def enqueue(self, message_id, operation_type, requester, text, source_token=""):
        if operation_type not in {"CREATE_DOCUMENT", "COPY_DOCUMENT"}:
            raise ValueError("Unsupported queued operation")
        identity = operation_identity(message_id, operation_type, source_token)
        now = utc_now()
        with closing(self.connect()) as connection, connection:
            if connection.execute("SELECT 1 FROM messages WHERE message_id=?", (message_id,)).fetchone():
                return False
            cursor = connection.execute("""INSERT OR IGNORE INTO work_queue
                (operation_id,message_id,operation_type,requester,source_token,
                 command_text,status,enqueued_at,updated_at)
                VALUES (?,?,?,?,?,?,'PENDING',?,?)""",
                (identity, message_id, operation_type, requester, source_token, text, now, now))
            return cursor.rowcount == 1

    def take_next(self, worker_id):
        with closing(self.connect()) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT * FROM work_queue WHERE status='PENDING'
                ORDER BY enqueued_at, operation_id LIMIT 1""").fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute("""UPDATE work_queue SET status='ACTIVE',
                started_at=?, updated_at=?, worker_id=? WHERE operation_id=?
                AND status='PENDING'""", (now, now, worker_id, row["operation_id"]))
            return dict(row) | {"status": "ACTIVE", "started_at": now, "worker_id": worker_id}

    def set_job_state(self, operation_id, status, recovery_result=None):
        if status not in {"PENDING", "HELD", "DONE"}:
            raise ValueError("Invalid queue terminal state")
        with closing(self.connect()) as connection, connection:
            connection.execute("""UPDATE work_queue SET status=?, recovery_result=?,
                updated_at=?, worker_id=NULL WHERE operation_id=?""",
                (status, recovery_result, utc_now(), operation_id))

    def get_notification_status(self, operation_id):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT notification_status FROM work_queue WHERE operation_id=?",
                                     (operation_id,)).fetchone()
            return row[0] if row else None

    def claim_notification(self, operation_id):
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("""UPDATE work_queue SET notification_status='SENDING',
                updated_at=? WHERE operation_id=? AND notification_status='PENDING'""",
                (utc_now(), operation_id))
            return cursor.rowcount == 1

    def mark_notification(self, operation_id, status):
        if status not in {"SENT", "UNKNOWN"}:
            raise ValueError("Invalid notification status")
        with closing(self.connect()) as connection, connection:
            connection.execute("UPDATE work_queue SET notification_status=?, updated_at=? WHERE operation_id=?",
                               (status, utc_now(), operation_id))

    def list_interrupted(self):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("""SELECT * FROM work_queue
                WHERE status IN ('ACTIVE','HELD') ORDER BY enqueued_at""")]

    def get_job(self, operation_id):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM work_queue WHERE operation_id=?",
                                     (operation_id,)).fetchone()
            return dict(row) if row else None

    def list_all(self):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT message_id,operation_id,status FROM work_queue")]

    def is_message_claimed(self, message_id):
        with closing(self.connect()) as connection:
            return connection.execute("SELECT 1 FROM messages WHERE message_id=?",
                                      (message_id,)).fetchone() is not None

    def record_heartbeat(self, worker_id, active_operation_id=None, progressed=False,
                         fatal_exception=None):
        now = utc_now()
        with closing(self.connect()) as connection, connection:
            connection.execute("""INSERT INTO worker_health
                (worker_id,heartbeat_at,last_progress_at,active_operation_id,fatal_exception)
                VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                heartbeat_at=excluded.heartbeat_at,
                last_progress_at=CASE WHEN ? THEN excluded.last_progress_at
                                      ELSE worker_health.last_progress_at END,
                active_operation_id=excluded.active_operation_id,
                fatal_exception=excluded.fatal_exception""",
                (worker_id, now, now if progressed else None,
                 active_operation_id, fatal_exception, int(progressed)))

    def get_worker_heartbeat(self, worker_id):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM worker_health WHERE worker_id=?",
                                     (worker_id,)).fetchone()
            return dict(row) if row else None

    def queue_length(self):
        with closing(self.connect()) as connection:
            return connection.execute("SELECT count(*) FROM work_queue WHERE status='PENDING'").fetchone()[0]

    def active_job(self):
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM work_queue WHERE status='ACTIVE' LIMIT 1").fetchone()
            return dict(row) if row else None
