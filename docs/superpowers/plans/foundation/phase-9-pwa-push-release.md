# Phase 9: PWA, Web Push, release

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X6, X8); they supersede conflicting code in this file.

**Goal:** Make CopperChat installable and shippable: PWA manifest + service
worker, Web Push (VAPID) for "run finished", "needs attention" and
"rate-limit window reopened" (only when no client is visible), the app Docker
image on top of the KiCad base image, `docker-compose.yml` with an optional
`cloudflared` service, deployment/architecture docs, the README, a tag-driven
release workflow, a KiCad CI matrix and an end-to-end smoke job.

**Depends on:** 6B (viewer, for the smoke test), 7 (turn-end hooks, history), 8 (skills, uploads).

**Parallel with:** — (last phase).

**Read first:** the index (`docs/superpowers/plans/2026-10-15-foundation-plan.md`:
Global Constraints, C1, C2 `push_subscriptions`, C7 + C7a incl. "C7 (Phase 9)
visibility", C8 push routes, Phase Conventions); spec §3 (Deployment, Auth
(model), Remote access rows), §4 (image layers, mounts, `.env`), §9, §11 (CI
matrix), §12; `docs/spikes/1c-api-server.md` (pinned commit);
`docker/kicad-base/SUPPORTED_COMMITS` (Phase 2). Kontor reference (read-only):
`~/CloudCLI/kontor/server/pushDispatcher.ts`, `server/pushConfig.ts`,
`public/sw.js`, `public/manifest.webmanifest`.

**PR title:** `foundation phase 9: PWA, web push, release`

**Files:**
- Create: `backend/src/copperchat/db/migrations/0005_push.sql`
- Create: `backend/src/copperchat/push/__init__.py` (empty), `push/vapid.py`, `push/store.py`, `push/visibility.py`, `push/dispatcher.py`, `push/notifier.py`
- Create: `backend/src/copperchat/web/routes_push.py`
- Modify: `backend/src/copperchat/config.py` (`vapid_subject`), `backend/src/copperchat/web/app.py` (router, `app.state.push`), `backend/src/copperchat/web/ws.py` (visibility frame), `backend/src/copperchat/agent/protocol.py` (`VisibilityFrame`), the Phase 5 module defining `Broadcaster` (`add_listener`), `backend/pyproject.toml` (`pywebpush`)
- Create: `protocol-fixtures/visibility.json`
- Create: `frontend/public/manifest.webmanifest`, `frontend/public/sw.js`, `frontend/public/icons/{icon-192.png,icon-512.png,icon-maskable-512.png,badge-72.png,apple-touch-icon.png}` (generated), `scripts/gen-icons.py`
- Create: `frontend/src/push/push.ts`, `frontend/src/push/push.test.ts`, `frontend/src/push/visibility.ts`, `frontend/src/push/visibility.test.ts`, `frontend/src/pages/settings/NotificationsSection.test.tsx`
- Modify: `frontend/src/pages/settings/NotificationsSection.tsx` (replace 6A stub), `frontend/src/protocol/frames.ts` (visibility frame), `frontend/src/main.tsx` (SW registration + visibility reporter), `frontend/index.html` (manifest + apple meta)
- Create: `docker/app/Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`
- Create: `docs/deployment/cloudflare.md`, `docs/architecture.md`; Modify: `README.md`
- Create: `.github/workflows/release.yml`, `frontend/e2e/smoke-docker.spec.ts`, `scripts/e2e-token-missing.sh`; Modify: `.github/workflows/ci.yml`

