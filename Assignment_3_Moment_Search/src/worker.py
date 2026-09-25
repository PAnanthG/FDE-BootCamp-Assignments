"""Ingest worker entrypoint — serves the Prefect flow.

    python -m src.worker

flow.serve() registers the "ms-ingest-video/ingest" deployment in Prefect Cloud
(idempotent) and long-polls for scheduled runs — outbound HTTPS only, no
ports. Scale horizontally by running more replicas of this process; each
executes up to WORKER_CONCURRENCY runs at once.

Sample seeding is NOT done here — it's a one-shot startup gate (seed.py /
src/seeding.py) that the whole stack waits on, so the app never serves a
half-indexed corpus. This worker only handles user uploads + YouTube adds.

Embedding goes to the warm CLIP service when CLIP_SERVICE_URL is set
(docker-compose default); unset, each run loads the model in-process.
"""
import os
import time

from prefect import serve
from prefect.flows import EntrypointType

from .db import init_schema
from .ingest.deck import ingest_deck
from .ingest.paper import ingest_paper
from .ingest.pipeline import ingest_video


def main():
    init_schema()  # make sure migrations ran before consuming runs
    from .rag import vector_store
    vector_store.ensure_collection()  # up front, not mid-first-ingest
    # Fair scheduler (WFQ): admits pending videos round-robin across users so
    # one bulk uploader can't starve everyone else (src/dispatcher.py).
    from . import dispatcher
    dispatcher.start_in_background()
    limit = int(os.getenv("WORKER_CONCURRENCY", "2"))
    # serve() talks to Prefect Cloud on startup; a transient outage (e.g. a 503)
    # used to crash the worker permanently and stop the machine. Self-heal:
    # retry forever so a blip pauses ingest instead of killing the worker.
    while True:
        try:
            # THREE deployments from ONE process. prefect.serve() takes several
            # deployments and long-polls for all of them, so `limit` remains a
            # single shared concurrency budget across videos, papers and decks -
            # which is what we want: capacity is the machine, not the kind.
            # Serving them separately would let a paper backfill and a video
            # backfill each consume `limit` runs and blow the memory ceiling.
            print(f"[worker] serving ms-ingest-video/ingest, "
                  f"ms-ingest-paper/ingest-paper, ms-ingest-deck/ingest-deck "
                  f"(shared concurrency {limit})")
            # entrypoint_type=MODULE_PATH is load-bearing, not a style choice.
            # Prefect defaults to a FILE_PATH entrypoint ("src/ingest/paper.py:
            # ingest_paper"). At run time the runner loads the flow from that
            # path as a top-level module with no package, so every relative
            # import in the file ("from .. import db") raises, and the run dies
            # before it starts with "ValueError: Empty module name".
            #
            # This affects the PROVIDED video flow too - it is not specific to
            # the document flows. It went unnoticed upstream because the sample
            # seed calls ingest_video() in-process (src/seeding.py) and never
            # goes through a deployment.
            #
            # MODULE_PATH stores "src.ingest.paper:ingest_paper" instead, which
            # imports as a proper package member and resolves relative imports.
            serve(
                ingest_video.to_deployment(
                    name="ingest", entrypoint_type=EntrypointType.MODULE_PATH),
                ingest_paper.to_deployment(
                    name="ingest-paper", entrypoint_type=EntrypointType.MODULE_PATH),
                ingest_deck.to_deployment(
                    name="ingest-deck", entrypoint_type=EntrypointType.MODULE_PATH),
                limit=limit,
            )
            # serve() returning is NOT success. It blocks forever while healthy,
            # so a return means the runner stopped - a dropped Prefect
            # websocket, an internal task-group failure, whatever. The original
            # `break` here treated that as a clean shutdown: the process exited
            # 0, Docker's `restart: unless-stopped` saw a normal exit, and the
            # worker stayed dead. Observed in practice - the queue silently
            # stopped draining for three hours with no error anywhere, and
            # sources sat `pending` forever.
            #
            # Only an explicit signal (KeyboardInterrupt/SIGTERM below) is a
            # real shutdown. Anything else: log it and re-serve.
            print("[worker] serve() returned unexpectedly - re-serving in 15s")
            time.sleep(15)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[worker] serve crashed: {type(exc).__name__}: {exc} — retrying in 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
