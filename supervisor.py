"""Phase 5 Windows 常驻方案准备；尚未注册计划任务。"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from config import PROJECT_ROOT, RUNTIME_DIR, SUPERVISOR_RESTART_SECONDS
from single_instance import MainInstanceLock


LOGGER = logging.getLogger(__name__)
SUPERVISOR_LOCK_PATH = RUNTIME_DIR / "feishu_template_bot_supervisor.lock"
MAIN_ENTRY = PROJECT_ROOT / "main.py"
SUPERVISOR_LOG = RUNTIME_DIR / "supervisor.log"


def run_supervisor():
    """仅重启 Main 进程；Main 内的恢复扫描决定旧写任务能否继续。"""
    lock = MainInstanceLock(SUPERVISOR_LOCK_PATH)
    if not lock.acquire():
        raise RuntimeError("FEISHU_TEMPLATE_BOT_SUPERVISOR_INSTANCE_ALREADY_RUNNING")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    try:
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as stream:
            while True:
                child = subprocess.Popen([sys.executable, "-u", str(MAIN_ENTRY)],
                                         cwd=str(PROJECT_ROOT), stdout=stream, stderr=stream,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                LOGGER.info("FEISHU_TEMPLATE_BOT_MAIN_STARTED pid=%s", child.pid)
                code = child.wait()
                LOGGER.warning("FEISHU_TEMPLATE_BOT_MAIN_EXITED code=%s; restart scheduled", code)
                import threading
                threading.Event().wait(SUPERVISOR_RESTART_SECONDS)
    finally:
        lock.release()


if __name__ == "__main__":
    run_supervisor()
