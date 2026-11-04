"""Read-only client of the Mouser Search API v1 (mouser-api spec sections 3-6).

One :class:`MouserClient` per API key and process (:func:`client_for`), shared by the main
agent's tools and every part-researcher, so the cache and the rate limiter are global.
The key is sent only to api.mouser.com as the ``apiKey`` query parameter and never
appears in a result, an error or a log record (:func:`redact`, :class:`_RedactFilter`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from copperchat.config import Settings
from copperchat.errors import StructuredToolError

BASE_URL = "https://api.mouser.com"
KEYWORD_PATH = "/api/v1/search/keyword"
PART_PATH = "/api/v1/search/partnumber"
CACHE_TTL_S = 3600.0
MAX_CACHE_ENTRIES = 2000
# Mouser's published limits: 30 calls per minute, 1000 per day.
PER_MINUTE = 30
PER_DAY = 1000
MAX_MPNS = 10
MAX_KEYWORD_RESULTS = 50
MAX_KEYWORD_CHARS = 200
MAX_OFFERS = 5
MAX_TEXT = 200
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_MINUTE = 60.0
_DAY = 86400.0

_KEY_PARAM = re.compile(r"(apikey=)[^&\s\"'<>]+", re.IGNORECASE)
_FALLBACK = "Look it up on the web instead (main agent: research_part; part-researcher: WebSearch)"

log = logging.getLogger(__name__)


def redact(text: str, key: str | None) -> str:
    """``text`` without the key and without the value of any ``apiKey=`` parameter."""
    if key:
        text = text.replace(key, "***")
    return _KEY_PARAM.sub(r"\1***", text)


class _RedactFilter(logging.Filter):
    """Redacts ``apiKey=`` in records of httpx/httpcore (httpx logs every request URL)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message, None)
        if clean != message:
            record.msg, record.args = clean, None
        return True


_FILTER = _RedactFilter()


def _install_log_filter() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


def _error(what: str, why: str, code: str, **details: Any) -> StructuredToolError:
    suggestion = (
        "Wait and retry later, or look it up on the web instead (main agent: research_part; "
        "part-researcher: WebSearch)"
        if code == "rate_limited"
        else _FALLBACK
    )
    return StructuredToolError(what, why, suggestion, code=code, details=details or None)


def _invalid(why: str) -> StructuredToolError:
    return StructuredToolError(
        "Invalid Mouser query", why, "Fix the arguments and call again", code="invalid_input"
    )


def not_configured() -> StructuredToolError:
    return StructuredToolError(
        "Mouser is not configured",
        "no MOUSER_API_KEY is set for this CopperChat instance",
        "Use research_part (the part-researcher searches the web); the part-researcher "
        "itself uses WebSearch",
        code="not_configured",
    )


# ---- parsing

_NUM = re.compile(r"[0-9][0-9.,]*")


def parse_price(raw: str | None) -> float | None:
    """Best-effort numeric value of a Mouser price string (``2,79 €``, ``$1,234.50``)."""
    if not raw:
        return None
    m = _NUM.search(raw)
    if m is None:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        # One comma is a decimal comma (EUR: "0,123 €"); several are thousands groups.
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _str(v: Any, cap: int = MAX_TEXT) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:cap] if s else None


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(str(v).strip().replace(",", "").replace(".", ""))
    except ValueError:
        return None


def _lifecycle(part: dict[str, Any]) -> str:
    raw = _str(part.get("LifecycleStatus"))
    if raw:
        return raw
    if str(part.get("IsDiscontinued") or "").strip().lower() == "true":
        return "Discontinued"
    return "Active"