**Acceptance criteria:**
- Migration `0005_push.sql` creates `push_subscriptions` exactly as in C2.
- VAPID keys are generated once into `/data/vapid.json` (mode 0600) and reused; `GET /api/push/vapid-public-key` returns the base64url uncompressed P-256 public key.
- `POST /api/push/subscribe` upserts by endpoint; `DELETE /api/push/subscribe` removes it; both need the session cookie.
- C→S frame `visibility{projectId, visible}` validated by pydantic and zod, fixture `protocol-fixtures/visibility.json` checked by both suites.
- Pushes are sent for: run finished (`completed`), needs attention (`run-finished` with reason `error`, or the run's final assistant text ends with `?`), rate-limit window reopened — and only when no WebSocket client reports `visible: true`.
- The notification click opens `/p/<projectId>`; 404/410 from the push service deletes the subscription.
- `sw.js` handles only `push` / `notificationclick` and never caches (in particular never `/api`).
- Settings → Notifications: permission state, subscribe/unsubscribe, iOS hint to install to the Home Screen when not standalone, HTTPS hint when `PushManager` is missing.
- `docker compose up` with `.env` runs the app on 8080; `--profile tunnel` adds `cloudflared`.
- Container without token and without `DEV_FAKE_AGENT` exits non-zero and prints `claude setup-token`.
- `release.yml` publishes `ghcr.io/peterus/copperchat:<version>` and `:latest` on tag `v*`.
- CI `kicad` job is a matrix over `docker/kicad-base/SUPPORTED_COMMITS`; `e2e` job runs the Docker smoke + Playwright happy path.

**Verification commands:**
```bash
cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src
cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build
docker build -f docker/app/Dockerfile --build-arg KICAD_COMMIT=$(cat docker/kicad-base/KICAD_COMMIT) -t copperchat:dev .
bash scripts/e2e-token-missing.sh copperchat:dev
```

**CI expectations:** `backend`, `frontend`, `kicad (<commit>)` per supported
commit, `e2e` all green. `release.yml` is not triggered by the PR (tags only).

---

## Interfaces → Produces (contract additions; coordinator adds to index C7a)

```python
# C1 Settings (Phase 9)
vapid_subject: str = "mailto:copperchat@localhost"

# C7 (Phase 9) — protocol
class VisibilityFrame(Wire):   # C→S {"type":"visibility","projectId":str,"visible":bool}

# Broadcaster (Phase 5) gains:
Listener = Callable[[ServerFrame], Awaitable[None]]
def add_listener(self, listener: Listener) -> None   # called for every published frame, errors logged

# copperchat.push
VapidKeys(public_key_b64url: str, private_pem: str); load_or_create_vapid(data_dir: Path) -> VapidKeys
PushStore(db): upsert(endpoint, p256dh, auth), delete(endpoint) -> bool, delete_id(id), all() -> list[PushSub]
VisibilityRegistry: set(conn_id: int, project_id: str, visible: bool), drop(conn_id), any_visible() -> bool
PushDispatcher(store, keys, subject, *, sender=None): async send_all(payload: dict) -> int
PushNotifier(db, dispatcher, visibility, project_names): async on_frame(frame) ; async close()
app.state.push: PushServices(keys, store, visibility, dispatcher, notifier)
```

Frontend: `@/push/push` (`pushSupport()`, `isStandalone()`, `isIos()`,
`subscribePush()`, `unsubscribePush()`, `currentSubscription()`,
`registerServiceWorker()`), `@/push/visibility` (`startVisibilityReporter(getProjectId)`).

---

### Task 9.1: Migration 0005, VAPID keys and subscription store

**Files:**
- Create: `backend/src/copperchat/db/migrations/0005_push.sql`, `backend/src/copperchat/push/__init__.py`, `push/vapid.py`, `push/store.py`
- Modify: `backend/pyproject.toml` (add `pywebpush>=2.0` — MPL-2.0, GPL-3.0-compatible; it pulls `py-vapid` MPL-2.0, `cryptography` Apache-2.0/BSD)
- Test: `backend/tests/unit/push/test_vapid_store.py`

**Interfaces:**
- Consumes: C2 `Database`.
- Produces: `VapidKeys`, `load_or_create_vapid`, `PushSub`, `PushStore`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/push/test_vapid_store.py
import base64
import json
import stat
from pathlib import Path

from copperchat.db import Database
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_vapid_generated_once_and_reused(tmp_path: Path) -> None:
    k1 = load_or_create_vapid(tmp_path)
    raw = _b64d(k1.public_key_b64url)
    assert len(raw) == 65 and raw[0] == 0x04          # uncompressed P-256 point
    path = tmp_path / "vapid.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["publicKey"] == k1.public_key_b64url
    k2 = load_or_create_vapid(tmp_path)
    assert k2 == k1


async def test_store_upsert_delete(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "c.db")
    try:
        store = PushStore(db)
        await store.upsert("https://push.example/a", "p1", "a1")
        await store.upsert("https://push.example/a", "p2", "a2")   # same endpoint → update
        await store.upsert("https://push.example/b", "p3", "a3")
        subs = await store.all()
        assert [(s.endpoint, s.p256dh, s.auth) for s in subs] == [
            ("https://push.example/a", "p2", "a2"), ("https://push.example/b", "p3", "a3")]
        assert await store.delete("https://push.example/a") is True
        assert await store.delete("https://push.example/a") is False
        await store.delete_id(subs[1].id)
        assert await store.all() == []
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/push/test_vapid_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.push'`.

- [ ] **Step 3: Write minimal implementation**

```sql
-- backend/src/copperchat/db/migrations/0005_push.sql
CREATE TABLE push_subscriptions (
  id INTEGER PRIMARY KEY,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

```python
# backend/src/copperchat/push/vapid.py
"""VAPID key pair, generated once into <data_dir>/vapid.json (ported from Kontor pushConfig.ts,
which read env vars; CopperChat generates and persists instead)."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


@dataclass(frozen=True)
class VapidKeys:
    public_key_b64url: str
    private_pem: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_or_create_vapid(data_dir: Path) -> VapidKeys:
    path = data_dir / "vapid.json"
    if path.exists():
        raw = json.loads(path.read_text())
        return VapidKeys(raw["publicKey"], raw["privatePem"])
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    keys = VapidKeys(_b64url(pub), pem)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"publicKey": keys.public_key_b64url, "privatePem": keys.private_pem}, fh)
    os.replace(tmp, path)
    return keys
```

```python
# backend/src/copperchat/push/store.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from copperchat.db import Database


@dataclass(frozen=True)
class PushSub:
    id: int
    endpoint: str
    p256dh: str
    auth: str


class PushStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, endpoint: str, p256dh: str, auth: str) -> None:
        await self.db.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, p256dh, auth, datetime.now(UTC).isoformat()),
        )

    async def delete(self, endpoint: str) -> bool:
        row = await self.db.fetchone("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        if row is None:
            return False
        await self.delete_id(int(row["id"]))
        return True

    async def delete_id(self, sub_id: int) -> None:
        await self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))

    async def all(self) -> list[PushSub]:
        rows = await self.db.fetchall("SELECT id, endpoint, p256dh, auth FROM push_subscriptions ORDER BY id")
        return [PushSub(int(r["id"]), r["endpoint"], r["p256dh"], r["auth"]) for r in rows]
```

`backend/pyproject.toml`: add `"pywebpush>=2.0"` to `[project].dependencies`, then `cd backend && uv lock`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv sync && uv run pytest tests/unit/push/test_vapid_store.py -v && uv run mypy src`
Expected: 2 passed; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/db/migrations/0005_push.sql backend/src/copperchat/push backend/pyproject.toml backend/uv.lock backend/tests/unit/push/test_vapid_store.py
git commit -m "feat: push subscriptions table, store and persisted VAPID keys

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.2: `visibility` frame (contract addition) and visibility registry

**Files:**
- Create: `protocol-fixtures/visibility.json`, `backend/src/copperchat/push/visibility.py`
- Modify: `backend/src/copperchat/agent/protocol.py`, `backend/src/copperchat/web/ws.py`, `frontend/src/protocol/frames.ts`
- Test: `backend/tests/unit/push/test_visibility.py`; the existing Phase 5 fixture test (`backend/tests/unit/agent/test_protocol_fixtures.py`) and Phase 6A `frontend/src/protocol/frames.test.ts` pick up the new fixture automatically.

**Interfaces:**
- Consumes: Phase 5 `Wire`, `ProjectId`, `ClientFrame`, `client_frame_adapter`; Phase 6A zod `ClientFrame`, `ALL_FRAME_TYPES`.
- Produces: `VisibilityFrame`; zod member `{type:'visibility', projectId, visible}`; `VisibilityRegistry`.

- [ ] **Step 1: Write the failing test**

```json
// protocol-fixtures/visibility.json
{"type": "visibility", "directions": ["c2s"],
 "valid": [{"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": true},
           {"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": false}],
 "invalid": [{"type": "visibility", "projectId": "a1b2c3d4e5f6"},
             {"type": "visibility", "projectId": null, "visible": true},
             {"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": "yes"}]}
```

```python
# backend/tests/unit/push/test_visibility.py
import pytest
from pydantic import ValidationError

from copperchat.agent.protocol import VisibilityFrame, client_frame_adapter
from copperchat.push.visibility import VisibilityRegistry


def test_visibility_frame_parses() -> None:
    f = client_frame_adapter.validate_python({"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": True})
    assert isinstance(f, VisibilityFrame) and f.visible is True
    with pytest.raises(ValidationError):
        client_frame_adapter.validate_python({"type": "visibility", "projectId": "a1b2c3d4e5f6", "visible": "x"})


def test_registry_any_visible() -> None:
    r = VisibilityRegistry()
    assert r.any_visible() is False
    r.set(1, "p1", True)
    r.set(2, "p2", False)
    assert r.any_visible() is True
    r.set(1, "p1", False)
    assert r.any_visible() is False
    r.set(3, "p1", True)
    r.drop(3)                       # socket closed while visible
    assert r.any_visible() is False
```

Add to Phase 6A's `frontend/src/protocol/frames.test.ts` (the fixture loop already covers parsing; this asserts the type list):

```ts
it('knows the visibility frame', () => {
  expect(ALL_FRAME_TYPES).toContain('visibility');
  expect(ClientFrame.safeParse({ type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: true }).success).toBe(true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/push/test_visibility.py tests/unit/agent -q; cd ../frontend && npx vitest run src/protocol`
Expected: FAIL — `ImportError: cannot import name 'VisibilityFrame'`; fixture tests fail on `visibility.json`; vitest fails `expected [...] to contain 'visibility'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/agent/protocol.py` — add the model and extend the union:

```python
class VisibilityFrame(Wire):
    type: Literal["visibility"] = "visibility"
    project_id: ProjectId
    visible: bool


ClientFrame = Annotated[
    SubscribeFrame | SendFrame | InterruptFrame | CancelQueuedFrame | PingFrame | VisibilityFrame,
    Field(discriminator="type"),
]
```

```python
# backend/src/copperchat/push/visibility.py
from __future__ import annotations


class VisibilityRegistry:
    """Which WebSocket connections currently show a CopperChat page in the foreground.
    One user per instance, so 'no client of that user is visible' == no visible connection."""

    def __init__(self) -> None:
        self._visible: dict[int, str] = {}

    def set(self, conn_id: int, project_id: str, visible: bool) -> None:
        if visible:
            self._visible[conn_id] = project_id
        else:
            self._visible.pop(conn_id, None)

    def drop(self, conn_id: int) -> None:
        self._visible.pop(conn_id, None)

    def any_visible(self) -> bool:
        return bool(self._visible)
```

`backend/src/copperchat/web/ws.py` — in the client-frame dispatch (the
`isinstance` chain over parsed `ClientFrame`s) add, and in the connection's
`finally` block drop the connection:

```python
from copperchat.agent.protocol import VisibilityFrame

        elif isinstance(frame, VisibilityFrame):
            websocket.app.state.push.visibility.set(id(websocket), frame.project_id, frame.visible)
# ...
    finally:
        websocket.app.state.push.visibility.drop(id(websocket))
```

`frontend/src/protocol/frames.ts` — add the member to `ClientFrame` and the
type list:

```ts
  z.object({ type: z.literal('visibility'), projectId: ProjectId, visible: z.boolean() }),
```

and append `'visibility'` to `ALL_FRAME_TYPES`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -m "not kicad" -q; cd ../frontend && npx vitest run src/protocol`
Expected: all pass, including the fixture suites with `visibility.json`.

- [ ] **Step 5: Commit**

```bash
git add protocol-fixtures/visibility.json backend/src/copperchat/agent/protocol.py backend/src/copperchat/push/visibility.py backend/src/copperchat/web/ws.py backend/tests/unit/push/test_visibility.py frontend/src/protocol/frames.ts frontend/src/protocol/frames.test.ts
git commit -m "feat: visibility client frame and visibility registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.3: Push dispatcher, notifier and routes

**Files:**
- Create: `backend/src/copperchat/push/dispatcher.py`, `push/notifier.py`, `backend/src/copperchat/web/routes_push.py`
- Modify: `backend/src/copperchat/config.py` (`vapid_subject: str = "mailto:copperchat@localhost"`), the Phase 5 `Broadcaster` module (`add_listener`), `backend/src/copperchat/web/app.py`
- Test: `backend/tests/unit/push/test_dispatcher.py`, `backend/tests/unit/push/test_notifier.py`, `backend/tests/unit/push/test_routes_push.py`

**Interfaces:**
- Consumes: `PushStore`, `VapidKeys`, `VisibilityRegistry`, C7 `RunFinishedFrame`, `RateLimitedFrame`, `messages` table, `Broadcaster`.
- Produces: `PushDispatcher`, `PushNotifier`, `PushServices`, `Broadcaster.add_listener`, routes C8 push.

Event rules (spec §9):

| Frame | Notification |
|---|---|
| `run-finished` `completed`, final assistant text not ending in `?` | "Run finished" — first 120 chars of the final text |
| `run-finished` `error`, or final assistant text ends with `?` | "Needs your attention" |
| `rate-limited{resetsAt}` | timer until `resetsAt`, then "Rate limit window reopened" |
| `run-finished` `interrupted` / `rate_limited` | none (user pressed Stop / parked) |

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/push/test_dispatcher.py
from pathlib import Path

from copperchat.db import Database
from copperchat.push.dispatcher import PushDispatcher, PushSendError
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid


async def test_send_all_deletes_gone_subscriptions(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "c.db")
    store = PushStore(db)
    for name in ("ok", "gone", "notfound", "flaky"):
        await store.upsert(f"https://push.example/{name}", "p", "a")
    sent: list[str] = []

    def sender(sub: dict, data: str) -> None:
        name = sub["endpoint"].rsplit("/", 1)[1]
        if name == "gone":
            raise PushSendError(410)
        if name == "notfound":
            raise PushSendError(404)
        if name == "flaky":
            raise PushSendError(500)
        sent.append(data)

    d = PushDispatcher(store, load_or_create_vapid(tmp_path), "mailto:x@y", sender=sender)
    assert await d.send_all({"title": "t", "body": "b", "url": "/p/abc"}) == 1
    assert '"url": "/p/abc"' in sent[0]
    assert [s.endpoint.rsplit("/", 1)[1] for s in await store.all()] == ["ok", "flaky"]
    await db.close()
```

```python
# backend/tests/unit/push/test_notifier.py
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from copperchat.agent.protocol import RateLimitedFrame, RunFinishedFrame
from copperchat.db import Database
from copperchat.push.notifier import PushNotifier
from copperchat.push.visibility import VisibilityRegistry

PID = "a1b2c3d4e5f6"


class FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_all(self, payload: dict) -> int:
        self.sent.append(payload)
        return 1


@pytest.fixture
async def env(tmp_path: Path):
    db = await Database.open(tmp_path / "c.db")
    d = FakeDispatcher()
    vis = VisibilityRegistry()
    n = PushNotifier(db, d, vis, project_names=lambda pid: "ecc83-pp")
    yield db, d, vis, n
    await n.close()
    await db.close()


async def _assistant(db: Database, run_id: str, text: str) -> None:
    await db.execute(
        "INSERT INTO messages(project_id, run_id, role, kind, content, status, metadata, created_at, updated_at) "
        "VALUES (?, ?, 'assistant', 'text', ?, 'done', '{}', '', '')", (PID, run_id, text))


async def test_run_finished_sends_when_hidden(env) -> None:
    db, d, vis, n = env
    await _assistant(db, "r1", "ERC is clean.")
    await n.on_frame(RunFinishedFrame(project_id=PID, run_id="r1", reason="completed"))
    assert d.sent == [{"title": "ecc83-pp: run finished", "body": "ERC is clean.",
                       "url": f"/p/{PID}", "tag": f"run-{PID}"}]


async def test_question_and_error_need_attention(env) -> None:
    db, d, vis, n = env
    await _assistant(db, "r2", "Thinking…")
    await _assistant(db, "r2", "Should I run DRC too?")
    await n.on_frame(RunFinishedFrame(project_id=PID, run_id="r2", reason="completed"))
    await n.on_frame(RunFinishedFrame(project_id=PID, run_id="r3", reason="error"))
    assert [p["title"] for p in d.sent] == ["ecc83-pp: needs your attention"] * 2
    assert d.sent[0]["body"] == "Should I run DRC too?"


async def test_suppressed_when_visible_and_for_interrupt(env) -> None:
    db, d, vis, n = env
    vis.set(1, PID, True)
    await n.on_frame(RunFinishedFrame(project_id=PID, run_id="r1", reason="error"))
    vis.set(1, PID, False)
    await n.on_frame(RunFinishedFrame(project_id=PID, run_id="r1", reason="interrupted"))
    assert d.sent == []


async def test_rate_limit_reopened(env) -> None:
    db, d, vis, n = env
    soon = (datetime.now(UTC) + timedelta(milliseconds=50)).isoformat()
    await n.on_frame(RateLimitedFrame(project_id=PID, resets_at=soon))
    await asyncio.sleep(0.3)
    assert [p["title"] for p in d.sent] == ["ecc83-pp: rate limit window reopened"]
```

```python
# backend/tests/unit/push/test_routes_push.py
from fastapi.testclient import TestClient


def test_push_routes(logged_in_client: TestClient) -> None:
    c = logged_in_client
    key = c.get("/api/push/vapid-public-key").json()["publicKey"]
    assert isinstance(key, str) and len(key) == 87
    body = {"endpoint": "https://push.example/x", "keys": {"p256dh": "p", "auth": "a"}}
    assert c.post("/api/push/subscribe", json=body).status_code == 204
    assert c.request("DELETE", "/api/push/subscribe", json={"endpoint": "https://push.example/x"}).status_code == 204
    assert c.post("/api/push/subscribe", json={"endpoint": "http://insecure/x", "keys": body["keys"]}).status_code == 422


def test_push_routes_need_login(client: TestClient) -> None:
    assert client.get("/api/push/vapid-public-key").status_code == 401
```

(`client` / `logged_in_client` are the Phase 3 `backend/tests/conftest.py`
fixtures. If Phase 3 named them differently, use its names.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/push -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.push.dispatcher'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/push/dispatcher.py
"""Ported from Kontor server/pushDispatcher.ts (sendToAll + dead-subscription cleanup)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from copperchat.push.store import PushStore
from copperchat.push.vapid import VapidKeys

log = logging.getLogger(__name__)
Sender = Callable[[dict[str, Any], str], None]


class PushSendError(Exception):
    def __init__(self, status: int | None) -> None:
        super().__init__(f"push send failed (status {status})")
        self.status = status


def _webpush_sender(keys: VapidKeys, subject: str) -> Sender:
    vapid = Vapid01.from_pem(keys.private_pem.encode())

    def send(sub: dict[str, Any], data: str) -> None:
        try:
            webpush(subscription_info=sub, data=data, vapid_private_key=vapid,
                    vapid_claims={"sub": subject}, ttl=3600, timeout=10)
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            raise PushSendError(status) from exc

    return send


class PushDispatcher:
    def __init__(self, store: PushStore, keys: VapidKeys, subject: str, *, sender: Sender | None = None) -> None:
        self.store = store
        self.sender = sender or _webpush_sender(keys, subject)

    async def send_all(self, payload: dict[str, Any]) -> int:
        data = json.dumps(payload)
        ok = 0
        for sub in await self.store.all():
            info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
            try:
                await asyncio.to_thread(self.sender, info, data)
                ok += 1
            except PushSendError as exc:
                if exc.status in (404, 410):
                    await self.store.delete_id(sub.id)
                    log.info("removed dead push subscription %s (status %s)", sub.id, exc.status)
                else:
                    log.warning("push send failed for subscription %s: %s", sub.id, exc)
        return ok
```

```python
# backend/src/copperchat/push/notifier.py
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from copperchat.agent.protocol import RateLimitedFrame, RunFinishedFrame
from copperchat.db import Database
from copperchat.push.visibility import VisibilityRegistry


class Dispatcher(Protocol):
    async def send_all(self, payload: dict[str, Any]) -> int: ...


class PushNotifier:
    def __init__(self, db: Database, dispatcher: Dispatcher, visibility: VisibilityRegistry,
                 project_names: Callable[[str], str]) -> None:
        self.db, self.dispatcher, self.visibility = db, dispatcher, visibility
        self.project_names = project_names
        self._timers: dict[str, asyncio.Task[None]] = {}

    async def on_frame(self, frame: Any) -> None:
        if isinstance(frame, RunFinishedFrame):
            await self._run_finished(frame)
        elif isinstance(frame, RateLimitedFrame):
            self._schedule_reopen(frame.project_id, frame.resets_at)

    async def _final_text(self, project_id: str, run_id: str) -> str:
        row = await self.db.fetchone(
            "SELECT content FROM messages WHERE project_id = ? AND run_id = ? AND role = 'assistant' "
            "AND kind = 'text' ORDER BY id DESC LIMIT 1", (project_id, run_id))
        return (row["content"] if row else "").strip()

    async def _run_finished(self, f: RunFinishedFrame) -> None:
        if f.reason not in ("completed", "error"):
            return
        text = await self._final_text(f.project_id, f.run_id)
        attention = f.reason == "error" or text.endswith("?")
        title = "needs your attention" if attention else "run finished"
        body = text[:120] if text else ("The run ended with an error." if f.reason == "error" else "Done.")
        await self._send(f.project_id, title, body, tag=f"run-{f.project_id}")

    def _schedule_reopen(self, project_id: str, resets_at: str) -> None:
        if (old := self._timers.pop(project_id, None)) is not None:
            old.cancel()
        when = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        delay = max(0.0, (when - datetime.now(UTC)).total_seconds())

        async def fire() -> None:
            await asyncio.sleep(delay)
            self._timers.pop(project_id, None)
            await self._send(project_id, "rate limit window reopened",
                             "Queued messages continue now.", tag=f"ratelimit-{project_id}")

        self._timers[project_id] = asyncio.create_task(fire())

    async def _send(self, project_id: str, title: str, body: str, *, tag: str) -> None:
        if self.visibility.any_visible():
            return
        await self.dispatcher.send_all({"title": f"{self.project_names(project_id)}: {title}",
                                        "body": body, "url": f"/p/{project_id}", "tag": tag})

    async def close(self) -> None:
        for t in self._timers.values():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._timers.clear()
```

`Broadcaster` (Phase 5 module) — add:

```python
Listener = Callable[[ServerFrame], Awaitable[None]]

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)       # self._listeners: list[Listener] = [] in __init__

    # at the end of publish(project_id, frame):
        for listener in self._listeners:
            try:
                await listener(frame)
            except Exception:
                log.exception("broadcast listener failed")
