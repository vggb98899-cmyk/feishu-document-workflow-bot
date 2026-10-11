"""Windows 内核文件锁：第二个 FEISHU_TEMPLATE_BOT Main 无法同时持有同一实例锁。"""
from __future__ import annotations

import msvcrt

from config import INSTANCE_LOCK_PATH


class MainInstanceLock:
    def __init__(self, path=INSTANCE_LOCK_PATH):
        self.path = path
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None
