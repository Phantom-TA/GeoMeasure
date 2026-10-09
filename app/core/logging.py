import logging

_FORMAT = "%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Called in the API process and in every worker process (spawned workers start bare)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=_FORMAT)
    root.setLevel(level)
