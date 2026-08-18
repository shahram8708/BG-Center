import logging
import uuid

from flask import request

_LOGGER = logging.getLogger("bgcc")


def request_id():
    return request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]


def log_event(event, **fields):
    meta = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    _LOGGER.info("event=%s rid=%s %s", event, request_id(), meta)


def log_error(event, error, **fields):
    meta = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    _LOGGER.exception("event=%s rid=%s %s error=%r", event, request_id(), meta, error)
