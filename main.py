"""FEISHU_TEMPLATE_BOT 正式入口：长连接 Main 与独立 consumer worker。"""
import logging
from config import LOG_PATH, load_settings
from utils import configure_logging

def main():
    configure_logging()
    from observability_adapter import build_observability
    observer = build_observability()
    runtime = None
    instance_lock = None
    try:
        settings = load_settings()
        from single_instance import MainInstanceLock
        from app_factory import build_application
        configure_logging(settings, LOG_PATH)
        instance_lock = MainInstanceLock()
        if not instance_lock.acquire():
            raise RuntimeError("FEISHU_TEMPLATE_BOT_MAIN_INSTANCE_ALREADY_RUNNING")
        runtime, connection = build_application(settings, observer)
        runtime.start()
        logging.info("REAL_CREATE_GATE=%s REAL_COPY_GATE=%s", settings.enable_real_create, settings.enable_real_copy)
        logging.info("EVENT_HANDLER=im.message.receive_v1; LONG_CONNECTION_STARTING")
        connection.start()
    except KeyboardInterrupt:
        logging.info("STOPPED_BY_USER")
        return 0
    except Exception as error:
        observer.configuration_error(error)
        logging.exception("STARTUP_FAILED")
        return 1
    finally:
        if runtime:
            runtime.stop()
        if instance_lock:
            instance_lock.release()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
