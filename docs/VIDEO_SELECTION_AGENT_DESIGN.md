# FR-005 분석 대상 영상 선택 — LangGraph 기반 구현 계획

> **상태**: Phase-3 완료 (2026-04) + **scope_filter 통합 (2026-06)**. 본 문서는 구현 전 작성된 설계안이며, 아래 "Context" / "현재 상태" 섹션은 **착수 시점 진단**임. 실제 구현은 [video_selection_agent/README.md](../video_selection_agent/README.md) 참조.
>
> **2026-06 추가**: `diversity_filter` 와 `llm_rerank` 사이에 `scope_filter` 노드 신설 — "여러 제품 비교/랭킹 영상"을 후보에서 제외(피드백 ②). 데스크탑 GPU 워커(`services/fetch_worker` `/scope-classify`)의 klue/roberta-large 분류기 호출. 상세: 본 문서 "비교영상 필터링" 섹션 + [video_selection_agent/README.md](../video_selection_agent/README.md).

## Context

FR-005("분석 대상 영상 선택")는 제품별로 수집한 유튜브 리뷰 영상 후보군 중 **AI 자동(Auto)** 또는 **사용자 직접(Custom)** 방식으로 3~10개(FR-022 상한)를 확정해 분석 파이프라인으로 넘기는 기능이다.