def _breaks(part: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = part.get("PriceBreaks")
    for b in raw if isinstance(raw, list) else []:
        if not isinstance(b, dict):
            continue
        price = _str(b.get("Price"), 40)
        out.append(
            {
                "quantity": _int(b.get("Quantity")),
                "price": price,
                "value": parse_price(price),
                "currency": _str(b.get("Currency"), 8),
            }
        )
    return out


def _on_order(part: dict[str, Any]) -> list[dict[str, Any]]:
    raw = part.get("AvailabilityOnOrder")
    out: list[dict[str, Any]] = []
    for o in raw if isinstance(raw, list) else []:
        if isinstance(o, dict):
            d = _str(o.get("Date"), 40)
            out.append({"quantity": _int(o.get("Quantity")), "date": d[:10] if d else None})
    return out[:5]


def _stock(part: dict[str, Any]) -> int:
    return _int(part.get("AvailabilityInStock")) or 0


def _offer(part: dict[str, Any]) -> dict[str, Any]:
    return {
        "mpn": _str(part.get("ManufacturerPartNumber"), 64),
        "manufacturer": _str(part.get("Manufacturer"), 80),
        "mouser_part_number": _str(part.get("MouserPartNumber"), 64),
        "description": _str(part.get("Description")),
        "lifecycle": _lifecycle(part),
        "suggested_replacement": _str(part.get("SuggestedReplacement"), 64),
        "stock": _stock(part),
        "availability": _str(part.get("Availability"), 80),
        "on_order": _on_order(part),
        "lead_time": _str(part.get("LeadTime"), 40),
        "min_order_qty": _int(part.get("Min")),
        "order_multiple": _int(part.get("Mult")),
        "price_breaks": _breaks(part),
        "datasheet_url": _str(part.get("DataSheetUrl"), 500),
        "product_url": _str(part.get("ProductDetailUrl"), 500),
        "rohs": _str(part.get("ROHSStatus"), 40),
    }


def _hit(part: dict[str, Any]) -> dict[str, Any]:
    breaks = _breaks(part)
    first = breaks[0] if breaks else None
    return {
        "mpn": _str(part.get("ManufacturerPartNumber"), 64),
        "manufacturer": _str(part.get("Manufacturer"), 80),
        "mouser_part_number": _str(part.get("MouserPartNumber"), 64),
        "description": _str(part.get("Description")),
        "stock": _stock(part),
        "price_qty1": first["price"] if first else None,
        "currency": first["currency"] if first else None,
        "lifecycle": _lifecycle(part),
    }


# ---- client


class MouserClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
        per_minute: int = PER_MINUTE,
        per_day: int = PER_DAY,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        base_url: str = BASE_URL,
    ) -> None:
        self.__key = api_key
        self._transport = transport
        self._clock = clock
        self._today = today
        self._per_minute = per_minute
        self._per_day = per_day
        self._max_cache = max_cache_entries
        self._base_url = base_url
        self._cache: OrderedDict[tuple[str, ...], tuple[float, Any]] = OrderedDict()
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()
        _install_log_filter()

    def __repr__(self) -> str:
        return "MouserClient(api_key=***)"

    # ---- cache
    def _get(self, key: tuple[str, ...]) -> tuple[bool, Any]:
        hit = self._cache.get(key)
        if hit is None:
            return False, None
        expires, value = hit
        if expires <= self._clock():
            del self._cache[key]
            return False, None
        return True, value

    def _put(self, key: tuple[str, ...], value: Any) -> None:
        self._cache[key] = (self._clock() + CACHE_TTL_S, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)

    # ---- rate limit
    async def _acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            while self._calls and self._calls[0] <= now - _DAY:
                self._calls.popleft()
            if len(self._calls) >= self._per_day:
                wait = self._calls[0] + _DAY - now
                raise _error(
                    "Mouser daily request limit reached",
                    f"{self._per_day} requests in 24 h",
                    "rate_limited",
                    retry_after_s=math.ceil(wait),
                )
            recent = [t for t in self._calls if t > now - _MINUTE]
            if len(recent) >= self._per_minute:
                wait = recent[0] + _MINUTE - now
                raise _error(
                    "Mouser request limit reached",
                    f"{self._per_minute} requests per minute",
                    "rate_limited",
                    retry_after_s=math.ceil(wait),
                )
            self._calls.append(now)

    # ---- HTTP
    async def _post(
        self, path: str, body: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Parts and NumberOfResult of the response; every failure is structured."""
        await self._acquire()
        key = self.__key
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, transport=self._transport, timeout=_TIMEOUT
            ) as http:
                resp = await http.post(path, params={"apiKey": key}, json=body)
        except httpx.HTTPError as exc:
            log.warning("Mouser request failed: %s", type(exc).__name__)
            raise _error(
                "Mouser request failed",
                f"{type(exc).__name__} (network error or timeout)",
                "upstream_error",
            ) from None
        if resp.status_code == 429:
            raise _error("Mouser request limit reached", "Mouser answered HTTP 429", "rate_limited")
        if resp.status_code >= 400:
            log.warning("Mouser answered HTTP %d", resp.status_code)
            raise _error(
                "Mouser request failed",
                f"Mouser answered HTTP {resp.status_code}",
                "upstream_error",
            )
        try:
            data = resp.json()
        except ValueError:
            raise _error(
                "Mouser request failed", "the response is not JSON", "upstream_error"
            ) from None
        if not isinstance(data, dict):
            raise _error("Mouser request failed", "unexpected response shape", "upstream_error")
        results = data.get("SearchResults")
        errors = data.get("Errors")
        if isinstance(errors, list) and errors and not results:
            msgs = [
                redact(str(e.get("Message") or e.get("Code") or "error"), key)[:200]
                for e in errors
                if isinstance(e, dict)
            ]
            raise _error(
                "Mouser rejected the request",
                "; ".join(msgs) or "unknown error",
                "upstream_error",
            )
        if results is None:
            return [], 0
        if not isinstance(results, dict):
            raise _error("Mouser request failed", "unexpected response shape", "upstream_error")
        parts = results.get("Parts") or []
        if not isinstance(parts, list):
            raise _error("Mouser request failed", "unexpected response shape", "upstream_error")
        return [p for p in parts if isinstance(p, dict)], _int(results.get("NumberOfResult"))

    # ---- public API
    async def search(self, keyword: object, limit: object = 10) -> dict[str, Any]:
        """Arguments as the model sent them; validated here."""
        if not isinstance(keyword, str) or not keyword.strip():
            raise _invalid("keyword is empty")
        keyword = keyword.strip()
        if len(keyword) > MAX_KEYWORD_CHARS:
            raise _invalid(f"keyword is longer than {MAX_KEYWORD_CHARS} characters")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not (1 <= limit <= MAX_KEYWORD_RESULTS)
        ):
            raise _invalid(f"limit must be 1..{MAX_KEYWORD_RESULTS}")
        ckey = ("kw", keyword.casefold(), str(limit))
        found, cached = self._get(ckey)
        if not found:
            body = {
                "SearchByKeywordRequest": {
                    "keyword": keyword,
                    "records": limit,
                    "startingRecord": 0,
                }
            }
            parts, total = await self._post(KEYWORD_PATH, body)
            cached = {"total": total, "hits": [_hit(p) for p in parts[:limit]]}
            self._put(ckey, cached)
        return {
            "source": "Mouser",
            "as_of": self._today().isoformat(),
            "keyword": keyword,
            **cached,
        }

    async def parts(self, mpns: object) -> dict[str, Any]:
        """Arguments as the model sent them; validated here."""
        if not isinstance(mpns, list) or not mpns:
            raise _invalid("mpns must be a non-empty list of part numbers")
        if len(mpns) > MAX_MPNS:
            raise _invalid(f"at most {MAX_MPNS} part numbers per call")
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in mpns:
            if not isinstance(raw, str):
                raise _invalid("every part number must be a string")
            mpn = raw.strip()
            if not 3 <= len(mpn) <= 40:
                raise _invalid(f"{mpn!r}: a part number has 3 to 40 characters")
            if "|" in mpn:
                raise _invalid(f"{mpn!r}: '|' is not allowed in a part number")
            if mpn.casefold() not in seen:
                seen.add(mpn.casefold())
                wanted.append(mpn)
        results: dict[str, list[dict[str, Any]]] = {}
        missing: list[str] = []
        for mpn in wanted:
            found, cached = self._get(("pn", mpn.casefold()))
            if found:
                results[mpn.casefold()] = cached
            else:
                missing.append(mpn)
        if missing:
            body = {
                "SearchByPartRequest": {
                    "mouserPartNumber": "|".join(missing),
                    "partSearchOptions": "Exact",
                }
            }
            parts, _total = await self._post(PART_PATH, body)
            for mpn in missing:
                fold = mpn.casefold()
                offers = [
                    _offer(p)
                    for p in parts
                    if fold
                    in (
                        str(p.get("ManufacturerPartNumber") or "").strip().casefold(),
                        str(p.get("MouserPartNumber") or "").strip().casefold(),
                    )
                ][:MAX_OFFERS]
                results[fold] = offers
                self._put(("pn", fold), offers)
        out: list[dict[str, Any]] = []
        for mpn in wanted:
            offers = results[mpn.casefold()]
            if offers:
                out.append({"mpn": mpn, "found": True, "offers": offers})
            else:
                out.append({"mpn": mpn, "found": False, "note": "not found at Mouser"})
        return {"source": "Mouser", "as_of": self._today().isoformat(), "parts": out}


_clients: dict[str, MouserClient] = {}


def client_for(settings: Settings) -> MouserClient | None:
    """The process-wide client for the configured key, or None without a key."""
    secret = settings.mouser_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key:
        return None
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = MouserClient(key)
    return client
