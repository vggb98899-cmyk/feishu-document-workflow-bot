"""组装既有业务链与 Phase 5 队列/worker，入口只负责启动。"""
from __future__ import annotations

from functools import partial

from config import DATABASE_PATH
from copy_client import CopyClient
from copy_store import CopyStore
from copy_workflow import CopyWorkflow
from create_workflow import CreateWorkflow
from document_client import DocumentClient
from feishu_client import create_client, create_connection, get_bot_open_id, reply_message
from message_handler import MessageHandler
from operation_store import OperationStore
from reconciliation import ReadOnlyReconciler
from work_queue import WorkQueue
from worker_dispatch import BusinessDispatcher
from worker_runtime import WorkerRuntime


def build_application(settings, observer):
    client = create_client(settings)
    bot_open_id = get_bot_open_id(client)
    create_store = OperationStore(DATABASE_PATH)
    copy_store = CopyStore(DATABASE_PATH)
    queue = WorkQueue(DATABASE_PATH)
    documents = DocumentClient(client, settings)
    copies = CopyClient(client, settings)
    create_workflow = CreateWorkflow(settings, create_store, documents, observer)
    copy_workflow = CopyWorkflow(settings, copy_store, copies, observer)
    reply = partial(reply_message, client)
    handler = MessageHandler(bot_open_id, create_store, reply,
                             create_workflow, copy_workflow, queue)
    dispatcher = BusinessDispatcher(create_workflow, copy_workflow, create_store, reply)
    reconciler = ReadOnlyReconciler(create_store, copy_store, documents, copies, reply)
    runtime = WorkerRuntime(queue, reconciler, dispatcher)
    connection = create_connection(settings, handler.handle_event)
    runtime.connection_probe = lambda: bool(
        getattr(getattr(connection, "_conn", None), "open", False)
        or (getattr(connection, "_conn", None) is not None
            and not getattr(getattr(connection, "_conn", None), "closed", True)))
    connection.on_reconnecting = lambda: setattr(runtime, "connection_state", "RECONNECTING")
    connection.on_reconnected = lambda: setattr(runtime, "connection_state", "CONNECTED")
    return runtime, connection
