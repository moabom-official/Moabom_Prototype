"""/scope-classify — batch classify videos as comparison(1) / non-comparison(0).

Backed by the scope-classifier klue/roberta-large checkpoint loaded once on
first call. Same Bearer auth as /transcript.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.fetch_worker.auth import require_bearer
from services.fetch_worker.scope_logic import MODEL_VERSION, classify_batch

MAX_ITEMS = 50

router = APIRouter(dependencies=[Depends(require_bearer)])


class ScopeItem(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)
    title: str = ""
    description: str = ""


class ScopeRequest(BaseModel):
    items: list[ScopeItem] = Field(default_factory=list)


class ScopeResultOut(BaseModel):
    video_id: str
    label: int
    confidence: float
    latency_ms: int


class ScopeResponse(BaseModel):
    results: list[ScopeResultOut]
    model_version: str
    total_latency_ms: int


@router.post("/scope-classify", response_model=ScopeResponse)
def scope_classify(req: ScopeRequest) -> ScopeResponse:
    if len(req.items) > MAX_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Too many items: {len(req.items)} > {MAX_ITEMS}",
        )
    t0 = time.perf_counter()
    try:
        raw = classify_batch(item.model_dump() for item in req.items)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    total_ms = int((time.perf_counter() - t0) * 1000)
    return ScopeResponse(
        results=[
            ScopeResultOut(
                video_id=r.video_id,
                label=r.label,
                confidence=r.confidence,
                latency_ms=r.latency_ms,
            )
            for r in raw
        ],
        model_version=MODEL_VERSION,
        total_latency_ms=total_ms,
    )
