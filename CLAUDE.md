# Moabom — Claude 작업 가이드

## 프로젝트 개요
**과제명**: 유튜브 테크 리뷰 종합 분석 에이전트 ("모아봄"). 다수의 유튜브 테크 리뷰 영상의 자막·댓글을 자동 수집·분석해 리뷰어별 성향(엄격/관대) 보정까지 반영한 제품 단위 종합 보고서를 제공하는 B2C 웹 서비스 MVP.

**핵심 파이프라인**: 사용자 입력 → 영상 선정 Agent → 댓글 필터링 Agent → 보고서 생성 파이프라인 → 7 섹션 종합 보고서. 보고서 출력 4단계: ①영상별 자막 기반 → ②영상별 댓글 기반 → ③영상별 자막+댓글 통합 → ④제품 단위 7 섹션 종합.

## 팀원·역할 (3인)
- **김유현** (팀장 / Project Manager) — 프로젝트 설계·UI/UX·영상 선택 Agent
- **김재현** (AI / Data Engineer) — 백엔드 아키텍처·DB 설계·댓글 필터링 Agent
- **한상민** (AI Agent Engineer) — 보고서 생성 파이프라인·Self-Healing

## 팀 협업 규칙 (중요)
- **3인 1팀**으로 GitHub(branch/PR) 기반 협업 중
- **새 기능을 구현할 땐 반드시 main에서 분기한 새 브랜치에서 작업**할 것. main 직접 커밋 금지. 브랜치 네이밍은 `feature/xxx`(기능), `fix/xxx`(버그), `docs/xxx`·`chore/xxx`(문서·잡일) 등 prefix 사용. 작업 완료 시 PR을 통해 머지.
- 다른 팀원이 작성한 코드는 **최소한으로만 수정**할 것. 인터페이스 변경이 불가피하면 먼저 공유.
- 각자 맡은 기능은 **전용 폴더/파일을 새로 만들어 모듈화**된 형태로 구현. 기존 파일에 로직 섞지 말 것.
- 비기능 요구사항 NR-007/012: 모델·모듈 교체 시 기존 시스템 수정이 최소화되도록 의존성을 얇게 유지.

## 기술 스택 (현 구현 기준 — 2026-05-14 최신)
**스택이 변경될 때마다 이 섹션(및 관련 문서)을 즉시 업데이트할 것.** 명세서 임시안과 현 구현이 갈리는 항목이 많으므로 아래 "현 구현"을 기준으로 작업.

- Frontend: **Jinja2 HTML templates** (`templates/`) + Vanilla JS + Markdown 렌더링 — 명세 임시안 React/TypeScript는 미적용
- Backend: FastAPI + Uvicorn + Pydantic
- DB: **PostgreSQL 15** (`psycopg2-binary`) — **14개 테이블 + Schema Auto-init** (기동 시 자동 생성). 주요 테이블: `video_transcripts`, `video_reports`, `product_integrated_reports`(INSERT 누적), `agent_decisions`, `aspect_extractions`, `comment_sentiments`
- 데이터 수집: YouTube Data API v3, `youtube-transcript-api`, `yt-dlp`
- LLM: **RunYourAI GPT-4.1** (`openai/gpt-4.1-2025-04-14`, full) — 댓글 분류·감성 분석, 영상 선택 Agent, 보고서 생성 등 모든 LLM 호출에 사용
  - 환경변수: `RUNYOURAI_API_KEY` (Container App secret `runyourai-key`) / `RUNYOURAI_BASE_URL` (default `https://api.runyour.ai/v1`) / `RUNYOURAI_MODEL`
  - 호출 경로: `langchain-openai`(`ChatOpenAI` + `base_url`, OpenAI 호환 endpoint). 단일 진입점 `scripts/llm/__init__.py:get_chat_llm()`
  - PR #15에서 Azure OpenAI → RunYourAI 통합 게이트웨이로 이관 (Azure 잔존 클라이언트는 `video_selection_agent/llm/azure_openai_client.py` 파일명만 유지하고 내부는 RunYourAI 호출)
