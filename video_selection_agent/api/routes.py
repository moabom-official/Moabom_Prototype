"""FR-005 엔드포인트.

POST /products/{product_id}/select-videos
GET  /products/{product_id}/selection-runs/{run_id}

선정된 영상에 대해 댓글 수집·필터링 파이프라인까지 동기 실행해서 댓글/통합
보고서가 빈 채로 표시되지 않도록 한다 (Custom UI의 첫 번째 미리보기 호출은
`process_comments=false`로 스킵 가능).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Literal, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from scripts.database.queries import query_one
from video_selection_agent.core.agent import VideoSelectionAgent
from video_selection_agent.core.models import ProductContext
from video_selection_agent.core.policy import SelectionPolicyConfig
from video_selection_agent.persistence.repository import load_selection, save_selection


class SelectVideosRequest(BaseModel):
    mode: Literal["auto", "custom"] = "auto"
    k: int = Field(5, ge=3, le=10)
    candidate_pool_size: int = Field(30, ge=25, le=50)
    selected_video_ids: Optional[list[str]] = None
    weights_override: Optional[dict[str, float]] = None
    # Custom UI의 미리보기 호출에서는 false로 두어 댓글 파이프라인을 건너뛴다.
    # 그 외(Auto-only, Custom 확정)에서는 반드시 true 여야 댓글/통합 보고서가 채워짐.
    process_comments: bool = True


_FALLBACK_POSITIVE_KEYWORDS = {
    "좋다", "훌륭", "추천", "완벽", "최고", "멋진", "빠르다", "빠른", "강력", "강력한",
    "좋은", "좋습니다", "훌륭합니다", "amazing", "great", "excellent", "awesome",
    "best", "love", "perfect", "worth", "impressed", "beautiful", "fast", "powerful",
}
_FALLBACK_NEGATIVE_KEYWORDS = {
    "나쁘다", "문제", "느리다", "느린", "비싸다", "비싼", "약하다", "약한", "못쓸",
    "망했", "실망", "후회", "환불", "bad", "terrible", "poor", "awful", "slow",
    "expensive", "waste", "regret", "disappointing", "broken", "fragile",
}


def _fallback_collect_comments(video_id: str) -> int:
    """Agent 불가 시 단순 YouTube API 수집 + 키워드 sentiment.

    `scripts/api/sync.py` 의 fallback 분기와 동일한 결과를 만들어 댓글/통합
    보고서가 빈 채로 남지 않도록 graceful degrade.
    """
    from scripts.database.connection import get_connection
    from scripts.database.queries import execute_update
    from scripts.youtube.comment_service import fetch_video_comments

    try:
        comments = fetch_video_comments(video_id, max_pages=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[SELECT] [{video_id}] Fallback fetch failed: {exc}")
        return 0

    if not comments:
        return 0

    inserted = 0
    for c in comments:
        try:
            execute_update(
                """INSERT INTO comments (comment_id, video_id, text_raw, is_product_related)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (comment_id) DO UPDATE SET
                       video_id = EXCLUDED.video_id,
                       text_raw = EXCLUDED.text_raw,
                       is_product_related = EXCLUDED.is_product_related""",
                (c["comment_id"], video_id, c["text_raw"], True),
            )

            text = (c["text_raw"] or "").lower()
            pos = sum(1 for kw in _FALLBACK_POSITIVE_KEYWORDS if kw in text)
            neg = sum(1 for kw in _FALLBACK_NEGATIVE_KEYWORDS if kw in text)
            if pos > neg:
                label, score = "positive", 0.7
            elif neg > pos:
                label, score = "negative", 0.3
            else:
                label, score = "neutral", 0.5

            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM comment_sentiments WHERE comment_id = %s",
                (c["comment_id"],),
            )
            cur.execute(
                """INSERT INTO comment_sentiments (comment_id, sentiment_label, sentiment_score, analysis_weight, created_at)
                   VALUES (%s, %s, %s, %s, NOW())""",
                (c["comment_id"], label, score, 1.0),
            )
            conn.commit()
            cur.close()
            conn.close()
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"[SELECT] [{video_id}] Fallback insert failed for comment "
                f"{c.get('comment_id')}: {exc}"
            )
            continue

    return inserted


def _process_comments_for_videos(product_name: str, video_ids: Iterable[str]) -> None:
    """선정된 영상들에 대해 댓글 수집·LLM 분류·감정 분석 파이프라인을 실행.

    Agent(`comment_filtering_agent`) 사용 가능 시 정확도 높은 LLM 경로로 병렬 처리,
    불가 시 단순 YouTube API 수집 + 키워드 sentiment 로 graceful degrade.
    """
    ids = [vid for vid in video_ids if vid]
    if not ids:
        return

    agent_available = False
    parallel_workers = 3
    process_comments_with_agent = None
    try:
        from scripts.api.sync import (
            AGENT_AVAILABLE,
            PARALLEL_WORKERS,
            process_comments_with_agent as _process_agent,
        )

        agent_available = AGENT_AVAILABLE
        parallel_workers = PARALLEL_WORKERS
        process_comments_with_agent = _process_agent
    except ImportError as exc:
        print(f"[SELECT] Comment agent module unavailable: {exc}")

    if agent_available and process_comments_with_agent is not None:
        print(
            f"[SELECT] Comment processing start (agent): "
            f"videos={len(ids)}, parallel={parallel_workers}"
        )
        with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
            futures = {
                executor.submit(process_comments_with_agent, vid, product_name): vid
                for vid in ids
            }
            for future in as_completed(futures):
                vid = futures[future]
                try:
                    stats = future.result()
                    print(f"[SELECT] [{vid}] Agent comments processed: {stats}")
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[SELECT] [{vid}] Agent failed: {exc}; "
                        "falling back to simple collection"
                    )
                    n = _fallback_collect_comments(vid)
                    print(f"[SELECT] [{vid}] Fallback collected={n}")
    else:
        print(f"[SELECT] Comment processing start (fallback): videos={len(ids)}")
        for vid in ids:
            n = _fallback_collect_comments(vid)
            print(f"[SELECT] [{vid}] Fallback collected={n}")


def _decision_to_response(decision: Any) -> dict:
    score_lookup = getattr(decision, "all_scores", {}) or {}
    # SelectedVideo 에는 없지만 VideoCandidate 에는 이미 있는 메타데이터를
    # 응답에만 함께 실어 보내기 위한 룩업 (선정 로직/저장 구조는 불변).
    candidate_by_id = {c.video_id: c for c in decision.candidates_preview}

    def _candidate_payload(c: Any) -> dict:
        base = {
            "video_id": c.video_id,
            "title": c.title,
            "channel_name": c.channel_name,
        }
        sb = score_lookup.get(c.video_id)
        if sb is not None:
            base.update({
                "final_score": sb.final_score,
                "tier": sb.tier,
                "rank": sb.rank,
                "dimensions": sb.dimensions,
                "weighted_contributions": sb.weighted_contributions,
                "rationale_short": sb.llm_rationale_short,
            })
        return base

    candidates_sorted = sorted(
        decision.candidates_preview,
        key=lambda c: (
            score_lookup[c.video_id].final_score
            if c.video_id in score_lookup
            else float("-inf")
        ),
        reverse=True,
    )

    return {
        "run_id": str(decision.run_id),
        "mode": decision.mode,
        "selected": [
            {
                "video_id": v.video_id,
                "title": v.title,
                "channel_name": v.channel_name,
                "tier": v.tier,
                "rank": v.rank,
                "final_score": v.final_score,
                "dimensions": v.dimensions,
                "weighted_contributions": v.weighted_contributions,
                "rationale_short": v.rationale_short,
                "rationale_full": v.rationale_full,
                "selection_reasons": v.selection_reasons,
                # VideoCandidate 에서 그대로 가져온 표시용 메타데이터 (없으면 None).
                "thumbnail_url": getattr(
                    candidate_by_id.get(v.video_id), "thumbnail_url", None
                ),
                "duration_seconds": getattr(
                    candidate_by_id.get(v.video_id), "duration_seconds", None
                ),
                "channel_subscriber_count": getattr(
                    candidate_by_id.get(v.video_id), "channel_subscriber_count", None
                ),
            }
            for v in decision.selected
        ],
        "candidates_preview": [_candidate_payload(c) for c in candidates_sorted],
        "diversity_report": {
            "channels_unique": decision.diversity_report.channels_unique,
            "tier_distribution": decision.diversity_report.tier_distribution,
            "max_channel_occurrence": decision.diversity_report.max_channel_occurrence,
        },
        "candidate_count": decision.candidate_count,
        "model_used": decision.model_used,
        "policy_version": decision.policy_version,
    }


def register_selection_routes(app: FastAPI) -> None:
    """main.py에서 1회 호출."""

    @app.post("/products/{product_id}/select-videos")
    async def select_videos(product_id: int, request: SelectVideosRequest) -> dict:
        product = query_one(
            "SELECT * FROM tech_products WHERE product_id = %s", (product_id,)
        )
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        context = ProductContext(
            product_id=product_id,
            name=product["name"],
            brand=product.get("brand"),
            category=product.get("category"),
            keywords=[],
        )

        policy = SelectionPolicyConfig(candidate_pool_size=request.candidate_pool_size)
        agent = VideoSelectionAgent(policy=policy)
        decision = agent.select(
            product=context,
            mode=request.mode,
            k=request.k,
            selected_video_ids=request.selected_video_ids,
        )

        selected_ids = {v.video_id for v in decision.selected}
        all_scores = {
            vid: {
                "final_score": float(sb.final_score),
                # Merge ScoreBreakdown.extras (e.g. scope_label/scope_confidence
                # from scope_filter node) into dimensions_json. Keeps the JSONB
                # column the single source of per-video auxiliary signals
                # without a schema change.
                "dimensions": {**sb.dimensions, **sb.extras},
                "tier": sb.tier,
                "rank": sb.rank,
                "rationale_short": sb.llm_rationale_short,
                "rationale_full": sb.llm_rationale_full,
                "selected": vid in selected_ids,
            }
            for vid, sb in decision.all_scores.items()
        }
        candidate_lookup = {
            c.video_id: {
                "title": c.title,
                "description": c.description,
                "published_at": c.published_at,
                "thumbnail_url": c.thumbnail_url,
                "view_count": c.view_count,
                "like_count": c.like_count,
                "comment_count": c.comment_count,
                "channel_id": c.channel_id,
                "channel_name": c.channel_name,
                "channel_subscriber_count": c.channel_subscriber_count,
                "duration_seconds": c.duration_seconds,
            }
            for c in decision.candidates_preview
        }
        save_selection(
            decision,
            all_scores=all_scores,
            candidate_lookup=candidate_lookup,
            reset_existing_videos=request.process_comments,
        )

        if request.process_comments:
            _process_comments_for_videos(
                product_name=context.name,
                video_ids=[v.video_id for v in decision.selected],
            )

        return _decision_to_response(decision)

    @app.get("/products/{product_id}/selection-runs/{run_id}")
    async def get_selection_run(product_id: int, run_id: UUID) -> dict:
        decision = load_selection(run_id)
        if decision is None or decision.product_id != product_id:
            raise HTTPException(status_code=404, detail="Selection run not found")
        return _decision_to_response(decision)
