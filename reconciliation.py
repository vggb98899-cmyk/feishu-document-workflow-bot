"""Worker 重启时只读核单；绝不对旧任务再次调用 create/copy/grant。"""
from __future__ import annotations

import logging


LOGGER = logging.getLogger(__name__)


class ReadOnlyReconciler:
    def __init__(self, create_store, copy_store, create_client, copy_client, notify=None):
        self.create_store = create_store
        self.copy_store = copy_store
        self.create_client = create_client
        self.copy_client = copy_client
        self.notify = notify

    def notify_success(self, queue, job, operation_type, url):
        if not self.notify or not url or not queue.claim_notification(job["operation_id"]):
            return
        text = ("文档已创建成功。系统恢复后已确认结果：\n" if operation_type == "CREATE_DOCUMENT"
                else "副本已复制成功。系统恢复后已确认结果：\n") + url
        try:
            reply_id = self.notify(job["message_id"], text)
            queue.mark_notification(job["operation_id"], "SENT")
            LOGGER.info("RECOVERY_NOTIFICATION_SENT operation_id=%s reply_id=%s",
                        job["operation_id"], reply_id)
        except Exception:
            queue.mark_notification(job["operation_id"], "UNKNOWN")
            LOGGER.warning("RECOVERY_NOTIFICATION_UNKNOWN operation_id=%s",
                           job["operation_id"])

    def reconcile_create(self, operation):
        message_id = operation["message_id"]
        document_id = operation.get("document_id")
        if not document_id:
            if operation["create_attempts"]:
                self.create_store.save_operation(message_id, "AMBIGUOUS", "MANUAL_REVIEW")
                return "AMBIGUOUS_NO_DOCUMENT_ID"
            self.create_store.save_operation(message_id, "FAILED_SAFE", "NO_WRITE_ISSUED",
                                             checkpoint="FAILED_SAFE")
            return "FAILED_SAFE_NO_WRITE"
        try:
            folder = operation["folder_token"]
            if not folder:
                raise PermissionError("Missing saved create parent")
            self.create_client.confirm_placement(document_id, folder)
            url = self.create_client.get_document_url(document_id)
            can_edit = self.create_client.has_requester_edit(document_id, operation["sender_id"])
            public_view = self.create_client.get_document_public_view(document_id)
            if not can_edit or public_view != "YES":
                self.create_store.save_operation(message_id, "PARTIAL", "READ_ONLY_RECONCILIATION",
                                                 collaborator_granted=int(can_edit),
                                                 child_public_view=public_view, url=url)
                return "PARTIAL_EXISTING_DOCUMENT"
            self.create_store.save_operation(message_id, "SUCCESS", "DONE", url=url,
                                             collaborator_granted=1,
                                             child_public_view="YES", checkpoint="VERIFIED")
            return "SUCCESS_EXISTING_DOCUMENT"
        except Exception:
            LOGGER.warning("CREATE_READ_ONLY_RECONCILIATION_INCOMPLETE message_id=%s", message_id)
            self.create_store.save_operation(message_id, "PARTIAL", "READ_ONLY_RECONCILIATION")
            return "PARTIAL_REVIEW_EXISTING_DOCUMENT"

    def reconcile_copy(self, operation):
        message_id = operation["message_id"]
        document_id = operation.get("document_id")
        if not document_id:
            if operation["copy_attempts"]:
                self.copy_store.save(message_id, "AMBIGUOUS", "MANUAL_REVIEW")
                return "AMBIGUOUS_NO_COPY_TOKEN"
            self.copy_store.save(message_id, "FAILED_SAFE", "NO_WRITE_ISSUED",
                                 checkpoint="FAILED_SAFE")
            return "FAILED_SAFE_NO_WRITE"
        try:
            parent = operation["parent_token"]
            if not parent:
                raise PermissionError("Missing saved copy parent")
            self.copy_client.confirm_placement(document_id, parent)
            url = self.copy_client.get_url(document_id)
            can_edit = self.copy_client.reader.has_requester_edit(document_id, operation["requester"])
            if not can_edit:
                self.copy_store.save(message_id, "PARTIAL", "READ_ONLY_RECONCILIATION", url=url)
                return "PARTIAL_EXISTING_COPY"
            self.copy_store.save(message_id, "SUCCESS", "DONE", url=url,
                                 requester_edit=1, checkpoint="VERIFIED")
            return "SUCCESS_EXISTING_COPY"
        except Exception:
            LOGGER.warning("COPY_READ_ONLY_RECONCILIATION_INCOMPLETE message_id=%s", message_id)
            self.copy_store.save(message_id, "PARTIAL", "READ_ONLY_RECONCILIATION")
            return "PARTIAL_REVIEW_EXISTING_COPY"

    def scan(self, queue):
        """先处理旧活动任务，再处理无队列的历史未完成 operation。"""
        results = {}
        processed_messages = set()
        for job in queue.list_interrupted():
            message_id = job["message_id"]
            processed_messages.add(message_id)
            operation = (self.create_store.get_operation(message_id)
                         if job["operation_type"] == "CREATE_DOCUMENT"
                         else self.copy_store.get(message_id))
            if operation is None:
                # worker 只会在原子认领 operation 后执行 API；尚未认领即可证明没有 write。
                if queue.is_message_claimed(message_id):
                    queue.set_job_state(job["operation_id"], "HELD", "INCONSISTENT_CLAIM")
                    results[job["operation_id"]] = "HELD_INCONSISTENT_CLAIM"
                else:
                    queue.set_job_state(job["operation_id"], "PENDING", "FAILED_SAFE_REQUEUED")
                    results[job["operation_id"]] = "FAILED_SAFE_REQUEUED"
                continue
            if operation["status"] == "SUCCESS":
                result = "SUCCESS_ALREADY_PERSISTED"
            elif operation["status"] in {"BLOCKED", "FAILED_SAFE"}:
                result = "FAILED_SAFE_ALREADY_PERSISTED"
            elif job["operation_type"] == "CREATE_DOCUMENT":
                result = self.reconcile_create(operation)
            else:
                result = self.reconcile_copy(operation)
            queue.set_job_state(job["operation_id"],
                                "DONE" if result.startswith("SUCCESS") else "HELD", result)
            if result.startswith("SUCCESS"):
                url = operation.get("url")
                if not url:
                    url = (self.create_client.get_document_url(operation["document_id"])
                           if job["operation_type"] == "CREATE_DOCUMENT"
                           else self.copy_client.get_url(operation["document_id"]))
                self.notify_success(queue, job, job["operation_type"], url)
            results[job["operation_id"]] = result
        for operation in self.create_store.list_recoverable():
            if operation["message_id"] not in processed_messages:
                results["CREATE:" + operation["message_id"]] = self.reconcile_create(operation)
        for operation in self.copy_store.list_recoverable():
            if operation["message_id"] not in processed_messages:
                results["COPY:" + operation["message_id"]] = self.reconcile_copy(operation)
        return results
