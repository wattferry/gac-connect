"""Request/response envelope encryption for the GAC gateway.

Every request body is wrapped as ``{"encryptKey", "encryptData"}``:

* a random 32-byte AES key + 16-byte IV encrypt the JSON body with AES-256-CBC
  (PKCS7) into ``encryptData``;
* ``key + b"@DS@" + iv`` is RSA-PKCS1v1.5 wrapped to the endpoint's public key
  into ``encryptKey``.

The gateway encrypts its responses the same way, to a keypair whose private half
this library holds, so responses are unwrapped by reversing the process.

Sensitive header fields (the VIN) are AES-256-CBC encrypted with the *same*
per-request session key/iv as the body, and named in ``fnc-sensitive-fields``.
"""
from __future__ import annotations

import base64
import json
import secrets
import string
from typing import Any

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_SEP = b"@DS@"
_ALPHABET = string.ascii_letters + string.digits


def _compact(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _random_key_iv() -> tuple[bytes, bytes]:
    # The client draws its session key/iv from an alphanumeric alphabet (not raw
    # bytes); keep that so the wire format matches exactly.
    key = "".join(secrets.choice(_ALPHABET) for _ in range(32)).encode()
    iv = "".join(secrets.choice(_ALPHABET) for _ in range(16)).encode()
    return key, iv


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def aes_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 - protocol-mandated
    return enc.update(padded) + enc.finalize()


def encrypt_envelope(
    body: Any,
    public_key: RSAPublicKey,
    *,
    key: bytes | None = None,
    iv: bytes | None = None,
) -> tuple[dict[str, str], bytes, bytes]:
    """Return ``(wrapper, key, iv)``. The key/iv are returned so the same pair can
    encrypt sensitive header fields for this request."""
    if key is None or iv is None:
        key, iv = _random_key_iv()
    encrypted_data = aes_cbc_encrypt(key, iv, _compact(body))
    encrypted_key = public_key.encrypt(key + _SEP + iv, padding.PKCS1v15())
    wrapper = {
        "encryptKey": base64.b64encode(encrypted_key).decode(),
        "encryptData": base64.b64encode(encrypted_data).decode(),
    }
    return wrapper, key, iv


def decrypt_envelope(wrapper: Any, private_key: RSAPrivateKey) -> Any | None:
    """Reverse :func:`encrypt_envelope` on a gateway response. Returns the decoded
    JSON, or ``None`` if the payload is not a decryptable envelope."""
    if not isinstance(wrapper, dict):
        return None
    enc_key, enc_data = wrapper.get("encryptKey"), wrapper.get("encryptData")
    if not isinstance(enc_key, str) or not isinstance(enc_data, str):
        return None
    try:
        unwrapped = private_key.decrypt(base64.b64decode(enc_key), padding.PKCS1v15())
    except ValueError:
        return None
    if _SEP not in unwrapped:
        return None
    key, iv = unwrapped.split(_SEP, 1)
    try:
        plaintext = aes_cbc_decrypt(key, iv, base64.b64decode(enc_data))
        return json.loads(plaintext)
    except (ValueError, json.JSONDecodeError):
        return None


def encrypt_sensitive(value: str, key: bytes, iv: bytes) -> str:
    """Encrypt a sensitive header value (e.g. the VIN) with the request's own
    session key/iv."""
    return base64.b64encode(aes_cbc_encrypt(key, iv, value.encode())).decode()
