"""Inbound payment provider notifications. CSRF-exempt; authenticity comes from provider signatures."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.payments import handle_webhook
from app.web.deps import rate_limit

router = APIRouter(prefix="/webhooks")
MAX_WEBHOOK_BYTES = 256_000


@router.post("/{channel_code}")
async def provider_webhook(
    request: Request, channel_code: str, db: Session = Depends(get_db)
) -> PlainTextResponse:
    rate_limit(request, "webhook", 300, 60)
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return PlainTextResponse("too large", status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    from starlette.concurrency import run_in_threadpool

    status, text = await run_in_threadpool(
        handle_webhook, db, channel_code[:40], headers, body, request.state.client_ip
    )
    return PlainTextResponse(text, status_code=status)
