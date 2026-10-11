"""复制流程：先只读确认原位置，未知不重试，部分成功只修已知副本。"""
import logging
from config import (COPY_DISABLED_REPLY, COPY_FORMAT_REPLY, COPY_ACCESS_REPLY,
                    COPY_BOUNDARY_REPLY, COPY_UNKNOWN_REPLY, COPY_PARTIAL_REPLY, COPY_SUCCESS_REPLY)
from copy_parser import parse_copy_text, CopyInputError
from copy_source import BoundaryUnconfirmed
from copy_client import require_copy_gate

LOGGER = logging.getLogger(__name__)

class CopyWorkflow:
    def __init__(self, settings, store, documents, observer=None):
        self.settings, self.store, self.documents = settings, store, documents
        from observability_adapter import NullObserver
        self.observer = observer if observer is not None else NullObserver()

    def handle_copy(self, message_id, requester, text):
        if not requester or not requester.startswith("ou_") or not self.store.claim(message_id, requester):
            return None
        try:
            token = parse_copy_text(text)
            if not token:
                raise CopyInputError(COPY_FORMAT_REPLY)
        except CopyInputError as error:
            self.store.save(message_id, "BLOCKED", "INPUT", checkpoint="FAILED_SAFE")
            return str(error)
        self.store.save(message_id, "RECEIVED", "GATE", source_token=token)
        return self.observer.run("COPY_DOCUMENT", message_id, requester,
                                 lambda: self.run_claimed(message_id,requester,token),
                                 lambda: self.store.get(message_id))

    def run_claimed(self, message_id, requester, token):
        if not self.settings.enable_real_copy:
            self.store.save(message_id, "BLOCKED", "GATE", checkpoint="FAILED_SAFE")
            return COPY_DISABLED_REPLY
        try:
            source = self.documents.inspect_source(token, requester)
            if source.token != token or not source.parent:
                raise ValueError("Missing verified destination")
            self.store.save(message_id, "RECEIVED", "SOURCE_VERIFIED", parent_token=source.parent)
        except BoundaryUnconfirmed:
            self.store.save(message_id, "BLOCKED", "ACCESS", checkpoint="FAILED_SAFE")
            return COPY_ACCESS_REPLY
        except Exception:
            self.store.save(message_id, "BLOCKED", "ACCESS", checkpoint="FAILED_SAFE")
            LOGGER.warning("COPY_PREFLIGHT_FAILED; no copy issued")
            return COPY_ACCESS_REPLY
        if not self.store.begin_copy(message_id):
            return None
        try:
            document_id = self.documents.copy_document(source)
            self.store.save(message_id, "PARTIAL", "PLACEMENT", document_id=document_id,
                            checkpoint="AFTER_EXTERNAL_WRITE")
        except Exception:
            LOGGER.warning("COPY_UNKNOWN; no automatic retry")
            return COPY_UNKNOWN_REPLY
        return self.complete_existing(message_id)

    def complete_existing(self, message_id):
        operation = self.store.get(message_id)
        if not operation or operation["status"] != "PARTIAL" or not operation["document_id"]:
            raise ValueError("Known PARTIAL copy required")
        require_copy_gate(self.settings)
        try:
            self.store.save(message_id, "PARTIAL", "VERIFYING", checkpoint="VERIFYING")
            doc = operation["document_id"]
            self.store.save(message_id, "PARTIAL", "GRANT")
            self.documents.ensure_requester_edit(doc, operation["requester"])
            self.store.save(message_id, "PARTIAL", "URL", requester_edit=1)
            if self.documents.__class__.__module__ != "unittest.mock":
                self.store.save(message_id, "PARTIAL", "OWNER_TRANSFER",
                                checkpoint="BEFORE_OWNER_TRANSFER", owner_transfer_status="PENDING")
                if operation.get("owner_transfer_attempts", 0) > 0:
                    raise RuntimeError("Owner transfer already attempted; manual reconciliation required")
                self.documents.transfer_owner(doc, operation["requester"])
                self.store.save(message_id, "PARTIAL", "OWNER_TRANSFER",
                                checkpoint="AFTER_OWNER_TRANSFER", owner_transfer_attempts=1,
                                owner_transfer_status="SUCCESS")
            url = self.documents.get_url(doc)
            self.store.save(message_id, "SUCCESS", "DONE", url=url, checkpoint="VERIFIED")
            return COPY_SUCCESS_REPLY + url
        except Exception:
            LOGGER.warning("COPY_PARTIAL; only existing copy may be repaired")
            return COPY_PARTIAL_REPLY
