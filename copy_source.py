"""应用身份只读核验原稿及用户指定的候选目录；绝不扫描根目录。"""
from dataclasses import dataclass, field
import re
import lark_oapi as lark
from lark_oapi.api.docx.v1 import GetDocumentRequest
from lark_oapi.api.drive.v1 import ListFileRequest, ListPermissionMemberRequest
from document_client import DocumentClient, require_success
from config import (COPY_TOKEN_PATTERN, COPY_PAGE_SIZE, COPY_MAX_FOLDER_PAGES,
                    EDIT_CAPABLE_PERMISSIONS)

class ParentUnresolved(ValueError):
    pass

class BoundaryUnconfirmed(PermissionError):
    pass

@dataclass(frozen=True)
class CopySource:
    token: str = field(repr=False)
    parent: str = field(repr=False)
    title: str = field(repr=False)

def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(COPY_TOKEN_PATTERN, token):
        raise ValueError("Invalid resource identifier")
    return token

def tenant(request):
    request.token_types = {lark.AccessTokenType.TENANT}
    return request

class SourceInspector:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    def read_members(self, token, kind):
        request = ListPermissionMemberRequest.builder().token(token).type(kind).build()
        response = self.client.drive.v1.permission_member.list(tenant(request))
        require_success(response, "COPY_MEMBERS_READ")
        return response.data.items or []

    def find_in_folder(self, token, folder):
        validate_token(folder)
        page_token = None
        seen = set()
        for _ in range(COPY_MAX_FOLDER_PAGES):
            builder = ListFileRequest.builder().folder_token(folder).page_size(COPY_PAGE_SIZE)
            if page_token:
                builder.page_token(page_token)
            response = self.client.drive.v1.file.list(tenant(builder.build()))
            require_success(response, "COPY_FOLDER_READ")
            matches = [item for item in response.data.files or [] if item.token == token and item.type == "docx"]
            if matches:
                if len(matches) != 1 or matches[0].parent_token != folder:
                    raise ParentUnresolved("Ambiguous parent")
                return True
            if not response.data.has_more:
                return False
            page_token = response.data.next_page_token
            if not page_token or page_token in seen:
                raise ParentUnresolved("Invalid pagination")
            seen.add(page_token)
        raise ParentUnresolved("Folder read budget exceeded")

    def inspect(self, token, requester):
        validate_token(token)
        response = self.client.docx.v1.document.get(tenant(GetDocumentRequest.builder().document_id(token).build()))
        require_success(response, "COPY_SOURCE_READ")
        document = response.data.document
        if not document or document.document_id != token or not document.title:
            raise ValueError("Source metadata incomplete")
        # 应用身份成功读取 docx 即完成 source access 核验；发起人无需是原稿协作者。
        from config import require_public_folder
        return CopySource(token, require_public_folder(self.settings), document.title)