- 에이전틱 워크플로우: **LangGraph** (`video_selection_agent/graph/`, FR-005 영상 선택 StateGraph). 영상 선정 Agent 8-step (fetch_candidate → enrich_metadata → score_quantitative 6차원 가중합 → 다양성 필터 → **scope_filter(비교영상 제외)** → LLM Re-rank → finalize_selection → generate_rationale). scope_filter 는 데스크탑 GPU 워커(`services/fetch_worker` `/scope-classify`, klue/roberta-large)를 HTTP 호출해 "여러 제품 비교/랭킹 영상"을 후보에서 제외(피드백 ②). label=1 & conf≥0.7 → rank=-1(finalize fallback 으로도 부활 불가). 학습/전처리는 별 repo scope-classifier. 워커 부재 시 pass-through, `SCOPE_FILTER_ENABLED=0` kill switch.
- 댓글 필터링 Agent 7-step: 수집 → 전처리 → Rule Soft Filter(12종) → 후보 가공 Top 300 → Multi-Criteria 6기준 선정 → LLM 5-class 분류 → Agent Decision Engine + ABSA 감성 분석. 영상 단위 **ThreadPoolExecutor 병렬 처리**.
- PDF 출력: **ReportLab** (한글 폰트 적용)
- 인프라/배포: **Azure Container Apps** (`rg-moabom` / `cae-moabom` / `ca-moabom`, FastAPI Port 8000, **CPU 1.0 / Mem 2Gi, min=2 / max=5 replicas**) + **Azure PostgreSQL Flexible Server** (**Standard_B2s, 2 vCPU / 4GB, max_connections=200**, db: `techdb`, sslmode=require) + **Azure Container Registry** (`moabom-app:tag`) + **Azure Log Analytics** (30일 보존). Docker · docker-compose 로컬 개발. 2026-05-14 v1 사용자 설문 배포 대응으로 상향 (이전 0.5/1Gi · min=1/max=1 · B1ms).
- MLOps: Airflow 실험 단계 (`dags/youtube_product_sync_dag.py`) — 운영 미연결
- 감성 분석: 현재 RunYourAI GPT-4.1 API. **KLUE-BERT 자체 운영 검토 중** (Break-even 약 1.24만 댓글/일).
- 회원/인증: **미구현** (5월 4주차 착수 예정, Google OAuth)
- **보고서 파이프라인 v2 (Phase 0~3, 전부 운영 연결·머지 완료)**: Phase0 회귀 안전망 `regression/` (보고서 4종 contract+golden, dev 의존성 `requirements-dev.txt`) · Phase1 4종 다중 LLM 검증 `scripts/reports/_verification.py` (환각 최소화) · Phase2-a 입력 ①②③ 종합 확장 `scripts/reports/_pir_input.py` · Phase2-b RAG `scripts/rag/` (sqlite3 + 순수 파이썬 코사인, RunYourAI 게이트웨이 임베딩 — pgvector 의도적 미선택) · Phase3 제품 이미지 `scripts/product_image/` (Serper 검색 + 비전 LLM, `tech_products.image_url` 컬럼)
- 명세 임시안 중 **미적용/미구현**: Redis, VectorDB, Gemini, Transformers, scikit-learn, vLLM, ELK (RAG는 위 Phase2-b로 적용됨 — VectorDB는 여전히 미사용)

