"""scope_filter 노드 오프라인 회귀.

worker 호출은 모두 monkeypatch — 네트워크/DB/LLM 0.

검증:
1. SCOPE_FILTER_ENABLED=0 → 노드가 즉시 skip, rank 보존.
2. SCOPE_WORKER_URL 미설정 → client 가 None 반환 → 노드는 trace 만 남기고 pass-through.
3. worker 응답이 label=1, conf 임계 이상 → 해당 video rank=-1, extras 채워짐.
4. worker 응답이 label=1 이지만 confidence 가 임계 미만 → rank 보존, extras 만 기록.
5. finalize_selection 의 rank=0 fallback 이 rank=-1 후보를 살리지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import importlib

import pytest

from video_selection_agent.core.models import ScoreBreakdown, VideoCandidate
from video_selection_agent.graph.nodes import finalize_selection as finalize_mod
from video_selection_agent.graph.nodes import scope_filter as scope_filter_mod
from video_selection_agent.scope_filter import client as scope_client

# `nodes/__init__.py` re-exports the function under the same name as the
# submodule, so attribute lookup on `nodes.scope_filter` returns the function.
# Resolve the actual module object via importlib so monkeypatch can rewrite
# `classify_videos` on it.
scope_filter_module = importlib.import_module(
    "video_selection_agent.graph.nodes.scope_filter"
)


def _make_candidate(vid: str, title: str = "", description: str = "") -> VideoCandidate:
    return VideoCandidate(
        video_id=vid,
        title=title or f"video {vid}",
        description=description,
        channel_id=f"ch-{vid}",
        channel_name=f"channel {vid}",
        published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
        view_count=10000,
        like_count=100,
        comment_count=10,
        channel_subscriber_count=50000,
        thumbnail_url="",
    )


def _make_score(vid: str, final_score: float = 0.5, rank: int = 1) -> ScoreBreakdown:
    return ScoreBreakdown(
        video_id=vid,
        final_score=final_score,
        dimensions={"relevance": 0.5},
        weighted_contributions={"relevance": 0.5},
        rank=rank,
    )


def _make_state(candidates, scores, *, k_min: int = 3, k_max: int = 5):
    from video_selection_agent.core.policy import SelectionPolicyConfig

    return {
        "candidates": candidates,
        "scores": {s.video_id: s for s in scores},
        "policy": SelectionPolicyConfig(k_min=k_min, k_max=k_max),
        "trace": [],
    }


def test_disabled_via_env_skips_immediately(monkeypatch):
    monkeypatch.setenv("SCOPE_FILTER_ENABLED", "0")
    candidates = [_make_candidate("a"), _make_candidate("b")]
    scores = [_make_score("a"), _make_score("b")]
    state = _make_state(candidates, scores)

    called = {"n": 0}

    def fake_classify(items):
        called["n"] += 1
        return [{"video_id": i["video_id"], "label": 1, "confidence": 0.99, "latency_ms": 10} for i in items]

    monkeypatch.setattr(scope_filter_module, "classify_videos", fake_classify)
    out = scope_filter_mod(state)

    assert called["n"] == 0, "disabled 시 worker 호출 금지"
    assert all(out["scores"][vid].rank == 1 for vid in ("a", "b"))
    assert any("disabled" in line for line in out["trace"])


def test_worker_unconfigured_returns_none_then_passthrough(monkeypatch):
    """SCOPE_WORKER_URL 미설정 → client 가 None → 노드는 trace 만 남기고 통과."""
    monkeypatch.delenv("SCOPE_WORKER_URL", raising=False)
    monkeypatch.delenv("SCOPE_WORKER_TOKEN", raising=False)

    # client 단위
    assert scope_client.classify_videos([{"video_id": "a", "title": "t"}]) is None

    # 노드 단위 — env 누락 상태로 실제 client 가 None 반환하게 두기
    monkeypatch.setenv("SCOPE_FILTER_ENABLED", "1")
    candidates = [_make_candidate("a")]
    scores = [_make_score("a")]
    state = _make_state(candidates, scores)
    out = scope_filter_mod(state)

    assert out["scores"]["a"].rank == 1, "worker 부재 시 rank 보존"
    assert "scope_label" not in out["scores"]["a"].extras
    assert any("pass-through" in line for line in out["trace"])


def test_high_confidence_comparison_gets_blocked(monkeypatch):
    monkeypatch.setenv("SCOPE_FILTER_ENABLED", "1")
    monkeypatch.setenv("SCOPE_MIN_CONFIDENCE", "0.7")

    candidates = [
        _make_candidate("cmp", "아이폰 16 vs 갤럭시 S24 비교"),
        _make_candidate("rev", "M4 맥북 프로 리뷰"),
    ]
    scores = [_make_score("cmp"), _make_score("rev")]
    state = _make_state(candidates, scores)

    def fake_classify(items):
        out = []
        for it in items:
            if it["video_id"] == "cmp":
                out.append({"video_id": "cmp", "label": 1, "confidence": 0.95, "latency_ms": 120})
            else:
                out.append({"video_id": it["video_id"], "label": 0, "confidence": 0.91, "latency_ms": 110})
        return out

    monkeypatch.setattr(scope_filter_module, "classify_videos", fake_classify)
    out = scope_filter_mod(state)

    cmp = out["scores"]["cmp"]  # noqa: A001 — local var only
    rev = out["scores"]["rev"]
    assert cmp.rank == -1
    assert rev.rank == 1
    assert cmp.extras["scope_label"] == 1
    assert cmp.extras["scope_confidence"] == pytest.approx(0.95, abs=1e-4)
    assert rev.extras["scope_label"] == 0
    assert any("1 blocked" in line for line in out["trace"])


def test_low_confidence_comparison_kept(monkeypatch):
    monkeypatch.setenv("SCOPE_FILTER_ENABLED", "1")
    monkeypatch.setenv("SCOPE_MIN_CONFIDENCE", "0.7")

    candidates = [_make_candidate("borderline", "아이패드 프로 vs 에어 살짝 비교 + 리뷰")]
    scores = [_make_score("borderline")]
    state = _make_state(candidates, scores)

    monkeypatch.setattr(
        scope_filter_module,
        "classify_videos",
        lambda items: [
            {"video_id": "borderline", "label": 1, "confidence": 0.55, "latency_ms": 80}
        ],
    )
    out = scope_filter_mod(state)

    sb = out["scores"]["borderline"]
    assert sb.rank == 1, "임계 미만 confidence 는 차단 금지"
    assert sb.extras["scope_label"] == 1
    assert sb.extras["scope_confidence"] == pytest.approx(0.55, abs=1e-4)
    assert any("0 blocked" in line for line in out["trace"])


def test_finalize_does_not_revive_scope_blocked(monkeypatch):
    """rank=-1 마킹은 finalize_selection 의 rank=0 fallback 으로 부활하지 않는다."""
    from video_selection_agent.core.policy import SelectionPolicyConfig

    candidates = [_make_candidate("blocked"), _make_candidate("low"), _make_candidate("ok")]
    blocked = _make_score("blocked", final_score=0.99, rank=-1)
    low = _make_score("low", final_score=0.3, rank=0)
    ok = _make_score("ok", final_score=0.6, rank=1)

    state = {
        "candidates": candidates,
        "scores": {s.video_id: s for s in (blocked, low, ok)},
        "policy": SelectionPolicyConfig(k_min=3, k_max=5),
        "k_requested": 3,
        "mode": "auto",
        "trace": [],
    }
    out = finalize_mod(state)
    final_ids = [v.video_id for v in out["final_selection"]]

    assert "blocked" not in final_ids, "rank=-1 은 fallback 으로도 살아나면 안 됨"
    # rank=0 은 k_min 미달 시 부활 가능 → low 가 들어와도 OK
    assert "ok" in final_ids
