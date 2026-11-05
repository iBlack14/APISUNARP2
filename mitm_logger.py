"""Addon mitmproxy: registra todo el trafico HTTP/HTTPS en traffic.log (JSON lines)."""

import json
import os
import time

from mitmproxy import http

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic.log")
MAX_BODY = 20000


def _headers(flow: http.HTTPFlow) -> dict:
    interesting = {}
    for k, v in flow.request.headers.items():
        kl = k.lower()
        if kl in (
            "authorization", "cookie", "x-api-key", "api-key", "apikey",
            "x-auth-token", "x-access-token", "x-csrf-token", "x-xsrf-token",
            "proxy-authorization", "x-api-token", "x-session-token",
            "x-requested-with", "content-type", "origin", "referer",
        ) or "token" in kl or "auth" in kl or "session" in kl:
            interesting[k] = v
    return interesting


def _resp_headers(flow: http.HTTPFlow) -> dict:
    interesting = {}
    for k, v in flow.response.headers.items():
        kl = k.lower()
        if kl in (
            "set-cookie", "authorization", "x-api-key", "content-type",
        ) or "token" in kl or "auth" in kl or "session" in kl:
            interesting[k] = v
    return interesting


def _body(content: bytes | None) -> str | None:
    if not content:
        return None
    try:
        text = content[:MAX_BODY].decode("utf-8", errors="replace")
        return text
    except Exception:
        return f"<binario {len(content)} bytes>"


def response(flow: http.HTTPFlow) -> None:
    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "status": flow.response.status_code if flow.response else None,
        "req_headers": _headers(flow),
        "req_body": _body(flow.request.content),
        "resp_headers": _resp_headers(flow) if flow.response else {},
        "resp_body": _body(flow.response.content) if flow.response else None,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['ts']}] {entry['method']} {entry['url']} -> {entry['status']}", flush=True)
