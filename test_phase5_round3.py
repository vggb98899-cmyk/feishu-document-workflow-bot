import time
import unittest
import uuid
from unittest.mock import Mock

from config import PROJECT_ROOT
from copy_store import CopyStore
from operation_store import OperationStore
from reconciliation import ReadOnlyReconciler
from work_queue import WorkQueue, operation_identity


class Round3Tests(unittest.TestCase):
    def setUp(self):
        folder = PROJECT_ROOT / "evidence" / ("phase5r3-unit-" + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.path = folder / "messages.sqlite3"
        self.create_store = OperationStore(self.path)
        self.copy_store = CopyStore(self.path)
        self.queue = WorkQueue(self.path)
        self.create = Mock()
        self.create.get_document_url.return_value = "https://tenant.feishu.cn/docx/recovered"
        self.create.confirm_placement.return_value = None
        self.create.has_requester_edit.return_value = True
        self.create.get_document_public_view.return_value = "YES"
        self.copy = Mock()
        self.copy.get_url.return_value = "https://tenant.feishu.cn/docx/copied"
        self.copy.confirm_placement.return_value = None
        self.copy.reader.has_requester_edit.return_value = True

    def make_create(self, message="m-create"):
        self.create_store.claim_operation(message, "ou_requester")
        self.create_store.save_operation(message, "PARTIAL", "GRANT",
                                         document_id="recovered", folder_token="folder",
                                         checkpoint="AFTER_EXTERNAL_WRITE")
        self.queue.enqueue(message, "CREATE_DOCUMENT", "ou_requester", "创建文档")
        # enqueue is intentionally blocked by existing messages; model recovered queue row directly.
        with self.queue.connect() as c, c:
            c.execute("INSERT OR REPLACE INTO work_queue(operation_id,message_id,operation_type,requester,command_text,status,enqueued_at,updated_at) VALUES(?,?,?,?,?,'ACTIVE',datetime('now'),datetime('now'))",
                      (operation_identity(message,"CREATE_DOCUMENT"), message, "CREATE_DOCUMENT", "ou_requester", "创建文档"))
        return message

    def test_recovered_create_notifies_once_without_create(self):
        message = self.make_create()
        notifications = Mock(return_value="reply-1")
        reconciler = ReadOnlyReconciler(self.create_store, self.copy_store, self.create, self.copy, notifications)
        result = reconciler.scan(self.queue)
        self.assertIn("SUCCESS_EXISTING_DOCUMENT", result.values())
        self.assertEqual(notifications.call_count, 1)
        self.assertEqual(self.queue.get_notification_status(operation_identity(message,"CREATE_DOCUMENT")), "SENT")
        self.assertEqual(reconciler.scan(self.queue), {})
        self.assertEqual(notifications.call_count, 1)
        self.create.create_document.assert_not_called()

    def test_recovered_copy_notifies_once_without_copy(self):
        message = "m-copy"
        self.copy_store.claim(message, "ou_requester")
        self.copy_store.save(message, "PARTIAL", "PLACEMENT", document_id="copied",
                              parent_token="folder", checkpoint="AFTER_EXTERNAL_WRITE")
        with self.queue.connect() as c, c:
            c.execute("INSERT INTO work_queue(operation_id,message_id,operation_type,requester,source_token,command_text,status,enqueued_at,updated_at) VALUES(?,?,?,?,? ,?,'ACTIVE',datetime('now'),datetime('now'))",
                      (operation_identity(message,"COPY_DOCUMENT","source"), message, "COPY_DOCUMENT", "ou_requester", "source", "复制副本"))
        notifications = Mock(return_value="reply-copy")
        reconciler = ReadOnlyReconciler(self.create_store, self.copy_store, self.create, self.copy, notifications)
        result = reconciler.scan(self.queue)
        self.assertIn("SUCCESS_EXISTING_COPY", result.values())
        self.assertEqual(notifications.call_count, 1)
        self.copy.copy_document.assert_not_called()

    def test_notification_failure_unknown_keeps_business_success(self):
        message = self.make_create("m-failure")
        notifications = Mock(side_effect=TimeoutError("reply result unknown"))
        reconciler = ReadOnlyReconciler(self.create_store, self.copy_store, self.create, self.copy, notifications)
        reconciler.scan(self.queue)
        operation_id = operation_identity(message,"CREATE_DOCUMENT")
        self.assertEqual(self.create_store.get_operation(message)["status"], "SUCCESS")
        self.assertEqual(self.queue.get_notification_status(operation_id), "UNKNOWN")
        self.assertEqual(notifications.call_count, 1)
        reconciler.scan(self.queue)
        self.assertEqual(notifications.call_count, 1)

    def test_ambiguous_and_partial_do_not_send_success(self):
        message = "m-ambiguous"
        self.create_store.claim_operation(message, "ou_requester")
        self.create_store.begin_create(message)
        self.queue.enqueue(message, "CREATE_DOCUMENT", "ou_requester", "创建文档")
        with self.queue.connect() as c, c:
            c.execute("UPDATE work_queue SET status='ACTIVE' WHERE message_id=?", (message,))
        notifications = Mock()
        reconciler = ReadOnlyReconciler(self.create_store, self.copy_store, self.create, self.copy, notifications)
        reconciler.scan(self.queue)
        self.assertEqual(notifications.call_count, 0)
        self.assertEqual(self.create_store.get_operation(message)["status"], "AMBIGUOUS")

    def test_notification_status_claim_is_atomic(self):
        message = self.make_create("m-atomic")
        operation_id = operation_identity(message,"CREATE_DOCUMENT")
        self.assertTrue(self.queue.claim_notification(operation_id))
        self.assertFalse(self.queue.claim_notification(operation_id))
        self.queue.mark_notification(operation_id, "UNKNOWN")
        self.assertFalse(self.queue.claim_notification(operation_id))
