"""本地配置与 Phase 2 写入 Gate；只读取本项目 .env。"""
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
DATABASE_PATH = RUNTIME_DIR / "messages.sqlite3"
LOG_PATH = RUNTIME_DIR / "bot.log"
HEALTH_PATH = RUNTIME_DIR / "phase5_health.json"
INSTANCE_LOCK_PATH = RUNTIME_DIR / "feishu_template_bot_main.lock"
WORKER_POLL_SECONDS = 0.5
WORKER_HEARTBEAT_SECONDS = 2
WORKER_STALE_SECONDS = 8
CONNECTION_UNHEALTHY_SECONDS = 180
SUPERVISOR_RESTART_SECONDS = 3
FEISHU_DOMAIN = "https://open.feishu.cn"
BOT_INFO_URI = "/open-apis/bot/v3/info"
API_TIMEOUT_SECONDS = 10
DATABASE_TIMEOUT_SECONDS = 10
TEST_COMMAND = "测试"
TEST_REPLY = "创建模板机器人：连接正常"

@dataclass(frozen=True)
class Settings:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    enable_real_create: bool = False
    public_folder_token: str = ""
    enable_real_copy: bool = False
    copy_source_folders: tuple[str, ...] = field(default=(), repr=False)
    crash_after_external_write: bool = False
    document_title: str = DOCUMENT_TITLE if 'DOCUMENT_TITLE' in globals() else "【创建模板机器人PoC】测试文档"

def load_settings(env_path: Path = ENV_PATH) -> Settings:
    if not env_path.is_file():
        raise ValueError("项目 .env 缺失，启动已停止")
    values = dotenv_values(env_path, interpolate=False, encoding="utf-8-sig")
    credentials = [values.get(key, "").strip() if values.get(key) else ""
                   for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")]
    if any(not value or value.startswith("your_") for value in credentials):
        raise ValueError("项目 .env 凭证缺失或仍为占位符，启动已停止")
    gate = (values.get("AI_TEMPLATE_BOT_ENABLE_REAL_CREATE") or "false").strip().lower()
    if gate not in {"true", "false"}:
        raise ValueError("AI_TEMPLATE_BOT_ENABLE_REAL_CREATE 必须为 true 或 false")
    copy_gate = (values.get("AI_TEMPLATE_BOT_ENABLE_REAL_COPY") or "false").strip().lower()
    if copy_gate not in {"true", "false"}:
        raise ValueError("AI_TEMPLATE_BOT_ENABLE_REAL_COPY 必须为 true 或 false")
    candidates = tuple(item.strip() for item in (values.get("FEISHU_COPY_SOURCE_FOLDERS") or "").split(",") if item.strip())
    crash_injection = (values.get("FEISHU_TEMPLATE_BOT_CRASH_AFTER_EXTERNAL_WRITE") or "false").strip().lower()
    if crash_injection not in {"true", "false"}:
        raise ValueError("FEISHU_TEMPLATE_BOT_CRASH_AFTER_EXTERNAL_WRITE 必须为 true 或 false")
    title = (values.get("FEISHU_TEMPLATE_BOT_CRASH_TEST_TITLE") or "").strip()
    return Settings(*credentials, enable_real_create=gate == "true",
                    public_folder_token=(values.get("FEISHU_PUBLIC_FOLDER_TOKEN") or "").strip(),
                    enable_real_copy=copy_gate == "true", copy_source_folders=candidates,
                    crash_after_external_write=crash_injection == "true",
                    document_title=title or "【创建模板机器人PoC】测试文档")
CREATE_COMMAND = "创建文档"
DOCUMENT_TITLE = "【创建模板机器人PoC】测试文档"
COLLABORATOR_PERMISSION = "edit"
DOCUMENT_URL_HOST_SUFFIX = "feishu.cn"
CREATE_DISABLED_REPLY = "创建文档功能正在测试中，真实创建尚未启用。"
CREATE_UNKNOWN_REPLY = "创建结果尚不确定，需要人工核对；系统不会自动再次创建。"
CREATE_PARTIAL_REPLY = "文档已创建，但授权或链接确认尚未完成，需要人工核对；不会重新创建。"

PUBLIC_VIEW_ENTITY = "tenant_readable"
FOLDER_PROBE_PAGE_SIZE = 1
FOLDER_BLOCKED_REPLY = "公共目录配置或权限尚未满足要求，未创建文档，请联系维护人员核对。"

def require_public_folder(settings):
    token = settings.public_folder_token.strip()
    if not token or not token.isascii() or not token.isalnum() or token.startswith("your"):
        raise ValueError("FEISHU_PUBLIC_FOLDER_TOKEN 缺失或无效；禁止退回根目录")
    return token

HELP_COMMANDS = {"帮助", "help"}
HELP_REPLY = (
    "创建模板机器人使用说明：\n"
    "1. 创建文档：群聊发送 @创建模板机器人 创建文档；单聊发送 创建文档。\n"
    "文档进入公共目录，你获得编辑权限。\n"
    "2. 复制副本：群聊发送 @创建模板机器人 复制副本 <飞书文档链接>；单聊发送 复制副本 <飞书文档链接>。\n"
    "暂支持新版docx，副本统一进入配置的公共目录。\n"
    "3. 测试：发送 测试，检查机器人连接。\n"
    "4. 帮助：发送 帮助 或 help；群聊中请先 @机器人。"
)
SHORT_HELP_REPLY = (
    "未识别该指令。可发送：帮助/help 查看说明，或 创建文档，或 复制副本 <飞书文档链接>。"
    "群聊请先 @机器人。"
)

EDIT_CAPABLE_PERMISSIONS = frozenset({"edit", "full_access"})

COPY_COMMAND = "复制副本"
COPY_DISABLED_REPLY = "复制副本尚未开放，当前不会执行。"
COPY_UNSUPPORTED_REPLY = "当前复制副本暂只支持新版飞书文档。"
COPY_FORMAT_REPLY = "请使用：复制副本 <一个新版飞书文档链接>。"
COPY_ACCESS_REPLY = "机器人暂时没有该原稿访问权限，请先将创建模板机器人添加为原稿协作者。"
# Retained only for historical evidence; no production COPY path references this text.
COPY_PARENT_REPLY = "（历史文案：不再使用）"
COPY_BOUNDARY_REPLY = "暂未确认原稿访问权限或受限目录的可见范围，未执行复制，请联系维护人员核验。"
COPY_UNKNOWN_REPLY = "复制结果尚不确定，需要人工核对；不会自动再次复制。"
COPY_PARTIAL_REPLY = "副本已生成，但位置、权限或链接尚未全部确认，需要人工核对；不会再次复制。"
COPY_SUCCESS_REPLY = "副本已生成，你已具备编辑权限：\n"
COPY_NAME_SUFFIX = " - 副本"
COPY_MAX_NAME_BYTES = 256
COPY_PAGE_SIZE = 200
COPY_MAX_FOLDER_PAGES = 20
COPY_TOKEN_PATTERN = r"[A-Za-z0-9]{10,128}"

PROJECT_ID = "FEISHU_TEMPLATE_BOT"
PROJECT_NAME = "创建模板机器人"
APP_VERSION = "1.0.0"
OBSERVABILITY_AUDIT_PATH = RUNTIME_DIR / "observability_audit.jsonl"
OBSERVABILITY_TRIGGER = "FEISHU_BOT"
