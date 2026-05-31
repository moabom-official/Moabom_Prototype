"""HTTP client for the desktop scope-classify worker.

Mirrors `scripts/youtube/transcript_service.py:_fetch_via_worker` —
requests + Bearer, short timeout, 3-attempt exponential backoff. Returns
`None` on any failure so the caller (graph node) can fall through to
"pass-through" mode without breaking selection.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests


def classify_videos(
    items: list[dict],
    *,
    timeout_seconds: int = 30,
) -> Optional[list[dict]]:
    """POST {items: [...]} to /scope-classify.

    items: [{"video_id": str, "title": str, "description": str}, ...]
    Returns the list under response["results"] on success.
    Returns None if:
      - SCOPE_WORKER_URL or SCOPE_WORKER_TOKEN is not configured
      - worker responds with 4xx/5xx beyond retries
      - all retries time out
    """
    if not items:
        return []

    base_url = os.environ.get("SCOPE_WORKER_URL")
    token = os.environ.get("SCOPE_WORKER_TOKEN")
    if not base_url or not token:
        return None

    url = base_url.rstrip("/") + "/scope-classify"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"items": items}

    last_status: Optional[int] = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
            last_status = resp.status_code
            if resp.status_code == 200:
                data = resp.json()
                return data.get("results", [])
            if 500 <= resp.status_code < 600:
                print(
                    f"[SCOPE] worker 5xx ({resp.status_code}), retry {attempt + 1}/3",
                    flush=True,
                )
                time.sleep(2 ** attempt)
                continue
            # 4xx → config/auth error; don't retry
            print(
                f"[SCOPE] worker client error {resp.status_code}: {resp.text[:200]}",
                flush=True,
            )
            return None
        except requests.exceptions.RequestException as exc:
            print(
                f"[SCOPE] worker request error attempt {attempt + 1}/3: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(2 ** attempt)
            continue

    print(f"[SCOPE] worker exhausted retries (last_status={last_status})", flush=True)
    return None
