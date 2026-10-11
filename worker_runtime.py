"""独立 consumer 线程、独立心跳和 Main 内死亡监控。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from config import (HEALTH_PATH, WORKER_HEARTBEAT_SECONDS, WORKER_POLL_SECONDS,
                    WORKER_STALE_SECONDS, CONNECTION_UNHEALTHY_SECONDS)


LOGGER = logging.getLogger(__name__)


class WorkerRuntime:
    def __init__(self, queue, reconciler, dispatch, health_path=HEALTH_PATH,
                 poll_seconds=WORKER_POLL_SECONDS, stale_seconds=WORKER_STALE_SECONDS):
        self.queue = queue
        self.reconciler = reconciler
        self.dispatch = dispatch
        self.health_path = health_path
        self.poll_seconds = poll_seconds
        self.stale_seconds = stale_seconds
        self.worker_thread = None
        self.worker_id = None
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.fatal_exception = None
        self.crash_detected = 0
        self.worker_recovered = 0
        self.last_crash_snapshot = None
        self.recovery_results = []
        self.connection_probe = None
        self.last_connection_ok = None
        self.connection_state = "STARTING"
        self.started_at = datetime.now(timezone.utc)
        self._guard = threading.Lock()

    def start(self):
        with self._guard:
            self._start_worker_locked()
        self.monitor_thread = threading.Thread(target=self._monitor,
                                               name="FEISHU_TEMPLATE_BOT-Main-Monitor", daemon=True)
        self.monitor_thread.start()

    def _start_worker_locked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            raise RuntimeError("Second worker forbidden while first is alive")
        self.worker_id = str(uuid.uuid4())
        self.worker_thread = threading.Thread(target=self._run_worker,
                                              name="FEISHU_TEMPLATE_BOT-Worker-" + self.worker_id[:8],
                                              daemon=True)
        self.worker_thread.start()

    def _run_worker(self):
        worker_id = self.worker_id
        try:
            self.queue.record_heartbeat(worker_id, progressed=True)
            results = self.reconciler.scan(self.queue)
            self.recovery_results.append(results)
            if results:
                LOGGER.warning("WORKER_RECOVERY_SCAN count=%d", len(results))
            self.queue.record_heartbeat(worker_id, progressed=True)
            while not self.stop_event.is_set():
                job = self.queue.take_next(worker_id)
                if job is None:
                    self.queue.record_heartbeat(worker_id)
                    self.stop_event.wait(self.poll_seconds)
                    continue
                self.queue.record_heartbeat(worker_id, job["operation_id"], progressed=True)
                try:
                    self.dispatch(job)
                except Exception:
                    # 外部请求可能已发出。留下 ACTIVE，恢复仅核单；不自动重跑。
                    LOGGER.exception("WORKER_JOB_INTERRUPTED operation_id=%s", job["operation_id"])
                    raise
                self.queue.set_job_state(job["operation_id"], "DONE", "WORKFLOW_RETURNED")
                self.queue.record_heartbeat(worker_id, progressed=True)
        except BaseException as error:
            self.fatal_exception = type(error).__name__
            LOGGER.error("WORKER_FATAL worker_id=%s exception_type=%s",
                         worker_id, self.fatal_exception)
            try:
                self.queue.record_heartbeat(worker_id, fatal_exception=self.fatal_exception)
            except Exception:
                LOGGER.warning("WORKER_FATAL_HEARTBEAT_FAILED")

    def _monitor(self):
        while not self.stop_event.is_set():
            with self._guard:
                thread = self.worker_thread
                if thread and not thread.is_alive():
                    self.crash_detected += 1
                    self.last_crash_snapshot = self.snapshot()
                    LOGGER.error("WORKER_CRASH_DETECTED old_worker_id=%s", self.worker_id)
                    self._start_worker_locked()
                    self.worker_recovered += 1
                    LOGGER.warning("WORKER_RESTARTED new_worker_id=%s", self.worker_id)
            self._update_connection()
            try:
                self._write_health(self.snapshot())
            except Exception:
                LOGGER.warning("HEALTH_SNAPSHOT_WRITE_FAILED")
            self.stop_event.wait(self.poll_seconds)

    def _update_connection(self):
        if not self.connection_probe:
            return
        try:
            connected = bool(self.connection_probe())
        except Exception:
            connected = False
        if connected:
            self.connection_state = "CONNECTED"
            self.last_connection_ok = datetime.now(timezone.utc).isoformat()
        elif self.connection_state != "STARTING":
            self.connection_state = "RECONNECTING"
        since_ok = (datetime.now(timezone.utc) -
                    datetime.fromisoformat(self.last_connection_ok)).total_seconds() if self.last_connection_ok else (
                        datetime.now(timezone.utc) - self.started_at).total_seconds()
        if not connected and since_ok > CONNECTION_UNHEALTHY_SECONDS:
            self.connection_state = "DISCONNECTED_UNHEALTHY"

    def snapshot(self):
        worker = self.worker_thread
        heartbeat = self.queue.get_worker_heartbeat(self.worker_id) if self.worker_id else None
        timestamp = heartbeat["heartbeat_at"] if heartbeat else None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds() if timestamp else None
        active = self.queue.active_job()
        active_age = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(active["started_at"])).total_seconds() if active else None
        return {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "main_process_alive": True, "main_pid": os.getpid(),
            "worker_alive": bool(worker and worker.is_alive() and age is not None and
                                 age <= self.stale_seconds),
            "worker_thread_alive": bool(worker and worker.is_alive()),
            "worker_id": self.worker_id,
            "worker_heartbeat": timestamp,
            "worker_last_progress_at": heartbeat["last_progress_at"] if heartbeat else None,
            "queue_length": self.queue.queue_length(),
            "active_job": active["operation_id"] if active else None,
            "active_job_age_seconds": round(active_age, 3) if active_age is not None else None,
            "fatal_exception": self.fatal_exception,
            "crash_detected": self.crash_detected,
            "worker_recovered": self.worker_recovered,
            "connection_state": self.connection_state,
            "connection_healthy": self.connection_state == "CONNECTED",
            "last_connection_ok": self.last_connection_ok,
        }

    def _write_health(self, snapshot):
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path = self.health_path.with_suffix(".tmp")
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(snapshot_path, self.health_path)

    def stop(self):
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join(timeout=WORKER_HEARTBEAT_SECONDS)
        if self.monitor_thread:
            self.monitor_thread.join(timeout=WORKER_HEARTBEAT_SECONDS)
