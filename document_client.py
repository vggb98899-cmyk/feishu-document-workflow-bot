"""Phase 2 唯一文档 API 适配层：应用身份、双重 Gate、无 create 重试。"""
from dataclasses import dataclass
from urllib.parse import urlsplit
import lark_oapi as lark
from lark_oapi.api.docx.v1 import CreateDocumentRequest, CreateDocumentRequestBody
from lark_oapi.api.drive.v1 import (
    BaseMember, CreatePermissionMemberRequest, BatchQueryMetaRequest,
    MetaRequest, RequestDoc, ListPermissionMemberRequest,
)
from config import DOCUMENT_TITLE, COLLABORATOR_PERMISSION, DOCUMENT_URL_HOST_SUFFIX, require_public_folder, EDIT_CAPABLE_PERMISSIONS

@dataclass(frozen=True)
class CreatedDocument:
    document_id: str
    revision_id: int | None = None
    folder_token: str | None = None

def require_write_gate(settings):
    if settings.enable_real_create is not True:
        raise PermissionError("Real document writes are disabled")

def require_success(response, stage):
    if not response.success():
        raise RuntimeError(stage + " code=" + str(response.code))

class DocumentClient:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    def create_document(self):
        require_write_gate(self.settings)
        folder = require_public_folder(self.settings)
        body = (CreateDocumentRequestBody.builder().title(self.settings.document_title)
                .folder_token(folder).build())
        request = CreateDocumentRequest.builder().request_body(body).build()
        request.token_types = {lark.AccessTokenType.TENANT}
        response = self.client.docx.v1.document.create(request)
        require_success(response, "DOCUMENT_CREATE")
        document = response.data.document
        if not document or not isinstance(document.document_id, str) or not document.document_id:
            raise RuntimeError("DOCUMENT_CREATE missing document_id; UNKNOWN")
        return CreatedDocument(document.document_id, document.revision_id, folder)

    def grant_collaborator(self, document_id, sender_open_id):
        require_write_gate(self.settings)
        if not document_id or not sender_open_id:
            raise ValueError("Missing document or requester")
        member = (BaseMember.builder().member_type("openid").member_id(sender_open_id)
                  .perm(COLLABORATOR_PERMISSION).build())
        request = (CreatePermissionMemberRequest.builder().token(document_id).type("docx")
                   .need_notification(False).request_body(member).build())
        request.token_types = {lark.AccessTokenType.TENANT}
        response = self.client.drive.v1.permission_member.create(request)
        require_success(response, "COLLABORATOR_GRANT")
        granted = response.data.member
        is_confirmed = (granted and granted.member_id == sender_open_id
                        and granted.member_type == "openid"
                        and granted.perm in EDIT_CAPABLE_PERMISSIONS)
        if not is_confirmed and not self.has_requester_edit(document_id, sender_open_id):
            raise RuntimeError("COLLABORATOR_GRANT requester edit permission not confirmed")

    def transfer_owner(self, document_id, sender_open_id):
        from lark_oapi.api.drive.v1 import TransferOwnerPermissionMemberRequest, Owner
        owner = Owner.builder().member_type("openid").member_id(sender_open_id).build()
        request = (TransferOwnerPermissionMemberRequest.builder().token(document_id).type("docx")
                   .need_notification(False).remove_old_owner(False).request_body(owner).build())
        request.token_types = {lark.AccessTokenType.TENANT}
        response = self.client.drive.v1.permission_member.transfer_owner(request)
        require_success(response, "OWNER_TRANSFER")

    def read_owner_open_id(self, document_id):
        reader = getattr(self.client.drive.v1.permission_member, "get_owner", None)
        return reader(document_id) if reader else None

    def has_requester_edit(self, document_id, sender_open_id):
        request = (ListPermissionMemberRequest.builder().token(document_id)
                   .type("docx").build())
        request.token_types = {lark.AccessTokenType.TENANT}
        response = self.client.drive.v1.permission_member.list(request)
        require_success(response, "COLLABORATOR_READ_BACK")
        return any(member.member_type == "openid" and member.member_id == sender_open_id
                   and member.perm in EDIT_CAPABLE_PERMISSIONS
                   for member in response.data.items or [])

    def get_document_url(self, document_id):
        document = RequestDoc.builder().doc_token(document_id).doc_type("docx").build()
        body = MetaRequest.builder().request_docs([document]).with_url(True).build()
        request = BatchQueryMetaRequest.builder().request_body(body).build()
        request.token_types = {lark.AccessTokenType.TENANT}
        response = self.client.drive.v1.meta.batch_query(request)
        require_success(response, "DOCUMENT_URL")
        for meta in response.data.metas or []:
            if meta.doc_token != document_id or meta.doc_type != "docx":
                continue
            parsed = urlsplit(meta.url or "")
            host = parsed.hostname or ""
            if (parsed.scheme == "https" and not parsed.username and not parsed.password
                    and (host == DOCUMENT_URL_HOST_SUFFIX or
                         host.endswith("." + DOCUMENT_URL_HOST_SUFFIX))):
                return meta.url
        raise RuntimeError("DOCUMENT_URL missing valid official URL")

    def verify_public_folder(self):
        from folder_access import verify_public_folder
        return verify_public_folder(self.client, self.settings)

    def inspect_public_folder(self):
        from folder_access import inspect_public_folder
        return inspect_public_folder(self.client, self.settings)

    def get_document_public_view(self, document_id):
        from folder_access import get_document_public_view
        return get_document_public_view(self.client, document_id)

    def confirm_placement(self, document_id, folder_token):
        """恢复时只读核对已经保存的文档与目标父目录。"""
        from copy_source import SourceInspector
        if not SourceInspector(self.client, self.settings).find_in_folder(document_id, folder_token):
            raise PermissionError("Existing document parent cannot be confirmed")
