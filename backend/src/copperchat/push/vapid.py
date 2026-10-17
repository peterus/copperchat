"""VAPID key pair, generated once into ``<data_dir>/vapid.json`` (mode 0600) and reused.

Kontor (server/pushConfig.ts) reads the keys from env variables; CopperChat generates and
persists them instead, so a fresh install needs no extra setup.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

VAPID_FILE = "vapid.json"


@dataclass(frozen=True)
class VapidKeys:
    public_key_b64url: str  # uncompressed P-256 point, base64url without padding
    private_pem: str  # PKCS#8 PEM


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_or_create_vapid(data_dir: Path) -> VapidKeys:
    path = data_dir / VAPID_FILE
    if path.exists():
        raw = json.loads(path.read_text())
        return VapidKeys(str(raw["publicKey"]), str(raw["privatePem"]))
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    keys = VapidKeys(_b64url(pub), pem)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{VAPID_FILE}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"publicKey": keys.public_key_b64url, "privatePem": keys.private_pem}, fh)
    os.chmod(tmp, 0o600)  # O_CREAT's mode is masked by the umask; an existing tmp keeps its mode
    os.replace(tmp, path)
    return keys
