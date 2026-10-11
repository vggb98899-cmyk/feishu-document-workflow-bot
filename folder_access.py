"""目标目录与公共查看只读核验，不提供任何权限写入接口。"""
import lark_oapi as lark
from lark_oapi.api.drive.v1 import ListFileRequest, ListPermissionMemberRequest
from lark_oapi.api.drive.v2 import GetPermissionPublicRequest
from config import require_public_folder, PUBLIC_VIEW_ENTITY, FOLDER_PROBE_PAGE_SIZE

def require_read_success(response, stage):
    if not response.success():
        raise PermissionError(stage + " code=" + str(response.code))

def get_link_share_entity(client, token, resource_type):
    request = GetPermissionPublicRequest.builder().token(token).type(resource_type).build()
    request.token_types = {lark.AccessTokenType.TENANT}
    response = client.drive.v2.permission_public.get(request)
    require_read_success(response, "PUBLIC_PERMISSION_READ")
    return response.data.permission_public.link_share_entity

def inspect_public_folder(client, settings):
    folder = require_public_folder(settings)
    request = ListFileRequest.builder().folder_token(folder).page_size(FOLDER_PROBE_PAGE_SIZE).build()
    request.token_types = {lark.AccessTokenType.TENANT}
    response = client.drive.v1.file.list(request)
    require_read_success(response, "FOLDER_CHILDREN_READ")
    request = ListPermissionMemberRequest.builder().token(folder).type("folder").build()
    request.token_types = {lark.AccessTokenType.TENANT}
    response = client.drive.v1.permission_member.list(request)
    require_read_success(response, "FOLDER_MEMBERS_READ")
    # 按已实际核验的 appid 协作者识别，不能拿用户/机器人昵称替代。
    permissions = [member.perm for member in response.data.items or []
                   if member.member_type == "appid" and member.member_id == settings.app_id]
    return {"folder_token": folder, "app_can_place": any(
        perm in {"edit", "full_access"} for perm in permissions),
        "link_share_entity": get_link_share_entity(client, folder, "folder")}

def verify_public_folder(client, settings):
    inspection = inspect_public_folder(client, settings)
    if not inspection["app_can_place"]:
        raise PermissionError("Application lacks verified folder collaborator permission")
    if inspection["link_share_entity"] != PUBLIC_VIEW_ENTITY:
        raise PermissionError("Folder link sharing must be tenant_readable")
    return inspection["folder_token"]

def get_document_public_view(client, document_id):
    return "YES" if get_link_share_entity(client, document_id, "docx") == PUBLIC_VIEW_ENTITY else "NO"
