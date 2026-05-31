"""LangGraph StateGraph 빌더.

노드 연결 + 조건부 엣지:
  - fetch 결과 0건이면 즉시 END (우회로 짧게)
  - diversity_filter 생존자가 k_min 미만이면 relax 루프(최대 1회)
  - LLM 노드는 실패 시에도 trace만 남기고 계속 (노드 내부에서 처리)

langgraph 미설치 시 `_FallbackLinearGraph`가 동일한 로직을 파이썬으로 에뮬레이션.
"""
from __future__ import annotations

from typing import Any

from video_selection_agent.graph.nodes import (
    diversity_filter,
    enrich_metadata,
    fetch_candidates,
    finalize_selection,
    generate_rationale,
    llm_rerank,
    score_quantitative,
    scope_filter,
)
from video_selection_agent.graph.state import SelectionState


def _route_after_fetch(state: SelectionState) -> str:
    return "end" if not state.get("candidates") else "enrich"


def _route_after_diversity(state: SelectionState) -> str:
    policy = state["policy"]
    scores = state.get("scores", {})
    survivors = sum(1 for s in scores.values() if s.rank > 0)
    if survivors < policy.k_min and state.get("relax_attempts", 0) < 1:
        return "relax"
    return "llm_rerank"


def relax_constraints(state: SelectionState) -> SelectionState:
    """다양성 제약 완화: max_per_channel을 1 증가. 최대 1회 루프."""
    policy = state["policy"]
    from video_selection_agent.core.policy import SelectionPolicyConfig

    relaxed = SelectionPolicyConfig(
        k_min=policy.k_min,
        k_max=policy.k_max,
        candidate_pool_size=policy.candidate_pool_size,
        max_per_channel=policy.max_per_channel + 1,
        mega_tier_ratio_cap=min(1.0, policy.mega_tier_ratio_cap + 0.2),
        small_or_below_min_ratio=policy.small_or_below_min_ratio,
        weights=policy.weights,
        policy_version=policy.policy_version + "+relax1",
    )
    trace = list(state.get("trace", []))
    trace.append(
        f"relax_constraints: max_per_channel={relaxed.max_per_channel}, "
        f"mega_ratio_cap={relaxed.mega_tier_ratio_cap:.2f}"
    )
    return {
        **state,
        "policy": relaxed,
        "relax_attempts": state.get("relax_attempts", 0) + 1,
        "trace": trace,
    }


def build_graph() -> Any:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return _FallbackLinearGraph()

    graph = StateGraph(SelectionState)
    graph.add_node("fetch_candidates", fetch_candidates)
    graph.add_node("enrich_metadata", enrich_metadata)
    graph.add_node("score_quantitative", score_quantitative)
    graph.add_node("diversity_filter", diversity_filter)
    graph.add_node("relax_constraints", relax_constraints)
    graph.add_node("scope_filter", scope_filter)
    graph.add_node("llm_rerank", llm_rerank)
    graph.add_node("finalize_selection", finalize_selection)
    graph.add_node("generate_rationale", generate_rationale)

    graph.add_edge(START, "fetch_candidates")
    graph.add_conditional_edges(
        "fetch_candidates",
        _route_after_fetch,
        {"end": END, "enrich": "enrich_metadata"},
    )
    graph.add_edge("enrich_metadata", "score_quantitative")
    graph.add_edge("score_quantitative", "diversity_filter")
    graph.add_conditional_edges(
        "diversity_filter",
        _route_after_diversity,
        {"relax": "relax_constraints", "llm_rerank": "scope_filter"},
    )
    graph.add_edge("relax_constraints", "score_quantitative")
    graph.add_edge("scope_filter", "llm_rerank")
    graph.add_edge("llm_rerank", "finalize_selection")
    graph.add_edge("finalize_selection", "generate_rationale")
    graph.add_edge("generate_rationale", END)

    return graph.compile()


class _FallbackLinearGraph:
    """langgraph 미설치 시 선형 실행.

    조건부 엣지를 파이썬으로 직접 에뮬레이션.
    """

    def invoke(self, state: SelectionState) -> SelectionState:
        state = fetch_candidates(state)  # type: ignore[assignment]
        if not state.get("candidates"):
            return state

        state = enrich_metadata(state)  # type: ignore[assignment]
        state = score_quantitative(state)  # type: ignore[assignment]
        state = diversity_filter(state)  # type: ignore[assignment]

        while _route_after_diversity(state) == "relax":
            state = relax_constraints(state)  # type: ignore[assignment]
            state = score_quantitative(state)  # type: ignore[assignment]
            state = diversity_filter(state)  # type: ignore[assignment]

        state = scope_filter(state)  # type: ignore[assignment]
        state = llm_rerank(state)  # type: ignore[assignment]
        state = finalize_selection(state)  # type: ignore[assignment]
        state = generate_rationale(state)  # type: ignore[assignment]
        return state
