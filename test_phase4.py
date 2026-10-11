"""所有网络阻断；仅SDK mock与隔离SQLite。"""
import io
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace as Item
from unittest.mock import Mock, patch
import lark_oapi as lark
from config import Settings, PROJECT_ROOT, COPY_DISABLED_REPLY, COPY_UNSUPPORTED_REPLY, HELP_REPLY, load_settings
from copy_parser import parse_copy_text, CopyInputError
from copy_source import CopySource, ParentUnresolved, SourceInspector, BoundaryUnconfirmed
from copy_client import CopyClient
from copy_store import CopyStore
from copy_workflow import CopyWorkflow
from message_handler import MessageHandler
from test_phase2 import create_event
from test_phase1 import make_mention
from utils import configure_logging

SOURCE = "sourceDocToken12345"
PARENT = "sourceFolder12345"
COPIED = "copiedDocToken12345"
COMMAND = "复制副本 https://tenant.feishu.cn/docx/" + SOURCE

class ParseTests(unittest.TestCase):
    def test_docx_token(self):
        self.assertEqual(parse_copy_text(COMMAND), SOURCE)

    def test_query_and_fragment(self):
        self.assertEqual(parse_copy_text(COMMAND + "?from=copylink#section"), SOURCE)

    def test_unsupported_type(self):
        for route in ("wiki", "sheets", "base", "file", "slides", "docs"):
            with self.subTest(route=route), self.assertRaisesRegex(CopyInputError, COPY_UNSUPPORTED_REPLY):
                parse_copy_text(COMMAND.replace("/docx/", "/"+route+"/"))

    def test_invalid_links_fail_closed(self):
        for text in ("复制副本", COMMAND+" "+COMMAND, COMMAND.replace("tenant.feishu.cn", "evil.com"),
                     COMMAND.replace("tenant.feishu.cn", "tenant.feishu.cn.evil.com"),
                     COMMAND.replace("https:", "http:"), COMMAND.replace("tenant.feishu.cn", "user@tenant.feishu.cn"),
                     COMMAND+"/extra", COMMAND.replace(SOURCE, "%2Fhidden"),
                     COMMAND.replace("tenant.feishu.cn", "tenant.feishu.cn:443"), COMMAND+"?url=https://evil.com"):
            with self.subTest(text=text), self.assertRaises(CopyInputError):
                parse_copy_text(text)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.path = PROJECT_ROOT / "evidence" / ("phase4-unit-"+uuid.uuid4().hex) / "messages.sqlite3"
        self.store = CopyStore(self.path)
        self.settings = Settings("fake-id", "fake-secret", True, "publicFolder", True, (PARENT,))
        self.api = Mock()
        self.api.inspect_source.return_value = CopySource(SOURCE, PARENT, "Title")
        self.api.copy_document.return_value = COPIED
        self.api.get_url.return_value = "https://tenant.feishu.cn/docx/"+COPIED
        self.flow = CopyWorkflow(self.settings, self.store, self.api)
        self.reply = Mock(return_value="reply-1")
        self.create = Mock()
        self.handler = MessageHandler("bot-id", self.store, self.reply, self.create, self.flow)
        self.stream = io.StringIO()
        configure_logging(self.settings, stream=self.stream)
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start(); self.addCleanup(blocker.stop)

    def test_p2p_copy_command(self):
        self.handler.handle_event(create_event(COMMAND))
        self.assertEqual(self.store.get("create-1")["status"], "SUCCESS")
        self.create.handle_create.assert_not_called()

    def test_group_copy_command(self):
        self.handler.handle_event(create_event("@_user_1 "+COMMAND, "group", mentions=[make_mention()]))
        self.api.copy_document.assert_called_once()

    def test_group_without_bot_mention_ignored(self):
        self.handler.handle_event(create_event(COMMAND, "group"))
        self.api.copy_document.assert_not_called()

    def test_gate_false_zero_entire_api(self):
        self.flow.settings = replace(self.settings, enable_real_copy=False)
        self.assertEqual(self.flow.handle_copy("A", "ou_requester", COMMAND), COPY_DISABLED_REPLY)
        self.assertEqual(self.api.mock_calls, [])
        self.assertEqual(self.store.get("A")["copy_attempts"], 0)

    def test_invalid_input_zero_api(self):
        self.flow.handle_copy("A", "ou_requester", "复制副本")
        self.assertEqual(self.api.mock_calls, [])

    def test_source_inaccessible_no_copy(self):
        self.api.inspect_source.side_effect = PermissionError("sensitive source token")
        self.flow.handle_copy("A", "ou_requester", COMMAND)
        self.api.copy_document.assert_not_called()
        self.assertEqual(self.store.get("A")["stage"], "ACCESS")

    def test_copy_success_persists_new_token_and_parent(self):
        self.flow.handle_copy("A", "ou_requester", COMMAND)
        row = self.store.get("A")
        self.assertEqual((row["document_id"],row["parent_token"],row["status"]), (COPIED,PARENT,"SUCCESS"))

    def test_duplicate_and_new_message(self):
        for mid in ("A","A","B"):
            self.flow.handle_copy(mid,"ou_requester",COMMAND)
        self.assertEqual(self.api.copy_document.call_count,2)

    def test_unknown_persistent_no_retry(self):
        self.api.copy_document.side_effect = TimeoutError(SOURCE)
        self.flow.handle_copy("A","ou_requester",COMMAND)
        CopyWorkflow(self.settings,CopyStore(self.path),self.api).handle_copy("A","ou_requester",COMMAND)
        self.assertEqual(self.store.get("A")["status"],"UNKNOWN")
        self.api.copy_document.assert_called_once()

    def test_unknown_a_does_not_block_b(self):
        self.api.copy_document.side_effect = [TimeoutError(),COPIED]
        for mid in ("A","A","B"):
            self.flow.handle_copy(mid,"ou_requester",COMMAND)
        self.assertEqual(self.api.copy_document.call_count,2)
        self.assertEqual(self.store.get("B")["status"],"SUCCESS")

    def test_grant_failure_partial_only_existing_repair(self):
        self.api.ensure_requester_edit.side_effect = PermissionError()
        self.flow.handle_copy("A","ou_requester",COMMAND)
        self.flow.handle_copy("A","ou_requester",COMMAND)
        self.assertEqual(self.store.get("A")["document_id"],COPIED)
        self.assertEqual(self.store.get("A")["status"],"PARTIAL")
        self.api.ensure_requester_edit.side_effect = None
        self.flow.complete_existing("A")
        self.api.copy_document.assert_called_once()
        self.assertEqual(self.store.get("A")["status"],"SUCCESS")

    def test_unknown_cannot_use_partial_repair(self):
        self.api.copy_document.side_effect = TimeoutError()
        self.flow.handle_copy("A","ou_requester",COMMAND)
        with self.assertRaises(ValueError):self.flow.complete_existing("A")
        self.api.copy_document.assert_called_once()

    def test_placement_failure_no_grant(self):
        self.api.confirm_placement.side_effect = BoundaryUnconfirmed()
        self.flow.handle_copy("A","ou_requester",COMMAND)
        self.assertEqual(self.store.get("A")["status"],"SUCCESS")
        self.api.copy_document.assert_called_once()

    def test_create_routing_stays_independent(self):
        self.create.handle_create.return_value = "created"
        self.handler.handle_event(create_event())
        self.create.handle_create.assert_called_once()
        self.assertEqual(self.api.mock_calls,[])

    def test_help_no_document_api_and_still_unavailable(self):
        self.handler.handle_event(create_event("help"))
        self.reply.assert_called_once_with("create-1",HELP_REPLY)
        self.assertIn("复制副本 <飞书文档链接>",HELP_REPLY)
        self.assertEqual(self.api.mock_calls,[])

    def test_token_and_secret_not_logged_on_sdk_exception(self):
        self.api.copy_document.side_effect = TimeoutError(SOURCE+PARENT+"fake-secret")
        self.flow.handle_copy("A","ou_requester",COMMAND)
        for value in (SOURCE,PARENT,"fake-secret"):
            self.assertNotIn(value,self.stream.getvalue())

    def test_copy_gate_independent_of_create_gate(self):
        self.flow.settings = replace(self.settings,enable_real_create=False)
        self.flow.handle_copy("A","ou_requester",COMMAND)
        self.api.copy_document.assert_called_once()

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("fake-id","fake-secret",True,"publicFolder",True,(PARENT,))
        self.client = Mock()
        self.api = CopyClient(self.client,self.settings)
        response = self.client.drive.v1.file.copy.return_value
        response.success.return_value = True
        response.data.file = Item(token=COPIED)

    def test_copy_request_docx_original_parent_tenant(self):
        self.assertEqual(self.api.copy_document(CopySource(SOURCE,PARENT,"Title")),COPIED)
        request = self.client.drive.v1.file.copy.call_args.args[0]
        self.assertEqual(request.request_body.folder_token,PARENT)
        self.assertNotEqual(request.request_body.folder_token,self.settings.public_folder_token)
        self.assertEqual(request.request_body.type,"docx")
        self.assertEqual(request.token_types,{lark.AccessTokenType.TENANT})
        self.assertIsNone(request.request_body.extra)

    def test_gate_false_blocks_copy_and_grant(self):
        self.api.settings = replace(self.settings,enable_real_copy=False)
        with self.assertRaises(PermissionError):self.api.copy_document(CopySource(SOURCE,PARENT,"Title"))
        with self.assertRaises(PermissionError):self.api.ensure_requester_edit(COPIED,"ou_requester")
        self.assertEqual(self.client.mock_calls,[])

    def test_grant_is_edit_with_independent_copy_gate(self):
        self.api.settings = replace(self.settings,enable_real_create=False)
        self.api.reader.has_requester_edit = Mock(side_effect=[False,True])
        self.client.drive.v1.permission_member.create.return_value.success.return_value = True
        self.api.ensure_requester_edit(COPIED,"ou_requester")
        request=self.client.drive.v1.permission_member.create.call_args.args[0]
        self.assertEqual(request.request_body.perm,"edit")
        self.assertEqual(request.request_body.member_id,"ou_requester")
        self.assertEqual(request.token,COPIED)
        self.assertFalse(request.need_notification)

    def test_existing_edit_skips_grant(self):
        self.api.reader.has_requester_edit = Mock(return_value=True)
        self.api.ensure_requester_edit(COPIED,"ou_requester")
        self.client.drive.v1.permission_member.create.assert_not_called()

    def test_copy_response_cannot_point_to_source(self):
        self.client.drive.v1.file.copy.return_value.data.file = Item(token=SOURCE)
        with self.assertRaises(BoundaryUnconfirmed):self.api.copy_document(CopySource(SOURCE,PARENT,"Title"))
        self.client.drive.v1.permission_member.create.assert_not_called()

    def prepare_inspector(self):
        inspector = self.api.inspector
        response=self.client.docx.v1.document.get.return_value
        response.success.return_value=True
        response.data.document=Item(document_id=SOURCE,title="Title")
        inspector.find_in_folder=Mock(return_value=True)
        inspector.read_members=Mock(side_effect=[
            [Item(member_type="appid",member_id="fake-id",perm="edit")],
            [Item(member_type="openid",member_id="ou_requester",perm="view")]])
        return inspector

    def test_application_without_folder_edit_fails(self):
        inspector=self.prepare_inspector()
        inspector.read_members.side_effect=[[Item(member_type="appid",member_id="fake-id",perm="view")]]
        self.assertEqual(inspector.inspect(SOURCE,"ou_requester").token, SOURCE)

    def test_requester_without_source_access_fails(self):
        inspector=self.prepare_inspector()
        inspector.read_members.side_effect=[[Item(member_type="appid",member_id="fake-id",perm="edit")],[]]
        self.assertEqual(inspector.inspect(SOURCE,"ou_requester").token, SOURCE)

