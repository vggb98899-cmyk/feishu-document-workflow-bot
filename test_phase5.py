"""真实 consumer 线程死亡注入；只用隔离 SQLite 和假的外部 API。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
import uuid
import threading
from pathlib import Path
from types import SimpleNamespace as Item
from unittest.mock import Mock

from config import PROJECT_ROOT, Settings
from create_workflow import CreateWorkflow
from copy_workflow import CopyWorkflow
from copy_source import CopySource
from copy_store import CopyStore
from message_handler import MessageHandler
from operation_store import OperationStore
from reconciliation import ReadOnlyReconciler
from single_instance import MainInstanceLock
from test_phase2 import create_event
from work_queue import WorkQueue, operation_identity
from worker_runtime import WorkerRuntime


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        import threading
        threading.Event().wait(0.03)
    return False


class CrashQueue(WorkQueue):
    crash_on_idle = False

    def take_next(self, worker_id):
        if self.crash_on_idle:
            self.crash_on_idle = False
            raise SystemExit("real consumer exit")
        return super().take_next(worker_id)


class Phase5CrashTests(unittest.TestCase):
    def setUp(self):
        folder = PROJECT_ROOT / "evidence" / ("phase5-unit-" + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.path = folder / "messages.sqlite3"
        self.create_store = OperationStore(self.path)
        self.copy_store = CopyStore(self.path)
        self.queue = WorkQueue(self.path)
        self.create_api = Mock()
        self.create_api.get_document_url.return_value = "https://tenant.feishu.cn/docx/knownCreate"
        self.create_api.has_requester_edit.return_value = True
        self.create_api.get_document_public_view.return_value = "YES"
        self.copy_api = Mock()
        self.copy_api.reader.has_requester_edit.return_value = True
        self.copy_api.get_url.return_value = "https://tenant.feishu.cn/docx/knownCopy"
        self.reconciler = ReadOnlyReconciler(self.create_store, self.copy_store,
                                             self.create_api, self.copy_api)
        self.health_path = folder / "health.json"
        self.runtime = None

    def tearDown(self):
        if self.runtime:
            self.runtime.stop()

    def start_worker(self, dispatch, queue=None):
        self.runtime = WorkerRuntime(queue or self.queue, self.reconciler, dispatch,
                                     self.health_path, poll_seconds=0.03, stale_seconds=0.7)
        self.runtime.start()
        return self.runtime

    def test_idle_real_worker_death_main_detects_and_recovers(self):
        queue = CrashQueue(self.path)
        queue.crash_on_idle = True
        runtime = self.start_worker(lambda job: None, queue)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1))
        crash = runtime.last_crash_snapshot
        self.assertTrue(crash["main_process_alive"])
        self.assertFalse(crash["worker_alive"])
        self.assertEqual(runtime.crash_detected, 1)
        self.assertTrue(runtime.snapshot()["worker_alive"])
        self.assertEqual(queue.queue_length(), 0)
        self.assertIsNone(queue.active_job())

    def test_idle_heartbeat_updates_without_business_messages(self):
        runtime = self.start_worker(lambda job: None)
        self.assertTrue(wait_until(lambda: runtime.snapshot()["worker_alive"]))
        first = runtime.snapshot()["worker_heartbeat"]
        self.assertTrue(wait_until(lambda: runtime.snapshot()["worker_heartbeat"] != first))
        self.assertEqual(runtime.snapshot()["queue_length"], 0)

    def test_live_but_stalled_worker_is_unhealthy_without_second_worker(self):
        entered, release = threading.Event(), threading.Event()
        self.queue.enqueue("stalled", "CREATE_DOCUMENT", "ou_requester", "创建文档")

        def dispatch(job):
            entered.set()
            release.wait(2)

        runtime = WorkerRuntime(self.queue, self.reconciler, dispatch,
                                self.health_path, poll_seconds=0.03, stale_seconds=0.15)
        self.runtime = runtime
        runtime.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(wait_until(lambda: runtime.snapshot()["worker_thread_alive"] and
                                       not runtime.snapshot()["worker_alive"]))
            self.assertEqual(runtime.worker_recovered, 0)
            self.assertEqual(runtime.crash_detected, 0)
            self.assertIsNotNone(runtime.snapshot()["active_job"])
        finally:
            release.set()

    def test_before_claim_real_crash_safe_requeue_without_external_write(self):
        message_id = "before-claim"
        self.queue.enqueue(message_id, "CREATE_DOCUMENT", "ou_requester", "创建文档")
        calls = []

        def dispatch(job):
            calls.append(job["operation_id"])
            if len(calls) == 1:
                raise SystemExit("consumer died before operation claim")

        runtime = self.start_worker(dispatch)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1 and
                                   self.queue.get_job(operation_identity(message_id, "CREATE_DOCUMENT"))["status"] == "DONE"))
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.queue.is_message_claimed(message_id))
        self.assertIn("FAILED_SAFE_REQUEUED", runtime.recovery_results[-1].values())

    def test_real_post_write_pre_success_crash_create_read_back_no_recreate(self):
        message_id = "post-write-create"
        self.queue.enqueue(message_id, "CREATE_DOCUMENT", "ou_requester", "创建文档")
        external_calls = []

        def dispatch(job):
            self.create_store.claim_operation(job["message_id"], job["requester"])
            self.create_store.save_operation(message_id, "RECEIVED", "FOLDER_VERIFIED",
                                             folder_token="knownFolder")
            self.create_store.begin_create(message_id)
            external_calls.append("CREATE")  # fake API 已返回 ID；没有飞书网络调用。
            self.create_store.save_operation(message_id, "PARTIAL", "GRANT",
                                             document_id="knownCreate", folder_token="knownFolder",
                                             checkpoint="AFTER_EXTERNAL_WRITE")
            raise SystemExit("consumer died after returned ID, before SUCCESS")

        runtime = self.start_worker(dispatch)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1 and
                                   self.create_store.get_operation(message_id)["status"] == "SUCCESS"))
        self.assertEqual(external_calls, ["CREATE"])
        self.assertEqual(self.create_store.get_operation(message_id)["checkpoint"], "VERIFIED")
        self.assertIn("SUCCESS_EXISTING_DOCUMENT", runtime.recovery_results[-1].values())
        self.create_api.confirm_placement.assert_called_with("knownCreate", "knownFolder")
        self.create_api.grant_collaborator.assert_not_called()

    def test_real_post_write_pre_success_crash_copy_read_back_no_recopy(self):
        message_id = "post-write-copy"
        self.queue.enqueue(message_id, "COPY_DOCUMENT", "ou_requester",
                           "复制副本 https://tenant.feishu.cn/docx/source", "source")
        external_calls = []

        def dispatch(job):
            self.copy_store.claim(job["message_id"], job["requester"])
            self.copy_store.save(message_id, "RECEIVED", "SOURCE_VERIFIED",
                                 source_token="source", parent_token="knownFolder")
            self.copy_store.begin_copy(message_id)
            external_calls.append("COPY")
            self.copy_store.save(message_id, "PARTIAL", "PLACEMENT",
                                 document_id="knownCopy", checkpoint="AFTER_EXTERNAL_WRITE")
            raise SystemExit("consumer died after copy token, before SUCCESS")

        runtime = self.start_worker(dispatch)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1 and
                                   self.copy_store.get(message_id)["status"] == "SUCCESS"))
        self.assertEqual(external_calls, ["COPY"])
        self.assertIn("SUCCESS_EXISTING_COPY", runtime.recovery_results[-1].values())
        self.copy_api.confirm_placement.assert_called_with("knownCopy", "knownFolder")
        self.copy_api.ensure_requester_edit.assert_not_called()

    def test_http_unknown_real_worker_crash_becomes_ambiguous_no_retry(self):
        message_id = "http-timeout"
        self.queue.enqueue(message_id, "CREATE_DOCUMENT", "ou_requester", "创建文档")
        external_attempts = []

        def dispatch(job):
            self.create_store.claim_operation(message_id, job["requester"])
            self.create_store.begin_create(message_id)
            external_attempts.append("REQUEST_SENT")
            raise SystemExit("consumer died after transport timeout, result unknown")

        runtime = self.start_worker(dispatch)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1 and
                                   self.create_store.get_operation(message_id)["status"] == "AMBIGUOUS"))
        self.assertEqual(external_attempts, ["REQUEST_SENT"])
        self.assertEqual(self.create_store.get_operation(message_id)["create_attempts"], 1)
        self.assertEqual(self.queue.get_job(operation_identity(message_id,"CREATE_DOCUMENT"))["status"], "HELD")
        self.assertIn("AMBIGUOUS_NO_DOCUMENT_ID", runtime.recovery_results[-1].values())

    def test_external_success_before_id_checkpoint_is_manual_ambiguous(self):
        message_id = "success-id-not-saved"
        self.queue.enqueue(message_id, "COPY_DOCUMENT", "ou_requester", "复制副本", "source")
        external_calls = []

        def dispatch(job):
            self.copy_store.claim(message_id, job["requester"])
            self.copy_store.begin_copy(message_id)
            external_calls.append("COPY_RETURNED_TOKEN")
            raise SystemExit("worker died before returned token could be persisted")

        runtime = self.start_worker(dispatch)
        self.assertTrue(wait_until(lambda: runtime.worker_recovered == 1 and
                                   self.copy_store.get(message_id)["status"] == "AMBIGUOUS"))
        self.assertEqual(external_calls, ["COPY_RETURNED_TOKEN"])
        self.assertIsNone(self.copy_store.get(message_id)["document_id"])
        self.assertEqual(self.copy_store.get(message_id)["copy_attempts"], 1)
        self.copy_api.copy_document.assert_not_called()

    def test_ambiguous_copy_without_token_is_read_only_and_held(self):
        message_id = "unknown-copy"
        self.copy_store.claim(message_id, "ou_requester")
        self.copy_store.begin_copy(message_id)
        self.queue.enqueue("separate", "COPY_DOCUMENT", "ou_requester", "复制副本", "source")
        runtime = self.start_worker(lambda job: None)
        self.assertTrue(wait_until(lambda: self.copy_store.get(message_id)["status"] == "AMBIGUOUS"))
        self.assertEqual(self.copy_store.get(message_id)["copy_attempts"], 1)
        self.copy_api.copy_document.assert_not_called()
        self.assertIn("AMBIGUOUS_NO_COPY_TOKEN", runtime.recovery_results[0].values())

    def test_actual_timeout_exception_is_not_failed_safe_or_retried(self):
        settings = Settings("fake-app", "fake-secret", True, "knownFolder", True,
                            ("knownFolder",))
        create_api = Mock()
        create_api.verify_public_folder.return_value = "knownFolder"
        create_api.create_document.side_effect = TimeoutError("transport result unknown")
        create_flow = CreateWorkflow(settings, self.create_store, create_api)
        create_flow.handle_create("timeout-create", "ou_requester")
        create_flow.handle_create("timeout-create", "ou_requester")
        operation = self.create_store.get_operation("timeout-create")
        self.assertEqual(operation["checkpoint"], "BEFORE_EXTERNAL_WRITE")
        self.assertEqual(operation["create_attempts"], 1)
        self.assertEqual(self.reconciler.reconcile_create(operation), "AMBIGUOUS_NO_DOCUMENT_ID")
        create_api.create_document.assert_called_once()
        copy_api = Mock()
        copy_api.inspect_source.return_value = CopySource("sourceToken", "knownFolder", "Title")
        copy_api.copy_document.side_effect = ConnectionError("transport result unknown")
        copy_flow = CopyWorkflow(settings, self.copy_store, copy_api)
        text = "复制副本 https://tenant.feishu.cn/docx/sourceToken"
        copy_flow.handle_copy("timeout-copy", "ou_requester", text)
        copy_flow.handle_copy("timeout-copy", "ou_requester", text)
        copy_operation = self.copy_store.get("timeout-copy")
        self.assertEqual(copy_operation["copy_attempts"], 1)
        self.assertEqual(self.reconciler.reconcile_copy(copy_operation), "AMBIGUOUS_NO_COPY_TOKEN")
        copy_api.copy_document.assert_called_once()

    def test_known_copy_without_requester_edit_remains_partial_no_grant(self):
        message_id = "copy-partial"
        self.copy_store.claim(message_id, "ou_requester")
        self.copy_store.begin_copy(message_id)
        self.copy_store.save(message_id, "PARTIAL", "GRANT",
                             document_id="knownCopy", parent_token="knownFolder")
        self.copy_api.reader.has_requester_edit.return_value = False
        runtime = self.start_worker(lambda job: None)
        self.assertTrue(wait_until(lambda: runtime.recovery_results))
        self.assertEqual(self.copy_store.get(message_id)["status"], "PARTIAL")
        self.copy_api.ensure_requester_edit.assert_not_called()

    def test_queue_operation_identity_is_stable_and_source_sensitive(self):
        first = operation_identity("same-message", "COPY_DOCUMENT", "source1")
        self.assertEqual(first, operation_identity("same-message", "COPY_DOCUMENT", "source1"))
        self.assertNotEqual(first, operation_identity("same-message", "COPY_DOCUMENT", "source2"))
        self.assertNotEqual(first, operation_identity("same-message", "CREATE_DOCUMENT"))
        self.assertTrue(self.queue.enqueue("same-message", "COPY_DOCUMENT", "ou_requester",
                                           "复制副本", "source1"))
        self.assertFalse(self.queue.enqueue("same-message", "COPY_DOCUMENT", "ou_requester",
                                            "复制副本", "source1"))

    def test_historical_claimed_message_is_never_requeued(self):
        self.create_store.claim_message("old-message")
        self.assertFalse(self.queue.enqueue("old-message", "CREATE_DOCUMENT",
                                            "ou_requester", "创建文档"))
        self.assertEqual(self.queue.queue_length(), 0)

    def test_second_main_instance_file_lock_is_denied(self):
        path = self.health_path.parent / "main.lock"
        first, second = MainInstanceLock(path), MainInstanceLock(path)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()

    def test_second_real_process_cannot_take_main_instance_lock(self):
        path = self.health_path.parent / "cross-process.lock"
        code = ("from pathlib import Path; import sys; "
                "from single_instance import MainInstanceLock; "
                "lock=MainInstanceLock(Path(sys.argv[1])); "
                "print(int(lock.acquire()),flush=True); sys.stdin.readline(); lock.release()")
        child = subprocess.Popen([sys.executable, "-c", code, str(path)],
                                 cwd=str(PROJECT_ROOT), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(child.stdout.readline().strip(), "1")
            second = MainInstanceLock(path)
            self.assertFalse(second.acquire())
            child.stdin.write("\n")
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=3), 0)
        finally:
            if child.poll() is None:
                child.stdin.write("\n")
                child.stdin.flush()
                child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_event_callback_only_enqueues_without_business_api(self):
        workflow = Mock()
        copy_workflow = Mock()
        reply = Mock()
        handler = MessageHandler("bot-id", self.create_store, reply,
                                 workflow, copy_workflow, self.queue)
        event = create_event(message_id="queued-event")
        handler.handle_event(event)
        self.assertEqual(self.queue.queue_length(), 1)
        workflow.handle_create.assert_not_called()
        copy_workflow.handle_copy.assert_not_called()
        reply.assert_not_called()
