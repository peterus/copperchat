"""WebSocket /api/ws: cookie auth, Origin check, schema validation, replay+subscribe,
heartbeat (index contract C7).

One connection may subscribe to several projects (the chat store re-subscribes every
loaded chat after a reconnect); subscribing to a project again replaces that project's
subscription. Close codes: 4401 unauthenticated, 4403 foreign Origin, 4408 slow
subscriber dropped (the client reconnects and replays with ``sinceMessageId``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnected

from copperchat.agent.broadcaster import Subscription
from copperchat.agent.engine import RunEngine, UnknownAttachment
from copperchat.agent.protocol import (
    CancelQueuedFrame,
    ClientFrame,
    ErrorFrame,
    InterruptFrame,
    MessageFrame,
    PingFrame,
    PongFrame,
    RateLimitedFrame,
    RunStartedFrame,
    SendFrame,
    ServerPingFrame,
    SubscribeFrame,
    Wire,
    client_frame_adapter,
    dump_frame,
)
from copperchat.errors import CopperChatError
from copperchat.projects import get_project
from copperchat.web import auth

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

HEARTBEAT_S = 25.0
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_SLOW_SUBSCRIBER = 4408

_PID = re.compile(r"^[0-9a-f]{12}$")
# Module attribute (looked up at call time) so tests can patch it.
authenticate_connection = auth.authenticate_connection
# Errors raised by a send/receive on a socket that is gone: end the connection quietly.
_GONE = (WebSocketDisconnect, WebSocketDisconnected, OSError)


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _host_port(netloc: str, scheme: str | None) -> str:
    """Lower-case ``host[:port]``, with the scheme's default port dropped."""
    parts = urlsplit(f"//{netloc.strip()}")
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    try:
        port = parts.port
    except ValueError:
        return ""
    if port is None or (scheme is not None and _DEFAULT_PORTS.get(scheme) == port):
        return host
    return f"{host}:{port}"