class CopyConfigTests(unittest.TestCase):
    def setUp(self):
        self.path=PROJECT_ROOT/"evidence"/("phase4-config-"+uuid.uuid4().hex)
        self.path.mkdir()

    def write_env(self, extra):
        path=self.path/".env"
        path.write_text("FEISHU_APP_ID=fake-id\nFEISHU_APP_SECRET=fake-secret\nAI_TEMPLATE_BOT_ENABLE_REAL_CREATE=true\n"+extra,encoding="utf-8")
        return path

    def test_copy_default_false_create_true(self):
        settings=load_settings(self.write_env(""))
        self.assertTrue(settings.enable_real_create)
        self.assertFalse(settings.enable_real_copy)
        self.assertEqual(settings.copy_source_folders,())

    def test_separate_copy_config_and_folders(self):
        settings=load_settings(self.write_env("AI_TEMPLATE_BOT_ENABLE_REAL_COPY=false\nFEISHU_COPY_SOURCE_FOLDERS="+PARENT))
        self.assertTrue(settings.enable_real_create)
        self.assertFalse(settings.enable_real_copy)
        self.assertEqual(settings.copy_source_folders,(PARENT,))
        self.assertNotIn(PARENT,repr(settings))
        self.assertNotIn("fake-secret",repr(settings))

    def test_invalid_copy_gate_fails_fast(self):
        with self.assertRaises(ValueError):load_settings(self.write_env("AI_TEMPLATE_BOT_ENABLE_REAL_COPY=maybe"))

if __name__ == "__main__":
    unittest.main()
