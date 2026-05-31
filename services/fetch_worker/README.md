# Moabom Fetch Worker

Datacenter IP에서 YouTube 자막 fetch가 봇으로 차단되는 문제 + Azure CPU 머신에서 GPU 추론이 불가한 문제를 자취방 데스크탑(RTX 4060 Ti, residential IP)으로 위임하기 위한 얇은 FastAPI 워커.

- 분리 대상: `/transcript` (yt-dlp + caption URL fetch) + `/scope-classify` (klue/roberta-large 비교영상 분류). 댓글·메타데이터는 YouTube Data API v3 key 기반이라 IP 차단을 받지 않으므로 Azure가 그대로 호출.
- 외부 노출: Tailscale Funnel (`*.ts.net` HTTPS).
- 가역성: Azure 측 `YOUTUBE_FETCH_WORKER_URL` / `SCOPE_WORKER_URL` env를 비우면 즉시 Azure-only 경로로 복귀.

## 디렉토리

```
services/fetch_worker/
├── app.py              # FastAPI app
├── auth.py             # Bearer 토큰
├── transcript_logic.py # cookieless transcript fetch (residential IP 전제)
├── scope_logic.py      # klue/roberta-large GPU inference (lazy load singleton)
├── routes/
│   ├── health.py       # /healthz (auth 없음)
│   ├── transcript.py   # POST /transcript (Bearer 필수)
│   └── scope.py        # POST /scope-classify (Bearer 필수)
├── requirements.txt    # 운영 본체와 격리된 의존성
└── deploy/
    ├── moabom-fetch.service     # systemd unit 템플릿
    └── moabom-fetch.env.example # env 파일 예시
```

## 데스크탑 설치 (1회)

```bash
# 1. venv (이미 생성됨 — 새 머신이면 만들기)
cd /home/rtx4060ti/projects/Moabom_Prototype
uv venv services/fetch_worker/.venv --python 3.12
services/fetch_worker/.venv/bin/uv pip install -r services/fetch_worker/requirements.txt
# uv가 venv 안에 없으면 외부 uv로:
#   VIRTUAL_ENV=services/fetch_worker/.venv uv pip install -r services/fetch_worker/requirements.txt

# 2. 토큰 생성 + env 파일 (sudo)
TOKEN=$(openssl rand -base64 32)
echo "FETCH_WORKER_TOKEN=$TOKEN" | sudo tee /etc/moabom-fetch.env
sudo chown root:rtx4060ti /etc/moabom-fetch.env
sudo chmod 640 /etc/moabom-fetch.env
echo "$TOKEN"   # ← Azure secret에 동일하게 등록

# 3. systemd 등록
sudo cp services/fetch_worker/deploy/moabom-fetch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now moabom-fetch.service
sudo systemctl status moabom-fetch.service

# 4. 헬스체크 (로컬)
curl -s http://127.0.0.1:8080/healthz   # → {"status":"ok"}

# 5. Tailscale Funnel — 외부 HTTPS 노출
sudo tailscale funnel --bg 8080
sudo tailscale funnel status   # ts.net URL 확인
```

## scope-classify 추가 셋업 (GPU 추론)

`/scope-classify` 는 fine-tuned klue/roberta-large 체크포인트를 로컬에서 load. 별 repo `scope-classifier` 에 학습 환경과 inference 전처리(`build_input_text`)가 살아 있고, 워커는 그걸 editable 설치로 import 한다.

```bash
# scope-classifier repo 가 데스크탑에 이미 클론돼 있다고 가정
ls /home/rtx4060ti/projects/scope-classifier/runs/klue-roberta-large/best/   # model.safetensors 등 존재 확인

# venv 에 torch/transformers + scope_dataset 패키지 설치
cd /home/rtx4060ti/projects/Moabom_Prototype
services/fetch_worker/.venv/bin/pip install -r services/fetch_worker/requirements.txt
services/fetch_worker/.venv/bin/pip install -e /home/rtx4060ti/projects/scope-classifier

# env 에 모델 경로 추가
echo "SCOPE_MODEL_DIR=/home/rtx4060ti/projects/scope-classifier/runs/klue-roberta-large/best" | sudo tee -a /etc/moabom-fetch.env

sudo systemctl restart moabom-fetch.service
journalctl -u moabom-fetch.service -n 30   # "[SCOPE] Loaded ... on cuda" 확인

# 로컬 검증 (실제로는 첫 호출에서 1~3초 모델 load → 이후 ms 단위)
TOKEN=$(sudo cat /etc/moabom-fetch.env | grep FETCH_WORKER_TOKEN | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8080/scope-classify \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"video_id":"a","title":"아이폰 16 vs 갤럭시 S24 비교"},{"video_id":"b","title":"M4 맥북 프로 리뷰"}]}'
# → {"results":[{"video_id":"a","label":1,...},{"video_id":"b","label":0,...}], ...}
```

## Azure 측 연결 (1회)

```bash
# az CLI 필요 — 노트북에서 실행
URL="https://<rtx4060ti-b650m-k>.<tailnet>.ts.net"
TOKEN="<위에서 생성한 토큰>"

az containerapp secret set \
  -g rg-moabom -n ca-moabom \
  --secrets fetch-worker-url="$URL" fetch-worker-token="$TOKEN"

az containerapp update -g rg-moabom -n ca-moabom \
  --set-env-vars \
    YOUTUBE_FETCH_WORKER_URL=secretref:fetch-worker-url \
    YOUTUBE_FETCH_WORKER_TOKEN=secretref:fetch-worker-token \
    SCOPE_WORKER_URL=secretref:fetch-worker-url \
    SCOPE_WORKER_TOKEN=secretref:fetch-worker-token

# 새 revision으로 자동 restart
az containerapp revision list -g rg-moabom -n ca-moabom -o table
```

## 운영 cycle

- 상태: `sudo systemctl status moabom-fetch.service`
- 로그: `journalctl -u moabom-fetch.service -f`
- 재시작: `sudo systemctl restart moabom-fetch.service`
- Funnel 상태: `tailscale funnel status`
- Azure-only로 복귀 (비상): `az containerapp update -g rg-moabom -n ca-moabom --set-env-vars YOUTUBE_FETCH_WORKER_URL=""`

## Circuit breaker 동작

`scripts/youtube/transcript_service.py` 측 로직:

| 응답 | 동작 |
|---|---|
| 200 | worker 결과 사용 |
| 404 | 자막 없음 — fallback도 None 반환 |
| 401/4xx | 설정/인증 에러 즉시 fallback (cookie 경로) |
| 5xx | 3회 backoff 후 fallback |
| timeout | 3회 backoff 후 fallback |

env 비우면 분기 자체가 비활성화되어 기존 cookie 경로만 동작.
