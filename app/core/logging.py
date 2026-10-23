"""Structured JSON logging with redaction of secrets and tokens."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_REDACT_PATTERNS = [
    (re.compile(r"([?&](?:t|token|access_token)=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END)", re.S), r"\1[REDACTED]\2"),
    (
        re.compile(r"(\"?(?:password|secret|api_key|private_key|mnemonic)\"?\s*[:=]\s*)\"?[^\",\s]+", re.I),
        r"\1[REDACTED]",
    ),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("request_id", "ip", "method", "path", "status", "duration_ms", "user_id", "ua", "host"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(str(value)) if key == "path" else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our access log with client IP
