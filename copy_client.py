"""独立COPY写Gate；只操作复制结果，不复用CREATE写权限。"""
import threading
from lark_oapi.api.drive.v1 import CopyFileRequest, CopyFileRequestBody, BaseMember, CreatePermissionMemberRequest
from config import COPY_NAME_SUFFIX, COPY_MAX_NAME_BYTES
from copy_source import SourceInspector, tenant, validate_token, BoundaryUnconfirmed
from document_client import DocumentClient, require_success

# 官方copy API不支持并发；该锁不影响创建链，也不是次数上限。
COPY_API_LOCK = threading.Lock()

def require_copy_gate(settings):
    if settings.enable_real_copy is not True:
        raise PermissionError("Real copy writes are disabled")

class CopyClient:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.inspector = SourceInspector(client, settings)
        self.reader = DocumentClient(client, settings)

    def inspect_source(self, token, requester):
        return self.inspector.inspect(token, requester)

    def copy_document(self, source):
        require_copy_gate(self.settings)
        validate_token(source.token)
        target_folder = validate_token(source.parent)
        name = source.title.encode("utf-8")[:COPY_MAX_NAME_BYTES-len(COPY_NAME_SUFFIX.encode("utf-8"))].decode("utf-8", errors="ignore") + COPY_NAME_SUFFIX
        body = CopyFileRequestBody.builder().name(name).type("docx").folder_token(target_folder).build()
        request = CopyFileRequest.builder().file_token(source.token).request_body(body).build()
        with COPY_API_LOCK:
            response = self.client.drive.v1.file.copy(tenant(request))
        require_success(response, "COPY_REQUEST")
        # 一旦得到token先保存，其他字段/异步完成状态在后续回读；无token则UNKNOWN。
        copied = response.data.file
        token = validate_token(copied.token if copied else None)
        if token == source.token:
            raise BoundaryUnconfirmed("Copy did not return a distinct resource")
        return token

    def ensure_requester_edit(self, document_id, requester):
        require_copy_gate(self.settings)
        if self.reader.has_requester_edit(document_id, requester):
            return
        member = BaseMember.builder().member_type("openid").member_id(requester).perm("edit").build()
        request = CreatePermissionMemberRequest.builder().token(document_id).type("docx").need_notification(False).request_body(member).build()
        response = self.client.drive.v1.permission_member.create(tenant(request))
        require_success(response, "COPY_REQUESTER_GRANT")
        if not self.reader.has_requester_edit(document_id, requester):
            raise PermissionError("Copy requester edit not confirmed")

    def get_url(self, document_id):
        return self.reader.get_document_url(document_id)

    def transfer_owner(self, document_id, requester):
        return self.reader.transfer_owner(document_id, requester)

    def read_owner_open_id(self, document_id):
        return self.reader.read_owner_open_id(document_id)
