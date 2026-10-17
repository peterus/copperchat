import base64
import json
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from copperchat.db import Database
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_vapid_generated_once_and_reused(tmp_path: Path) -> None:
    k1 = load_or_create_vapid(tmp_path)
    raw = _b64d(k1.public_key_b64url)
    assert len(raw) == 65 and raw[0] == 0x04  # uncompressed P-256 point
    path = tmp_path / "vapid.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["publicKey"] == k1.public_key_b64url
    assert load_or_create_vapid(tmp_path) == k1
    # the public key belongs to the stored private key
    key = serialization.load_pem_private_key(k1.private_pem.encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert pub == raw


async def test_store_upsert_delete(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "c.db")
    try:
        store = PushStore(db)
        await store.upsert("https://push.example/a", "p1", "a1")
        await store.upsert("https://push.example/a", "p2", "a2")  # same endpoint → update
        await store.upsert("https://push.example/b", "p3", "a3")
        subs = await store.all()
        assert [(s.endpoint, s.p256dh, s.auth) for s in subs] == [
            ("https://push.example/a", "p2", "a2"),
            ("https://push.example/b", "p3", "a3"),
        ]
        assert await store.delete("https://push.example/a") is True
        assert await store.delete("https://push.example/a") is False
        await store.delete_id(subs[1].id)
        assert await store.all() == []
    finally:
        await db.close()
