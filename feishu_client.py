"""官方 SDK：只读机器人身份、长连接及指令结果回复。"""
import json
import logging
import uuid
import lark_oapi as lark
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
from config import API_TIMEOUT_SECONDS, BOT_INFO_URI, FEISHU_DOMAIN, TEST_REPLY

LOGGER = logging.getLogger(__name__)

def create_client(settings):
    return (lark.Client.builder().app_id(settings.app_id)
            .app_secret(settings.app_secret).domain(FEISHU_DOMAIN)
            .timeout(API_TIMEOUT_SECONDS).log_level(lark.LogLevel.INFO).build())

def get_bot_open_id(client):
    request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET)
               .uri(BOT_INFO_URI).token_types({lark.AccessTokenType.TENANT}).build())
    response = client.request(request)
    payload = json.loads(response.raw.content)
    if payload.get("code") != 0:
        raise RuntimeError("BOT_IDENTITY_FAILED code=" + str(payload.get("code")))
    open_id = payload.get("bot", {}).get("open_id")
    if not open_id:
        raise RuntimeError("BOT_IDENTITY_FAILED missing open_id")
    LOGGER.info("BOT_IDENTITY_VERIFIED")
    return open_id

def reply_test_message(client, message_id, text):
    if text != TEST_REPLY:
        raise ValueError("Phase 1 只允许固定测试回复")
    return reply_message(client, message_id, text)

def reply_message(client, message_id, text):
    body = (ReplyMessageRequestBody.builder().msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(str(uuid.uuid5(uuid.NAMESPACE_URL, message_id))).build())
    request = (ReplyMessageRequest.builder().message_id(message_id)
               .request_body(body).build())
    response = client.im.v1.message.reply(request)
    if not response.success():
        raise RuntimeError("REPLY_FAILED code=" + str(response.code))
    return response.data.message_id

def create_connection(settings, callback):
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(callback).build())
    return lark.ws.Client(settings.app_id, settings.app_secret,
                          event_handler=handler, domain=FEISHU_DOMAIN,
                          log_level=lark.LogLevel.INFO)