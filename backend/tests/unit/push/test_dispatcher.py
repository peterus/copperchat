import json
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.push import dispatcher as dispatcher_mod
from copperchat.push.dispatcher import PushDispatcher, PushSendError, webpush_sender
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid


async def test_send_all_deletes_gone_subscriptions(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "c.db")
    try:
        store = PushStore(db)
        for name in ("ok", "gone", "notfound", "flaky", "boom"):
            await store.upsert(f"https://push.example/{name}", "p", "a")
        sent: list[str] = []

        def sender(sub: dict[str, Any], data: str) -> None:
            name = sub["endpoint"].rsplit("/", 1)[1]
            if name == "gone":
                raise PushSendError(410)
            if name == "notfound":
                raise PushSendError(404)
            if name == "flaky":
                raise PushSendError(500)
            if name == "boom":
                raise RuntimeError("unexpected")
            sent.append(data)

        async def public(url: str) -> str | None:
            return None

        d = PushDispatcher(
            store,
            load_or_create_vapid(tmp_path),
            "mailto:x@y",
            sender=sender,
            check_endpoint=public,
        )
        assert await d.send_all({"title": "t", "body": "b", "url": "/p/abc"}) == 1
        assert json.loads(sent[0])["url"] == "/p/abc"
        remaining = [s.endpoint.rsplit("/", 1)[1] for s in await store.all()]
        assert remaining == ["ok", "flaky", "boom"]
    finally:
        await db.close()


def test_webpush_sender_uses_fresh_claims_per_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_webpush(**kw: Any) -> None:
        claims = kw["vapid_claims"]
        seen.append(dict(claims))
        claims["aud"] = "https://" + kw["subscription_info"]["endpoint"].split("/")[2]

    monkeypatch.setattr(dispatcher_mod, "webpush", fake_webpush)
    send = webpush_sender(load_or_create_vapid(tmp_path), "mailto:x@y")
    send({"endpoint": "https://a.example/1", "keys": {}}, "{}")
    send({"endpoint": "https://b.example/1", "keys": {}}, "{}")
    assert seen == [{"sub": "mailto:x@y"}, {"sub": "mailto:x@y"}]


def test_webpush_sender_maps_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        status_code = 410

    def gone(**kw: Any) -> None:
        raise dispatcher_mod.WebPushException("gone", response=Resp())

    monkeypatch.setattr(dispatcher_mod, "webpush", gone)
    send = webpush_sender(load_or_create_vapid(tmp_path), "mailto:x@y")
    with pytest.raises(PushSendError) as info:
        send({"endpoint": "https://a.example/1", "keys": {}}, "{}")
    assert info.value.status == 410

    def offline(**kw: Any) -> None:
        raise ConnectionError("down")

    monkeypatch.setattr(dispatcher_mod, "webpush", offline)
    with pytest.raises(PushSendError) as info2:
        send({"endpoint": "https://a.example/1", "keys": {}}, "{}")
    assert info2.value.status is None


async def test_send_all_rechecks_endpoints_before_sending(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "c.db")
    try:
        store = PushStore(db)
        await store.upsert("https://rebound.example/x", "p", "a")
        sent: list[str] = []

        async def now_private(url: str) -> str | None:
            return "host resolves to a non-public address (127.0.0.1)"

        d = PushDispatcher(
            store,
            load_or_create_vapid(tmp_path),
            "mailto:x@y",
            sender=lambda sub, data: sent.append(data),
            check_endpoint=now_private,
        )
        assert await d.send_all({"title": "t"}) == 0
        assert sent == []
        assert len(await store.all()) == 1  # skipped, not deleted
    finally:
        await db.close()


def test_webpush_sender_never_follows_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions: list[Any] = []
    monkeypatch.setattr(
        dispatcher_mod, "webpush", lambda **kw: sessions.append(kw["requests_session"])
    )
    send = webpush_sender(load_or_create_vapid(tmp_path), "mailto:x@y")
    send({"endpoint": "https://a.example/1", "keys": {}}, "{}")
    assert sessions[0].max_redirects == 0 and sessions[0].trust_env is False
