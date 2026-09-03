"""Block-puzzle captcha helpers.

The gateway's sign-in is gated by an anji-plus block puzzle. The get response
returns two images and a ``secretKey`` of the form ``<32B key>@DS@<16B iv>`` plus
a one-time ``token``. The user slides the piece to an x offset; from that we build
two proofs, both AES-256-CBC with the session key/iv from ``secretKey``:

* ``pointJson``     = enc('{"x":X,"y":5}')           — sent to /check and as randomStr
* ``captchaTicket`` = enc('<token>---{"x":X,"y":5}') — sent to send-code / login
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from . import crypto
from .const import PUZZLE_HEIGHT, PUZZLE_PIECE_WIDTH, PUZZLE_WIDTH

_SEP = b"@DS@"


@dataclass
class Captcha:
    """A pending puzzle. ``background`` and ``piece`` are base64 PNGs."""
    background: str
    piece: str
    secret_key: str
    token: str
    width: int = PUZZLE_WIDTH
    height: int = PUZZLE_HEIGHT
    piece_width: int = PUZZLE_PIECE_WIDTH

    @classmethod
    def from_data(cls, data: dict) -> Captcha:
        return cls(
            background=data["originalImageBase64"],
            piece=data["jigsawImageBase64"],
            secret_key=data["secretKey"],
            token=data["token"],
        )

    def _key_iv(self) -> tuple[bytes, bytes]:
        key, iv = self.secret_key.encode().split(_SEP, 1)
        return key, iv

    def _point(self, x: int) -> bytes:
        return json.dumps({"x": int(x), "y": 5}, separators=(",", ":")).encode()

    def point_json(self, x: int) -> str:
        key, iv = self._key_iv()
        return base64.b64encode(crypto.aes_cbc_encrypt(key, iv, self._point(x))).decode()

    def ticket(self, x: int) -> str:
        key, iv = self._key_iv()
        payload = f"{self.token}---".encode() + self._point(x)
        return base64.b64encode(crypto.aes_cbc_encrypt(key, iv, payload)).decode()
