"""Loads the material bundle used to sign and encrypt gateway traffic.

Values live in ``_material.pem`` alongside this module: two labelled PEM private
keys (``# @iov``, ``# @main``) and three ``# name: value`` constants
(``iov_hmac``, ``main_hmac``, ``captcha_key``). Public halves are derived at
import time. A missing bundle raises immediately with a clear message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

_BUNDLE = "_material.pem"

# "# @name" marker, then everything up to the next marker or a metadata "# x:" line.
_BLOCK = re.compile(
    r"#\s*@(?P<name>\w+)\s*\n(?P<pem>-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)",
    re.S,
)
_META = re.compile(r"^#\s*(\w+):\s*(.+?)\s*$", re.M)


@dataclass(frozen=True)
class Keypair:
    private: RSAPrivateKey
    public: RSAPublicKey

    @property
    def public_pem(self) -> bytes:
        return self.public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )


@dataclass(frozen=True)
class Material:
    iov: Keypair
    main: Keypair
    iov_hmac: bytes
    main_hmac: bytes
    captcha_key: bytes


def _load_private(pem_block: str) -> Keypair:
    key = serialization.load_pem_private_key(pem_block.encode(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("expected an RSA private key in the material bundle")
    return Keypair(private=key, public=key.public_key())


@lru_cache(maxsize=1)
def load_material() -> Material:
    try:
        text = resources.files(__package__).joinpath(_BUNDLE).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            f"{_BUNDLE} is missing from the gac_connect package; the library cannot "
            "reach the gateway without it."
        ) from exc

    blocks = {m.group("name").strip().lower(): m.group("pem") for m in _BLOCK.finditer(text)}
    meta = {k.lower(): v for k, v in _META.findall(text)}
    missing = [n for n in ("iov", "main") if n not in blocks] + [
        n for n in ("iov_hmac", "main_hmac", "captcha_key") if n not in meta
    ]
    if missing:
        raise RuntimeError(f"{_BUNDLE} is incomplete; missing: {', '.join(missing)}")

    return Material(
        iov=_load_private(blocks["iov"]),
        main=_load_private(blocks["main"]),
        iov_hmac=meta["iov_hmac"].encode(),
        main_hmac=meta["main_hmac"].encode(),
        captcha_key=meta["captcha_key"].encode(),
    )
