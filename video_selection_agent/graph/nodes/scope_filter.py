"""Node 4b: 비교/랭킹 영상 hard filter (rank=-1 마킹).

diversity_filter 직후, llm_rerank 직전. 살아남은(rank>0) 후보의 title +
description 을 데스크탑 worker `/scope-classify` 로 batch 분류한다.
`label=1` (comparison) 이고 confidence 가 임계치 이상이면 rank=-1 로 마킹.

finalize_selection 의 fallback 은 `rank == 0` 후보만 부활시키므로
rank=-1 은 자동 영구 제외 — 별도 코드 수정 불필요.

운영 안전망:
  - SCOPE_FILTER_ENABLED=0 → 즉시 skip
  - SCOPE_WORKER_URL 미설정 / worker 502·timeout → client.classify_videos가 None →
    rank 손대지 않고 trace 만 남김 (전체 통과)
"""
from __future__ import annotations

import os
import time

from video_selection_agent.graph.state import SelectionState
from video_selection_agent.scope_filter import classify_videos


def _is_enabled() -> bool:
    return os.environ.get("SCOPE_FILTER_ENABLED", "1") != "0"


def _min_confidence() -> float:
    try:
        return float(os.environ.get("SCOPE_MIN_CONFIDENCE", "0.7"))
    except ValueError:
        return 0.7


def scope_filter(state: SelectionState) -> SelectionState:
    trace = list(state.get("trace", []))

    if not _is_enabled():
        trace.append("scope_filter: disabled (SCOPE_FILTER_ENABLED=0)")
        return {**state, "trace": trace}

    scores = state.get("scores", {})
    candidates = {c.video_id: c for c in state.get("candidates", [])}

    eligible = [s for s in scores.values() if s.rank > 0]
    if not eligible:
        trace.append("scope_filter: no eligible candidates — skip")
        return {**state, "trace": trace}

    items = []
    for sb in eligible:
        c = candidates.get(sb.video_id)
        if c is None:
            continue
        items.append(
            {
                "video_id": sb.video_id,
                "title": c.title or "",
                "description": (c.description or "")[:2000],
            }
        )

    if not items:
        trace.append("scope_filter: no candidates with metadata — skip")
        return {**state, "trace": trace}

    t0 = time.perf_counter()
    results = classify_videos(items)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if results is None:
        trace.append(
            f"scope_filter: worker unavailable — pass-through {len(items)} candidates "
            f"({elapsed_ms}ms wall)"
        )
        return {**state, "trace": trace}

    min_conf = _min_confidence()
    blocked = 0
    by_id = {r.get("video_id"): r for r in results}
    for sb in eligible:
        r = by_id.get(sb.video_id)
        if r is None:
            continue
        label = int(r.get("label", 0))
        confidence = float(r.get("confidence", 0.0))
        per_latency = int(r.get("latency_ms", 0))
        sb.extras["scope_label"] = label
        sb.extras["scope_confidence"] = round(confidence, 4)
        sb.extras["scope_latency_ms"] = per_latency
        if label == 1 and confidence >= min_conf:
            sb.rank = -1
            blocked += 1

    trace.append(
        f"scope_filter: {len(items)} classified, {blocked} blocked "
        f"(min_conf={min_conf:.2f}, worker={elapsed_ms}ms)"
    )
    return {**state, "scores": scores, "trace": trace}