```

```python
# backend/src/copperchat/web/routes_push.py
from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from copperchat.push.dispatcher import PushDispatcher
from copperchat.push.notifier import PushNotifier
from copperchat.push.store import PushStore
from copperchat.push.vapid import VapidKeys
from copperchat.push.visibility import VisibilityRegistry
from copperchat.web.auth import require_user


@dataclass
class PushServices:
    keys: VapidKeys
    store: PushStore
    visibility: VisibilityRegistry
    dispatcher: PushDispatcher
    notifier: PushNotifier


class SubKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class SubscribeBody(BaseModel):
    endpoint: str = Field(pattern=r"^https://", max_length=2000)
    keys: SubKeys


class UnsubscribeBody(BaseModel):
    endpoint: str = Field(max_length=2000)


router = APIRouter(prefix="/api/push", dependencies=[Depends(require_user)])


@router.get("/vapid-public-key")
async def vapid_public_key(request: Request) -> dict[str, str]:
    return {"publicKey": request.app.state.push.keys.public_key_b64url}


@router.post("/subscribe", status_code=204)
async def subscribe(body: SubscribeBody, request: Request) -> Response:
    await request.app.state.push.store.upsert(body.endpoint, body.keys.p256dh, body.keys.auth)
    return Response(status_code=204)


