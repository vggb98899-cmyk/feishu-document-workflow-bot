"""识别固定指令和真实 sender，分发至独立工作流。"""
import json
import logging
from config import TEST_COMMAND, TEST_REPLY, CREATE_COMMAND, HELP_COMMANDS, HELP_REPLY, SHORT_HELP_REPLY

LOGGER = logging.getLogger(__name__)

def get_addressed_text(event, bot_open_id):
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    if getattr(sender, "sender_type", None) != "user":
        return None
    if not message or not message.message_id:
        return None
    try:
        content = json.loads(message.content)
        if message.message_type == "text":
            text = content.get("text") if isinstance(content, dict) else None
        elif message.message_type in {"post", "interactive", "share_doc"}:
            text, links = _extract_rich_message(content)
            if links:
                text = (text or "") + " " + " ".join(links)
        else:
            text = None
    except (TypeError, ValueError):
        return None
    if not isinstance(text, str):
        return None
    if message.chat_type == "group":
        mentions = message.mentions or []
        bot_mentions = [mention for mention in mentions
                        if getattr(getattr(mention, "id", None), "open_id", None) == bot_open_id
                        and getattr(mention, "key", None)]
        if not bot_open_id or not any(mention.key in text for mention in bot_mentions):
            return None
        for mention in bot_mentions:
            text = text.replace(mention.key, "")
    elif message.chat_type != "p2p":
        return None
    return text.strip()


def _extract_rich_message(content):
    """Extract visible text and official href values from Feishu rich content."""
    texts, links = [], []
    def visit(value):
        if isinstance(value, dict):
            tag = value.get("tag")
            if tag in {"text", "plain_text"} and isinstance(value.get("text"), str):
                texts.append(value["text"])
            href = value.get("href") or value.get("url")
            if isinstance(href, str) and href.startswith("https://"):
                links.append(href)
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(content)
    return " ".join(texts), list(dict.fromkeys(links))


def parse_command_message(event, bot_open_id, command):
    text = get_addressed_text(event, bot_open_id)
    return event.message.message_id if text == command else None

def parse_test_message(event, bot_open_id):
    return parse_command_message(event, bot_open_id, TEST_COMMAND)


def parse_create_message(event, bot_open_id):
    message_id = parse_command_message(event, bot_open_id, CREATE_COMMAND)
    sender_id = getattr(getattr(getattr(event, "sender", None), "sender_id", None), "open_id", None)
    if message_id and isinstance(sender_id, str) and sender_id.startswith("ou_"):
        return message_id, sender_id
    return None


class MessageHandler:
    def __init__(self, bot_open_id, store, reply, create_workflow=None, copy_workflow=None,
                 queue=None):
        self.bot_open_id = bot_open_id
        self.store = store
        self.reply = reply
        self.create_workflow = create_workflow
        self.copy_workflow = copy_workflow
        self.queue = queue

    def enqueue_business_event(self, event):
        if self.queue is None:
            return False
        text = get_addressed_text(event, self.bot_open_id)
        if text is None:
            return False
        from config import COPY_COMMAND
        operation = ("CREATE_DOCUMENT" if text == CREATE_COMMAND else
                     "COPY_DOCUMENT" if text == COPY_COMMAND or text.startswith(COPY_COMMAND + " ")
                     else None)
        if operation is None:
            return False
        requester = getattr(getattr(getattr(event, "sender", None), "sender_id", None),
                            "open_id", None)
        if not isinstance(requester, str) or not requester.startswith("ou_"):
            return True
        source_token = ""
        if operation == "COPY_DOCUMENT":
            from copy_parser import parse_copy_text, CopyInputError
            try:
                source_token = parse_copy_text(text)
            except CopyInputError:
                pass  # 无效格式仍入队由原业务链回复；不访问文档 API。
        queued = self.queue.enqueue(event.message.message_id, operation,
                                    requester, text, source_token or "")
        LOGGER.info("BUSINESS_EVENT_QUEUED operation=%s message_id=%s queued=%s",
                    operation, event.message.message_id, queued)
        return True

    def handle_event(self, data):
        message_id = None
        try:
            if self.enqueue_business_event(data.event):
                return
            if self.handle_copy_event(data.event):
                return
            if self.handle_help_event(data.event):
                return
            message_id = parse_test_message(data.event, self.bot_open_id)
            if not message_id:
                self.handle_create_event(data.event)
                return
            if not self.store.claim_message(message_id):
                LOGGER.info("DUPLICATE_IGNORED message_id=%s", message_id)
                return
            LOGGER.info("TEST_RECEIVED message_id=%s chat_type=%s",
                        message_id, data.event.message.chat_type)
            reply_id = self.reply(message_id, TEST_REPLY)
            self.store.finish_message(message_id, "replied", reply_id)
            LOGGER.info("TEST_REPLY_SUCCESS message_id=%s reply_id=%s", message_id, reply_id)
        except Exception:
            LOGGER.exception("MESSAGE_PROCESSING_FAILED message_id=%s", message_id)
            # 保留 claimed：发送结果不确定时，禁止重投触发重复发送。
    def handle_create_event(self, event):
        if not self.create_workflow:
            return
        parsed = parse_create_message(event, self.bot_open_id)
        if not parsed:
            return
        message_id, sender_id = parsed
        text = self.create_workflow.handle_create(message_id, sender_id)
        if text is None:
            return
        reply_id = self.reply(message_id, text)
        self.store.finish_message(message_id, "replied", reply_id)
        LOGGER.info("CREATE_COMMAND_REPLY_SUCCESS message_id=%s", message_id)

    def handle_help_event(self, event):
        text = get_addressed_text(event, self.bot_open_id)
        if text is None or text in {TEST_COMMAND, CREATE_COMMAND}:
            return False
        if text.casefold() in HELP_COMMANDS:
            reply_text = HELP_REPLY
        elif event.message.chat_type == "group":
            reply_text = SHORT_HELP_REPLY
        else:
            return False
        message_id = event.message.message_id
        if not self.store.claim_message(message_id):
            return True
        reply_id = self.reply(message_id, reply_text)
        self.store.finish_message(message_id, "replied", reply_id)
        LOGGER.info("HELP_REPLY_SUCCESS message_id=%s", message_id)
        return True

    def handle_copy_event(self, event):
        if self.copy_workflow is None:
            return False
        from config import COPY_COMMAND
        text = get_addressed_text(event, self.bot_open_id)
        if text is None or not (text == COPY_COMMAND or text.startswith(COPY_COMMAND + " ")):
            return False
        requester = getattr(getattr(getattr(event, "sender", None), "sender_id", None), "open_id", None)
        if not isinstance(requester, str) or not requester.startswith("ou_"):
            return True
        result = self.copy_workflow.handle_copy(event.message.message_id, requester, text)
        if result is not None:
            reply_id = self.reply(event.message.message_id, result)
            self.store.finish_message(event.message.message_id, "replied", reply_id)
            LOGGER.info("COPY_COMMAND_REPLY_SUCCESS")
        return True
