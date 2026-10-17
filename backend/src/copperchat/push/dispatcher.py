"""Send one payload to every stored subscription; drop subscriptions the push service
reports as gone (404/410). Ported from Kontor server/pushDispatcher.ts (sendToAll)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from copperchat.push.store import PushStore
from copperchat.push.vapid import VapidKeys

log = logging.getLogger(__name__)

# (subscription_info, data) -> None; raises PushSendError. Runs in a worker thread.
Sender = Callable[[dict[str, Any], str], None]
PUSH_TTL_S = 3600
PUSH_TIMEOUT_S = 10.0


class PushSendError(Exception):
    def __init__(self, status: int | None) -> None:
        super().__init__(f"push send failed (status {status})")
        self.status = status


def webpush_sender(keys: VapidKeys, subject: str) -> Sender:
    vapid = Vapid02.from_pem(keys.private_pem.encode())

    def send(sub: dict[str, Any], data: str) -> None:
        try:
            webpush(
                subscription_info=sub,
                data=data,
                vapid_private_key=vapid,
                # A fresh dict per call: webpush() writes the endpoint's "aud" into it.
                vapid_claims={"sub": subject},
                ttl=PUSH_TTL_S,
                timeout=PUSH_TIMEOUT_S,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            raise PushSendError(status) from exc
        except OSError as exc:  # requests' ConnectionError/Timeout are OSErrors
            raise PushSendError(None) from exc

    return send


class PushDispatcher:
    def __init__(
        self, store: PushStore, keys: VapidKeys, subject: str, *, sender: Sender | None = None
    ) -> None:
        self.store = store
        self.sender = sender or webpush_sender(keys, subject)

    async def send_all(self, payload: dict[str, Any]) -> int:
        """Returns the number of subscriptions the payload was delivered to."""
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
            except Exception:
                log.exception("push send failed for subscription %s", sub.id)
        return ok
