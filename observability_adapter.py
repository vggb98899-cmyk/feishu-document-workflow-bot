"""Privacy-preserving operation wrapper for the public edition."""
import logging

LOGGER = logging.getLogger(__name__)


class NullObserver:
    def run(self, operation, message_id, requester, action, read_state):
        return action()

    def configuration_error(self, error):
        LOGGER.error("CONFIGURATION_ERROR type=%s", type(error).__name__)


def build_observability():
    return NullObserver()