**현재 상태 (검증 완료)**:
- [scripts/youtube/video_service.py:9](../scripts/youtube/video_service.py#L9) `fetch_product_videos()`가 YouTube `search.list` 결과를 **그대로** 반환 — AI 선택 로직 없음.
- [scripts/api/sync.py:78](../scripts/api/sync.py#L78)에서 `max_results=5` **하드코딩** — 사용자 입력은 line 28에서 읽어도 무시됨 (버그).
- DB `videos` 테이블에 `channel_id / channel_subscriber_count / duration_seconds` 미저장 → 편향 보정 근거가 부족.
- [templates/product_detail.html](../templates/product_detail.html)에는 Auto/Custom 토글 없음, 선택 rationale 표시 UI 없음.
- LangChain/LangGraph 미사용 ([requirements.txt](../requirements.txt) 확인). Groq Llama 3.3 70B만 연결됨.

**문제 의식 (사용자 요구)**:
- 유튜브 알고리즘에 의한 대형 채널 편중을 완화해 관점·채널 다양성 확보.
- 사용자가 선택 결과의 근거(정량 점수 + 정성 이유)를 확인할 수 있는 Explainable AI.
- 저비용 LLM 운영 (Groq Llama 3.3 70B 채택).

**목표**: 기존 팀원 코드에 최소 영향을 주면서 `video_selection_agent/` 신규 모듈로 모듈화된 LangGraph 에이전트 구현.

## 주요 결정 (사용자 확정)

| 항목 | 선택 | 비고 |
|---|---|---|
| LLM | **Azure OpenAI GPT-4.1-mini** (deployment: `gpt-4.1-mini`, API version `2025-01-01-preview`) | 비용/성능 밸런스 최적 ~$0.005/회, 네이티브 구조화 출력(`response_format`)으로 JSON 파싱 안정. 사용자 Azure 구독 활용(`AZURE_OPENAI_*` 환경변수). `openai` SDK는 [requirements.txt](../requirements.txt)에서 `>=1.50.0`으로 상향 완료 (json_schema 지원). |
| 기존 sync 플로우 | **유지** + 신규 엔드포인트 추가 | 팀원 파이프라인 영향 제로 |
| Custom 모드 UX | Auto와 **동일한 30개 후보 풀** 제공 (체크박스) | UI 단일화 |
| LangGraph | **추가** (`langgraph>=0.2`, `langchain-core>=0.3`) | 새 의존성 OK |

## 스코프

**In scope**:
- 신규 `video_selection_agent/` 모듈 (LangGraph 워크플로우 + 점수 산출 + Azure GPT-4.1-mini rationale).
- 영상/채널 메타데이터 보강 (duration, channel_subscriber_count).
- 신규 API `POST /products/{id}/select-videos`.
- `product_detail.html`에 Auto/Custom 토글 + 후보 미리보기 모달 추가.
- DB 스키마 추가: `videos` 컬럼 확장, `video_selection_runs`, `video_selection_scores` 신규.

**Out of scope (후속)**:
- 댓글 감성 기반 "관점 다양성"(긍/부정 믹스) — 댓글 수집 이후 단계이므로 FR-010/011에서 처리. 현재는 제목 키워드 휴리스틱으로 근사만.
- 기존 `/products/{id}/sync` 엔드포인트 리팩터링.
- Custom 모드의 자유 검색 기능.

## 폴더/파일 구조

`comment_filtering_agent/` 컨벤션을 따른다.

```
video_selection_agent/
├── __init__.py
├── README.md
├── core/
│   ├── models.py                # VideoCandidate, ScoreBreakdown, SelectionDecision, DiversityReport
│   ├── policy.py                # SelectionPolicyConfig (k_min=3, k_max=10, max_per_channel=2, tier quota, 가중치)
│   └── agent.py                 # VideoSelectionAgent facade (graph 빌드 + .select() 노출)
├── graph/
│   ├── state.py                 # SelectionState (TypedDict, LangGraph shared state)
│   ├── builder.py               # build_graph() → compiled StateGraph
│   └── nodes/
│       ├── fetch_candidates.py  # multi-query YouTube search (25~50건)
│       ├── enrich_metadata.py   # videos.list + channels.list 보강
│       ├── score_quantitative.py# 결정적 점수 (LLM 미사용)
│       ├── diversity_filter.py  # 채널 상한, 티어 쿼터
│       ├── scope_filter.py      # (2026-06) 비교영상 분류 → label=1 & conf≥0.7 → rank=-1 제외
│       ├── llm_rerank.py        # LLM 1회 호출: topical_fit + 짧은 rationale
│       ├── finalize_selection.py# top-k 선정 (min 3, max 10)
│       └── generate_rationale.py# Azure GPT-4.1-mini 1회 호출: 선정작 2~3문장 rationale
├── scoring/
│   ├── relevance.py             # 제품명/브랜드 매칭
│   ├── engagement.py            # like/view, comment/view z-score
│   ├── recency.py               # exp(-days/180)
│   ├── channel_bias.py          # 1 - log10(subs)/7 역가중 (핵심 편향 완화)
│   ├── duration.py              # 4~25분 선호 삼각형
│   └── weights.py               # ScoringWeights 기본값
├── youtube/
│   ├── candidate_pool.py        # 다중 쿼리 확장 + 중복 제거
│   └── channel_service.py       # channels.list 래퍼
├── llm/
│   ├── azure_openai_client.py   # AzureOpenAI SDK 래퍼 (deployment=gpt-4.1-mini, response_format=json_schema)
│   ├── provider_factory.py      # Azure GPT-4.1-mini primary, 기존 llm/base_provider 추상 재사용 (향후 공급자 교체 대비)
│   └── rationale_prompts.py
├── persistence/
│   └── repository.py            # video_selection_runs / _scores upsert
├── api/
│   └── routes.py                # register_selection_routes(app)
└── tests/
    ├── test_scoring.py
    ├── test_diversity.py
    ├── test_graph.py
    └── test_integration.py
```

**팀원 코드 수정은 다음 4곳만**:
1. [main.py](../main.py) — 1줄: `register_selection_routes(app)` 호출.
2. [scripts/database/schema.py](../scripts/database/schema.py) — 기존 idempotent `IF NOT EXISTS` 패턴을 따라 ALTER/CREATE 추가 (하위 호환, 기존 컬럼 변경 없음).
3. [templates/product_detail.html](../templates/product_detail.html) — Auto/Custom 토글, 후보 미리보기 모달, "왜 선택됨?" 모달 추가 (기존 테이블/Sync 버튼 유지).
4. [requirements.txt](../requirements.txt) — `langgraph`, `langchain-core` 2줄 추가.

[scripts/api/sync.py](../scripts/api/sync.py)는 **손대지 않는다**. 신규 엔드포인트가 독립적으로 동작.

## LangGraph 워크플로우

### SelectionState (graph/state.py)

```python
class SelectionState(TypedDict, total=False):
    """노드 간 전달되는 공유 상태. total=False로 각 노드가 필요한 키만 추가."""
    run_id: UUID
    product: ProductContext               # id, name, brand, category, keywords
    mode: SelectionMode                   # "auto" | "custom"
    k_requested: int                      # 3..10
    selected_video_ids: list[str]         # custom 모드 사용자 선택
    policy: SelectionPolicyConfig

    candidates: list[VideoCandidate]
    scores: dict[str, ScoreBreakdown]     # video_id → breakdown
    diversity_report: DiversityReport
    llm_reranked: list[RerankResult]
    final_selection: list[SelectedVideo]

    errors: list[str]
    trace: list[str]                      # 노드별 감사 로그 (XAI 패널용)
    relax_attempts: int                   # diversity_filter 완화 루프 카운터
```

### 노드 그래프

아래 다이어그램은 실제 컴파일된 그래프에서 `build_graph().get_graph().draw_mermaid()`로 생성 — 코드 변경 시 재생성만 하면 자동 동기화.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([__start__]):::first
	fetch_candidates(fetch_candidates)
	enrich_metadata(enrich_metadata)
	score_quantitative(score_quantitative)
	diversity_filter(diversity_filter)
	relax_constraints(relax_constraints)
	scope_filter(scope_filter)
	llm_rerank(llm_rerank)
	finalize_selection(finalize_selection)
	generate_rationale(generate_rationale)
	__end__([__end__]):::last
	__start__ --> fetch_candidates;
	fetch_candidates -. end .-> __end__;
	fetch_candidates -. enrich .-> enrich_metadata;
	enrich_metadata --> score_quantitative;
	score_quantitative --> diversity_filter;
	diversity_filter -. relax .-> relax_constraints;
	diversity_filter -.-> scope_filter;
	scope_filter --> llm_rerank;
	relax_constraints --> score_quantitative;
	llm_rerank --> finalize_selection;
	finalize_selection --> generate_rationale;
	generate_rationale --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

조건부 엣지 요약:
- `fetch_candidates`: 후보 0건 → 즉시 `__end__` (에러 상태)
- `diversity_filter`: 생존자 < `k_min` && `relax_attempts < 1` → `relax_constraints` 루프 (최대 1회) → 다시 `score_quantitative`
- `llm_rerank` / `generate_rationale`: LLM 실패 시 노드 내부에서 graceful degradation (점수만으로 finalize, `selection_reasons` 기반 fallback rationale). 엣지는 항상 다음 노드로.

LLM 호출은 **정확히 2회/run**으로 제한 (비용/지연 관리).

## Explainable AI — 점수 설계

### 정량 차원 (scoring/)

| 차원 | 계산 | 기본 가중치 |
|---|---|---|
| `relevance` | 제품명·브랜드·카테고리 토큰을 title(×2) + description(×1)에서 매칭 후 정규화 (difflib 부분 매칭 포함) | 0.30 |
| `engagement` | (like/view + comment/view)의 후보 풀 내 z-score → sigmoid로 0–1 압축. **절대값이 아닌 상대값**이라 대형 채널 편중 완화 | 0.15 |
| `recency` | `exp(-days_since_published / 180)` | 0.10 |
| `channel_anti_bias` | `1 - min(1, log10(max(subs,1))/7)` (1k→0.57, 100k→0.28, 1M→0.14, 10M→0) — 역가중으로 중소 채널 우대 | 0.20 |
| `duration_fit` | 3분 이하 0, 4~25분 1, 60분에서 0으로 선형 감소. Shorts·라이브 제외 | 0.10 |
| `llm_topical_fit` | `llm_rerank` 출력 0–1 | 0.15 |

최종 점수 = 가중합. 가중치는 [video_selection_agent/scoring/weights.py](../video_selection_agent/scoring/weights.py)에서 튜닝.

### ScoreBreakdown (UI + DB 저장)

```python
@dataclass
class ScoreBreakdown:
    video_id: str
    final_score: float
    dimensions: dict[str, float]             # 차원명 → 원점수 0-1
    weighted_contributions: dict[str, float] # 차원명 → 가중치×원점수
    rank: int
    tier: Literal["mega", "large", "mid", "small", "micro"]
    llm_rationale_short: str                 # rerank 결과 (≤100자)
    llm_rationale_full: str                  # generate_rationale 결과 (2~3문장)
    selection_reasons: list[str]             # 예: ["최고 관련도", "중소 채널 관점", "최신 리뷰"]
```

### LLM 프롬프트 (Azure GPT-4.1-mini)

**llm_rerank** — 살아남은 후보 배치 1회:
```
당신은 "{product_name}" ({brand}, {category})에 대한 유튜브 리뷰 영상을 선별하는 전문가입니다.
각 후보에 대해 JSON을 반환하세요: {video_id, topical_fit (0-1), rationale_short (최대 100자 한국어)}.
감점: 언박싱만 있는 영상, 라이브스트림, 단순 리액션.
가점: 비교 리뷰, 장기 사용기, 스펙 심층 분석, 비판적 리뷰.
후보: [{video_id, title, channel_name, duration_min, description_snippet}, ...]
```

**generate_rationale** — 최종 k개 배치 1회:
```
다음 선정된 영상들에 대해 각각 2-3문장 한국어 rationale을 작성하세요.
반영 요소: 점수 차원 {dimensions}, 채널 티어 {tier}, 리뷰어 관점 다양성.
중립적·사실 기반으로 작성하고 과장 금지.
```

출력은 OpenAI의 네이티브 `response_format={"type":"json_schema", "json_schema": {...}}`로 스키마 강제 — 파싱 실패율 거의 0. 2회 호출 모두 `max_tokens`를 보수적으로 설정(예: rerank 2000, rationale 1500)해 비용 상한 고정.

## 편향 완화 전략

1. **채널 상한 (하드 제약)**: `max_per_channel = 2`. 최종 k개에 대해 최소 `ceil(k/2)` 개의 고유 채널 보장.
2. **티어 쿼터**: 구독자 수로 채널을 `mega(>1M) / large(100k~1M) / mid(10k~100k) / small(1k~10k) / micro(<1k)` 분류 후:
   - `mega` 비율 ≤ 40%.
   - 풀에 존재한다면 `mid + small + micro` 합계 ≥ 20%.
3. **anti-mega 가중치** (소프트): 위 `channel_anti_bias` 차원 — 로그 스케일 감점 곡선으로 중소 채널 우대.
4. **다중 쿼리 다양화** (쿼리 레벨):
   - `"{product} 리뷰"`, `"{product} review"`, `"{product} 단점"`, `"{brand} {product}"` 4종 병렬 → 중복 제거 → 25~50건.
   - "단점" 쿼리로 비판적 관점 시드.
5. **관점 다양성 근사**: 제목 키워드(`단점/실망/후회/비추` vs `최고/추천/완벽`)로 coarse tag 부여, 풀에 ≥3개 있으면 비홍보 제목 최소 1개 강제. 진짜 관점 다양성(댓글 감성 믹스)은 **FR-010/011 단계로 이월** (TODO 주석 명시).

## 비교영상 필터링 — scope_filter (2026-06 추가)

`diversity_filter` 직후, `llm_rerank` 직전에 실행. "여러 제품 비교/랭킹 영상"(피드백 ②)을 후보에서 제외해 영상별·종합 보고서 노이즈를 줄인다.

**아키텍처**: 추론은 데스크탑 GPU 워커가 담당하고 본 에이전트는 HTTP 클라이언트만 둔다 (운영 머신엔 GPU 없음 + 학습/전처리 코드를 운영 repo 와 분리해 train-serving skew 방지).

- 워커: [services/fetch_worker](../services/fetch_worker) 의 `POST /scope-classify` — klue/roberta-large (bf16, 자취방 RTX 4060 Ti). 전처리 `build_input_text` 는 별 repo [scope-classifier](https://github.com/moabom-official/scope-classifier) 에서 import. Tailscale Funnel + Bearer (transcript 워커와 동일 인증·서빙 인프라 재사용). 서빙 프레임워크(vLLM/Triton) 없이 FastAPI + transformers 직접 호출 — 단발 forward pass 분류라 동적 배칭 불필요.
- 클라이언트: [video_selection_agent/scope_filter/client.py](../video_selection_agent/scope_filter/client.py) — requests + Bearer + 3회 지수 백오프. 실패 시 `None`.
- 노드: [video_selection_agent/graph/nodes/scope_filter.py](../video_selection_agent/graph/nodes/scope_filter.py). `rank > 0` 후보만 분류 대상. **`label == 1`(비교영상) 이고 `confidence ≥ SCOPE_MIN_CONFIDENCE`(기본 0.7) 일 때만** `rank = -1` 로 제외. confidence 가 0.7 미만이면 비교영상으로 분류돼도 통과시켜, 애매한 분류로 좋은 단독 리뷰가 잘리는 것을 막는다 (임계 미만 false positive 방어). 임계값은 `SCOPE_MIN_CONFIDENCE` 환경변수로 조정.

**rank=-1 설계 (핵심)**: `finalize_selection` 의 부족분 fallback 은 `rank == 0` 후보만 부활시키므로(본 문서 finalize 설명 참조), scope 차단(rank=-1)은 마이너 제품의 부족분 보충에도 **절대 부활하지 않는다** — finalize 코드 수정 0.

**fail-safe**: `SCOPE_WORKER_URL` 미설정 / 워커 502·timeout → 모든 후보 통과(pass-through) + trace 기록. `SCOPE_FILTER_ENABLED=0` kill switch.

**저장**: 분류 결과(`scope_label / scope_confidence / scope_latency_ms`)는 `ScoreBreakdown.extras` 에 담겨 `dimensions_json` JSONB 에 merge (schema 변경 0).

**분류 정의 (v1 binary)**: `label=1` = 2개 이상 제품 동시 비교(셀프·모델 비교 포함), `label=0` = 그 외(단일 제품 리뷰·언박싱·뉴스·랭킹). roundup/news/unboxing 세분류는 v2 이슈.

**검증 (아이폰 17, k=5, pool 50)**: `scope_filter: 26 classified, 10 blocked` — 비교영상 차단, 선정 5개 전부 단독 리뷰. confidence<0.7 경계 케이스는 보존. 회귀 안전망: [regression/tests/test_scope_filter.py](../regression/tests/test_scope_filter.py) 5종 (offline mock).

## 데이터 모델 변경 ([scripts/database/schema.py](../scripts/database/schema.py))

모두 **추가만** — 기존 컬럼/테이블 변경 없음. 기존 `IF NOT EXISTS` 패턴 그대로 사용.

### videos 테이블 확장

```sql
ALTER TABLE videos ADD COLUMN IF NOT EXISTS channel_id VARCHAR(64);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS channel_name VARCHAR(255);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS channel_subscriber_count BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS selection_mode VARCHAR(16); -- 'auto' | 'custom'
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
```

### video_selection_runs (신규)

```sql
CREATE TABLE IF NOT EXISTS video_selection_runs (
    run_id          UUID PRIMARY KEY,
    product_id      INT NOT NULL REFERENCES tech_products(product_id) ON DELETE CASCADE,
    mode            VARCHAR(16) NOT NULL,
    model_used      VARCHAR(64),
    policy_version  VARCHAR(32),
    k_selected      INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    trace_json      JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_selection_runs_product ON video_selection_runs(product_id);
```

### video_selection_scores (신규)

```sql
CREATE TABLE IF NOT EXISTS video_selection_scores (
    id               BIGSERIAL PRIMARY KEY,
    run_id           UUID NOT NULL REFERENCES video_selection_runs(run_id) ON DELETE CASCADE,
    video_id         VARCHAR(64) NOT NULL,
    selected         BOOLEAN NOT NULL,
    rank             INTEGER,
    final_score      NUMERIC(6,4),
    dimensions_json  JSONB NOT NULL,
    tier             VARCHAR(16),
    rationale_short  TEXT,
    rationale_full   TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_selection_scores_run ON video_selection_scores(run_id);
CREATE INDEX IF NOT EXISTS idx_selection_scores_video ON video_selection_scores(video_id);
```

## API + UI 통합

### 신규 엔드포인트 ([video_selection_agent/api/routes.py](../video_selection_agent/api/routes.py))

```
POST /products/{product_id}/select-videos
Body:
  { "mode": "auto" | "custom",
    "k": 5,                       // 3..10
    "candidate_pool_size": 30,    // 25..50
    "selected_video_ids": [...],  // custom 모드 시 사용자 선택
    "weights_override": {...}     // 선택적
  }
200:
  { "run_id": "...",
    "mode": "auto",
    "selected": [
      { "video_id", "title", "channel_name", "tier",
        "final_score", "rank",
        "dimensions": {...}, "weighted_contributions": {...},
        "rationale_short", "rationale_full",
        "selection_reasons": [...] } ],
    "candidates_preview": [...],           // custom 모드: 30개 전체
    "diversity_report": { "channels_unique": 4, "tier_distribution": {...} },
    "candidate_count": 30 }

GET /products/{product_id}/selection-runs/{run_id}
  → 동일 shape (재조회용)
```

성공 시 `videos` 테이블에 `selection_mode` 포함하여 upsert → 이후 댓글/자막 파이프라인이 선정된 video_id에만 작동.

### UI 변경 ([templates/product_detail.html](../templates/product_detail.html))

**구현 결과** (Phase-3): 기존 Sync 버튼은 그대로 두고, **독립적인 보라색 `🎯 AI 영상 선택 (FR-005)` 버튼**을 추가. 기존 `/sync` 엔드포인트와 통합하지 않고 신규 `/select-videos` 엔드포인트만 호출 (선정 결과는 `videos` 테이블에 별도 upsert).

1. **모드 라디오**: `Auto (AI 자동)` / `Custom (직접 선택)`.
2. **K 슬라이더**: 3~10 (기본 5).
3. **풀 슬라이더**: 25~50 (기본 30).
4. **Auto 흐름**: "AI 선택 시작" 클릭 → `/select-videos` (auto) → 결과 모달에 순위 카드(티어 뱃지, 점수, 이유 칩, `rationale_short`) 표시.
5. **Custom 흐름**: `/select-videos` (mode=auto, pool=30)로 1차 호출 → 후보 30개 체크박스(Auto 선정분 미리체크) → 3~10개 선택 후 "Custom 선택으로 확정" → `/select-videos` (mode=custom, `selected_video_ids=[...]`)로 2차 호출.
6. **"왜 선택됨?" 버튼** (카드별): 6차원 점수 바 차트 + 가중 기여도 + `rationale_full` 모달 (순수 HTML/CSS, 라이브러리 불필요).

바닐라 JS — 기존 템플릿 스타일 유지.

## 후보 풀 전략

**다중 쿼리 다양화** ([video_selection_agent/youtube/candidate_pool.py](../video_selection_agent/youtube/candidate_pool.py)):

| 쿼리 | 목적 |
|---|---|
| `"{product_name} 리뷰"` | 한국어 리뷰 |
| `"{product_name} review"` | 영어 리뷰 |
| `"{product_name} 단점"` | 비판적 관점 시드 |
| `"{brand} {product_name}"` | 브랜드 정제 |

각 쿼리 15~20건 → dedupe 후 25~50건. `videos.list`(최대 50 ID/콜) 1회 + `channels.list` 1회로 보강.

**YouTube API 쿼터**: 4 × search(100 units) + 2 × list(1 units) = **402 units/회**. 일일 10,000 대비 여유 (FR-021 안전 범위).

## 검증 계획

### 유닛 테스트 ([video_selection_agent/tests/](../video_selection_agent/tests/))

**Phase-3 시점 구현 상태**: `test_graph.py`만 작성됨. 나머지는 TODO.

- `test_graph.py` ✅ — YouTube/LLM mock으로 `build_graph().invoke()` 실행, 최종 state에 정확히 k개 영상 + 필수 필드 존재.
- `test_scoring.py` (TODO) — 각 scoring 함수 (recency at 0/30/180/365일, duration_fit at 30초/10분/30분/2시간, channel_anti_bias at 500/50k/5M 구독).
- `test_diversity.py` (TODO) — 한 채널에서 8개 후보 → `max_per_channel=2` 검증; 티어 분포 검증.

### 통합 테스트
- `test_integration.py` (TODO) — 실제 제품(`iPhone 15 Pro`, `Galaxy S24`)에 대해 실제 YouTube + Azure GPT-4.1-mini 호출. Assert:
  - `3 ≤ len(final_selection) ≤ 10`.
  - `unique_channels ≥ ceil(k/2)`.
  - 각 선정에 non-empty `rationale_full` + `dimensions` 6개 모두 채워짐.
  - `mega` 티어 비율 ≤ 40%.

### 수동 품질 검수
- 5개 제품 카테고리(폰/노트북/이어폰/시계/모니터)에서 선정 실행 → rationale을 3축(점수 충실도 / 자연스러움 / 환각 없음)으로 1~3 평가. **평균 ≥2.5** 목표 (NR-004 근거 기반 주장 10% 이하 기준 충족 확인).

### End-to-end 수동 시나리오
1. `docker compose up -d postgres && python main.py`.
2. 브라우저 `/products`에서 제품 등록 → `/products/{id}`.
3. `🎯 AI 영상 선택 (FR-005)` 버튼 → Auto 모드 k=5 → 결과 모달의 점수/rationale 확인.
4. Custom 모드로 재실행 → 30개 후보 체크박스 → 3~10개 선택 후 "Custom 선택으로 확정".
5. "왜 선택됨?" 모달로 6차원 점수 바 + 가중 기여도 + `rationale_full` 확인.

## 리스크 / 후속 과제

1. **LLM 실패 경로**: Azure GPT-4.1-mini의 `response_format=json_schema`로 JSON 준수는 거의 100%지만 네트워크/rate-limit/Azure 리전 장애 가능성은 상존. `llm_rerank`/`generate_rationale` 두 노드 모두 실패 시 graceful degradation 엣지 보유 — rerank 실패 시 점수만으로 finalize, rationale 실패 시 `selection_reasons`로 자동 문구 생성. 비용 상한을 위해 `max_tokens` 고정.
2. **채널 구독자 수 API 의존**: `channels.list` 추가 쿼터 소모 (1 unit/콜). 하루 수백 번 호출해도 안전.
3. **관점 다양성 근사 한계**: 제목 키워드 기반은 coarse. 진짜 긍/부정 믹스는 댓글 분석 후에만 가능하므로 `diversity_filter.py`에 `TODO: FR-010/011 이후 감성 기반 믹스 추가` 주석 명시.
4. **K 상한 정책**: FR-022의 10개 제한을 API 경계에서 하드 enforce (pydantic validator).
5. **재선택 시 데이터 정책**: 동일 product_id로 재선택 시 기존 sync의 DELETE 패턴([sync.py:70-74](../scripts/api/sync.py#L70-L74))을 따라 이전 videos/comments/sentiments 삭제 후 새 선택 반영. `video_selection_runs`는 히스토리로 유지 (audit trail).
6. **Rationale 언어**: 한국어 기본 ([templates/product_detail.html](../templates/product_detail.html) 관례).

## 핵심 파일 요약

**신규**:
- [video_selection_agent/](../video_selection_agent/) 전체

**수정 (최소)**:
- [main.py](../main.py) — 라우터 등록 1줄
- [scripts/database/schema.py](../scripts/database/schema.py) — 추가 ALTER/CREATE 블록
- [templates/product_detail.html](../templates/product_detail.html) — Auto/Custom 토글 + 모달
- [requirements.txt](../requirements.txt) — `langgraph`, `langchain-core`
- [CLAUDE.md](../CLAUDE.md) — "기술 스택" 섹션에 LangGraph + Azure GPT-4.1-mini 반영 (스택 변경 시 즉시 동기화 규칙 준수)
- `.env` (완료) / [README.md](../README.md) — `AZURE_OPENAI_ENDPOINT/API_KEY/DEPLOYMENT/API_VERSION` 환경변수 문서화

**참조 (수정 없음)**:
- [comment_filtering_agent/core/agent.py](../comment_filtering_agent/core/agent.py) — `VideoSelectionAgent` 설계 시 컨벤션 참조
- [llm/base_provider.py](../llm/base_provider.py) — LLM 추상화 재사용
- [scripts/youtube/video_service.py](../scripts/youtube/video_service.py) — 기존 검색 로직 참고
