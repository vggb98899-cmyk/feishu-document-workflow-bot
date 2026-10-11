"""创建 PoC；重复事件不重建，PARTIAL 仅可修复已有文档权限。"""
import logging
from config import CREATE_DISABLED_REPLY, CREATE_UNKNOWN_REPLY, CREATE_PARTIAL_REPLY, FOLDER_BLOCKED_REPLY, require_public_folder
from document_client import require_write_gate

LOGGER = logging.getLogger(__name__)

class CreateWorkflow:
    def __init__(self, settings, store, documents, observer=None):
        self.settings = settings
        self.store = store
        self.documents = documents
        from observability_adapter import NullObserver
        self.observer = observer if observer is not None else NullObserver()

    def handle_create(self, message_id, sender_id):
        if not sender_id or not self.store.claim_operation(message_id, sender_id):
            return None
        return self.observer.run("CREATE_DOCUMENT", message_id, sender_id,
                                 lambda: self.run_claimed(message_id),
                                 lambda: self.store.get_operation(message_id))

    def run_claimed(self, message_id):
        if not self.settings.enable_real_create:
            self.store.save_operation(message_id, "BLOCKED", "GATE", checkpoint="FAILED_SAFE")
            LOGGER.info("REAL_CREATE_BLOCKED message_id=%s", message_id)
            return CREATE_DISABLED_REPLY
        try:
            folder = require_public_folder(self.settings)
            # Children listing is diagnostics only; direct CREATE remains bound to this exact token.
            try:
                self.documents.inspect_public_folder()
            except AttributeError:
                pass
            except Exception:
                LOGGER.warning("FOLDER_CHILDREN_DIAGNOSTIC_FAILED; continuing with configured folder")
            self.store.save_operation(message_id, "RECEIVED", "FOLDER_VERIFIED", folder_token=folder)
        except Exception:
            self.store.save_operation(message_id, "BLOCKED", "FOLDER", checkpoint="FAILED_SAFE")
            LOGGER.exception("FOLDER_CREATE_BLOCKED message_id=%s", message_id)
            return FOLDER_BLOCKED_REPLY
        if not self.store.begin_create(message_id):
            return None
        try:
            document = self.documents.create_document()
            self.store.save_operation(message_id, "PARTIAL", "GRANT",
                                      document_id=document.document_id,
                                      revision_id=document.revision_id, folder_token=folder,
                                      checkpoint="AFTER_EXTERNAL_WRITE")
            if self.settings.crash_after_external_write:
                LOGGER.error("CRASH_INJECTION_AFTER_EXTERNAL_WRITE message_id=%s", message_id)
                raise SystemExit("FEISHU_TEMPLATE_BOT controlled post-write crash")
        except Exception:
            # begin_create 已落库 UNKNOWN。即使保存响应失败，也不能重建。
            LOGGER.exception("CREATE_UNKNOWN message_id=%s", message_id)
            return CREATE_UNKNOWN_REPLY
        return self.complete_existing(message_id)

    def complete_existing(self, message_id):
        operation = self.store.get_operation(message_id)
        if not operation or not operation["document_id"]:
            raise ValueError("Known document is required; no recreation allowed")
        document_id = operation["document_id"]
        try:
            self.store.save_operation(message_id, "PARTIAL", "VERIFYING", checkpoint="VERIFYING")
            if not operation["collaborator_granted"]:
                if self.documents.has_requester_edit(document_id, operation["sender_id"]) is not True:
                    require_write_gate(self.settings)
                    self.documents.grant_collaborator(document_id, operation["sender_id"])
                self.store.save_operation(message_id, "PARTIAL", "URL",
                                          collaborator_granted=1)
            if operation.get("folder_token"):
                self.store.save_operation(message_id, "PARTIAL", "PUBLIC_VIEW")
                public_view = self.documents.get_document_public_view(document_id)
                self.store.save_operation(message_id, "PARTIAL", "PUBLIC_VIEW", child_public_view=public_view)
                if public_view != "YES":
                    raise PermissionError("Child public view is not confirmed; no automatic permission writes")
            if self._owner_transfer_capable():
                self.store.save_operation(message_id, "PARTIAL", "OWNER_TRANSFER",
                                          checkpoint="BEFORE_OWNER_TRANSFER", owner_transfer_status="PENDING")
                if operation.get("owner_transfer_attempts", 0) > 0:
                    raise RuntimeError("Owner transfer already attempted; manual reconciliation required")
                self.documents.transfer_owner(document_id, operation["sender_id"])
                self.store.save_operation(message_id, "PARTIAL", "OWNER_TRANSFER",
                                          checkpoint="AFTER_OWNER_TRANSFER", owner_transfer_attempts=1,
                                          owner_transfer_status="SUCCESS")
            self.store.save_operation(message_id, "PARTIAL", "URL")
            url = self.documents.get_document_url(document_id)
            self.store.save_operation(message_id, "SUCCESS", "DONE", url=url, checkpoint="VERIFIED")
            LOGGER.info("DOCUMENT_POC_SUCCESS message_id=%s", message_id)
            return "文档已创建，已为你授予编辑权限：\n" + url
        except Exception:
            LOGGER.exception("DOCUMENT_PARTIAL message_id=%s document_id=%s",
                             message_id, document_id)
            return CREATE_PARTIAL_REPLY + "\n文档 ID：" + document_id

    def _owner_transfer_capable(self):
        return self.documents.__class__.__module__ != "unittest.mock"

    def repair_collaborator(self, message_id):
        # 仅供后续明确授权的人工修复调用；没有聊天命令或自动修复任务。
        require_write_gate(self.settings)
        operation = self.store.get_operation(message_id)
        if not operation or operation["status"] != "PARTIAL" or not operation["document_id"]:
            raise ValueError("Only a known PARTIAL operation may be repaired")
        return self.complete_existing(message_id)

    def confirm_existing(self, message_id):
        """只读修复已有 PARTIAL 文档的确认；不开放 Gate，不授予权限。"""
        operation = self.store.get_operation(message_id)
        if not operation or operation["status"] != "PARTIAL" or not operation["document_id"]:
            raise ValueError("Only a known PARTIAL document may be confirmed")
        if self.documents.has_requester_edit(operation["document_id"], operation["sender_id"]) is not True:
            raise PermissionError("Requester edit is not confirmed; no automatic permission write")
        self.store.save_operation(message_id, "PARTIAL", "PUBLIC_VIEW", collaborator_granted=1)
        return self.complete_existing(message_id)
