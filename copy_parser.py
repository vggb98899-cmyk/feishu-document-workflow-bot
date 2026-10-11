"""仅解析字符串；从不请求输入URL。"""
import re
from urllib.parse import urlsplit
from config import COPY_COMMAND, COPY_TOKEN_PATTERN, COPY_FORMAT_REPLY, COPY_UNSUPPORTED_REPLY

class CopyInputError(ValueError):
    pass

def parse_copy_text(text):
    if text != COPY_COMMAND and not text.startswith(COPY_COMMAND + " "):
        return None
    raw_text = text.strip()
    markdown = re.fullmatch(r"复制副本\s+\[[^\]]+\]\((https://[^)]+)\)", raw_text)
    if markdown:
        raw = markdown.group(1)
    else:
        parts = raw_text.split()
        if len(parts) != 2 or parts[0] != COPY_COMMAND:
            raise CopyInputError(COPY_FORMAT_REPLY)
        raw = parts[1]
    if any(char in raw for char in "\\<>\"'") or any(ord(char) < 32 for char in raw):
        raise CopyInputError(COPY_FORMAT_REPLY)
    try:
        url = urlsplit(raw)
        host = url.hostname or ""
        valid_host = re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.feishu\.cn", host)
        if url.scheme != "https" or not valid_host or url.username or url.password or url.port is not None:
            raise CopyInputError(COPY_FORMAT_REPLY)
    except ValueError:
        raise CopyInputError(COPY_FORMAT_REPLY) from None
    if re.search(r"https?[:%]", url.query, re.IGNORECASE):
        raise CopyInputError(COPY_FORMAT_REPLY)
    match = re.fullmatch(r"/([a-zA-Z0-9_-]+)/(" + COPY_TOKEN_PATTERN + r")/?", url.path)
    if not match:
        raise CopyInputError(COPY_FORMAT_REPLY)
    if match[1] != "docx":
        raise CopyInputError(COPY_UNSUPPORTED_REPLY)
    return match[2]
