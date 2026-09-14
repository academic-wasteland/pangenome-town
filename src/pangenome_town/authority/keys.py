"""Ed25519 keys and detached-in-document signatures over canonical JSON.

Canonical form: the document without its `proof`, serialized with sorted keys,
compact separators, and UTF-8. This is not W3C Data Integrity; it is a small,
explicit scheme that is easy to reproduce.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import PROOF_TYPE

PREFIX = "ed25519:"


class AuthorityKeyError(ValueError):
    pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise AuthorityKeyError("expected base64url text")
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        raise AuthorityKeyError(f"invalid base64url: {error}") from error


def generate() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _seed(key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def save_private(key: Ed25519PrivateKey, path: Path, *, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise AuthorityKeyError(f"refusing to overwrite existing key {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, (_b64encode(_seed(key)) + "\n").encode("ascii"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def load_private(path: Path) -> Ed25519PrivateKey:
    try:
        seed = _b64decode(Path(path).read_text(encoding="ascii").strip())
    except OSError as error:
        raise AuthorityKeyError(f"cannot read key {path}: {error}") from error
    if len(seed) != 32:
        raise AuthorityKeyError(f"key {path} is not a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return PREFIX + _b64encode(public.public_bytes(Encoding.Raw, PublicFormat.Raw))


def parse_public(text: Any) -> Ed25519PublicKey:
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise AuthorityKeyError("public key must look like ed25519:<base64url>")
    raw = _b64decode(text[len(PREFIX):])
    if len(raw) != 32:
        raise AuthorityKeyError("Ed25519 public key must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def canonical_bytes(document: dict[str, Any]) -> bytes:
    body = {key: value for key, value in document.items() if key != "proof"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(document: dict[str, Any], private_key: Ed25519PrivateKey, verification_method: str, *, created: str | None = None) -> dict[str, Any]:
    unsigned = {key: value for key, value in document.items() if key != "proof"}
    signature = private_key.sign(canonical_bytes(unsigned))
    return {
        **unsigned,
        "proof": {
            "type": PROOF_TYPE,
            "verificationMethod": verification_method,
            "created": created or now_iso(),
            "proofValue": _b64encode(signature),
        },
    }


def verify(document: Any, public_key: Ed25519PublicKey) -> bool:
    try:
        if not isinstance(document, dict):
            return False
        proof = document.get("proof")
        if not isinstance(proof, dict) or proof.get("type") != PROOF_TYPE:
            return False
        signature = _b64decode(proof.get("proofValue"))
        public_key.verify(signature, canonical_bytes(document))
        return True
    except (InvalidSignature, AuthorityKeyError, TypeError, ValueError):
        return False
