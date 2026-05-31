"""Video Selection Agent 데이터 모델.

모든 노드·스코어링·영속화 계층이 공유하는 도메인 모델.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID


ChannelTier = Literal["mega", "large", "mid", "small", "micro"]
SelectionMode = Literal["auto", "custom"]


@dataclass
class ProductContext:
    """선택 대상 제품 정보 (LangGraph state 초기값)."""
    product_id: int
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    keywords: list[str] = field(default_factory=list)


@dataclass
class VideoCandidate:
    """YouTube 후보 영상 + 채널 메타데이터."""
    video_id: str
    title: str
    description: str
    channel_id: str
    channel_name: str
    published_at: datetime
    duration_seconds: int
    view_count: int
    like_count: int
    comment_count: int
    channel_subscriber_count: int
    thumbnail_url: str
    # 쿼리 출처 추적 (다중 쿼리 전략)
    source_query: Optional[str] = None


@dataclass
class ScoreBreakdown:
    """영상 1개에 대한 정량 점수 + LLM rationale.

    UI/DB에 그대로 노출되어 XAI 패널·`video_selection_scores` 테이블을 채움.
    """
    video_id: str
    final_score: float
    dimensions: dict[str, float]
    weighted_contributions: dict[str, float]
    rank: int = 0
    tier: ChannelTier = "mid"
    llm_rationale_short: str = ""
    llm_rationale_full: str = ""
    selection_reasons: list[str] = field(default_factory=list)
    # Auxiliary signals attached by downstream nodes (e.g., scope_filter writes
    # scope_label/scope_confidence/scope_latency_ms here). Persisted by merging
    # into `dimensions_json` at save time — kept separate to preserve the
    # numeric-only contract of `dimensions`.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankResult:
    """`llm_rerank` 노드 출력 (LLM 1회 호출 결과 중 한 항목)."""
    video_id: str
    topical_fit: float
    rationale_short: str


@dataclass
class SelectedVideo:
    """최종 확정된 선정작."""
    video_id: str
    title: str
    channel_name: str
    tier: ChannelTier
    rank: int
    final_score: float
    dimensions: dict[str, float]
    weighted_contributions: dict[str, float]
    rationale_short: str
    rationale_full: str
    selection_reasons: list[str]


@dataclass
class DiversityReport:
    """선정 결과의 다양성 감사 로그."""
    channels_unique: int
    tier_distribution: dict[ChannelTier, int]
    max_channel_occurrence: int
    violated_constraints: list[str] = field(default_factory=list)


@dataclass
class SelectionDecision:
    """에이전트 최종 산출물 (API 응답 & DB 저장 단위)."""
    run_id: UUID
    product_id: int
    mode: SelectionMode
    selected: list[SelectedVideo]
    candidates_preview: list[VideoCandidate]
    diversity_report: DiversityReport
    candidate_count: int
    model_used: str
    policy_version: str
    trace: list[str] = field(default_factory=list)
    # 영속화 전용 (API 응답에는 포함하지 않음)
    all_scores: dict[str, ScoreBreakdown] = field(default_factory=dict)