## 핵심 동작 메모
- **7 섹션 종합 보고서** (`product_integrated_reports`, INSERT 누적 — UPSERT 아님): ①한 줄 구매 판정 + 종합 점수 + 합의도 ②핵심 요약(카드 3장) ③6차원 평가표(배터리·가격·카메라·성능·디스플레이·디자인) ④합의 기반 장단점(2명 이상 + 빈도 N/N, 다이버징 막대) ⑤소비자 여론(댓글 기반 — 가중 비율 + aspect 상위 + 대표 댓글) ⑥전작 대비 변화표 ⑦추천/비추(2열 칩 매처, 영상 N 근거). v1 사용자 피드백 반영해 v0의 Divergence·리뷰어 vs 실사용자 갭·경쟁/대체 비교 3 섹션 제거 후, ⑤ 소비자 여론은 `_pir_comment_aggregator` 가 `comment_filtering_agent` 결과(comments/comment_sentiments/aspect_extractions)를 READ ONLY 로 제품 단위 집계해 LLM 입력으로 주입한다.
- **환각 방지 4규칙** (보고서 ④ 생성 시 자동 검증): ①근거 명시 ②합의도 정량화 ③등장 제품만 비교 ④데이터 부족 명시. 검증 실패 시 Heuristic Fallback("데이터 부족" 명시 모드)으로 자동 전환. 추가로 보고서 4종은 `_verification.py` 로 다중 LLM 교차 검증 후 적재 (Phase 1).
- **토큰 예산 안전망**: 영상별 보고서 1500자 cap + 전체 18K 토큰 자동 비례 축소.
- **Self-Healing** (한상민 담당, 보고서 ④ 통합 인사이트 엔드포인트):
  - 자막: 선정 영상 중 `video_transcripts` / `video_reports.transcript_report` 가 없는 영상은 `collect_transcript_reports_for_product` 가 fetch+build 후 UPSERT.
  - 댓글: `agent_decisions` 행이 없는 영상은 `ensure_comment_analysis_for_videos` 가 `sync.py` 의 `process_comments_with_agent` (7-step 댓글 agent) 를 그대로 호출해 collect → rule filter → LLM classify → ABSA 까지 적재. 댓글 파이프라인은 한 줄도 수정하지 않고 entry point 만 사용.
  - 두 self-healing 은 `asyncio.gather` 로 병렬 실행 (서로 다른 YouTube API endpoint + 다른 LLM + 다른 테이블). 일부 영상 실패는 격리되어 다른 영상 처리에 영향 없음.
- **캐시 정책 (FR-020)**: 동일 제품 재요청 시 DB 캐시 즉시 반환 (2초 이내). 영상 자막은 `video_transcripts`에 영구 캐시.

## 저장소 구조
```
main.py                       # FastAPI 진입점
scripts/                      # 운영 본체 (api / database / youtube / analysis / reports / rag / product_image / utils)
comment_filtering_agent/      # 댓글 7-step 필터 Agent (filters / classifiers / analyzers / core)
video_selection_agent/        # 영상 선정 LangGraph Agent (graph / scoring / youtube / llm / persistence / api)
regression/                   # 보고서 양식 회귀 안전망 (Phase 0, pytest · requirements-dev.txt)
services/fetch_worker/        # 홈서버 워커 (운영): /transcript 자막 fetch + /scope-classify 비교영상 GPU 분류
app/  dags/  llm/             # 병렬 리팩터링·실험 모듈 (운영 미연결)
templates/                    # Jinja2 HTML
docs/                         # 과제 기획서, 요구사항명세서, 설계 문서, 중간 산출물
```

## 참고 문서
- [README.md](README.md) — 실행·환경 설정
- [docs/중간보고서_모아봄_최종.pdf](docs/중간보고서_모아봄_최종.pdf) — **현 시점 가장 최신**. 시스템 아키텍처·시퀀스·UI 와이어프레임·진행 현황·이슈/리스크·향후 계획·기여도
- [docs/중간발표_모아봄.pdf](docs/중간발표_모아봄.pdf) — 발표 자료 (Appendix에 댓글 필터 Step 02~07 상세)
- [docs/요구사항명세서_모아봄_v5.pdf](docs/요구사항명세서_모아봄_v5.pdf) — FR-001~025, NR-001~015 전체 명세
- [docs/인공지능종합설계_과제기획서_모아봄.pdf](docs/인공지능종합설계_과제기획서_모아봄.pdf) — 배경·범위·일정·역할 분담
- [docs/COMMENT_FILTERING_AGENT_DESIGN.md](docs/COMMENT_FILTERING_AGENT_DESIGN.md) — 댓글 필터 Agent 설계
- [docs/VIDEO_SELECTION_AGENT_DESIGN.md](docs/VIDEO_SELECTION_AGENT_DESIGN.md) — 영상 선정 Agent 설계 (`docs/assets/video_selection_agent_flowchart.png` 다이어그램)
