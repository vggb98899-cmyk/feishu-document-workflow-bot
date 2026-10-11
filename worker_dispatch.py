"""单 worker 顺序消费队列；保留原 CREATE/COPY 工作流与回复逻辑。"""
from __future__ import annotations

import logging


LOGGER = logging.getLogger(__name__)


class BusinessDispatcher:
    def __init__(self, create_workflow, copy_workflow, messages, reply):
        self.create_workflow = create_workflow
        self.copy_workflow = copy_workflow
        self.messages = messages
        self.reply = reply

    def __call__(self, job):
        operation = job["operation_type"]
        message_id = job["message_id"]
        if operation == "CREATE_DOCUMENT":
            response = self.create_workflow.handle_create(message_id, job["requester"])
        elif operation == "COPY_DOCUMENT":
            response = self.copy_workflow.handle_copy(message_id, job["requester"],
                                                      job["command_text"])
        else:
            raise ValueError("Unsupported worker operation")
        if response is not None:
            reply_id = self.reply(message_id, response)
            self.messages.finish_message(message_id, "replied", reply_id)
            LOGGER.info("WORKER_BUSINESS_REPLY_SUCCESS operation=%s message_id=%s",
                        operation, message_id)
