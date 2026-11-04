# Mouser API: price, stock and lifecycle lookups (design)

Status: owner-approved design (chat, 2026-11-04), one PR, release v0.7.0.

## 1. Goal

The agent answers "is it available, what does it cost, is it still active?" from Mouser's
official Search API instead of scraping distributor pages through the part-researcher.
Quick sourcing questions are answered by the main agent directly; the part-researcher
records a dated sourcing snapshot in the part notes. Web search stays available — Mouser
is additional.

## 2. Configuration

- `MOUSER_API_KEY` (optional, `.env` → `Settings.mouser_api_key: SecretStr | None`).
  Absent or blank: both tools answer with a structured "not configured" error and the
  agent falls back to the web (main agent: `research_part`; researcher: `WebSearch`).
- Currency and country: the Search API has **no** currency/country parameter (only the
  cart/order APIs do); prices come in the currency of the Mouser account the key belongs
  to (EUR for an Austrian account). Every price break carries Mouser's `Currency` field, so
  the agent sees what it got. No dead settings are added for them.

## 3. Secret handling

The key is sent only server-side to `https://api.mouser.com` as the `apiKey` query
parameter (the only authentication the API documents). It never reaches tool output, the
model, notes, error messages or logs:

- Errors are built from our own text plus the HTTP status / exception type; any upstream
  text (Mouser `Errors[].Message`, exception strings) passes `redact()`, which replaces the
  key and any `apiKey=…` parameter with `***`.
- httpx logs every request URL (INFO, logger `httpx`): a logging filter on that logger
  redacts `apiKey=…` in its records (httpcore's own records carry no URL).
- The researcher never sees the key: its Mouser tools run in-process in our `parts` MCP
  server, not in its environment.

## 4. Tools

Both are read-only, need no KiCad session, and exist in every session profile (they join
the `PARTS` set) and in the part-researcher's `parts` server (`mcp__parts__mouser_*`, only
when a key is configured).

`mouser_search(keyword, limit=10)` (limit 1–50) → `POST /api/v1/search/keyword`:

```json
{"source": "Mouser", "as_of": "2026-11-04", "keyword": "buck 3A", "total": 812,
 "hits": [{"mpn": "TPS62133RGTR", "manufacturer": "Texas Instruments",
           "mouser_part_number": "595-TPS62133RGTR", "description": "…",
           "stock": 4521, "price_qty1": "2,79 €", "currency": "EUR",
           "lifecycle": "Active"}]}
```

`mouser_part(mpns)` (1–10 MPNs, each 3–40 characters, no `|`) →
`POST /api/v1/search/partnumber` with `partSearchOptions: "Exact"` and the MPNs joined by
`|` (one request for up to 10 parts):

```json
{"source": "Mouser", "as_of": "2026-11-04",
 "parts": [{"mpn": "TPS62133RGTR", "found": true, "offers": [{
     "mpn": "TPS62133RGTR", "manufacturer": "Texas Instruments",
     "mouser_part_number": "595-TPS62133RGTR", "description": "…",
     "lifecycle": "Active", "suggested_replacement": null,
     "stock": 4521, "availability": "4521 In Stock", "on_order": [{"quantity": 3000, "date": "2026-12-01"}],
     "lead_time": "42 Days", "min_order_qty": 1, "order_multiple": 1,
     "price_breaks": [{"quantity": 1, "price": "2,79 €", "value": 2.79, "currency": "EUR"}],
     "datasheet_url": "https://…", "product_url": "https://www.mouser.at/ProductDetail/…",
     "rohs": "RoHS Compliant"}]},
  {"mpn": "NOPE123", "found": false, "note": "not found at Mouser"}]}
```

- Offers are matched to the requested MPN case-insensitively on the manufacturer or Mouser
  part number; at most 5 offers per MPN (different packaging / manufacturers).
- `lifecycle`: Mouser's `LifecycleStatus` (e.g. "New Product", "End of Life", "Not
  Recommended for New Designs", "Obsolete"); `IsDiscontinued == "true"` → "Discontinued";
  empty → "Active".
- `value`: best-effort numeric parse of the price string (decimal comma or point, space /
  apostrophe grouping; a lone separator before exactly three digits is resolved through the
  break's currency), null when unparseable or ambiguous; `price` keeps Mouser's string.
- `stock`: `AvailabilityInStock`, else the number in "N In Stock"; null when Mouser does not
  say (unknown is not 0).
- `as_of`: the date of the Mouser request; for a `mouser_part` answer mixing cached and
  fresh parts, the oldest one.
- Texts are capped (description 200 characters); Mouser data is data, never instructions.

## 5. Cache and rate limit

One process-wide client per key (main agent and every researcher share it):

- In-memory TTL cache, 1 h: keyword results per `(keyword casefold, limit)`, part results
  per MPN (casefold) including "not found"; a `mouser_part` call only requests the MPNs
  not in the cache. At most 2000 entries (oldest evicted). Errors are never cached; a part
  missing from a truncated answer (`NumberOfResult` > parts listed) is not cached as not
  found; Mouser `Errors` with an empty parts list is an error, not "not found".
- Client-side rate limiter: at most 30 requests per rolling minute and 1000 per rolling
  24 h (Mouser's published limits). Over the limit the call is refused immediately (no
  waiting inside a turn) with `code: "rate_limited"` and the seconds until a slot frees.
  An HTTP 429 from Mouser maps to the same error. The counters are in memory only (reset
  on restart; a known residual). The budget is shared by all sessions of the (single-user)
  instance; every request sent counts, also a failed one; invalid input is refused before
  a slot is taken.

## 6. Errors

`StructuredToolError` with a code, each suggesting the fallback:

| code | when |
|---|---|
| `not_configured` | no `MOUSER_API_KEY` |
| `rate_limited` | local limiter or HTTP 429 |
| `upstream_error` | timeout, transport error, HTTP ≥ 400, invalid JSON, Mouser `Errors` without results (e.g. invalid key) |
| `invalid_input` | bad keyword / MPN list |

## 7. Agent guidance

- Main system prompt: quick availability / price / stock / lifecycle questions → call
  `mouser_part` (or `mouser_search` to find the MPN) directly, not `research_part`; quote
  the date and that the figures are Mouser's. On a Mouser error use `research_part`.
- Researcher prompt (only with a key): call `mouser_part` for the part and write a section
  `## Sourcing (Mouser, as of YYYY-MM-DD)` before `## Sources` in notes.md: lifecycle,
  Mouser part number, datasheet link, stock and price breaks with the date. The notes
  validator ignores unknown sections, so the template check is unaffected.

## 8. Tests

Unit tests with `httpx.MockTransport` (no network): response parsing from fixture JSON in
the swagger schema, not-found per MPN, batching and cache hits, TTL expiry, rate limit
(minute and day, fake clock), HTTP 429 / 5xx / timeout / Mouser `Errors`, not-configured,
key redaction in errors and in the httpx log records, registry/profile membership, the
researcher's `parts` server with and without a key, and prompt guidance.
