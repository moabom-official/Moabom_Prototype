# Video Selection Agent (FR-005)

LangGraph 기반 유튜브 리뷰 영상 선택 에이전트. 제품별로 25~50개 후보 풀에서 **Auto / Custom** 방식으로 3~10개를 선정한다. 비교영상 필터(scope) + 편향 완화 (대형 채널 쏠림 억제) + Explainable AI (정량 점수 6차원 + LLM rationale).

설계 문서: [docs/VIDEO_SELECTION_AGENT_DESIGN.md](../docs/VIDEO_SELECTION_AGENT_DESIGN.md)

## 상태

**Phase-3 + scope 통합 완료**:
- ✅ Phase-1: 폴더 구조·데이터 모델·노드 stub·API 엔드포인트·DB 스키마.
- ✅ Phase-2: 정량 스코어링 (6차원) + YouTube API 연동 + 다양성 필터.
- ✅ Phase-3: LLM rerank + rationale + 웹 UI 통합.
- ✅ **scope 통합** (2026-06): `scope_filter` 노드 추가 — "여러 제품 비교/랭킹 영상"을 후보에서 제외해 영상별·종합 보고서 노이즈 제거. 데스크탑 GPU 워커(`services/fetch_worker` `/scope-classify`)의 klue/roberta-large 분류기(test acc 89.94%) 를 HTTP 로 호출. 별 repo [scope-classifier](https://github.com/moabom-official/scope-classifier) 가 학습·전처리 single source.

iPhone 15 Pro 기준 end-to-end 검증: 30 후보 → 29 LLM rerank → 5 선정, 한국어 rationale 자동 생성. 아이폰 17 기준 scope 검증: 26 분류 → 비교영상 10개 차단 → 선정 5개 전부 단독 리뷰.

## 구조

```
core/        데이터 모델, SelectionPolicyConfig, VideoSelectionAgent facade
graph/       LangGraph state / builder / 8개 노드
  nodes/     fetch_candidates → enrich_metadata → score_quantitative
             → diversity_filter ↔ relax_constraints (조건부 루프)
             → scope_filter → llm_rerank → finalize_selection → generate_rationale
scope_filter/ 데스크탑 GPU 워커(/scope-classify) HTTP 클라이언트 (비교영상 분류)
scoring/     6차원 정량 점수 (relevance / engagement / recency
             / channel_anti_bias / duration / weights)
youtube/     candidate_pool (다중 쿼리) / channel_service
llm/         LLM 클라이언트 + json_schema 프롬프트
persistence/ video_selection_runs / video_selection_scores 영속화
api/         POST /products/{id}/select-videos, GET /selection-runs/{run_id}
tests/       smoke / unit / integration (TODO)
```

`langgraph` 미설치 환경에서도 `_FallbackLinearGraph`가 동일 로직을 파이썬으로 에뮬레이션.

## 사용 — 코드

```python
from video_selection_agent.core.agent import VideoSelectionAgent
from video_selection_agent.core.models import ProductContext

agent = VideoSelectionAgent()
decision = agent.select(
    product=ProductContext(product_id=1, name="iPhone 15 Pro", brand="Apple"),
    mode="auto",  # 또는 "custom"
    k=5,          # 3~10
)
for v in decision.selected:
    print(f"#{v.rank} [{v.tier}] {v.title} — {v.final_score:.3f}")
    print(f"   {v.rationale_short}")
```

## 사용 — API

서버 기동: `docker compose up -d postgres && python main.py` → http://localhost:8000

### `POST /products/{product_id}/select-videos`

```json
{
  "mode": "auto",              // "auto" | "custom"
  "k": 5,                      // 3..10
  "candidate_pool_size": 30,   // 25..50
  "selected_video_ids": [],    // custom 모드에서 사용자 체크박스 선택
  "weights_override": null
}
```

응답:
```json
{
  "run_id": "uuid",
  "mode": "auto",
  "selected": [
    {
      "video_id": "...", "title": "...", "channel_name": "...",
      "tier": "large", "rank": 1, "final_score": 0.682,
      "dimensions": {"relevance": 0.73, "engagement": 0.82, ...},
      "weighted_contributions": {...},
      "rationale_short": "장기 사용기 중심으로 2026년에도 유효한 리뷰.",
      "rationale_full": "...",
      "selection_reasons": ["심층 리뷰 길이", "리뷰 적합성"]
    }
  ],
  "candidates_preview": [...],   // Custom 모드: 30개 후보 전체 (체크박스용)
  "diversity_report": {
    "channels_unique": 23,
    "tier_distribution": {"large": 13, "mega": 14, "mid": 2},
    "max_channel_occurrence": 1
  },
  "candidate_count": 30,
  "model_used": "gpt-4.1-mini",
  "policy_version": "v1.0.0-skeleton"
}
```

### `GET /products/{product_id}/selection-runs/{run_id}`

저장된 선정 결과 재조회.

## 사용 — UI

[templates/product_detail.html](../templates/product_detail.html) — `🎯 AI 영상 선택 (FR-005)` 보라색 버튼:

1. 모드 라디오 (Auto / Custom) + K 슬라이더 (3~10) + 풀 슬라이더 (25~50)
2. **AI 선택 시작** → 30~60초 (YouTube + 스코어링 + LLM rerank + rationale)
3. 결과 모달: 순위 카드 + 티어 뱃지 + 점수 + 이유 칩 + rationale_short
4. **왜 선택됨?** 모달: 6차원 점수 바 + 가중 기여도 + rationale_full
5. Custom 모드: 30개 후보 체크박스 (Auto 선정분 미리체크) → 3~10개 선택 → "Custom 선택으로 확정"

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | (필수) | `https://<resource>.cognitiveservices.azure.com` |
| `AZURE_OPENAI_API_KEY` | (필수) | Azure 리소스 키 |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4.1-mini` | 배포 이름 |
| `AZURE_OPENAI_API_VERSION` | `2025-01-01-preview` | API 버전 |
| `YOUTUBE_API_KEY` | (필수) | YouTube Data API v3 |
| `DATABASE_URL` | `postgresql://postgres:postgres@127.0.0.1:5432/techdb` | Postgres |
| `SCOPE_WORKER_URL` | (없으면 scope skip) | 데스크탑 워커 `/scope-classify` base URL (Tailscale Funnel `*.ts.net`) |
| `SCOPE_WORKER_TOKEN` | (없으면 scope skip) | 워커 Bearer 토큰 (fetch worker 와 동일 값) |
| `SCOPE_FILTER_ENABLED` | `1` | `0` 이면 scope_filter 노드 즉시 통과 (kill switch) |
| `SCOPE_MIN_CONFIDENCE` | `0.7` | 비교영상(label=1) 차단 임계 confidence |

LLM 미설정 시 자동 graceful degradation: rerank 실패 → 정량 점수만 사용, rationale 실패 → `selection_reasons` 기반 fallback 문구. scope 워커 미설정/장애 시에도 graceful: 모든 후보 통과(pass-through) + trace 기록.

## 비교영상 필터링 — scope_filter 노드

`diversity_filter` 직후, `llm_rerank` 직전에 실행. 다양성 통과 후보(rank > 0)의 제목+설명을 데스크탑 GPU 워커로 batch 분류한다.

- 워커: `services/fetch_worker` 의 `POST /scope-classify` (klue/roberta-large, bf16, residential GPU). 전처리 `build_input_text` 는 [scope-classifier](https://github.com/moabom-official/scope-classifier) 에서 import → train-serving skew 방지.
- 클라이언트: [scope_filter/client.py](scope_filter/client.py) — requests + Bearer + 3회 지수 백오프. 실패 시 `None` → 노드가 pass-through.
- 차단 로직: `label == 1`(비교영상) **이고** `confidence ≥ SCOPE_MIN_CONFIDENCE`(**기본 0.7**) 일 때만 `rank = -1` 로 마킹해 제외한다. 비교영상으로 분류돼도 **confidence 가 0.7 미만이면 통과**(애매한 케이스의 false positive 로 좋은 단독 리뷰가 잘려나가는 것을 막는 안전장치). `finalize_selection` 의 fallback 은 `rank == 0` 만 부활시키므로 **rank=-1 은 영구 제외** (finalize 코드 수정 없음). 임계값은 `SCOPE_MIN_CONFIDENCE` 로 조정 (운영 traffic 분포 보고 재튜닝 가능).
- 결과는 `ScoreBreakdown.extras` 에 `scope_label / scope_confidence / scope_latency_ms` 기록 → DB 저장 시 `dimensions_json` 에 merge (schema 변경 0).
- 분류 정의: `label=1` = 2개 이상 제품 동시 비교(셀프·모델 비교 포함), `label=0` = 그 외(단일 제품 리뷰·언박싱·뉴스·랭킹). v1 binary.

## 편향 완화 전략

1. **채널 상한** (하드): `max_per_channel = 2`
2. **티어 쿼터**: 메가 채널 비율 ≤ 40%, 풀에 중소 채널 있으면 ≥ 20%
3. **anti-mega 가중치** (소프트): `1 - log10(subs)/7` → 1k 구독자 0.57 vs 10M 0.0
4. **다중 쿼리**: `"리뷰" / "review" / "단점" / "{brand} {name}"` 4종 → 다양화
5. 다양성 부족 시 `relax_constraints` 노드로 1회 자동 완화 후 재시도

## YouTube API 쿼터

회당 4 × search(100) + 2 × list(1) = **402 units**. 일일 10,000 한도 대비 충분.

## 비용

LLM 호출은 **정확히 2회/run** (rerank + rationale). GPT-4.1-mini ~$0.005/run, `max_tokens` 고정으로 상한 보장.

## 후속 과제

- 댓글 감성 기반 진짜 관점 다양성 (FR-010/011 이후)
- 재선택 시 이전 분석 데이터 정리 정책 확정
- 통합 테스트 추가 ([tests/](tests/))
- scope v2: roundup·news·unboxing 등 multi-class 분류 (현재 binary)
- scope 운영 traffic 으로 분류 적중률 검증 → false positive/negative 를 v2 dataset 보강 후보로 수집
- scope 워커를 Azure 직접 서빙으로 전환 시 ONNX export (현재는 데스크탑 GPU PyTorch 서빙)
