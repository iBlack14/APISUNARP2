"""mitmproxy addon that records only non-sensitive traffic metadata."""

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from time import gmtime, strftime

from mitmproxy import http

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic.log")

log = logging.getLogger("traffic-metadata")
log.setLevel(logging.INFO)
log.addHandler(
    RotatingFileHandler(
        LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
)


def response(flow: http.HTTPFlow) -> None:
    response = flow.response
    if response is None:
        return

    entry = {
        "ts": strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()),
        "method": flow.request.method,
        "host": flow.request.pretty_host,
        "status": response.status_code,
        "duration_ms": round(
            (response.timestamp_end - flow.request.timestamp_start) * 1000
        )
        if response.timestamp_end is not None
        and flow.request.timestamp_start is not None
        else None,
    }
    log.info(json.dumps(entry, ensure_ascii=True))
