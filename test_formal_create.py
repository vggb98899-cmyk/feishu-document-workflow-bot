"""正式创建：每条新消息独立创建，旧消息不重建；只读修复既有文档。"""
import unittest
from types import SimpleNamespace as Item
from unittest.mock import Mock
from config import Settings
from document_client import DocumentClient
from operation_store import OperationStore
from create_workflow import CreateWorkflow
import test_phase2

class FormalCreateTests(unittest.TestCase):
    setUp = test_phase2.WorkflowTests.setUp

    def test_a_duplicate_then_b_total_two(self):
        self.workflow.handle_create("A", "ou_requester")
        self.workflow.handle_create("A", "ou_requester")
        self.assertEqual(self.documents.create_document.call_count, 1)
        self.workflow.handle_create("B", "ou_requester")
        self.assertEqual(self.documents.create_document.call_count, 2)
        self.assertEqual(self.store.get_operation("A")["create_attempts"], 1)
        self.assertEqual(self.store.get_operation("B")["create_attempts"], 1)

    def test_ten_new_messages_have_no_session_cap(self):
        for index in range(10):
            message_id = f"new-message-{index}"
            self.workflow.handle_create(message_id, "ou_requester")
            self.workflow.handle_create(message_id, "ou_requester")
            self.assertEqual(self.store.get_operation(message_id)["status"], "SUCCESS")
        self.assertEqual(self.documents.create_document.call_count, 10)

    def test_unknown_a_does_not_block_b(self):
        self.documents.create_document.side_effect = TimeoutError()
        self.workflow.handle_create("A", "ou_requester")
        self.assertEqual(self.store.get_operation("A")["status"], "UNKNOWN")
        self.documents.create_document.side_effect = None
        restarted = CreateWorkflow(self.settings, OperationStore(self.store.path), self.documents)
        restarted.handle_create("A", "ou_requester")
        self.assertEqual(self.documents.create_document.call_count, 1)
        restarted.handle_create("B", "ou_requester")
        self.assertEqual(self.documents.create_document.call_count, 2)
        self.assertEqual(self.store.get_operation("B")["status"], "SUCCESS")

    def test_legacy_global_trigger_removed_history_preserved(self):
        self.workflow.handle_create("A", "ou_requester")
        with self.store.connect() as connection:
            connection.execute("""CREATE TRIGGER poc_one_create_only
                BEFORE UPDATE OF create_attempts ON create_operations
                BEGIN SELECT RAISE(ABORT,'old global limit'); END""")
        reopened = OperationStore(self.store.path)
        with reopened.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='poc_one_create_only'").fetchone()[0], 0)
        self.assertEqual(reopened.get_operation("A")["document_id"], "doc-test")
        CreateWorkflow(self.settings, reopened, self.documents).handle_create("B", "ou_requester")
        self.assertEqual(self.documents.create_document.call_count, 2)

    def test_inherited_edit_does_not_write_collaborator(self):
        self.documents.has_requester_edit.return_value = True
        self.workflow.handle_create("A", "ou_requester")
        self.documents.grant_collaborator.assert_not_called()
        self.assertEqual(self.store.get_operation("A")["status"], "SUCCESS")

    def test_readonly_confirm_partial_with_gate_false(self):
        self.documents.grant_collaborator.side_effect = RuntimeError()
        self.workflow.handle_create("A", "ou_requester")
        self.assertEqual(self.store.get_operation("A")["status"], "PARTIAL")
        self.documents.reset_mock()
        self.documents.has_requester_edit.return_value = True
        self.workflow.settings = Settings("fake-id", "fake-secret", False, "testfolder")
        self.workflow.confirm_existing("A")
        self.documents.create_document.assert_not_called()
        self.documents.grant_collaborator.assert_not_called()
        self.assertEqual(self.store.get_operation("A")["status"], "SUCCESS")

    def test_readonly_confirm_rejects_insufficient_permission(self):
        self.store.claim_operation("A", "ou_requester")
        self.store.save_operation("A", "PARTIAL", "GRANT", document_id="doc-test")
        self.documents.has_requester_edit.return_value = False
        with self.assertRaises(PermissionError):
            self.workflow.confirm_existing("A")
        self.documents.create_document.assert_not_called()
        self.documents.grant_collaborator.assert_not_called()

class PermissionConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.documents = DocumentClient(self.client, Settings("fake-id", "fake-secret", True, "testfolder"))
        response = self.client.drive.v1.permission_member.create.return_value
        response.success.return_value = True
        self.response = response
        read = self.client.drive.v1.permission_member.list.return_value
        read.success.return_value = True
        read.data.items = []
        self.read = read

    def test_stronger_grant_response_is_sufficient(self):
        self.response.data.member = Item(member_id="ou_requester", member_type="openid", perm="full_access")
        self.documents.grant_collaborator("doc-test", "ou_requester")
        self.client.drive.v1.permission_member.list.assert_not_called()

    def test_sparse_response_falls_back_to_inherited_permission(self):
        self.response.data.member = None
        self.read.data.items = [Item(member_id="ou_requester", member_type="openid",
                                    perm="full_access", perm_type="container")]
        self.documents.grant_collaborator("doc-test", "ou_requester")
        self.client.drive.v1.permission_member.create.assert_called_once()
        self.client.drive.v1.permission_member.list.assert_called_once()

    def test_other_user_permission_does_not_confirm_requester(self):
        self.read.data.items = [Item(member_id="ou_other", member_type="openid", perm="full_access")]
        self.assertFalse(self.documents.has_requester_edit("doc-test", "ou_requester"))

    def test_view_does_not_count_as_edit(self):
        self.read.data.items = [Item(member_id="ou_requester", member_type="openid", perm="view")]
        self.assertFalse(self.documents.has_requester_edit("doc-test", "ou_requester"))

if __name__ == "__main__":
    unittest.main()
