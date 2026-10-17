import pytest

from copperchat.research.net import url_refusal


async def fixed(addresses: list[str]):  # type: ignore[no-untyped-def]
    async def resolver(host: str, port: int) -> list[str]:
        return addresses

    return resolver


@pytest.mark.parametrize(
    ("url", "addresses", "ok"),
    [
        ("https://www.ti.com/x.pdf", ["93.184.216.34"], True),
        ("http://www.ti.com/x.pdf", ["2606:2800:220:1:248:1893:25c8:1946"], True),
        ("https://a.example/x", ["93.184.216.34", "10.0.0.1"], False),  # any private → refuse
        ("https://a.example/x", ["127.0.0.1"], False),
        ("https://a.example/x", ["::1"], False),
        ("https://a.example/x", ["::ffff:192.168.1.1"], False),
        ("https://a.example/x", ["169.254.169.254"], False),
        ("https://a.example/x", ["100.64.0.1"], False),  # CGNAT
        ("https://a.example/x", ["0.0.0.0"], False),
        ("https://a.example/x", ["fe80::1%eth0"], False),
        ("https://a.example/x", ["224.0.0.1"], False),
        ("https://a.example/x", [], False),
        ("ftp://a.example/x", ["93.184.216.34"], False),
        ("file:///etc/passwd", ["93.184.216.34"], False),
        ("https:///nohost", ["93.184.216.34"], False),
        ("https://u:p@a.example/", ["93.184.216.34"], False),
        ("http://[::1", ["93.184.216.34"], False),
        ("", ["93.184.216.34"], False),
    ],
)
async def test_url_refusal(url: str, addresses: list[str], ok: bool) -> None:
    reason = await url_refusal(url, resolver=await fixed(addresses))
    assert (reason is None) is ok, reason


async def test_unresolvable_host_refused() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    assert "does not resolve" in (
        await url_refusal("https://nope.invalid/", resolver=failing) or ""
    )
