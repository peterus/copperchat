import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from copperchat.errors import StructuredToolError
from copperchat.mouser import client as mc
from copperchat.mouser.client import MouserClient, parse_price, redact

FIXTURES = Path(__file__).parents[2] / "fixtures/mouser"
KEY = "sekr3t-key-0000-1111"


def fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text()))


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Recorder:
    """MockTransport handler: records requests, answers with queued responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def body(self, i: int = -1) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.requests[i].content))


def make(rec: Recorder, clock: FakeClock | None = None, **kw: Any) -> MouserClient:
    return MouserClient(
        KEY,
        transport=httpx.MockTransport(rec),
        clock=clock or FakeClock(),
        today=lambda: date(2026, 11, 4),
        **kw,
    )


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=fixture(name))


# ---- parsing


@pytest.mark.parametrize(
    ("raw", "currency", "value"),
    [
        ("2,79 €", "EUR", 2.79),
        ("€2.79", "EUR", 2.79),
        ("$1,234.50", "USD", 1234.5),
        ("1.012,50 €", "EUR", 1012.5),
        ("0,123 €", "EUR", 0.123),
        ("12 €", "EUR", 12.0),
        ("$1,234", "USD", 1234.0),
        ("1.234 €", "EUR", 1234.0),
        ("0.123 $", "USD", 0.123),
        ("¥1,234", "JPY", 1234.0),
        ("1,234", None, None),  # ambiguous without a currency
        ("0,123", None, 0.123),
        ("$0.123", "MXN", 0.123),
        ("1234,567", None, 1234.567),
        ("1 234,56 €", "EUR", 1234.56),
        ("1\u00a0234,56 €", "EUR", 1234.56),
        ("CHF 1'234.50", "CHF", 1234.5),
        ("n/a", "EUR", None),
        ("", "EUR", None),
        (None, "EUR", None),
    ],
)
def test_parse_price(raw: str | None, currency: str | None, value: float | None) -> None:
    assert parse_price(raw, currency) == value


async def test_part_parsing_and_request_shape() -> None:
    rec = Recorder(ok("partnumber.json"))
    out = await make(rec).parts(["TPS62133RGTR", "lm358dr", "OLD555"])
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/search/partnumber"
    assert req.url.params["apiKey"] == KEY
    assert rec.body() == {
        "SearchByPartRequest": {
            "mouserPartNumber": "TPS62133RGTR|lm358dr|OLD555",
            "partSearchOptions": "Exact",
        }
    }
    assert out["source"] == "Mouser"
    assert out["as_of"] == "2026-11-04"
    tps, lm, old = out["parts"]
    assert tps["mpn"] == "TPS62133RGTR" and tps["found"] is True
    offer = tps["offers"][0]
    assert offer == {
        "mpn": "TPS62133RGTR",
        "manufacturer": "Texas Instruments",
        "mouser_part_number": "595-TPS62133RGTR",
        "description": "Switching Voltage Regulators 3-17V 3A Step-Down Converter in 3x3 QFN",
        "lifecycle": "Active",
        "suggested_replacement": None,
        "stock": 4521,
        "availability": "4521 In Stock",
        "on_order": [{"quantity": 3000, "date": "2026-12-01"}],
        "lead_time": "42 Days",
        "min_order_qty": 1,
        "order_multiple": 1,
        "price_breaks": [
            {"quantity": 1, "price": "2,79 €", "value": 2.79, "currency": "EUR"},
            {"quantity": 10, "price": "2,01 €", "value": 2.01, "currency": "EUR"},
            {"quantity": 3000, "price": "1.012,50 €", "value": 1012.5, "currency": "EUR"},
        ],
        "datasheet_url": "https://www.ti.com/lit/ds/symlink/tps62133.pdf",
        "product_url": "https://www.mouser.at/ProductDetail/Texas-Instruments/TPS62133RGTR",
        "rohs": "RoHS Compliant",
    }
    # requested spelling is kept, matching is case-insensitive
    assert lm["mpn"] == "lm358dr" and lm["found"] is True
    lmo = lm["offers"][0]
    assert lmo["lifecycle"] == "Not Recommended for New Designs"
    assert lmo["suggested_replacement"] == "LM358BIDR"
    assert lmo["stock"] is None  # unknown, not zero
    assert lmo["min_order_qty"] == 2500 and lmo["order_multiple"] == 2500
    assert lmo["lead_time"] is None and lmo["availability"] is None
    oldo = old["offers"][0]
    assert oldo["lifecycle"] == "Discontinued"
    assert oldo["datasheet_url"] is None
    assert oldo["price_breaks"] == [
        {"quantity": 1, "price": "n/a", "value": None, "currency": "EUR"}
    ]


async def test_part_not_found_per_mpn() -> None:
    rec = Recorder(ok("partnumber.json"))
    out = await make(rec).parts(["TPS62133RGTR", "NOPE123"])
    assert out["parts"][1] == {"mpn": "NOPE123", "found": False, "note": "not found at Mouser"}


async def test_part_matches_mouser_part_number() -> None:
    rec = Recorder(ok("partnumber.json"))
    out = await make(rec).parts(["595-TPS62133RGTR"])
    assert out["parts"][0]["found"] is True
    assert out["parts"][0]["offers"][0]["mpn"] == "TPS62133RGTR"


async def test_part_offers_capped() -> None:
    data = fixture("partnumber.json")
    first = data["SearchResults"]["Parts"][0]
    data["SearchResults"]["Parts"] = [{**first, "MouserPartNumber": f"595-X{i}"} for i in range(9)]
    rec = Recorder(httpx.Response(200, json=data))
    out = await make(rec).parts(["TPS62133RGTR"])
    assert len(out["parts"][0]["offers"]) == mc.MAX_OFFERS


async def test_description_capped() -> None:
    data = fixture("partnumber.json")
    data["SearchResults"]["Parts"][0]["Description"] = "x" * 1000
    rec = Recorder(httpx.Response(200, json=data))
    out = await make(rec).parts(["TPS62133RGTR"])
    assert len(out["parts"][0]["offers"][0]["description"]) <= mc.MAX_TEXT


async def test_part_duplicates_requested_once() -> None:
    rec = Recorder(ok("partnumber.json"))
    out = await make(rec).parts(["TPS62133RGTR", "tps62133rgtr"])
    assert rec.body()["SearchByPartRequest"]["mouserPartNumber"] == "TPS62133RGTR"
    assert [p["mpn"] for p in out["parts"]] == ["TPS62133RGTR"]


@pytest.mark.parametrize(
    "mpns",
    [[], ["AB"], ["A" * 41], ["TPS|LM358"], [f"PART{i}" for i in range(11)], [""], [5]],
)
async def test_part_invalid_input(mpns: list[Any]) -> None:
    rec = Recorder(ok("partnumber.json"))
    with pytest.raises(StructuredToolError) as ei:
        await make(rec).parts(mpns)
    assert ei.value.code == "invalid_input"
    assert rec.requests == []


async def test_keyword_parsing_and_request_shape() -> None:
    rec = Recorder(ok("keyword.json"))
    out = await make(rec).search("buck 3A", 5)
    req = rec.requests[0]
    assert req.url.path == "/api/v1/search/keyword"
    assert req.url.params["apiKey"] == KEY
    assert rec.body() == {
        "SearchByKeywordRequest": {"keyword": "buck 3A", "records": 5, "startingRecord": 0}
    }
    assert out["keyword"] == "buck 3A" and out["total"] == 812 and out["as_of"] == "2026-11-04"
    assert out["hits"] == [
        {
            "mpn": "TPS62133RGTR",
            "manufacturer": "Texas Instruments",
            "mouser_part_number": "595-TPS62133RGTR",
            "description": "Switching Voltage Regulators 3-17V 3A Step-Down Converter in 3x3 QFN",
            "stock": 4521,
            "price": "2,79 €",
            "price_qty": 1,
            "currency": "EUR",
            "lifecycle": "Active",
        },
        {
            "mpn": "LT1234",
            "manufacturer": "Analog Devices Inc.",
            "mouser_part_number": "584-LT1234",
            "description": "Buck converter, end of life",
            "stock": 0,
            "price": None,
            "price_qty": None,
            "currency": None,
            "lifecycle": "End of Life",
        },
    ]


async def test_keyword_hits_capped_to_limit() -> None:
    rec = Recorder(ok("keyword.json"))
    out = await make(rec).search("buck", 1)
    assert len(out["hits"]) == 1


@pytest.mark.parametrize(
    ("kw", "limit"), [("", 5), ("  ", 5), ("x" * 201, 5), ("ok", 0), ("ok", 51)]
)
async def test_keyword_invalid_input(kw: str, limit: int) -> None:
    rec = Recorder(ok("keyword.json"))
    with pytest.raises(StructuredToolError) as ei:
        await make(rec).search(kw, limit)
    assert ei.value.code == "invalid_input"
    assert rec.requests == []


# ---- cache


async def test_part_cache_hit_and_partial_request() -> None:
    rec = Recorder(ok("partnumber.json"))
    c = make(rec)
    await c.parts(["TPS62133RGTR", "NOPE123"])
    out = await c.parts(["NOPE123", "tps62133rgtr"])
    assert len(rec.requests) == 1  # both cached, incl. the not-found
    assert out["parts"][0]["found"] is False
    assert out["parts"][1]["found"] is True and out["parts"][1]["mpn"] == "tps62133rgtr"
    await c.parts(["TPS62133RGTR", "LM358DR"])
    assert len(rec.requests) == 2
    assert rec.body()["SearchByPartRequest"]["mouserPartNumber"] == "LM358DR"


async def test_cache_expires() -> None:
    clock = FakeClock()
    rec = Recorder(ok("keyword.json"))
    c = make(rec, clock)
    await c.search("buck", 5)
    await c.search("BUCK", 5)
    assert len(rec.requests) == 1
    clock.t += mc.CACHE_TTL_S + 1
    await c.search("buck", 5)
    assert len(rec.requests) == 2


async def test_cache_bounded() -> None:
    rec = Recorder(ok("keyword.json"))
    c = make(rec, max_cache_entries=3, per_minute=100)
    for i in range(5):
        await c.search(f"kw{i}", 5)
    assert len(c._cache) <= 3
    await c.search("kw4", 5)  # newest still cached
    assert len(rec.requests) == 5


async def test_errors_not_cached() -> None:
    rec = Recorder(httpx.Response(500), ok("keyword.json"))
    c = make(rec)
    with pytest.raises(StructuredToolError):
        await c.search("buck", 5)
    await c.search("buck", 5)
    assert len(rec.requests) == 2


# ---- rate limit


async def test_rate_limit_per_minute() -> None:
    clock = FakeClock()
    rec = Recorder(ok("keyword.json"))
    c = make(rec, clock, per_minute=2, per_day=100)
    await c.search("a", 5)
    clock.t += 10
    await c.search("b", 5)
    with pytest.raises(StructuredToolError) as ei:
        await c.search("c", 5)
    assert ei.value.code == "rate_limited"
    assert ei.value.details == {"retry_after_s": 50}
    assert len(rec.requests) == 2
    await c.search("a", 5)  # cache hits do not count
    clock.t += 51
    await c.search("c", 5)
    assert len(rec.requests) == 3


async def test_rate_limit_per_day() -> None:
    clock = FakeClock()
    rec = Recorder(ok("keyword.json"))
    c = make(rec, clock, per_minute=100, per_day=2)
    await c.search("a", 5)
    clock.t += 3600
    await c.search("b", 5)
    with pytest.raises(StructuredToolError) as ei:
        await c.search("c", 5)
    assert ei.value.code == "rate_limited"
    assert ei.value.details == {"retry_after_s": 86400 - 3600}


async def test_http_429_is_rate_limited() -> None:
    rec = Recorder(httpx.Response(429, text=f"slow down apiKey={KEY}"))
    with pytest.raises(StructuredToolError) as ei:
        await make(rec).search("a", 5)
    assert ei.value.code == "rate_limited"
    assert KEY not in json.dumps(ei.value.to_dict())


# ---- upstream errors and redaction


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=f"boom {KEY}"),
        httpx.Response(403, text="forbidden"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"Errors": [], "SearchResults": {"Parts": "bad"}}),
        httpx.ReadTimeout(f"timed out https://api.mouser.com/api/v1/search/keyword?apiKey={KEY}"),
        httpx.ConnectError(f"connect failed apiKey={KEY}&x=1"),
    ],
)
async def test_upstream_errors_redacted(response: httpx.Response | Exception) -> None:
    rec = Recorder(response)
    with pytest.raises(StructuredToolError) as ei:
        await make(rec).search("a", 5)
    assert ei.value.code == "upstream_error"
    dumped = json.dumps(ei.value.to_dict())
    assert KEY not in dumped and KEY not in str(ei.value)
    assert "web" in ei.value.suggestion.lower() or "research_part" in ei.value.suggestion


async def test_mouser_errors_entity() -> None:
    data = fixture("invalid_key.json")
    data["Errors"][0]["Message"] = f"Invalid unique identifier {KEY}."
    rec = Recorder(httpx.Response(200, json=data))
    with pytest.raises(StructuredToolError) as ei:
        await make(rec).parts(["TPS62133RGTR"])
    assert ei.value.code == "upstream_error"
    assert "Invalid unique identifier" in ei.value.why
    assert KEY not in json.dumps(ei.value.to_dict())


async def test_stock_falls_back_to_availability_text() -> None:
    data = fixture("partnumber.json")
    part = data["SearchResults"]["Parts"][0]
    part["AvailabilityInStock"] = None
    part["Availability"] = "1,250 In Stock"
    rec = Recorder(httpx.Response(200, json=data))
    out = await make(rec).parts(["TPS62133RGTR"])
    assert out["parts"][0]["offers"][0]["stock"] == 1250


async def test_errors_with_empty_results_object_not_cached() -> None:
    data = fixture("invalid_key.json")
    data["SearchResults"] = {"NumberOfResult": 0, "Parts": []}
    rec = Recorder(httpx.Response(200, json=data), ok("partnumber.json"))
    c = make(rec)
    with pytest.raises(StructuredToolError) as ei:
        await c.parts(["TPS62133RGTR"])
    assert ei.value.code == "upstream_error"
    out = await c.parts(["TPS62133RGTR"])  # not negatively cached
    assert out["parts"][0]["found"] is True
    assert len(rec.requests) == 2


async def test_truncated_batch_does_not_cache_not_found() -> None:
    data = fixture("partnumber.json")
    data["SearchResults"]["NumberOfResult"] = 50  # more than the 3 parts listed
    rec = Recorder(httpx.Response(200, json=data))
    c = make(rec)
    out = await c.parts(["TPS62133RGTR", "MISSING1"])
    assert out["parts"][1]["found"] is False
    await c.parts(["TPS62133RGTR", "MISSING1"])
    assert len(rec.requests) == 2  # nothing from the truncated answer was cached
    assert rec.body()["SearchByPartRequest"]["mouserPartNumber"] == "TPS62133RGTR|MISSING1"


async def test_price_breaks_capped_and_first_break_quantity() -> None:
    data = fixture("partnumber.json")
    lm = data["SearchResults"]["Parts"][1]
    lm["PriceBreaks"] = [
        {"Quantity": 2500 * (i + 1), "Price": "0,10 €", "Currency": "EUR"} for i in range(30)
    ]
    rec = Recorder(httpx.Response(200, json=data))
    out = await make(rec).parts(["LM358DR"])
    assert len(out["parts"][0]["offers"][0]["price_breaks"]) == mc.MAX_BREAKS
    hit = mc._hit(lm)
    assert hit["price_qty"] == 2500 and hit["price"] == "0,10 €"


async def test_as_of_is_the_fetch_date() -> None:
    day = [date(2026, 11, 4)]
    rec = Recorder(ok("partnumber.json"))
    c = MouserClient(
        KEY, transport=httpx.MockTransport(rec), clock=FakeClock(), today=lambda: day[0]
    )
    await c.parts(["TPS62133RGTR"])
    day[0] = date(2026, 11, 5)
    out = await c.parts(["TPS62133RGTR", "LM358DR"])
    assert out["as_of"] == "2026-11-04"  # oldest data in the answer
    rec2 = Recorder(ok("keyword.json"))
    c2 = MouserClient(
        KEY, transport=httpx.MockTransport(rec2), clock=FakeClock(), today=lambda: day[0]
    )
    await c2.search("buck", 5)
    day[0] = date(2026, 11, 6)
    assert (await c2.search("buck", 5))["as_of"] == "2026-11-05"


@pytest.mark.parametrize("bad", ["TPS\ud800X", "TPS\x00123", "TPS\n123"])
async def test_unencodable_input_rejected_before_rate_limit(bad: str) -> None:
    rec = Recorder(ok("partnumber.json"))
    c = make(rec, per_minute=1)
    for call in (c.parts([bad]), c.search(bad, 5)):
        with pytest.raises(StructuredToolError) as ei:
            await call
        assert ei.value.code == "invalid_input"
    await c.parts(["TPS62133RGTR"])  # the one slot is still free
    assert len(rec.requests) == 1


async def test_rate_limit_under_concurrency() -> None:
    import asyncio

    rec = Recorder(ok("keyword.json"))
    c = make(rec, per_minute=2, per_day=100)
    results = await asyncio.gather(
        *(c.search(f"kw{i}", 5) for i in range(3)), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, StructuredToolError)]
    assert len(rec.requests) == 2
    assert len(errors) == 1 and errors[0].code == "rate_limited"


async def test_failed_requests_count_toward_the_limit() -> None:
    rec = Recorder(httpx.Response(500), ok("keyword.json"))
    c = make(rec, per_minute=1, per_day=100)
    with pytest.raises(StructuredToolError):
        await c.search("a", 5)
    with pytest.raises(StructuredToolError) as ei:
        await c.search("a", 5)
    assert ei.value.code == "rate_limited"


def test_redact() -> None:
    assert redact(f"x {KEY} y", KEY) == "x *** y"
    assert redact("https://a/b?apiKey=abc&records=1", KEY) == "https://a/b?apiKey=***&records=1"
    assert redact("apikey=abc", None) == "apikey=***"


async def test_httpx_log_records_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rec = Recorder(ok("keyword.json"))
    await make(rec).search("a", 5)
    # the transport is mocked, so emit what httpx would log for a real request
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "POST",
        rec.requests[0].url,
        "HTTP/1.1",
        200,
        "OK",
    )
    assert any("apiKey=***" in r.getMessage() for r in caplog.records)
    assert all(KEY not in r.getMessage() for r in caplog.records)
    assert all(KEY not in caplog.text for _ in [0])


def test_repr_hides_key() -> None:
    c = MouserClient(KEY)
    assert KEY not in repr(c)
