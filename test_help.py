"""帮助分支只允许消息回复，不进入文档工作流。"""
import io
import unittest
import uuid
from unittest.mock import Mock, patch
from config import PROJECT_ROOT, HELP_REPLY, SHORT_HELP_REPLY, Settings
from utils import MessageStore, configure_logging
from message_handler import MessageHandler
from test_phase1 import make_event, make_mention

class HelpTests(unittest.TestCase):
    def setUp(self):
        self.path = PROJECT_ROOT / "evidence" / ("help-unit-" + uuid.uuid4().hex) / "messages.sqlite3"
        self.store = MessageStore(self.path)
        self.reply = Mock(return_value="reply-id")
        self.workflow = Mock()
        self.handler = MessageHandler("bot-id", self.store, self.reply, self.workflow)
        configure_logging(Settings("fake-id", "fake-secret"), stream=io.StringIO())
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def tearDown(self):
        self.assertEqual(self.workflow.mock_calls, [])

    def test_p2p_help_aliases(self):
        for index, command in enumerate(("帮助", "help", "HELP", " Help ")):
            self.handler.handle_event(make_event(command, message_id=str(index)))
        self.assertEqual(self.reply.call_count, 4)
        self.assertTrue(all(call.args[1] == HELP_REPLY for call in self.reply.call_args_list))

    def test_group_help(self):
        self.handler.handle_event(make_event("@_user_1 帮助", "group", mentions=[make_mention()]))
        self.reply.assert_called_once_with("message-1", HELP_REPLY)

    def test_explicit_unknown_mention_short_help(self):
        self.handler.handle_event(make_event("@_user_1 你好", "group", mentions=[make_mention()]))
        self.reply.assert_called_once_with("message-1", SHORT_HELP_REPLY)

    def test_copy_instruction_only_gets_help(self):
        self.handler.handle_event(make_event("@_user_1 复制副本 文档链接", "group", mentions=[make_mention()]))
        self.reply.assert_called_once_with("message-1", SHORT_HELP_REPLY)
        self.assertIn("复制副本 <飞书文档链接>", HELP_REPLY)
        self.assertIn("创建文档", HELP_REPLY)
        self.assertIn("复制副本", HELP_REPLY)

    def test_other_mentions_or_no_mentions_silent(self):
        for mentions in (None, [make_mention("other")]):
            self.handler.handle_event(make_event("@_user_1 help", "group", mentions=mentions))
        self.reply.assert_not_called()

    def test_unknown_p2p_remains_silent(self):
        self.handler.handle_event(make_event("你好"))
        self.reply.assert_not_called()

    def test_duplicate_help_survives_restart(self):
        self.handler.handle_event(make_event("help"))
        restarted = MessageHandler("bot-id", MessageStore(self.path), self.reply, self.workflow)
        restarted.handle_event(make_event("help"))
        self.reply.assert_called_once()

    def test_reply_failure_not_retried(self):
        self.reply.side_effect = TimeoutError("message reply failed")
        self.handler.handle_event(make_event("help"))
        self.handler.handle_event(make_event("help"))
        self.reply.assert_called_once()

    def test_bot_and_malformed_events_ignored(self):
        event = make_event("help")
        event.event.sender.sender_type = "app"
        self.handler.handle_event(event)
        event = make_event("help")
        event.event.message.content = "{"
        self.handler.handle_event(event)
        self.reply.assert_not_called()

    def test_mention_without_command_gets_short_help(self):
        self.handler.handle_event(make_event("@_user_1", "group", mentions=[make_mention()]))
        self.reply.assert_called_once_with("message-1", SHORT_HELP_REPLY)

if __name__ == "__main__":
    unittest.main()