@router.delete("/subscribe", status_code=204)
async def unsubscribe(body: UnsubscribeBody, request: Request) -> Response:
    await request.app.state.push.store.delete(body.endpoint)
    return Response(status_code=204)
```

`backend/src/copperchat/web/app.py` — in the lifespan after `app.state.db`
and `app.state.agent_runtime` exist, before `mount_frontend` (which must stay last):

```python
from copperchat.projects import discover_projects
from copperchat.push.dispatcher import PushDispatcher
from copperchat.push.notifier import PushNotifier
from copperchat.push.store import PushStore
from copperchat.push.vapid import load_or_create_vapid
from copperchat.push.visibility import VisibilityRegistry
from copperchat.web.routes_push import PushServices, router as push_router

    keys = load_or_create_vapid(settings.data_dir)
    store = PushStore(app.state.db)
    visibility = VisibilityRegistry()
    dispatcher = PushDispatcher(store, keys, settings.vapid_subject)

    def project_name(pid: str) -> str:
        return next((p.name for p in discover_projects(settings.projects_root) if p.id == pid), "CopperChat")

    notifier = PushNotifier(app.state.db, dispatcher, visibility, project_name)
    app.state.push = PushServices(keys, store, visibility, dispatcher, notifier)
    app.state.agent_runtime.broadcaster.add_listener(notifier.on_frame)
    # on shutdown: await app.state.push.notifier.close()

app.include_router(push_router)
```

(`discover_projects` is Phase 3's C3 discovery function; use its exact name.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/push -v && uv run pytest -m "not kicad" -q && uv run mypy src`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat backend/tests/unit/push
git commit -m "feat: web push dispatcher, notifier and push routes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.4: PWA manifest, service worker, icons

**Files:**
- Create: `scripts/gen-icons.py`, `frontend/public/manifest.webmanifest`, `frontend/public/sw.js`, `frontend/public/icons/*.png` (generated, committed)
- Modify: `frontend/index.html`
- Test: `frontend/src/push/sw.test.ts`

**Interfaces:**
- Produces: `/sw.js` (scope `/`), `/manifest.webmanifest`, icons.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/push/sw.test.ts
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';

const src = readFileSync(fileURLToPath(new URL('../../public/sw.js', import.meta.url)), 'utf8');

function loadSw() {
  const handlers: Record<string, (e: any) => void> = {};
  const client = { url: 'https://cc.example/projects', focus: vi.fn(), navigate: vi.fn() };
  const self: any = {
    location: { origin: 'https://cc.example' },
    addEventListener: (t: string, h: (e: any) => void) => { handlers[t] = h; },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(), matchAll: vi.fn().mockResolvedValue([client]), openWindow: vi.fn() },
    registration: { showNotification: vi.fn() },
  };
  new Function('self', src)(self);
  return { handlers, self, client };
}

