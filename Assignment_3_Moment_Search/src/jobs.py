"""Prefect Cloud trigger layer — the API schedules runs, workers execute them.

One flow ("ms-ingest-video" — the "ms-" prefix keeps it distinct from the
digital-twin-akash flow living in the same Prefect workspace), one deployment
("ingest", registered by worker.py's flow.serve()). The API never imports the
pipeline or its heavy deps (torch, ffmpeg) — it just asks Prefect Cloud to
schedule a run; any live worker picks it up. Retries/backoff live on the
flow's tasks (src/ingest/pipeline.py); failed runs are visible + retryable in
the Prefect Cloud UI.
"""
from __future__ import annotations

from prefect.deployments import run_deployment

INGEST_DEPLOYMENT = "ms-ingest-video/ingest"

# One deployment per source kind. They are separate flows rather than one flow
# branching internally because their stages genuinely differ (a paper is never
# sampled for frames; a video is never captioned), and because a per-kind
# deployment makes the Prefect run view legible during a backfill - which is
# half of what the resilience evidence is read from.
DEPLOYMENTS = {
    "video": INGEST_DEPLOYMENT,
    "paper": "ms-ingest-paper/ingest-paper",
    "deck": "ms-ingest-deck/ingest-deck",
}


def enqueue_video(video_id: str, user_id: str) -> str:
    """Schedule the ingest flow for one video. Returns the Prefect flow-run id."""
    flow_run = run_deployment(
        name=INGEST_DEPLOYMENT,
        parameters={"video_id": video_id, "user_id": user_id},
        timeout=0,  # fire-and-forget: don't block the API waiting for the run
        flow_run_name=f"ingest-{video_id}",
    )
    return str(flow_run.id)


def enqueue_source(source_id: str, user_id: str, kind: str) -> str:
    """Schedule the right ingest flow for any source kind.

    Documents share the manifest table with videos, so the dispatcher claims
    both from one queue; without routing here it would hand a paper to the
    video flow, which would try to treat the PDF as an upload and fail.
    """
    if kind == "video":
        return enqueue_video(source_id, user_id)
    deployment = DEPLOYMENTS.get(kind)
    if deployment is None:
        raise ValueError(f"no ingest deployment for kind {kind!r}")
    # Parameter name is doc_id, not video_id: the document flows take doc_id.
    flow_run = run_deployment(
        name=deployment,
        parameters={"doc_id": source_id, "user_id": user_id},
        timeout=0,
        flow_run_name=f"ingest-{source_id}",
    )
    return str(flow_run.id)
