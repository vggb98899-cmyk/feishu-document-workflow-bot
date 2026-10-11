"""脱敏日志与轻量持久化去重。"""
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from config import DATABASE_TIMEOUT_SECONDS

class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.secrets = tuple(value for value in secrets if value)

    def format(self, record):
        rendered = super().format(record)
        for value in self.secrets:
            rendered = rendered.replace(value, "REDACTED")
        rendered = re.sub(r"(?:https?|wss?)://[^\s]+", "[URL_REDACTED]", rendered)
        return rendered

def configure_logging(settings=None, log_path=None, stream=None):
    secrets = (settings.app_id, settings.app_secret) if settings else ()
    formatter = RedactingFormatter(secrets)
    handlers = [logging.StreamHandler(stream)]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    sdk_logger = logging.getLogger("Lark")
    sdk_logger.handlers.clear()
    sdk_logger.propagate = True
    for name in ("httpx", "httpcore", "urllib3", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)

class MessageStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY, claimed_at TEXT NOT NULL,
                status TEXT NOT NULL, reply_id TEXT, completed_at TEXT)""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=DATABASE_TIMEOUT_SECONDS)

    def claim_message(self, message_id):
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, 'claimed', NULL, NULL)",
                (message_id, datetime.now(timezone.utc).isoformat()))
            return cursor.rowcount == 1

    def finish_message(self, message_id, status, reply_id=None):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "UPDATE messages SET status=?, reply_id=?, completed_at=? WHERE message_id=?",
                (status, reply_id, datetime.now(timezone.utc).isoformat(), message_id))
