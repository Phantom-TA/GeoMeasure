import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_TEXT_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(processName)s %(name)s: %(message)s"


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "process": record.processName,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Called in the API process and in every worker process (spawned workers start bare)."""
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, "_geomeasure", False)]:
        root.removeHandler(existing)
    handler._geomeasure = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
