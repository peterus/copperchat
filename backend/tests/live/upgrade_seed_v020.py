"""Seeds a running v0.2.0 server for the upgrade check (C6 Task 1, Review Focus 2): logs in,
sends one chat message that asks the agent to remember a code word, and waits for the
run to finish. Used by ``scripts/live-upgrade-check.sh``; speaks the v0.2.0 protocol (no
conversation ids).

    uv run python -m tests.live.upgrade_seed_v020 <base-url> <admin-password> <code-word>
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
import websockets


async def seed(base: str, password: str, word: str) -> str:
    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        r = await client.post("/api/auth/login", json={"username": "admin", "password": password})
        r.raise_for_status()
        [project] = (await client.get("/api/projects")).json()
        cookie = client.cookies["copperchat_session"]
    pid = project["id"]
    url = base.replace("http", "ws", 1) + "/api/ws"
    async with websockets.connect(
        url, additional_headers={"Cookie": f"copperchat_session={cookie}"}
    ) as ws:
        await ws.send(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
        text = f"Remember this code word for later: {word}. Reply only with OK."
        await ws.send(
            json.dumps(
                {
                    "type": "send",
                    "projectId": pid,
                    "clientMessageId": uuid.uuid4().hex,
                    "text": text,
                }
            )
        )
        async with asyncio.timeout(600):
            while True:
                frame = json.loads(await ws.recv())
                if frame["type"] == "error":
                    raise RuntimeError(f"server error frame: {frame}")
                if frame["type"] == "run-finished":
                    return str(frame["reason"])


if __name__ == "__main__":
    reason = asyncio.run(seed(*sys.argv[1:4]))
    print(f"v0.2.0 run finished: {reason}")
    sys.exit(0 if reason == "completed" else 1)
