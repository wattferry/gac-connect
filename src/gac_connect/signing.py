"""Request signing.

Each request carries a ``sig`` header: an uppercase HMAC-SHA256 over a canonical
string built from the encrypted body plus a fixed set of ``fnc-*`` headers. The
canonicalisation sorts keys and concatenates ``key + value`` with no separators,
matching the client's map-serialisation (nulls, bools and lists rendered the way
the client's runtime prints them).

The IoV gateway and the main API sign a slightly different header set with
different HMAC keys, so each has its own profile.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

# The header names each profile folds into the signature, alongside "body".
IOV_FIELDS = ("fnc-app-id", "fnc-request-id", "fnc-timestamp", "fnc-version")
MAIN_FIELDS = ("fnc-app-id", "fnc-requestId", "fnc-timestamp")


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    return str(value)


def _canonicalise(value: Any) -> str:
    if isinstance(value, dict):
        return "".join(_scalar(k) + _canonicalise(value[k]) for k in sorted(value, key=_scalar))
    return _scalar(value)


def _payload(headers: dict[str, str], fields: tuple[str, ...], body: Any | None) -> dict[str, Any]:
    signed: dict[str, Any] = {name: headers[name] for name in fields if name in headers}
    if body is not None:
        signed["body"] = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if isinstance(body, (dict, list))
            else body
        )
    return signed


def sign(headers: dict[str, str], body: Any | None, *, fields: tuple[str, ...], hmac_key: bytes) -> str:
    message = _canonicalise(_payload(headers, fields, body)).encode()
    return hmac.new(hmac_key, message, hashlib.sha256).hexdigest().upper()