describe('sw.js', () => {
  it('never registers a fetch handler (no caching, /api untouched)', () => {
    const { handlers } = loadSw();
    expect(handlers.fetch).toBeUndefined();
    expect(src).not.toMatch(/caches\./);
  });

  it('shows a notification from the push payload', async () => {
    const { handlers, self } = loadSw();
    let p: Promise<unknown> = Promise.resolve();
    handlers.push({ data: { json: () => ({ title: 'T', body: 'B', url: '/p/a1b2c3d4e5f6', tag: 'run-a1b2c3d4e5f6' }) },
                    waitUntil: (x: Promise<unknown>) => { p = x; } });
    await p;
    expect(self.registration.showNotification).toHaveBeenCalledWith('T', expect.objectContaining({
      body: 'B', tag: 'run-a1b2c3d4e5f6', data: { url: '/p/a1b2c3d4e5f6' } }));
  });

  it('click focuses an open window and navigates to the project', async () => {
    const { handlers, client } = loadSw();
    let p: Promise<unknown> = Promise.resolve();
    handlers.notificationclick({ notification: { close: vi.fn(), data: { url: '/p/a1b2c3d4e5f6' } },
                                 waitUntil: (x: Promise<unknown>) => { p = x; } });
    await p;
    expect(client.focus).toHaveBeenCalled();
    expect(client.navigate).toHaveBeenCalledWith('/p/a1b2c3d4e5f6');
  });

  it('rejects foreign click targets', async () => {
    const { handlers, self } = loadSw();
    self.clients.matchAll.mockResolvedValue([]);
    let p: Promise<unknown> = Promise.resolve();
    handlers.notificationclick({ notification: { close: vi.fn(), data: { url: 'https://evil.example/' } },
                                 waitUntil: (x: Promise<unknown>) => { p = x; } });
    await p;
    expect(self.clients.openWindow).toHaveBeenCalledWith('/');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/push/sw.test.ts`
Expected: FAIL — `ENOENT: no such file or directory, open '.../public/sw.js'`.

- [ ] **Step 3: Write minimal implementation**

```js
// frontend/public/sw.js
// CopperChat service worker — push notifications ONLY.
// No fetch handler, no caching: /api and the app always go to the network.
/* eslint-disable no-restricted-globals */

self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()); });

function safeUrl(url) {
  return typeof url === 'string' && /^\/p\/[0-9a-f]{12}$/.test(url) ? url : '/';
}

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  event.waitUntil(self.registration.showNotification(data.title || 'CopperChat', {
    body: data.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/badge-72.png',
    tag: data.tag,
    data: { url: safeUrl(data.url) },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = safeUrl((event.notification.data || {}).url);
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      if (client.url.startsWith(self.location.origin)) {
        await client.focus();
        if ('navigate' in client) await client.navigate(url);
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
```

(`safeUrl` only accepts `/p/<12-hex id>`, so a push payload cannot redirect
the click to a foreign origin.)

```json
// frontend/public/manifest.webmanifest
{
  "name": "CopperChat",
  "short_name": "CopperChat",
  "description": "Chat with an AI agent about your KiCad projects. Powered by Claude.",
  "start_url": "/projects",
  "scope": "/",
  "display": "standalone",
  "background_color": "#14110f",
  "theme_color": "#b87333",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```

`frontend/index.html` — inside `<head>`:

```html
<link rel="manifest" href="/manifest.webmanifest" />
<meta name="theme-color" content="#b87333" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-title" content="CopperChat" />
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png" />
```

```python
# scripts/gen-icons.py
"""Generate CopperChat PWA icons with Pillow (run: uv run --with pillow scripts/gen-icons.py)."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"
COPPER, DARK = (184, 115, 51, 255), (20, 17, 15, 255)


def icon(size: int, *, maskable: bool = False, badge: bool = False) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if badge else DARK)
    d = ImageDraw.Draw(img)
    pad = size * (0.22 if maskable else 0.12)
    fg = (255, 255, 255, 255) if badge else COPPER
    w = max(2, round(size * 0.07))
    # a stylised trace with two pads: "copper chat bubble"
    d.rounded_rectangle([pad, pad, size - pad, size - pad * 1.6], radius=size * 0.12, outline=fg, width=w)
    d.polygon([(size * 0.32, size - pad * 1.6), (size * 0.32, size - pad), (size * 0.48, size - pad * 1.6)], fill=fg)
    r = size * 0.06
    for cx in (size * 0.36, size * 0.64):
        d.ellipse([cx - r, size * 0.42 - r, cx + r, size * 0.42 + r], fill=fg)
    d.line([(size * 0.36, size * 0.42), (size * 0.64, size * 0.42)], fill=fg, width=w)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192).save(OUT / "icon-192.png")
    icon(512).save(OUT / "icon-512.png")
    icon(512, maskable=True).save(OUT / "icon-maskable-512.png")
    icon(180).convert("RGB").save(OUT / "apple-touch-icon.png")
    icon(72, badge=True).save(OUT / "badge-72.png")


if __name__ == "__main__":
    main()
```

Run once: `uv run --with pillow python scripts/gen-icons.py` and commit the PNGs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/push/sw.test.ts && npm run build && test -f dist/sw.js && test -f dist/icons/icon-512.png`
Expected: 4 passed; build output contains `sw.js` and icons.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen-icons.py frontend/public frontend/index.html frontend/src/push/sw.test.ts
git commit -m "feat: PWA manifest, push-only service worker and icons

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.5: Push client, visibility reporter and NotificationsSection

**Files:**
- Create: `frontend/src/push/push.ts`, `frontend/src/push/visibility.ts`
- Modify: `frontend/src/pages/settings/NotificationsSection.tsx` (replaces the 6A stub), `frontend/src/main.tsx`
- Test: `frontend/src/push/push.test.ts`, `frontend/src/push/visibility.test.ts`, `frontend/src/pages/settings/NotificationsSection.test.tsx`

**Interfaces:**
- Consumes: `@/api/http` (`apiGet`, `apiPost`, `apiDelete`), `@/api/ws` (`wsClient.send(frame)`, `wsClient.onConnection(cb)` — use the 6A `createWsClient` API for sending a `ClientFrame` and observing `ConnectionConnection`), `@/stores/projects` (`useProjects().current`).
- Produces: see "Interfaces → Produces" above.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/push/push.test.ts
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as http from '@/api/http';
import { isIos, isStandalone, pushSupport, subscribePush, unsubscribePush, urlBase64ToUint8Array } from './push';

afterEach(() => vi.restoreAllMocks());

describe('push helpers', () => {
  it('decodes base64url VAPID keys', () => {
    expect(Array.from(urlBase64ToUint8Array('AQID-_8'))).toEqual([1, 2, 3, 251, 255]);
  });

  it('detects iOS and standalone', () => {
    expect(isIos('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)')).toBe(true);
    expect(isIos('Mozilla/5.0 (X11; Linux x86_64)')).toBe(false);
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q === '(display-mode: standalone)' }));
    expect(isStandalone()).toBe(true);
  });

  it('reports unsupported without PushManager', () => {
    expect(pushSupport({} as Window)).toBe('unsupported');
  });

  it('subscribes with the server key and posts the subscription', async () => {
    const sub = { endpoint: 'https://push.example/x', toJSON: () => ({ endpoint: 'https://push.example/x', keys: { p256dh: 'p', auth: 'a' } }), unsubscribe: vi.fn().mockResolvedValue(true) };
    const subscribe = vi.fn().mockResolvedValue(sub);
    const reg = { pushManager: { subscribe, getSubscription: vi.fn().mockResolvedValue(sub) } };
    vi.stubGlobal('navigator', { serviceWorker: { ready: Promise.resolve(reg) } });
    vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('granted') });
    vi.spyOn(http, 'apiGet').mockResolvedValue({ publicKey: 'AQID' });
    const post = vi.spyOn(http, 'apiPost').mockResolvedValue(undefined);
    const del = vi.spyOn(http, 'apiDelete').mockResolvedValue(undefined);
    await subscribePush();
    expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: new Uint8Array([1, 2, 3]) });
    expect(post).toHaveBeenCalledWith('/push/subscribe', { endpoint: 'https://push.example/x', keys: { p256dh: 'p', auth: 'a' } });
    await unsubscribePush();
    expect(sub.unsubscribe).toHaveBeenCalled();
    expect(del).toHaveBeenCalledWith('/push/subscribe', { endpoint: 'https://push.example/x' });
  });

  it('throws when permission is denied', async () => {
    vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('denied') });
    await expect(subscribePush()).rejects.toThrow(/blocked/);
  });
});
```

```ts
// frontend/src/push/visibility.test.ts
import { expect, it, vi } from 'vitest';
import { startVisibilityReporter } from './visibility';

it('sends visibility for the current project on change and on reconnect', () => {
  const sent: unknown[] = [];
  let stateCb: (s: string) => void = () => {};
  const ws = { send: (f: unknown) => sent.push(f), onConnection: (cb: (s: string) => void) => { stateCb = cb; return () => {}; } };
  let hidden = false;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => (hidden ? 'hidden' : 'visible'));
  const stop = startVisibilityReporter(() => 'a1b2c3d4e5f6', ws);
  expect(sent).toEqual([{ type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: true }]);
  hidden = true;
  document.dispatchEvent(new Event('visibilitychange'));
  stateCb('resumed');
  expect(sent.slice(1)).toEqual([
    { type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: false },
    { type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: false },
  ]);
  stop();
});

it('sends nothing without a current project', () => {
  const sent: unknown[] = [];
  startVisibilityReporter(() => null, { send: (f: unknown) => sent.push(f), onConnection: () => () => {} })();
  expect(sent).toEqual([]);
});
```

```tsx
// frontend/src/pages/settings/NotificationsSection.test.tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import * as push from '@/push/push';
import { NotificationsSection } from './NotificationsSection';

beforeEach(() => vi.restoreAllMocks());

it('shows the iOS install hint when not standalone', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('unsupported');
  vi.spyOn(push, 'isIos').mockReturnValue(true);
  vi.spyOn(push, 'isStandalone').mockReturnValue(false);
  render(<NotificationsSection />);
  expect(await screen.findByText(/Add to Home Screen/)).toBeInTheDocument();
});

it('explains HTTPS when push is unsupported elsewhere', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('unsupported');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  render(<NotificationsSection />);
  expect(await screen.findByText(/HTTPS/)).toBeInTheDocument();
});

it('subscribes and unsubscribes', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('default');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  vi.spyOn(push, 'currentSubscription').mockResolvedValue(null);
  const sub = vi.spyOn(push, 'subscribePush').mockResolvedValue();
  const unsub = vi.spyOn(push, 'unsubscribePush').mockResolvedValue();
  render(<NotificationsSection />);
  await userEvent.click(await screen.findByRole('button', { name: 'Enable notifications' }));
  expect(sub).toHaveBeenCalled();
  await userEvent.click(await screen.findByRole('button', { name: 'Disable notifications' }));
  expect(unsub).toHaveBeenCalled();
});

it('shows the blocked state', async () => {
  vi.spyOn(push, 'pushSupport').mockReturnValue('denied');
  vi.spyOn(push, 'isIos').mockReturnValue(false);
  vi.spyOn(push, 'currentSubscription').mockResolvedValue(null);
  render(<NotificationsSection />);
  expect(await screen.findByText(/blocked in your browser settings/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/push src/pages/settings`
Expected: FAIL — `Failed to resolve import "./push"` / `"./visibility"`; NotificationsSection test fails on the stub text.

- [ ] **Step 3: Write minimal implementation**

```ts
// frontend/src/push/push.ts
import { apiDelete, apiGet, apiPost } from '@/api/http';

export type PushSupport = 'unsupported' | 'default' | 'granted' | 'denied';

export function urlBase64ToUint8Array(b64url: string): Uint8Array {
  const b64 = (b64url + '='.repeat((4 - (b64url.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

export function isIos(ua: string = navigator.userAgent): boolean {
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes('Macintosh') && 'ontouchend' in globalThis);
}

export function isStandalone(): boolean {
  return window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true;
}

export function pushSupport(w: Window = window): PushSupport {
  if (!('serviceWorker' in w.navigator) || !('PushManager' in w) || !('Notification' in w)) return 'unsupported';
  return (w as Window & { Notification: typeof Notification }).Notification.permission as PushSupport;
}

export async function registerServiceWorker(): Promise<void> {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/sw.js', { scope: '/' });
  } catch (err) {
    console.warn('[push] service worker registration failed', err);
  }
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

export async function subscribePush(): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') throw new Error('Notifications are blocked for this site.');
  const { publicKey } = await apiGet<{ publicKey: string }>('/push/vapid-public-key');
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(publicKey),
  });
  const json = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
  await apiPost('/push/subscribe', { endpoint: json.endpoint, keys: json.keys });
}

export async function unsubscribePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await sub.unsubscribe();
  await apiDelete('/push/subscribe', { endpoint: sub.endpoint });
}
```

(If 6A's `apiDelete(path)` takes no body, extend it to
`apiDelete(path, body?)` sending JSON when `body` is given — a backwards-compatible addition.)

```ts
// frontend/src/push/visibility.ts
import type { ClientFrame } from '@/protocol/frames';

export interface VisibilityWs {
  send(frame: ClientFrame): void;
  onConnection(cb: (state: string) => void): () => void;
}

export function startVisibilityReporter(getProjectId: () => string | null, ws: VisibilityWs): () => void {
  const report = () => {
    const projectId = getProjectId();
    if (projectId) ws.send({ type: 'visibility', projectId, visible: document.visibilityState === 'visible' });
  };
  report();
  document.addEventListener('visibilitychange', report);
  const offConnection = ws.onConnection((s) => { if (s === 'open' || s === 'resumed') report(); });
  return () => { document.removeEventListener('visibilitychange', report); offConnection(); };
}
```

```tsx
// frontend/src/pages/settings/NotificationsSection.tsx
import { useEffect, useState } from 'react';
import { currentSubscription, isIos, isStandalone, pushSupport, subscribePush, unsubscribePush } from '@/push/push';

export function NotificationsSection() {
  const support = pushSupport();
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (support === 'unsupported') return;
    let alive = true;
    currentSubscription().then((s) => { if (alive) setSubscribed(s !== null); }).catch(() => setSubscribed(false));
    return () => { alive = false; };
  }, [support]);

  const run = async (fn: () => Promise<void>, next: boolean) => {
    setBusy(true);
    setError(null);
    try { await fn(); setSubscribed(next); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  let body: JSX.Element;
  if (support === 'unsupported') {
    body = isIos() && !isStandalone()
      ? <p className="text-sm">On iPhone and iPad (iOS 16.4 or later), open the Share menu and choose
          <strong> Add to Home Screen</strong>, then open CopperChat from the Home Screen to enable notifications.</p>
      : <p className="text-sm text-muted">This browser does not offer Web Push here. Push needs HTTPS
          (for example through Cloudflare Tunnel or another HTTPS reverse proxy).</p>;
  } else if (support === 'denied') {
    body = <p className="text-sm">Notifications are blocked in your browser settings for this site.</p>;
  } else {
    body = (
      <div className="space-y-2">
        <p className="text-sm text-muted">Get notified when a run finishes, the agent needs you, or the rate
          limit window reopens — only while no CopperChat window is visible.</p>
        {subscribed
          ? <button type="button" disabled={busy} onClick={() => run(unsubscribePush, false)}
              className="rounded border px-3 py-1">Disable notifications</button>
          : <button type="button" disabled={busy || subscribed === null} onClick={() => run(subscribePush, true)}
              className="rounded bg-[var(--color-accent)] px-3 py-1 text-white">Enable notifications</button>}
      </div>
    );
  }

  return (
    <section className="space-y-2">
      <h2 className="font-medium">Notifications</h2>
      {body}
      {error && <p role="alert" className="text-sm text-[var(--color-destructive)]">{error}</p>}
    </section>
  );
}
```

`frontend/src/main.tsx` — before `createRoot(...)`:

```ts
import { registerServiceWorker } from '@/push/push';
import { startVisibilityReporter } from '@/push/visibility';
import { wsClient } from '@/api/ws';
import { useProjects } from '@/stores/projects';

void registerServiceWorker();
startVisibilityReporter(() => useProjects.getState().current?.id ?? null, wsClient);
```

(`wsClient` must expose `send(frame)` and `onConnection(cb) → unsubscribe`; if 6A
names the state subscription differently, adapt `VisibilityWs` to that name.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm run typecheck && npm run lint && npm test -- --run`
Expected: all green, including the 6A SettingsPage test (it renders the new section; the section heading "Notifications" is unchanged).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/push frontend/src/pages/settings frontend/src/main.tsx frontend/src/api/http.ts
git commit -m "feat: push subscription UI, visibility reporting and SW registration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.6: App image, compose, `.env.example`, token-missing check

**Files:**
- Create: `docker/app/Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `scripts/e2e-token-missing.sh`
- Test: `scripts/e2e-token-missing.sh` (run locally and in CI)

**Interfaces:**
- Consumes: `docker/kicad-base/KICAD_COMMIT`, `ghcr.io/peterus/copperchat-kicad-base:<commit>` (Phase 2), `python -m copperchat` (Phase 3), `GET /api/health`.
- Produces: `ghcr.io/peterus/copperchat:<version>` layout: `/app/backend` (venv at `/app/backend/.venv`), `/app/frontend/dist` (= `Settings.frontend_dist`), user `copperchat` (uid 10001).

- [ ] **Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# scripts/e2e-token-missing.sh IMAGE — the container must refuse to start without model credentials.
set -euo pipefail
image="${1:?usage: e2e-token-missing.sh IMAGE}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/projects" "$tmp/data" && chmod 777 "$tmp/data"
set +e
out="$(docker run --rm -e ADMIN_PASSWORD=x -v "$tmp/projects:/projects:ro" -v "$tmp/data:/data" "$image" 2>&1)"
code=$?
set -e
echo "$out"
if [ "$code" -eq 0 ]; then echo "FAIL: container exited 0 without a token"; exit 1; fi
grep -q "claude setup-token" <<<"$out" || { echo "FAIL: message does not mention 'claude setup-token'"; exit 1; }
echo "OK: exit $code with setup-token hint"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `chmod +x scripts/e2e-token-missing.sh && docker build -f docker/app/Dockerfile --build-arg KICAD_COMMIT=$(cat docker/kicad-base/KICAD_COMMIT) -t copperchat:dev .`
Expected: FAIL — `failed to read dockerfile: open docker/app/Dockerfile: no such file or directory`.

- [ ] **Step 3: Write minimal implementation**

```dockerfile
# docker/app/Dockerfile — build context: repository root
ARG KICAD_COMMIT

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY protocol-fixtures/ /src/protocol-fixtures/
RUN npm run build

FROM ghcr.io/peterus/copperchat-kicad-base:${KICAD_COMMIT}
ARG KICAD_COMMIT
LABEL org.opencontainers.image.source="https://github.com/peterus/copperchat" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="CopperChat — chat with an AI agent about KiCad projects. Powered by Claude." \
      io.copperchat.kicad-commit="${KICAD_COMMIT}"
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/backend/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 \
    PROJECTS_ROOT=/projects DATA_DIR=/data FRONTEND_DIST=/app/frontend/dist HOME=/data
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /src/frontend/dist /app/frontend/dist
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin copperchat \
 && mkdir -p /data /projects && chown copperchat:copperchat /data
USER copperchat
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "copperchat"]
```

(The base image provides Python 3.13 from Debian trixie, which `uv` uses as
the system interpreter; `UV_PYTHON_DOWNLOADS=never` makes a mismatch fail the build.)

```gitignore
# .dockerignore
**/node_modules
**/.venv
frontend/dist
**/__pycache__
.git
projects
data
.env
```

```yaml
# docker-compose.yml
services:
  copperchat:
    image: ghcr.io/peterus/copperchat:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - "8080:8080"
    volumes:
      - ./projects:/projects
      - ./data:/data

  cloudflared:
    image: cloudflare/cloudflared:latest
    profiles: ["tunnel"]
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?set CLOUDFLARE_TUNNEL_TOKEN in .env}
    depends_on:
      - copperchat
```

```dotenv
# .env.example — copy to .env and fill in.
# Model credentials (one of the two). Create a token with:  claude setup-token
# CopperChat offers no login flow of its own; Anthropic's terms apply to your credentials.
CLAUDE_CODE_OAUTH_TOKEN=
# ANTHROPIC_API_KEY=

# CopperChat app login (always on). The password seeds the stored hash on first start.
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me

# Public HTTPS URL (used for push click-through and the VAPID subject contact).
# PUBLIC_BASE_URL=https://copperchat.example.com
# VAPID_SUBJECT=mailto:you@example.com

# Optional: Cloudflare Tunnel (docker compose --profile tunnel up -d)
# CLOUDFLARE_TUNNEL_TOKEN=

# Set when serving over plain http on a LAN (no HTTPS): cookies without the Secure flag.
# COOKIE_SECURE=false

# Development only: run without a model token using the fake agent.
# DEV_FAKE_AGENT=true
```

Note on mount permissions (goes into the README): `./data` must be writable by
uid 10001 (`sudo chown 10001 data`), `./projects` writable too because
autosave and `.kicad-agent/` live inside the projects.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
docker build -f docker/app/Dockerfile --build-arg KICAD_COMMIT=$(cat docker/kicad-base/KICAD_COMMIT) -t copperchat:dev .
bash scripts/e2e-token-missing.sh copperchat:dev
docker compose config -q && docker compose --profile tunnel config -q
```
Expected: image builds; script prints `OK: exit 2 with setup-token hint`; compose config valid (with `CLOUDFLARE_TUNNEL_TOKEN=x` exported for the second command).

- [ ] **Step 5: Commit**

```bash
git add docker/app/Dockerfile .dockerignore docker-compose.yml .env.example scripts/e2e-token-missing.sh
git commit -m "feat: app image, docker compose with optional cloudflared, env example

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.7: CI matrix, Docker e2e smoke, release workflow

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`, `frontend/e2e/smoke-docker.spec.ts`
- Test: the `e2e` CI job itself; locally `E2E_BASE_URL=http://localhost:8080 E2E_PASSWORD=secret npx playwright test e2e/smoke-docker.spec.ts`

**Interfaces:**
- Consumes: `docker/kicad-base/SUPPORTED_COMMITS` (Phase 2; one full commit hash per line, `#` comments allowed), fixture `backend/tests/fixtures/projects/ecc83` (Phase 4), `DEV_FAKE_AGENT` (C1), 6B viewer test id `viewer-slot` / KiCanvas element.
- Produces: CI jobs `kicad` (matrix), `e2e`; release images.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/e2e/smoke-docker.spec.ts — runs against the real app image with DEV_FAKE_AGENT=true
import { expect, test } from '@playwright/test';

test.skip(!process.env.E2E_BASE_URL, 'set E2E_BASE_URL (CI e2e job)');

test('login → open project → send → fake reply → viewer loads', async ({ page }) => {
  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Username').fill(process.env.E2E_USERNAME ?? 'admin');
  await page.getByLabel('Password').fill(process.env.E2E_PASSWORD ?? 'secret');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.getByRole('link', { name: /ecc83-pp/ }).first().click();
  await expect(page).toHaveURL(/\/p\/[0-9a-f]{12}$/);
  await expect(page.getByTestId('viewer-slot')).toBeVisible();
  const viewerReq = page.waitForResponse((r) => /\/api\/projects\/[0-9a-f]{12}\/view\/sch/.test(r.url()) && r.status() === 200);
  await page.getByRole('textbox', { name: 'Message' }).fill('hello');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('hello', { exact: true })).toBeVisible();
  await expect(page.locator('[data-role="assistant"]').last()).toBeVisible({ timeout: 30_000 });
  await viewerReq;
});
```

(`data-role="assistant"` is the attribute 6A's message rows carry; if 6A uses a
different selector for assistant rows, use that one. The viewer request is the
6B KiCanvas source fetch `GET /api/projects/{id}/view/sch`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run -d --name cc-e2e -p 8080:8080 -e DEV_FAKE_AGENT=true -e ADMIN_PASSWORD=secret -e COOKIE_SECURE=false -v $PWD/backend/tests/fixtures/projects:/projects copperchat:dev && cd frontend && E2E_BASE_URL=http://localhost:8080 npx playwright test e2e/smoke-docker.spec.ts`
Expected before Tasks 9.6/9.7 are complete: FAIL (`docker: Unable to find image 'copperchat:dev'` or the job does not exist in CI). After Task 9.6 it must pass locally — the CI wiring below is the deliverable.

- [ ] **Step 3: Write minimal implementation**

`frontend/playwright.config.ts` (6A) — make `baseURL` honour `E2E_BASE_URL`
and skip the dev `webServer` when it is set:

```ts
  use: { baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173' },
  webServer: process.env.E2E_BASE_URL ? undefined : { command: 'npm run dev', port: 5173, reuseExistingServer: true },
```

`.github/workflows/ci.yml` — replace the Phase 4 `kicad` job and add `e2e`:

```yaml
  kicad-commits:
    runs-on: ubuntu-latest
    outputs:
      commits: ${{ steps.list.outputs.commits }}
    steps:
      - uses: actions/checkout@v4
      - id: list
        run: |
          commits=$(grep -Ev '^\s*(#|$)' docker/kicad-base/SUPPORTED_COMMITS | jq -R . | jq -sc .)
          echo "commits=$commits" >> "$GITHUB_OUTPUT"

  kicad:
    needs: kicad-commits
    name: kicad (${{ matrix.commit }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        commit: ${{ fromJson(needs.kicad-commits.outputs.commits) }}
    container:
      image: ghcr.io/peterus/copperchat-kicad-base:${{ matrix.commit }}
    defaults:
      run:
        working-directory: backend
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked
      - run: uv run pytest -m kicad

  e2e:
    runs-on: ubuntu-latest
    needs: [backend, frontend]
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - id: kc
        run: echo "commit=$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)" >> "$GITHUB_OUTPUT"
      - name: Build app image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: docker/app/Dockerfile
          build-args: KICAD_COMMIT=${{ steps.kc.outputs.commit }}
          load: true
          tags: copperchat:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Token missing → non-zero exit with hint
        run: bash scripts/e2e-token-missing.sh copperchat:ci
      - name: Start app with fake agent and the ecc83 fixture
        run: |
          mkdir -p "$RUNNER_TEMP/projects" "$RUNNER_TEMP/data"
          cp -r backend/tests/fixtures/projects/ecc83 "$RUNNER_TEMP/projects/"
          chmod -R a+rwX "$RUNNER_TEMP/projects" "$RUNNER_TEMP/data"
          docker run -d --name cc -p 8080:8080 -e DEV_FAKE_AGENT=true -e ADMIN_PASSWORD=secret \
            -e COOKIE_SECURE=false -v "$RUNNER_TEMP/projects:/projects" -v "$RUNNER_TEMP/data:/data" copperchat:ci
          for i in $(seq 1 60); do curl -fsS http://localhost:8080/api/health && exit 0; sleep 2; done
          docker logs cc; exit 1
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci && npx playwright install --with-deps chromium
        working-directory: frontend
      - run: npx playwright test e2e/smoke-docker.spec.ts
        working-directory: frontend
        env:
          E2E_BASE_URL: http://localhost:8080
          E2E_PASSWORD: secret
      - if: failure()
        run: docker logs cc
```

```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: read
  packages: write

jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: kc
        run: echo "commit=$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)" >> "$GITHUB_OUTPUT"
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/peterus/copperchat
          tags: |
            type=semver,pattern={{version}}
            type=raw,value=latest
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: docker/app/Dockerfile
          build-args: KICAD_COMMIT=${{ steps.kc.outputs.commit }}
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

(`type=semver,pattern={{version}}` turns tag `v1.2.3` into `1.2.3`.)

- [ ] **Step 4: Run tests to verify they pass**

Run locally: `docker build -f docker/app/Dockerfile --build-arg KICAD_COMMIT=$(cat docker/kicad-base/KICAD_COMMIT) -t copperchat:dev . && bash scripts/e2e-token-missing.sh copperchat:dev && docker run -d --name cc-e2e -p 8080:8080 -e DEV_FAKE_AGENT=true -e ADMIN_PASSWORD=secret -e COOKIE_SECURE=false -v $PWD/backend/tests/fixtures/projects:/projects copperchat:dev && curl -fsS --retry 30 --retry-delay 2 --retry-all-errors http://localhost:8080/api/health && (cd frontend && E2E_BASE_URL=http://localhost:8080 npx playwright test e2e/smoke-docker.spec.ts); docker rm -f cc-e2e`
Expected: health JSON `{"status":"ok",...}`, 1 Playwright test passed. Then push and confirm CI jobs `kicad (<commit>)` per supported commit and `e2e` are green.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/release.yml frontend/e2e/smoke-docker.spec.ts frontend/playwright.config.ts
git commit -m "ci: kicad matrix over supported commits, docker e2e smoke, release workflow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9.8: README, deployment and architecture docs

**Files:**
- Modify: `README.md`
- Create: `docs/deployment/cloudflare.md`, `docs/architecture.md`
- Test: `scripts/check-docs.sh` (created here, run in the CI `backend` job as an extra step)

**Interfaces:**
- Consumes: everything above (names, env vars, compose profile).
- Produces: user-facing docs.

- [ ] **Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# scripts/check-docs.sh — guard compliance statements in user-facing docs (spec §12).
set -euo pipefail
fail() { echo "FAIL: $*"; exit 1; }
for f in README.md docs/deployment/cloudflare.md docs/architecture.md; do [ -f "$f" ] || fail "$f missing"; done
grep -q "claude setup-token" README.md || fail "README: setup-token quickstart"
grep -q "Powered by Claude" README.md || fail "README: Powered by Claude"
grep -qi "not Claude Code" README.md || fail "README: not Claude Code statement"
grep -q "Anthropic's terms" README.md || fail "README: Anthropic terms statement"
grep -q "GPL-3.0-only" README.md || fail "README: license"
grep -q "docker compose up" README.md || fail "README: compose quickstart"
grep -qiE '!\[[^]]*\]\(' README.md && fail "README: no screenshots in the foundation"
grep -q "16.4" docs/deployment/cloudflare.md || fail "cloudflare.md: iOS 16.4"
grep -q "Cloudflare Access" docs/deployment/cloudflare.md || fail "cloudflare.md: Access"
echo OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash scripts/check-docs.sh`
Expected: FAIL — `FAIL: docs/deployment/cloudflare.md missing`.

- [ ] **Step 3: Write minimal implementation**

`README.md` (full replacement):

````markdown
# CopperChat

Chat with an AI agent about your KiCad projects — in the browser, on desktop,
iPad and iPhone. CopperChat runs KiCad 11 headless, shows your schematic and
board, and lets a read-only agent inspect the design, run ERC/DRC and look at
rendered views. **Powered by Claude.**

CopperChat is an independent open-source project. It is **not Claude Code**
and is not affiliated with or endorsed by Anthropic.

## Quickstart

1. Create a model token with your own Claude subscription:
   ```bash
   claude setup-token
   ```
   (or use an `ANTHROPIC_API_KEY`). CopperChat offers no login flow of its
   own; Anthropic's terms apply to the credentials you use with it.
2. Create a directory with `docker-compose.yml` and `.env.example` from this
   repository, then:
   ```bash
   cp .env.example .env        # paste the token, set ADMIN_PASSWORD
   mkdir -p projects data && sudo chown 10001 data projects
   docker compose up -d
   ```
3. Put KiCad projects (any depth) into `./projects`, open
   http://localhost:8080 and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

The container refuses to start without a token and tells you to run
`claude setup-token`.

## Remote access and notifications

Expose CopperChat through any HTTPS reverse proxy. A ready-made option is
Cloudflare Tunnel + Cloudflare Access: set `CLOUDFLARE_TUNNEL_TOKEN` in `.env`
and run `docker compose --profile tunnel up -d` — see
[docs/deployment/cloudflare.md](docs/deployment/cloudflare.md). Web Push needs
HTTPS; on iPhone/iPad install the app to the Home Screen first (iOS 16.4+).

## What the foundation does

- Project picker over `/projects`, KiCad lock detection (read-only while the
  project is open in the KiCad GUI).
- Schematic and board viewer (KiCanvas) plus annotated agent views.
- Chat with queueing, Stop, resume after rate limits, autosave and per-turn undo.
- Skills, a part-research helper storing datasheets under `docs/parts/`,
  image/PDF attachments, git status/commit on request.

The agent never edits schematics or boards in this release.

## Configuration

| Variable | Meaning |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | model credentials (one required) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | app login (always on) |
| `PUBLIC_BASE_URL`, `VAPID_SUBJECT` | public URL / push contact |
| `CLOUDFLARE_TUNNEL_TOKEN` | optional `cloudflared` service (`--profile tunnel`) |
| `COOKIE_SECURE=false` | only for plain-http LAN use |

Mounts: `/projects` (your KiCad projects) and `/data` (database, agent
sessions, user skills, password hash, push subscriptions, VAPID keys).

## Development

See [docs/architecture.md](docs/architecture.md). Backend: `cd backend && uv
sync && uv run pytest -m "not kicad"`. Frontend: `cd frontend && npm ci &&
npm run dev`. `DEV_FAKE_AGENT=true` runs without a model token.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
````

`docs/deployment/cloudflare.md`:

````markdown
# Remote access with Cloudflare Tunnel and Cloudflare Access

CopperChat listens on plain HTTP port 8080. For use outside your LAN put it
behind HTTPS. **Any HTTPS reverse proxy works** (Caddy, Traefik, nginx); this
page describes Cloudflare Tunnel because it needs no open ports. Web Push and
installing the app on iOS both require HTTPS.

## 1. Create the tunnel

1. Cloudflare Zero Trust dashboard → Networks → Tunnels → Create a tunnel →
   type *Cloudflared*, name it `copperchat`.
2. Copy the tunnel token into `.env`: `CLOUDFLARE_TUNNEL_TOKEN=...`.
3. Add a public hostname, e.g. `copperchat.example.com`, service
   `http://copperchat:8080` (the compose service name).
4. Start: `docker compose --profile tunnel up -d`.

WebSockets work through the tunnel without extra settings.

## 2. Protect it with Cloudflare Access

CopperChat always has its own login, but a second factor in front of it is
recommended:

1. Zero Trust → Access → Applications → Add → *Self-hosted*, domain
   `copperchat.example.com`.
2. Policy: *Allow*, include your e-mail address (one-time PIN or your IdP).
3. Session duration: e.g. 1 month, so the installed PWA does not ask daily.

Set `PUBLIC_BASE_URL=https://copperchat.example.com` and
`VAPID_SUBJECT=mailto:you@example.com` in `.env`.

## 3. Install the app and enable notifications

- Desktop Chrome/Edge: install icon in the address bar.
- Android: browser menu → *Install app*.
- iPhone/iPad (**iOS 16.4 or later**): Safari → Share → *Add to Home Screen*,
  then open CopperChat from the Home Screen. Push only works in the installed app.

Then Settings → Notifications → *Enable notifications*. You get a
notification when a run finishes, when the agent needs your attention (error
or question), and when a rate-limit window reopens — only while no
CopperChat window is visible.
````

`docs/architecture.md`:

````markdown
# CopperChat architecture

## Images

- `ghcr.io/peterus/copperchat-kicad-base:<commit>` — KiCad 11 built from source
  at the commit in `docker/kicad-base/KICAD_COMMIT` (workflow `kicad-base.yml`).
  `docker/kicad-base/SUPPORTED_COMMITS` lists every base tag CI tests against.
- `ghcr.io/peterus/copperchat:<version>` — `docker/app/Dockerfile`: base image +
  Python backend (uv, `/app/backend/.venv`) + built frontend (`/app/frontend/dist`).
  Built by `release.yml` on tags `v*`; runs `python -m copperchat` as user
  `copperchat` (uid 10001).

## Backend (`backend/src/copperchat`)

| Module | Role |
|---|---|
| `config` | `Settings` from env/.env; startup fails without model credentials (`claude setup-token` hint) unless `DEV_FAKE_AGENT` |
| `db` | SQLite (WAL) + numbered migrations `0001`–`0005` |
| `projects` | discovery of `*.kicad_pro`, ids, KiCad lock detection |
| `kicad_session` | one `kicad-cli api-server` per open project, restart on hang/crash |
| `tools`, `kicad_tools`, `views` | read-only agent tools (structured JSON, full UUIDs, mm), ERC/DRC, agent views |
| `agent` | run engine per project: streaming input, queue, interrupt, resume, rate limit, message persistence, WebSocket protocol |
| `history`, `git_tools` | per-turn snapshots/undo, autosave, external-change detection, git status/diff/commit |
| `skills`, `research` | built-in + user + project skills; `part-researcher` one-shot query writing only to `docs/parts/` |
| `push` | VAPID keys in `/data/vapid.json`, subscriptions, notifier on run-finished / rate-limit frames |
| `web` | FastAPI app, login cookie, REST routes, `/api/ws`, static frontend |

Design data is read only through the running API server (or `kicad-cli` for
ERC/DRC/exports) — never by a CopperChat file parser. The main agent has no
file, shell or web tools.

## Frontend (`frontend/src`)

React 19 + Vite + zustand + zod. `protocol/frames.ts` mirrors the backend
protocol; `protocol-fixtures/*.json` are validated by both test suites. The
chat store merges the REST baseline and the WS replay by server id.
`public/sw.js` handles push only and never caches.

## Data flow of a turn

Composer → `send` frame → run engine queue → SDK streaming input → tool calls
through the in-process MCP server → messages persisted and broadcast →
turn end: autosave, snapshot, `view-updated` → viewer reload; push if no client
is visible.

## CI

`ci.yml`: `backend` (ruff, mypy, pytest without KiCad), `frontend` (typecheck,
lint, vitest, build), `kicad` (matrix over `SUPPORTED_COMMITS`, inside the base
image), `e2e` (app image with `DEV_FAKE_AGENT=true` and the `ecc83` fixture,
health check, Playwright happy path, token-missing check).
````

`.github/workflows/ci.yml` — in the `backend` job add a step (working
directory repo root): `- run: bash scripts/check-docs.sh` with
`working-directory: .`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `bash scripts/check-docs.sh`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/deployment/cloudflare.md docs/architecture.md scripts/check-docs.sh .github/workflows/ci.yml
git commit -m "docs: README quickstart, Cloudflare deployment and architecture overview

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

Spec coverage:
- §9 Notifications: VAPID (9.1), events + visibility gating + click target (9.2, 9.3, 9.4), HTTPS/iOS ≥ 16.4 (9.5 UI, 9.8 docs).
- §3 Deployment / Auth (model) / Remote access rows: compose, `.env`, optional `cloudflared`, any HTTPS proxy (9.6, 9.8).
- §4 image layers + mounts (9.6, docs 9.8); §10 missing token → startup error (9.6 script, 9.7 CI).
- §11 CI matrix over supported `kicad-base` tags + smoke (9.7).
- §12 naming/branding, "Powered by Claude", not Claude Code, no login flow, Anthropic's terms, GPL-3.0-only (9.8, guarded by `check-docs.sh`).

Contract additions (to be added to the index C7a): `visibility` frame
(C→S, fixture `protocol-fixtures/visibility.json`), `Settings.vapid_subject`,
`Broadcaster.add_listener`, `app.state.push` (`PushServices`), `/data/vapid.json`
format `{publicKey, privatePem}`, push payload `{title, body, url, tag}`.

Deviations / decisions:
- "No client visible" is global (any visible connection suppresses), matching
  "no client of that user" with one user per instance.
- "Needs attention" = `run-finished` reason `error` or final assistant text
  ending in `?`; `interrupted` and `rate_limited` finishes send nothing (the
  reopen notification covers the latter).
- VAPID keys are generated and persisted instead of Kontor's env variables.
- The service worker accepts only `/p/<12-hex>` click targets (open-redirect guard).
