"""Scope classifier inference on the worker (GPU).

Loads the fine-tuned klue/roberta-large checkpoint from `SCOPE_MODEL_DIR` once
on first call and serves batched binary classification (is_comparison).

The preprocessing (`build_input_text`) is imported from the scope-classifier
package so train/inference share the same normalization — install the
scope-classifier repo as an editable dependency in the worker venv:
    pip install -e ~/projects/scope-classifier
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

# Heavy deps imported lazily inside _ensure_loaded — keeps import-time cheap
# and lets unit tests stub the module without pulling torch.
_MODEL_LOCK = Lock()
_MODEL_STATE: dict = {}

MODEL_VERSION = "klue-roberta-large-v1"
MAX_LENGTH = 256
DEFAULT_BATCH_SIZE = 16


@dataclass
class ClassifyResult:
    video_id: str
    label: int
    confidence: float
    latency_ms: int


def _ensure_loaded() -> None:
    if _MODEL_STATE.get("ready"):
        return
    with _MODEL_LOCK:
        if _MODEL_STATE.get("ready"):
            return
        import torch  # type: ignore
        from transformers import (  # type: ignore
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )

        model_dir = os.environ.get("SCOPE_MODEL_DIR")
        if not model_dir:
            raise RuntimeError("SCOPE_MODEL_DIR not configured on worker")
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"SCOPE_MODEL_DIR does not exist: {model_dir}")

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device == "cuda" else torch.float32

        tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model = AutoModelForSequenceClassification.from_pretrained(
            model_dir, torch_dtype=dtype
        ).to(device).eval()

        _MODEL_STATE.update(
            tokenizer=tokenizer,
            model=model,
            device=device,
            torch=torch,
            ready=True,
        )
        print(
            f"[SCOPE] Loaded {MODEL_VERSION} from {model_dir} on {device} (dtype={dtype})",
            flush=True,
        )


def classify_batch(items: Iterable[dict]) -> list[ClassifyResult]:
    """Classify each item by title + description.

    items: iterable of {"video_id": str, "title": str, "description": str}
    Returns list aligned to input order. Each item gets its own latency_ms
    measured from the batch slice it belonged to.
    """
    items_list = list(items)
    if not items_list:
        return []

    _ensure_loaded()
    from scope_dataset.preprocessing import build_input_text  # type: ignore

    torch = _MODEL_STATE["torch"]
    tokenizer = _MODEL_STATE["tokenizer"]
    model = _MODEL_STATE["model"]
    device = _MODEL_STATE["device"]

    results: list[ClassifyResult] = []
    batch_size = int(os.environ.get("SCOPE_BATCH_SIZE", DEFAULT_BATCH_SIZE))

    for start in range(0, len(items_list), batch_size):
        slice_ = items_list[start : start + batch_size]
        texts = [
            build_input_text(it.get("title", ""), it.get("description", ""))
            for it in slice_
        ]
        t0 = time.perf_counter()
        enc = tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            logits = model(**enc).logits.float()
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        per_item_ms = max(1, elapsed_ms // len(slice_))

        for it, prob in zip(slice_, probs):
            label = int(prob.argmax())
            confidence = float(prob[label])
            results.append(
                ClassifyResult(
                    video_id=str(it.get("video_id", "")),
                    label=label,
                    confidence=confidence,
                    latency_ms=per_item_ms,
                )
            )

    return results
