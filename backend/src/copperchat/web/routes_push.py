"""Web Push routes (C8): VAPID public key, subscribe, unsubscribe."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from copperchat.push.dispatcher import EndpointCheck, PushDispatcher
from copperchat.push.endpoints import push_endpoint_refusal
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
    check_endpoint: EndpointCheck = push_endpoint_refusal


class SubKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class SubscribeBody(BaseModel):
    endpoint: str = Field(pattern=r"^https://", max_length=2000)
    keys: SubKeys


class UnsubscribeBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2000)


router = APIRouter(prefix="/api/push", tags=["push"], dependencies=[Depends(require_user)])


def _push(request: Request) -> PushServices:
    services: PushServices = request.app.state.push
    return services


@router.get("/vapid-public-key")
async def vapid_public_key(request: Request) -> dict[str, str]:
    return {"publicKey": _push(request).keys.public_key_b64url}


@router.post("/subscribe", status_code=204)
async def subscribe(body: SubscribeBody, request: Request) -> Response:
    push = _push(request)
    # The server later POSTs to this URL: browser push services on public hosts only (SSRF).
    refusal = await push.check_endpoint(body.endpoint)
    if refusal is not None:
        raise HTTPException(status_code=422, detail=f"push endpoint refused: {refusal}")
    await push.store.upsert(body.endpoint, body.keys.p256dh, body.keys.auth)
    return Response(status_code=204)


@router.delete("/subscribe", status_code=204)
async def unsubscribe(body: UnsubscribeBody, request: Request) -> Response:
    await _push(request).store.delete(body.endpoint)
    return Response(status_code=204)