def _origin_ok(ws: WebSocket, public_base_url: str | None) -> bool:
    """CSWSH guard. Browsers always send Origin on a WebSocket upgrade (and page script
    cannot forge it or the Host/X-Forwarded-Host headers); it must name this server as
    seen by the browser: the Host header, a proxy's X-Forwarded-Host, or the configured
    ``public_base_url``."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True  # not a browser; the session cookie is still required
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False  # e.g. "null" from sandboxed frames or file:// pages
    want = _host_port(parts.netloc, parts.scheme)
    candidates = [ws.headers.get("host", "")]
    forwarded = ws.headers.get("x-forwarded-host")
    if forwarded:
        candidates.append(forwarded.split(",")[0])
    allowed = {_host_port(c, parts.scheme) for c in candidates if c}
    if public_base_url:
        base = urlsplit(public_base_url)
        if base.netloc:
            allowed.add(_host_port(base.netloc, base.scheme or None))
    return bool(want) and want in allowed


def _str_field(data: Any, key: str) -> str | None:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


class _Connection:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.runtime: Any = ws.app.state.agent_runtime
        self.settings: Any = ws.app.state.settings
        self._send_lock = asyncio.Lock()
        self._subs: dict[str, tuple[Subscription, asyncio.Task[None] | None]] = {}

    async def send(self, frame: Wire) -> None:
        async with self._send_lock:
            await self.ws.send_text(dump_frame(frame))

    async def close_socket(self, code: int) -> None:
        async with self._send_lock:
            await self.ws.close(code=code)

    async def engine(self, project_id: str) -> RunEngine:
        engine: RunEngine | None = self.runtime.get(project_id)
        if engine is None:
            project = get_project(self.settings.projects_root, project_id)
            engine = await self.runtime.ensure(project)
        return engine

    def _drop(self, project_id: str) -> asyncio.Task[None] | None:
        entry = self._subs.pop(project_id, None)
        if entry is None:
            return None
        sub, task = entry
        sub.close()
        if task is not None:
            task.cancel()
        return task

    async def close(self) -> None:
        tasks = [t for pid in list(self._subs) if (t := self._drop(pid)) is not None]
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def subscribe(self, frame: SubscribeFrame) -> None:
        pid = frame.project_id
        self._drop(pid)
        engine = await self.engine(pid)
        # Snapshot and subscription are atomic w.r.t. publishes: live frames queue up in
        # `sub` while the replay is sent and are forwarded afterwards (no gap/duplicate).
        dtos, sub, run_id = await engine.replay_and_subscribe(frame.since_message_id)
        self._subs[pid] = (sub, None)  # registered first: close() also covers a failed replay
        try:
            for dto in dtos:
                await self.send(MessageFrame(project_id=pid, message=dto))
            if run_id is not None:  # a reconnecting client keeps its Stop button
                await self.send(RunStartedFrame(project_id=pid, run_id=run_id))
            resets_at = engine.rate_limited_until
            if resets_at is not None:  # parked: the client shows the countdown again
                await self.send(RateLimitedFrame(project_id=pid, resets_at=resets_at.isoformat()))
        except BaseException:
            if self._subs.get(pid, (None,))[0] is sub:
                self._drop(pid)  # never leave a subscription without a forwarder
            raise
        if self._subs.get(pid, (None,))[0] is sub:
            self._subs[pid] = (sub, asyncio.create_task(self._forward(sub)))

    async def _forward(self, sub: Subscription) -> None:
        try:
            async for frame in sub:
                await self.send(frame)
            if sub.dropped:
                await self.close_socket(CLOSE_SLOW_SUBSCRIBER)
        except _GONE:
            sub.close()  # the receive loop notices the disconnect and cleans up

    async def handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send(
                ErrorFrame(project_id=None, code="invalid_frame", message="frame is not valid JSON")
            )
            return
        try:
            frame: ClientFrame = client_frame_adapter.validate_python(data)
        except ValidationError as exc:
            pid = _str_field(data, "projectId")
            await self.send(
                ErrorFrame(
                    project_id=pid if pid is not None and _PID.fullmatch(pid) else None,
                    code="invalid_frame",
                    message=str(exc.errors()[0]["msg"]),
                    client_message_id=_str_field(data, "clientMessageId"),
                )
            )
            return
        if isinstance(frame, PingFrame):
            await self.send(PongFrame(project_id=None))
            return
        cmid = frame.client_message_id if isinstance(frame, SendFrame) else None
        try:
            await self._dispatch(frame)
        except UnknownAttachment as exc:
            await self.send(
                ErrorFrame(
                    project_id=frame.project_id,
                    code="unknown_attachment",
                    message=exc.message,
                    client_message_id=cmid,
                )
            )
        except CopperChatError as exc:
            await self.send(
                ErrorFrame(
                    project_id=frame.project_id,
                    code=exc.code,
                    message=exc.message,
                    client_message_id=cmid,
                )
            )
        except _GONE:
            raise
        except Exception:
            log.exception("websocket frame %s for %s failed", frame.type, frame.project_id)
            await self.send(
                ErrorFrame(
                    project_id=frame.project_id,
                    code="internal_error",
                    message="internal error",
                    client_message_id=cmid,
                )
            )

    async def _dispatch(
        self, frame: SubscribeFrame | SendFrame | InterruptFrame | CancelQueuedFrame
    ) -> None:
        if isinstance(frame, SubscribeFrame):
            await self.subscribe(frame)
        elif isinstance(frame, SendFrame):
            engine = await self.engine(frame.project_id)
            await engine.submit(frame.client_message_id, frame.text, list(frame.attachments))
        elif isinstance(frame, InterruptFrame):
            await (await self.engine(frame.project_id)).interrupt(frame.run_id)
        elif not await (await self.engine(frame.project_id)).cancel_queued(frame.message_id):
            await self.send(
                ErrorFrame(
                    project_id=frame.project_id,
                    code="not_cancellable",
                    message="message already started or finished",
                )
            )


async def _session_alive(ws: WebSocket) -> bool:
    """Re-check the upgrade request's session cookie against auth_sessions (logout)."""
    return await authenticate_connection(ws) is not None


async def _heartbeat(conn: _Connection) -> None:
    with contextlib.suppress(*_GONE):
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            if not await _session_alive(conn.ws):
                await conn.close_socket(CLOSE_UNAUTHENTICATED)
                return
            await conn.send(ServerPingFrame(project_id=None))


@router.websocket("/ws")
async def chat_ws(ws: WebSocket) -> None:
    if not _origin_ok(ws, ws.app.state.settings.public_base_url):
        await ws.close(code=CLOSE_FORBIDDEN_ORIGIN)
        return
    if await authenticate_connection(ws) is None:
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return
    await ws.accept()
    conn = _Connection(ws)
    heartbeat = asyncio.create_task(_heartbeat(conn))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if not await _session_alive(ws):  # logged out since the upgrade
                await conn.close_socket(CLOSE_UNAUTHENTICATED)
                break
            text = msg.get("text")
            if text is None:
                await conn.send(
                    ErrorFrame(
                        project_id=None, code="invalid_frame", message="binary frames unsupported"
                    )
                )
                continue
            await conn.handle(text)
    except _GONE:
        pass
    finally:
        heartbeat.cancel()
        await conn.close()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
