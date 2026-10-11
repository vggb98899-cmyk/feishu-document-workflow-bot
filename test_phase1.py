"""完全离线，禁止网络；测试产物只保存在项目 evidence 中。"""
import io
import json
import logging
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace as Item
from unittest.mock import Mock, patch
from config import PROJECT_ROOT, Settings, TEST_REPLY, load_settings
from utils import MessageStore, configure_logging
from message_handler import MessageHandler, parse_test_message
from feishu_client import create_connection, get_bot_open_id, reply_test_message

def make_event(text="测试", chat_type="p2p", message_id="message-1", mentions=None):
    message = Item(content=json.dumps({"text": text}), chat_type=chat_type,
                   message_id=message_id, message_type="text", mentions=mentions)
    return Item(event=Item(message=message, sender=Item(sender_type="user")))

def make_mention(open_id="bot-id", key="@_user_1"):
    return Item(id=Item(open_id=open_id), key=key)

class Phase1Tests(unittest.TestCase):
    def setUp(self):
        self.folder = PROJECT_ROOT / "evidence" / ("unit-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.store = MessageStore(self.folder / "messages.sqlite3")
        self.reply = Mock(return_value="reply-1")
        self.handler = MessageHandler("bot-id", self.store, self.reply)
        self.stream = io.StringIO()
        configure_logging(Settings("fake-app-id", "fake-secret"), stream=self.stream)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_missing_env_fails_fast(self):
        with self.assertRaisesRegex(ValueError, ".env"):
            load_settings(self.folder / ".env")

    def test_placeholder_or_incomplete_credentials_fail(self):
        env = self.folder / ".env"
        env.write_text("FEISHU_APP_ID=your_app_id\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_settings(env)

    def test_credentials_and_traceback_redacted(self):
        logging.info("credentials %s %s", "fake-app-id", "fake-secret")
        try:
            raise RuntimeError("fake-secret fake-app-id https://example.com?token=private")
        except RuntimeError:
            logging.exception("failure")
        logging.getLogger("Lark").warning("fake-secret fake-app-id")
        output = self.stream.getvalue()
        self.assertNotIn("fake-secret", output)
        self.assertNotIn("fake-app-id", output)
        self.assertNotIn("token=private", output)
        self.assertIn("Traceback", output)
        self.assertIn("REDACTED", output)
        self.assertNotIn("fake-secret", repr(Settings("fake-app-id", "fake-secret")))

    def test_p2p_test_command(self):
        self.handler.handle_event(make_event())
        self.reply.assert_called_once_with("message-1", TEST_REPLY)

    def test_group_bot_mention_cleaning(self):
        event = make_event(" @_user_1  测试\n", "group", mentions=[make_mention()])
        self.assertEqual(parse_test_message(event.event, "bot-id"), "message-1")
        self.handler.handle_event(event)
        self.reply.assert_called_once_with("message-1", TEST_REPLY)

    def test_rich_post_extracts_docx_link(self):
        from message_handler import get_addressed_text
        content = json.dumps({"zh_cn": {"title": "复制副本", "content": [[
            {"tag": "text", "text": "复制副本"},
            {"tag": "a", "text": "飞书文档", "href": "https://demo.feishu.cn/docx/Abc123456789"}
        ]]}})
        message = Item(content=content, chat_type="p2p", message_id="rich-1",
                       message_type="post", mentions=[])
        event = Item(message=message, sender=Item(sender_type="user"))
        self.assertIn("复制副本", get_addressed_text(event, "bot-id"))
        self.assertIn("https://demo.feishu.cn/docx/Abc123456789", get_addressed_text(event, "bot-id"))

    def test_group_without_bot_mention_ignored(self):
        for mentions in (None, [make_mention("other-user")]):
            self.handler.handle_event(make_event("@_user_1 测试", "group", mentions=mentions))
        self.reply.assert_not_called()

    def test_fake_mention_metadata_ignored(self):
        self.handler.handle_event(make_event("测试", "group", mentions=[make_mention()]))
        self.reply.assert_not_called()

    def test_non_test_commands_have_no_side_effect(self):
        for text in ("创建文档", "复制云文档", "删除文件", "测试一下", "", "测试 创建文档"):
            self.handler.handle_event(make_event(text))
        self.reply.assert_not_called()
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_duplicate_message_persists_across_restart(self):
        self.handler.handle_event(make_event())
        restarted = MessageHandler("bot-id", MessageStore(self.store.path), self.reply)
        restarted.handle_event(make_event())
        self.reply.assert_called_once()

    def test_failed_reply_not_repeated(self):
        self.reply.side_effect = RuntimeError("simulated ambiguous failure")
        self.handler.handle_event(make_event())
        self.handler.handle_event(make_event())
        self.reply.assert_called_once()
        self.assertIn("MESSAGE_PROCESSING_FAILED", self.stream.getvalue())

    def test_malformed_and_non_user_messages_ignored(self):
        for content in ("{", "[]", '{"text": 12}'):
            event = make_event()
            event.event.message.content = content
            self.handler.handle_event(event)
        event = make_event()
        event.event.sender.sender_type = "app"
        self.handler.handle_event(event)
        event = make_event()
        event.event.message.message_type = "image"
        self.handler.handle_event(event)
        self.reply.assert_not_called()

    def test_sdk_reply_is_only_fixed_text_and_stable_uuid(self):
        client = Mock()
        response = client.im.v1.message.reply.return_value
        response.success.return_value = True
        response.data.message_id = "reply-1"
        self.assertEqual(reply_test_message(client, "message-1", TEST_REPLY), "reply-1")
        first = client.im.v1.message.reply.call_args.args[0]
        reply_test_message(client, "message-1", TEST_REPLY)
        second = client.im.v1.message.reply.call_args.args[0]
        self.assertEqual(json.loads(first.request_body.content), {"text": TEST_REPLY})
        self.assertEqual(first.request_body.uuid, second.request_body.uuid)
        self.assertEqual(first.request_body.msg_type, "text")
        self.assertTrue(all(call[0] == "im.v1.message.reply" for call in client.mock_calls
                            if not call[0].startswith("im.v1.message.reply()")))
        with self.assertRaises(ValueError):
            reply_test_message(client, "message-1", "创建文档")

    def test_identity_request_is_read_only(self):
        client = Mock()
        client.request.return_value.raw.content = b'{"code":0,"bot":{"open_id":"bot-id"}}'
        self.assertEqual(get_bot_open_id(client), "bot-id")
        request = client.request.call_args.args[0]
        self.assertEqual(request.http_method.name, "GET")
        self.assertEqual(request.uri, "/open-apis/bot/v3/info")

    def test_sdk_dispatcher_routes_receive_event(self):
        callback = Mock()
        with patch('lark_oapi.ws.client.ExpiringCache'):
            connection = create_connection(Settings("fake-app-id", "fake-secret"), callback)
        payload = {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                   "event": {"message": {"message_id": "sample", "message_type": "text",
                             "chat_type": "p2p", "content": '{"text":"测试"}'},
                             "sender": {"sender_type": "user"}}}
        connection._event_handler._do_without_validation(json.dumps(payload).encode())
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[0].event.message.message_id, "sample")

if __name__ == "__main__":
    unittest.main(verbosity=2)
