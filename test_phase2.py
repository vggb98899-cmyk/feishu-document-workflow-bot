"""工作流离线测试；True 只出现在注入的假配置，外部能力均为 mock。"""
import io
import logging
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from types import SimpleNamespace as Item
from config import PROJECT_ROOT, Settings, CREATE_DISABLED_REPLY, load_settings
from create_workflow import CreateWorkflow
from document_client import CreatedDocument
from message_handler import MessageHandler, parse_create_message
from operation_store import OperationStore
from test_phase1 import make_event, make_mention
from utils import configure_logging

def create_event(text="创建文档", chat_type="p2p", message_id="create-1", mentions=None):
    event = make_event(text, chat_type, message_id, mentions)
    event.event.sender.sender_id = Item(open_id="ou_requester")
    return event

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = PROJECT_ROOT / "evidence" / ("phase2-unit-" + uuid.uuid4().hex)
        self.store = OperationStore(self.folder / "messages.sqlite3")
        self.documents = Mock()
        self.documents.verify_public_folder.return_value = "testfolder"
        self.documents.get_document_public_view.return_value = "YES"
        self.documents.create_document.return_value = CreatedDocument("doc-test", 1)
        self.documents.get_document_url.return_value = "https://example.feishu.cn/docx/doc-test"
        self.settings = Settings("fake-id", "fake-secret", True, "testfolder")
        self.workflow = CreateWorkflow(self.settings, self.store, self.documents)
        self.reply = Mock(return_value="reply-id")
        self.handler = MessageHandler("bot-id", self.store, self.reply, self.workflow)
        self.stream = io.StringIO()
        configure_logging(self.settings, stream=self.stream)
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_group_command_and_sender(self):
        event = create_event(" @_user_1 创建文档 ", "group", mentions=[make_mention()])
        self.assertEqual(parse_create_message(event.event, "bot-id"), ("create-1", "ou_requester"))
        self.handler.handle_event(event)
        self.documents.grant_collaborator.assert_called_once_with("doc-test", "ou_requester")

    def test_p2p_success_saves_document_and_url(self):
        self.handler.handle_event(create_event())
        operation = self.store.get_operation("create-1")
        self.assertEqual(operation["document_id"], "doc-test")
        self.assertEqual(operation["revision_id"], 1)
        self.assertEqual(operation["status"], "SUCCESS")
        self.assertEqual(operation["collaborator_granted"], 1)
        self.assertIn("https://", self.reply.call_args.args[1])

    def test_gate_false_blocks_entire_api_layer(self):
        self.workflow.settings = Settings("fake-id", "fake-secret")
        self.handler.handle_event(create_event())
        self.documents.assert_not_called()
        self.assertEqual(self.documents.mock_calls, [])
        self.reply.assert_called_once_with("create-1", CREATE_DISABLED_REPLY)
        self.assertEqual(self.store.get_operation("create-1")["create_attempts"], 0)

    def test_blocked_message_cannot_create_after_gate_changes(self):
        self.workflow.settings = Settings("fake-id", "fake-secret")
        self.handler.handle_event(create_event())
        self.workflow.settings = self.settings
        self.handler.handle_event(create_event())
        self.documents.create_document.assert_not_called()

    def test_duplicate_across_restart(self):
        self.handler.handle_event(create_event())
        restarted = CreateWorkflow(self.settings, OperationStore(self.store.path), self.documents)
        self.assertIsNone(restarted.handle_create("create-1", "ou_requester"))
        self.documents.create_document.assert_called_once()

    def test_concurrent_delivery_creates_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.workflow.handle_create("same-id", "ou_requester"), range(4)))
        self.documents.create_document.assert_called_once()

    def test_grant_failure_is_partial_and_repair_never_creates(self):
        self.documents.grant_collaborator.side_effect = RuntimeError("grant rejected")
        self.handler.handle_event(create_event())
        operation = self.store.get_operation("create-1")
        self.assertEqual((operation["status"], operation["document_id"]), ("PARTIAL", "doc-test"))
        self.assertEqual(operation["collaborator_granted"], 0)
        self.handler.handle_event(create_event())
        self.documents.create_document.assert_called_once()
        self.documents.grant_collaborator.side_effect = None
        self.workflow.repair_collaborator("create-1")
        self.documents.create_document.assert_called_once()
        self.assertEqual(self.store.get_operation("create-1")["status"], "SUCCESS")

    def test_timeout_unknown_no_retry_even_restart(self):
        self.documents.create_document.side_effect = TimeoutError("fake-secret fake-id")
        self.handler.handle_event(create_event())
        self.handler.handle_event(create_event())
        restarted = CreateWorkflow(self.settings, OperationStore(self.store.path), self.documents)
        restarted.handle_create("create-1", "ou_requester")
        self.assertEqual(self.store.get_operation("create-1")["status"], "UNKNOWN")
        self.documents.create_document.assert_called_once()
        self.documents.grant_collaborator.assert_not_called()
        self.assertNotIn("fake-secret", self.stream.getvalue())
        self.assertNotIn("fake-id", self.stream.getvalue())

    def test_crash_before_response_already_unknown(self):
        self.store.claim_operation("create-1", "ou_requester")
        self.assertTrue(self.store.begin_create("create-1"))
        restarted = CreateWorkflow(self.settings, OperationStore(self.store.path), self.documents)
        restarted.handle_create("create-1", "ou_requester")
        self.documents.create_document.assert_not_called()
        self.assertEqual(self.store.get_operation("create-1")["status"], "UNKNOWN")

    def test_document_saved_before_grant(self):
        def check_saved(*args):
            self.assertEqual(self.store.get_operation("create-1")["document_id"], "doc-test")
        self.documents.grant_collaborator.side_effect = check_saved
        self.handler.handle_event(create_event())

    def test_save_failure_never_grants_or_recreates(self):
        original_save = self.store.save_operation
        def fail_document_save(message_id, status, stage, **fields):
            if "document_id" in fields:
                raise OSError("disk failure")
            return original_save(message_id, status, stage, **fields)
        with patch.object(self.store, "save_operation", side_effect=fail_document_save):
            self.handler.handle_event(create_event())
        self.handler.handle_event(create_event())
        self.documents.create_document.assert_called_once()
        self.documents.grant_collaborator.assert_not_called()
        self.assertEqual(self.store.get_operation("create-1")["status"], "UNKNOWN")

    def test_reply_failure_does_not_recreate(self):
        self.reply.side_effect = TimeoutError()
        self.handler.handle_event(create_event())
        self.handler.handle_event(create_event())
        self.assertEqual(self.store.get_operation("create-1")["status"], "SUCCESS")
        self.documents.create_document.assert_called_once()

    def test_url_failure_preserves_grant_and_document(self):
        self.documents.get_document_url.side_effect = RuntimeError("metadata unavailable")
        self.handler.handle_event(create_event())
        operation = self.store.get_operation("create-1")
        self.assertEqual((operation["status"], operation["stage"]), ("PARTIAL", "URL"))
        self.assertEqual(operation["collaborator_granted"], 1)
        self.documents.get_document_url.side_effect = None
        self.workflow.repair_collaborator("create-1")
        self.documents.create_document.assert_called_once()
        self.documents.grant_collaborator.assert_called_once()

    def test_unauthorized_and_non_commands_no_api(self):
        events = [create_event("创建文档 任意标题"), create_event("复制文档"),
                  create_event("创建文档", "group"),
                  create_event("@_user_1 创建文档", "group", mentions=[make_mention("other")])]
        missing_sender = create_event()
        missing_sender.event.sender.sender_id = None
        app_sender = create_event()
        app_sender.event.sender.sender_type = "app"
        for event in events + [missing_sender, app_sender]:
            self.handler.handle_event(event)
        self.assertEqual(self.documents.mock_calls, [])
        self.reply.assert_not_called()

    def test_test_command_regression(self):
        self.handler.handle_event(make_event())
        self.reply.assert_called_once_with("message-1", "创建模板机器人：连接正常")
        self.assertEqual(self.documents.mock_calls, [])

    def test_repair_rejects_unknown_and_gate_false(self):
        with self.assertRaises(ValueError):
            self.workflow.repair_collaborator("missing")
        self.workflow.settings = Settings("fake-id", "fake-secret")
        with self.assertRaises(PermissionError):
            self.workflow.repair_collaborator("missing")

    def test_config_default_false_and_invalid_fails(self):
        env = self.folder / ".env"
        env.write_text("FEISHU_APP_ID=fake-id\nFEISHU_APP_SECRET=fake-secret\n", encoding="utf-8")
        self.assertFalse(load_settings(env).enable_real_create)
        with env.open("a", encoding="utf-8") as stream:
            stream.write("AI_TEMPLATE_BOT_ENABLE_REAL_CREATE=maybe\n")
        with self.assertRaises(ValueError):
            load_settings(env)

if __name__ == "__main__":
    unittest.main()
