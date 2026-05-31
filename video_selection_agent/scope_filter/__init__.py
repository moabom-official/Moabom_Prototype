"""Scope classifier integration (desktop GPU worker via Tailscale Funnel).

Filters comparison/round-up videos out of the selection candidate pool so
that per-video and integrated reports stay focused on single-product reviews.

Inference itself runs in `services/fetch_worker/routes/scope.py` on the
desktop; this package only contains the HTTP client and the LangGraph node
that calls it.
"""
from video_selection_agent.scope_filter.client import classify_videos

__all__ = ["classify_videos"]
