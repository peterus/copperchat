import pytest

from copperchat.push import endpoints
from copperchat.push.endpoints import push_endpoint_refusal


@pytest.mark.parametrize(
    "url",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/QOabc",
        "https://wns2-db5p.notify.windows.com/w/?token=abc",
    ],
)
async def test_browser_push_services_allowed(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(u: str) -> str | None:
        return None

    monkeypatch.setattr(endpoints, "url_refusal", public)
    assert await push_endpoint_refusal(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://evil.example/fcm.googleapis.com",
        "https://fcm.googleapis.com.evil.example/x",
        "https://notfcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "not a url",
    ],
)
async def test_everything_else_refused(url: str) -> None:
    assert await push_endpoint_refusal(url) is not None


async def test_private_resolution_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def private(u: str) -> str | None:
        return "resolves to 10.0.0.1"

    monkeypatch.setattr(endpoints, "url_refusal", private)
    assert await push_endpoint_refusal("https://fcm.googleapis.com/x") == "resolves to 10.0.0.1"
