"""Loads the material bundle used to sign and encrypt gateway traffic.

Values live in ``_material.pem`` alongside this module: two labelled PEM private
keys (``# @iov``, ``# @main``) and three ``# name: value`` constants
(``iov_hmac``, ``main_hmac``, ``captcha_key``). Public halves are derived at
import time. A missing bundle raises immediately with a clear message.

A non-default bundle (a separate national app's profile) may omit ``@iov`` and
``iov_hmac``: My AION (UK) ships its own main-API material but signs and encrypts
its IoV requests with GAC International's iov key/hmac, so a bundle missing iov
material inherits it from the default bundle.

A profile whose IoV responses come back under a different keypair can ship an
``@iov_response`` block; that keypair decrypts its IoV responses and push
messages, while ``iov``/``iov_hmac`` still cover the request it sends. When the
block is absent, responses decrypt with ``iov`` (the usual case).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
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
    # Keypair the gateway encrypts IoV responses (and push messages) to. Usually
    # the same as ``iov``; a profile whose IoV responses come back under a
    # different keypair ships its own ``@iov_response`` block, while still using
    # ``iov``/``iov_hmac`` for the request it sends.
    iov_response: Keypair = None  # type: ignore[assignment]  # set in load_material


def _load_private(pem_block: str) -> Keypair:
    key = serialization.load_pem_private_key(pem_block.encode(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("expected an RSA private key in the material bundle")
    return Keypair(private=key, public=key.public_key())


@cache
def load_material(bundle: str = _BUNDLE) -> Material:
    try:
        text = resources.files(__package__).joinpath(bundle).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            f"{bundle} is missing from the gac_connect package; the library cannot "
            "reach that backend without it."
        ) from exc

    blocks = {m.group("name").strip().lower(): m.group("pem") for m in _BLOCK.finditer(text)}
    meta = {k.lower(): v for k, v in _META.findall(text)}
    missing = [n for n in ("main",) if n not in blocks] + [
        n for n in ("main_hmac", "captcha_key") if n not in meta
    ]
    if missing:
        raise RuntimeError(f"{bundle} is incomplete; missing: {', '.join(missing)}")

    if "iov" in blocks and "iov_hmac" in meta:
        iov, iov_hmac = _load_private(blocks["iov"]), meta["iov_hmac"].encode()
    elif bundle == _BUNDLE:
        raise RuntimeError(f"{bundle} is incomplete; missing: iov, iov_hmac")
    else:
        base = load_material(_BUNDLE)
        iov, iov_hmac = base.iov, base.iov_hmac

    # Responses (and push messages) decrypt with ``iov`` unless the bundle ships a
    # separate response keypair.
    iov_response = _load_private(blocks["iov_response"]) if "iov_response" in blocks else iov

    return Material(
        iov=iov,
        main=_load_private(blocks["main"]),
        iov_hmac=iov_hmac,
        main_hmac=meta["main_hmac"].encode(),
        captcha_key=meta["captcha_key"].encode(),
        iov_response=iov_response,
    )
