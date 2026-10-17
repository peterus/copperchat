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
        ("https://a.example/x", ["64:ff9b::7f00:1"], False),  # NAT64 → 127.0.0.1
        ("https://a.example/x", ["64:ff9b::a00:1"], False),  # NAT64 → 10.0.0.1
        ("https://a.example/x", ["64:ff9b::5db8:d822"], True),  # NAT64 → public
        ("https://a.example/x", ["::127.0.0.1"], False),  # IPv4-compatible
        ("https://a.example/x", ["2002:c0a8:0101::1"], False),  # 6to4 of 192.168.1.1
        ("http://[64:ff9b::a9fe:a9fe]/", [], False),  # literal NAT64 metadata IP
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


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1\\x.8.8.8.8.nip.io/a",  # WHATWG: host 127.0.0.1
        "http://ti.com\\@127.0.0.1/",
        "http://ti.com /x",
        "http://ti.com\t/x",
        "http://ti.com%2f@127.0.0.1/",
        "http://ti_com.example/",
    ],
)
async def test_parser_differential_urls_refused(url: str) -> None:
    assert await url_refusal(url, resolver=await fixed(["93.184.216.34"])) is not None


@pytest.mark.parametrize("address", ["fec0::1", "::ffff:0:a00:1", "::ffff:0:7f00:1"])
async def test_more_embedded_private_addresses(address: str) -> None:
    assert await url_refusal("https://a.example/", resolver=await fixed([address])) is not None
