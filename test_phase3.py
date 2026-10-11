"""Phase 3 离线测试；模拟目录权限，不创建任何飞书文档。"""
import io
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace as Item
from unittest.mock import Mock, patch
from config import Settings, PROJECT_ROOT, load_settings, require_public_folder
from document_client import DocumentClient, CreatedDocument
from folder_access import verify_public_folder, get_document_public_view
from operation_store import OperationStore
from create_workflow import CreateWorkflow
from utils import configure_logging

class PublicFolderTests(unittest.TestCase):
    def setUp(self):
        self.folder = PROJECT_ROOT / "evidence" / ("phase3-unit-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.settings = Settings("fake-id", "fake-secret", True, "testfolder")
        self.client = Mock()
        self.client.drive.v1.file.list.return_value.success.return_value = True
        members = self.client.drive.v1.permission_member.list.return_value
        members.success.return_value = True
        members.data.items = [Item(member_type="appid", member_id="fake-id", perm="full_access")]
        public = self.client.drive.v2.permission_public.get.return_value
        public.success.return_value = True
        public.data.permission_public.link_share_entity = "tenant_readable"
        self.store = OperationStore(self.folder / "operations.sqlite3")
        self.documents = Mock()
        self.documents.verify_public_folder.return_value = "testfolder"
        self.documents.create_document.return_value = CreatedDocument("doc-id", 1, "testfolder")
        self.documents.get_document_public_view.return_value = "YES"
        self.documents.get_document_url.return_value = "https://example.feishu.cn/docx/doc-id"
        self.workflow = CreateWorkflow(self.settings, self.store, self.documents)
        configure_logging(self.settings, stream=io.StringIO())
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_folder_load_only_from_given_env(self):
        env = self.folder / ".env"
        env.write_text("FEISHU_APP_ID=fake-id\nFEISHU_APP_SECRET=fake-secret\nFEISHU_PUBLIC_FOLDER_TOKEN=testfolder\n", encoding="utf-8")
        with patch.dict("os.environ", {"FEISHU_PUBLIC_FOLDER_TOKEN": "otherproject"}):
            self.assertEqual(load_settings(env).public_folder_token, "testfolder")
        self.assertFalse(load_settings(env).enable_real_create)

    def test_missing_folder_fail_closed_before_any_create(self):
        for token in ("", " ", "your_folder_token", "https://example.com"):
            settings = Settings("fake-id", "fake-secret", True, token)
            with self.assertRaises(ValueError):
                DocumentClient(self.client, settings).create_document()
        self.client.docx.v1.document.create.assert_not_called()

    def test_missing_folder_workflow_blocked_not_unknown(self):
        self.workflow.settings = Settings("fake-id", "fake-secret", True)
        self.workflow.handle_create("message", "ou_user")
        self.documents.create_document.assert_not_called()
        operation = self.store.get_operation("message")
        self.assertEqual((operation["status"], operation["stage"], operation["create_attempts"]),
                         ("BLOCKED", "FOLDER", 0))

    def test_verified_appid_and_readonly_public(self):
        self.assertEqual(verify_public_folder(self.client, self.settings), "testfolder")
        for method in (self.client.drive.v1.file.list,
                       self.client.drive.v1.permission_member.list,
                       self.client.drive.v2.permission_public.get):
            self.assertEqual(method.call_args.args[0].http_method.name, "GET")
        self.assertEqual(self.client.drive.v1.file.list.call_args.args[0].folder_token, "testfolder")

    def test_readable_folder_without_app_collaborator_rejected(self):
        self.client.drive.v1.permission_member.list.return_value.data.items = [
            Item(member_type="openid", member_id="fake-id", perm="full_access")]
        with self.assertRaises(PermissionError):
            verify_public_folder(self.client, self.settings)

    def test_folder_api_failure_no_root_fallback(self):
        self.client.drive.v1.file.list.return_value.success.return_value = False
        self.client.drive.v1.file.list.return_value.code = 1061004
        with self.assertRaises(PermissionError):
            verify_public_folder(self.client, self.settings)
        self.assertEqual(self.client.drive.v1.file.list.call_count, 1)
        self.client.docx.v1.document.create.assert_not_called()

    def test_saved_target_and_requester_edit_workflow(self):
        self.workflow.handle_create("message", "ou_user")
        operation = self.store.get_operation("message")
        self.assertEqual(operation["folder_token"], "testfolder")
        self.assertEqual(operation["document_id"], "doc-id")
        self.assertEqual(operation["child_public_view"], "YES")
        self.documents.grant_collaborator.assert_called_once_with("doc-id", "ou_user")

    def test_child_public_view_not_assumed_or_written(self):
        self.documents.get_document_public_view.return_value = "NO"
        self.workflow.handle_create("message", "ou_user")
        self.workflow.handle_create("message", "ou_user")
        operation = self.store.get_operation("message")
        self.assertEqual((operation["status"], operation["stage"], operation["child_public_view"]),
                         ("PARTIAL", "PUBLIC_VIEW", "NO"))
        self.documents.create_document.assert_called_once()
        self.assertFalse(any("patch" in call[0] or "move" in call[0] for call in self.documents.mock_calls))

    def test_child_permission_read_error_remains_unknown(self):
        self.documents.get_document_public_view.side_effect = PermissionError("scope unavailable")
        self.workflow.handle_create("message", "ou_user")
        operation = self.store.get_operation("message")
        self.assertEqual(operation["child_public_view"], "UNKNOWN")
        self.assertEqual(operation["status"], "PARTIAL")
        self.assertEqual(operation["collaborator_granted"], 1)

    def test_gate_false_does_not_even_probe_folder(self):
        self.workflow.settings = Settings("fake-id", "fake-secret", False, "testfolder")
        self.workflow.handle_create("message", "ou_user")
        self.assertEqual(self.documents.mock_calls, [])

    def test_child_public_read_only(self):
        self.assertEqual(get_document_public_view(self.client, "doc-id"), "YES")
        request = self.client.drive.v2.permission_public.get.call_args.args[0]
        self.assertEqual(request.http_method.name, "GET")
        self.assertEqual(request.type, "docx")
        self.client.drive.v2.permission_public.patch.assert_not_called()

    def test_existing_database_keeps_history(self):
        self.workflow.handle_create("message", "ou_user")
        reopened = OperationStore(self.store.path)
        self.assertEqual(reopened.get_operation("message")["document_id"], "doc-id")
        self.assertFalse(reopened.claim_operation("message", "ou_user"))

if __name__ == "__main__":
    unittest.main()
