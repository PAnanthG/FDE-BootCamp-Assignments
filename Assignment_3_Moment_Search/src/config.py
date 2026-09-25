"""Central env-driven config — every knob in one place.

Same conventions as the digital-twin-akash service: module-level constants,
provider-neutral STORAGE_* credentials with AWS_* fallbacks, Prefect Cloud
read straight from PREFECT_API_URL / PREFECT_API_KEY by the SDK.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"  # local-provider storage root (dev only)


def _envbool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- Database (Neon Postgres) — videos manifest, source of truth ------------
DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- API auth ----------------------------------------------------------------
# Bearer token required on every mutating endpoint (presign, register, delete,
# retry). The tenant is the X-User-Id header (default "default") — swap this
# for real per-user auth (JWT/Clerk) later without touching the data model:
# every bucket key, Postgres row, and Qdrant point is already user_id-tagged.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID", "default")

# --- Object storage (videos + frame thumbnails) ------------------------------
# STORAGE_PROVIDER: local | aws | gcp | gcp_native | flyio
# aws/gcp/flyio share one boto3 S3 client (different endpoints); gcp_native
# uses Google's SDK + service-account JSON; local writes under ./data (dev).
STORAGE_PROVIDER = os.getenv("STORAGE_PROVIDER", "local").strip().lower()
STORAGE_BUCKET = (os.getenv("STORAGE_BUCKET", "")
                  or os.getenv("BUCKET_NAME", "")            # injected by `fly storage create`
                  or os.getenv("GCS_BUCKET_NAME", "")        # gcp_native conventions
                  or os.getenv("GOOGLE_CLOUD_BUCKET_NAME", ""))
STORAGE_ACCESS_KEY_ID = os.getenv("STORAGE_ACCESS_KEY_ID", "").strip() or os.getenv("AWS_ACCESS_KEY_ID", "").strip()
STORAGE_SECRET_ACCESS_KEY = os.getenv("STORAGE_SECRET_ACCESS_KEY", "").strip() or os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
AWS_REGION = os.getenv("STORAGE_REGION", "").strip() or os.getenv("AWS_REGION", "auto")
_PROVIDER_ENDPOINTS = {
    "aws": None,  # boto3 default
    "flyio": "https://fly.storage.tigris.dev",
    "gcp": "https://storage.googleapis.com",
}
STORAGE_ENDPOINT = os.getenv("AWS_ENDPOINT_URL_S3", "").strip() or _PROVIDER_ENDPOINTS.get(STORAGE_PROVIDER)


def gcs_service_account_info() -> dict:
    """Service-account JSON for STORAGE_PROVIDER=gcp_native, rebuilt from the
    GOOGLE_CLOUD_* env vars (the standard exploded-JSON convention)."""
    key = os.getenv("GOOGLE_CLOUD_PRIVATE_KEY", "").strip()
    # dotenv strips surrounding quotes locally, but `fly secrets import` keeps
    # them literally — strip defensively so the PEM is valid in both places.
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    return {
        "type": "service_account",
        "project_id": os.getenv("GOOGLE_CLOUD_PROJECT_ID", ""),
        "private_key_id": os.getenv("GOOGLE_CLOUD_PRIVATE_KEY_ID", ""),
        "private_key": key.replace("\\n", "\n"),  # dotenv keeps \n literal inside quotes
        "client_email": os.getenv("GOOGLE_CLOUD_CLIENT_EMAIL", ""),
        "client_id": os.getenv("GOOGLE_CLOUD_CLIENT_ID", ""),
        "auth_uri": os.getenv("GOOGLE_CLOUD_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": os.getenv("GOOGLE_CLOUD_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": os.getenv(
            "GOOGLE_CLOUD_AUTH_PROVIDER_X509_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
        "client_x509_cert_url": os.getenv("GOOGLE_CLOUD_CLIENT_X509_CERT_URL", ""),
        "universe_domain": os.getenv("GOOGLE_CLOUD_UNIVERSE_DOMAIN", "googleapis.com"),
    }


# Bucket key layout — every key is user-scoped (tenant isolation at the path level):
#   uploads/{user_id}/{video_id}.{ext}      raw uploaded video (presigned PUT target)
#   frames/{user_id}/{video_id}/NNNNNN.jpg  downscaled frame thumbnails (citations)
UPLOAD_KEY_PREFIX = "uploads/"
FRAME_KEY_PREFIX = "frames/"

# --- Presigned uploads (browser -> bucket, bypassing the API) -----------------
PRESIGN_EXPIRY_S = _int("PRESIGN_EXPIRY_S", 900)          # presigned PUT lifetime
PRESIGN_GET_EXPIRY_S = _int("PRESIGN_GET_EXPIRY_S", 3600)  # thumbnails / playback
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 2048)                # register rejects bigger objects
ALLOWED_UPLOAD_TYPES = ("video/",)                         # content-type must start with

# --- Video ingest lifecycle ---------------------------------------------------
# pending  = registered, waiting in our fair queue (not yet sent to Prefect)
# queued   = the dispatcher picked it and scheduled a Prefect run
# fetching = acquiring the source file; sampling = frames + dedup + thumbnails;
# embedding = CLIP + Qdrant upsert; skipped = duplicate (user_id, source_hash).
VIDEO_STATUSES = ("pending", "queued", "fetching", "sampling", "embedding",
                  "indexed", "skipped", "failed")
# Document stages (papers/decks): chunking = parse + split; captioning = vision
# model on image-only slides. Same lifecycle otherwise.
DOCUMENT_STATUSES = ("pending", "queued", "fetching", "captioning", "chunking",
                     "embedding", "indexed", "skipped", "failed")
SOURCE_KINDS = ("video", "paper", "deck")
# In-flight = occupying execution capacity (scheduled or running).
# MUST list every working stage of every kind. A stage missing here is capacity
# the dispatcher believes is free while it is actually busy, so it over-admits
# and the ingest load that the <=1.3x search-latency SLA is measured against
# stops being bounded by DISPATCH_MAX_INFLIGHT.
INFLIGHT_STATUSES = ("queued", "fetching", "sampling", "captioning", "chunking",
                     "embedding")

# --- Fair scheduling (WFQ) ----------------------------------------------------
# FIFO (default off): register enqueues to Prefect immediately -> Prefect runs
# them in submitted order, so one user with 50 videos blocks everyone behind
# them. Fair dispatch (WFQ, on): videos wait `pending` in Postgres and a
# dispatcher admits them round-robin ACROSS users, keeping only
# DISPATCH_MAX_INFLIGHT running at once — so the waiting line is fairly ordered
# in OUR DB, not FIFO inside Prefect. No user can starve the others.
ENABLE_FAIR_DISPATCH = _envbool("ENABLE_FAIR_DISPATCH", True)
# Max videos executing at once. Set to your total capacity:
# (worker machines) x WORKER_CONCURRENCY — anything above that would just pile
# up FIFO inside Prefect and defeat the fairness.
DISPATCH_MAX_INFLIGHT = _int("DISPATCH_MAX_INFLIGHT", _int("WORKER_CONCURRENCY", 2))
DISPATCH_INTERVAL_S = _float("DISPATCH_INTERVAL_S", 3.0)  # how often the dispatcher tops up

# --- Crash recovery: reconcile sources orphaned by a dead worker -------------
# A source stuck in an in-flight status (queued/fetching/.../embedding) with no
# update in this many seconds is orphaned - the worker that owned it died
# mid-run, and nothing else in the system revisits a non-'pending' row, so it
# would sit there forever. Worse than losing just that source: it permanently
# occupies one slot of DISPATCH_MAX_INFLIGHT capacity forever, since
# count_inflight() counts it as busy. Observed live: a single killed worker
# orphaned 6 sources, which exactly exhausted DISPATCH_MAX_INFLIGHT=6 at the
# time and silently deadlocked the entire queue - new, unrelated registrations
# included, not just the 6 that crashed.
#
# set_status/set_progress touch updated_at on every real tick of a running
# flow, so updated_at only goes stale when nothing is left alive to update it.
# 600s has wide margin over the worst legitimate stall observed or configured:
# t_fetch's own retries span up to ~150s, t_embed_index's up to ~120s, plus
# real embedding time (12-35s/doc, measured) - so a healthy, still-retrying
# task is never mistaken for an orphan.
RECONCILE_STALE_AFTER_S = _int("RECONCILE_STALE_AFTER_S", 600)
# A source that is STILL landing back here after this many attempts is failing
# for a real reason (bad content, a bug), not bad luck - stop resetting it to
# 'pending' forever (which would loop indefinitely) and mark it failed, so it
# stops consuming dispatch capacity and reconciler attention for something
# that will never succeed unattended.
RECONCILE_MAX_ATTEMPTS = _int("RECONCILE_MAX_ATTEMPTS", 5)

# --- Frame sampling (the biggest scaling lever) --------------------------------
# interval: one frame every FRAME_INTERVAL_SEC (widened to respect MAX_FRAMES).
# scene:    one frame per detected cut (ffmpeg scene filter).
FRAME_STRATEGY = os.getenv("FRAME_STRATEGY", "interval").strip().lower()
FRAME_INTERVAL_SEC = _float("FRAME_INTERVAL_SEC", 2.0)
SCENE_THRESHOLD = _float("SCENE_THRESHOLD", 0.4)
MAX_FRAMES = _int("MAX_FRAMES", 400)
THUMB_WIDTH = _int("THUMB_WIDTH", 480)   # frames are downscaled in the ffmpeg pass
THUMB_QUALITY = _int("THUMB_QUALITY", 3)  # ffmpeg -q:v (2 best .. 31 worst)

# Perceptual-hash dedup — drop visually-identical neighbours BEFORE embedding.
DEDUP_ENABLED = _envbool("DEDUP_ENABLED", True)
DEDUP_MAX_DISTANCE = _int("DEDUP_MAX_DISTANCE", 4)  # Hamming distance on 64-bit dHash

# --- CLIP embeddings ------------------------------------------------------------
# One model encodes frames and text queries into a shared space. Runs on CPU
# inside the worker today; EMBED_VERSION is stamped on every Qdrant point so a
# future re-embed (or an external GPU CLIP service) can replace stale vectors
# without guessing.
CLIP_MODEL = os.getenv("CLIP_MODEL", "clip-ViT-B-32").strip()
CLIP_BATCH = _int("CLIP_BATCH", 128)   # frames per embed call (inner batch is 32)
# Threads per embedding session (torch for CLIP; ONNXRuntime for bge - each has
# its own pool, this caps both). `_text_model()` builds a FRESH ONNX session
# with `threads=` on every call and is NOT cached, so with EMBED_DOCS_LOCAL the
# worker can have up to WORKER_CONCURRENCY of these running at once - one per
# concurrent flow run. A flat default sized for one session oversubscribes as
# concurrency rises: measured at WORKER_CONCURRENCY=4 with the old flat 6,
# worker CPU hit 1000-1050% (ALL 10 cores) during the embedding stage, and
# search p95 during that window went 3x worse than at WORKER_CONCURRENCY=2 -
# not because search got more expensive, but because there was no CPU left on
# the host for the api/clip containers to be scheduled on. Docker does not
# reserve CPU per container by default, so full saturation in one container
# starves every other one, regardless of how little work they actually need.
#
# Dividing by WORKER_CONCURRENCY keeps (sessions x threads) <= cpu_count even
# in the worst case where every concurrent flow is embedding simultaneously,
# so raising ingest concurrency no longer costs the search path its CPU.
_worker_concurrency = _int("WORKER_CONCURRENCY", 2)
EMBED_SERVICE_THREADS = _int(
    "EMBED_SERVICE_THREADS",
    max(1, (os.cpu_count() or 4) // max(1, _worker_concurrency)))
# Embed document chunks IN the worker instead of calling the shared service.
# The service exists to keep ONE warm CLIP model, which is genuinely expensive
# to load (~15-30s). The bge text model is small and cheap, so centralising it
# buys nothing and costs everything: bulk ingest batches and latency-critical
# query encodes end up in the same process, and a 64-chunk batch blocks search
# for seconds. Video frames still go to the service - that is what it is for.
EMBED_DOCS_LOCAL = _envbool("EMBED_DOCS_LOCAL", False)

# --- Dedicated document-embedding service (src/embed_service.py) -------------
# Bulk chunk embedding gets its OWN service, separate from the clip service.
# Two distinct problems it solves, both measured:
#   1. Memory. Prefect runs each flow as a subprocess, so WORKER_CONCURRENCY=6
#      meant six copies of the bge model in the worker - 7.25 GiB of a 7.75 GiB
#      box, and concurrency 8 was OOM-killed. One warm model replaces all six.
#   2. Utilisation. bge scales sub-linearly with threads (3.3 chunks/s at 1
#      thread, 9.0 at 4), so many small single-threaded inferences waste the
#      box. The service micro-batches concurrent requests into one larger call.
# It must NOT be merged back into clip_service: that one is on the search path,
# and bulk batches sharing it caused 8.5s query stalls.
# Empty = fall back to EMBED_DOCS_LOCAL / clip-service behaviour.
DOC_EMBED_SERVICE_URL = os.getenv("DOC_EMBED_SERVICE_URL", "").strip().rstrip("/")
# Threads for that service. Leaves headroom so it cannot starve api/clip the way
# an uncapped embedder did (Docker reserves no CPU per container by default).
DOC_EMBED_SERVICE_THREADS = _int("DOC_EMBED_SERVICE_THREADS",
                                 max(1, (os.cpu_count() or 4) - 4))
# How long to wait for sibling requests before running a batch. Milliseconds:
# invisible next to a multi-second ingest, long enough for concurrent flow runs
# to coalesce.
DOC_EMBED_BATCH_WINDOW_MS = _int("DOC_EMBED_BATCH_WINDOW_MS", 25)
# Upper bound on one coalesced inference, so a burst cannot build an
# unboundedly large batch and spike memory.
DOC_EMBED_MAX_BATCH = _int("DOC_EMBED_MAX_BATCH", 256)
# Upper bound on one inference in PADDED TOKENS (batch_size x longest sequence).
#
# DOC_EMBED_MAX_BATCH alone does NOT bound memory: a transformer's activation
# cost is O(batch x seq^2), so "256 chunks" means wildly different peaks
# depending on how long those chunks are. Worse, ONNXRuntime's CPU arena grows
# to the largest batch it has ever seen and NEVER returns it to the OS - so a
# single oversized batch permanently raises this container's floor.
#
# Measured on this box: ONE batch of 170 chunks took the service from 238 MiB
# to 2.906 GiB in a single inference, and it stayed there. Left running across
# a session it reached 5.5 GiB of a 7.75 GiB VM - 72% of the whole machine,
# while idle at 0.2% CPU - which is what put the box into page reclaim and
# produced the rare multi-second search stalls documented in D12.
#
# SIZING - and a measured warning against tightening this.
#
# The first value tried here was 12000 (~40 paper chunks). It worked as
# designed on memory (peak 2.9 GiB -> 1.36 GiB) and made SEARCH LATENCY WORSE:
# it split 6 coalesced batches into 14 sub-batches, and because bge scales
# sub-linearly, more-but-smaller inferences keep this service's 6 ONNX threads
# hot for a LONGER wall-clock window on a 10-core box. Measured p95
# time-to-citations during ingest went 657ms -> 1466ms (SLA ratio 1.94 ->
# 5.72) while memory fell. Memory and latency moved in opposite directions,
# which is what ruled memory OUT as the cause of the search stalls - the
# contention is CPU occupancy, not page reclaim (see DECISIONS.md D12).
#
# So this is deliberately set ABOVE the largest batch observed in practice
# (~170 chunks ~= 51k padded tokens): normal work runs as ONE inference and is
# never fragmented, while a pathological burst still cannot grow the arena the
# way an uncapped batch did. It is a ceiling, not a target. Lower it only with
# a benchmark in hand - the throughput/latency cost showed up immediately.
DOC_EMBED_MAX_BATCH_TOKENS = _int("DOC_EMBED_MAX_BATCH_TOKENS", 56000)
# bge-small truncates at 512 tokens; ~4 chars/token for English prose. Used only
# to size batches, never to alter what gets embedded.
DOC_EMBED_MODEL_MAX_TOKENS = _int("DOC_EMBED_MODEL_MAX_TOKENS", 512)
# Inference-service URL ("embedding is a URL"). Set -> api/worker send batches
# to the warm clip_service.py container instead of loading the model in-process
# (which costs each Prefect run subprocess a fresh ~15-30s torch load). Unset
# -> in-process embedding (simple mode, no extra service). Point it at a GPU
# machine later — nothing else changes.
CLIP_SERVICE_URL = os.getenv("CLIP_SERVICE_URL", "").strip().rstrip("/")
# Vector dimension override. 0 = auto: known CLIP models resolve from a table
# (so the API can create the collection at boot WITHOUT loading the model);
# unknown models load the model to measure. Set explicitly for custom models.
CLIP_DIM = _int("CLIP_DIM", 0)
EMBED_VERSION = os.getenv("EMBED_VERSION", f"{CLIP_MODEL}-v1")

# --- Multimodal: transcript (text) branch (Path 1) -----------------------------
# The visual branch is CLIP frames (above). This adds a SECOND branch: YouTube
# captions, chunked by time, embedded with a small semantic text model (bge via
# fastembed — CPU, free), in a separate Qdrant collection. At query time both
# branches run and fuse by RANK (RRF) — CLIP scores (~0.3) and text scores
# (~0.7) live on different scales, so raw-score comparison is meaningless.
# Uploaded files have no captions, so this is YouTube-only; a video with no
# captions just indexes visually (the branch is skipped, never fatal).
ENABLE_TRANSCRIPT = _envbool("ENABLE_TRANSCRIPT", True)
TEXT_COLLECTION = os.getenv("TEXT_COLLECTION", "moments_text")
# Transcript-branch embedding PROVIDER — env decides the model:
#   fastembed (default) -> bge via fastembed: CPU, free, NO API key (keeps search
#                          working keyless for a fresh cloner). Dim 384.
#   openai              -> OpenAI (or any OpenAI-compatible) embeddings API, e.g.
#                          text-embedding-3-small. Hosted, stronger retrieval,
#                          costs per call + needs a key. Reuses the LLM_* key /
#                          base_url by default (override with TEXT_EMBED_API_KEY /
#                          TEXT_EMBED_BASE_URL). Setting the provider alone flips
#                          the default model+dim to 3-small / 1536.
# The model & dim MUST match between indexing and querying, so switching provider
# means RE-SEEDING the transcript collection (its vector dim changes). The two
# branches fuse by RANK (RRF), so the text model is independent of CLIP.
TEXT_EMBED_PROVIDER = os.getenv("TEXT_EMBED_PROVIDER", "fastembed").strip().lower()
_TE_OPENAI = TEXT_EMBED_PROVIDER == "openai"
TEXT_EMBED_MODEL = os.getenv(
    "TEXT_EMBED_MODEL",
    "text-embedding-3-small" if _TE_OPENAI else "BAAI/bge-small-en-v1.5")
TEXT_EMBED_DIM = _int("TEXT_EMBED_DIM", 1536 if _TE_OPENAI else 384)
# openai provider: falls back to the LLM key/base_url in embeddings.py so ONE
# OpenAI key can power both the answer and the text embeddings.
TEXT_EMBED_API_KEY = os.getenv("TEXT_EMBED_API_KEY", "").strip()
TEXT_EMBED_BASE_URL = os.getenv("TEXT_EMBED_BASE_URL", "").strip()
TEXT_EMBED_VERSION = os.getenv("TEXT_EMBED_VERSION", f"{TEXT_EMBED_MODEL}-v1")
# Transcript chunking: group caption cues into ~CHUNK_SECONDS windows so a chunk
# is a coherent spoken passage with a real t_start/t_end, not one tiny cue.
TRANSCRIPT_CHUNK_SECONDS = _float("TRANSCRIPT_CHUNK_SECONDS", 20.0)

# --- Document ingest (papers + decks) -----------------------------------------
# Vision captions for image-only slides are independent per slide and each one
# is a round trip to a hosted model, so they run concurrently. 4 is a deliberate
# middle: the wall-clock win is nearly all captured by the first few workers,
# while going wider mostly buys provider rate-limit errors. Set 1 to serialise.
DECK_CAPTION_CONCURRENCY = _int("DECK_CAPTION_CONCURRENCY", 4)
# Chunks per embedding call. Bounds peak memory and keeps one oversized request
# from failing a whole document when the provider is a hosted API.
DOC_EMBED_BATCH = _int("DOC_EMBED_BATCH", 64)
# Chunk sizing is per-kind rather than one global number - see docparse.
PAPER_CHUNK_CHARS = _int("PAPER_CHUNK_CHARS", 1200)
PAPER_CHUNK_OVERLAP = _int("PAPER_CHUNK_OVERLAP", 150)
DECK_CHUNK_CHARS = _int("DECK_CHUNK_CHARS", 1800)
DECK_CHUNK_OVERLAP = _int("DECK_CHUNK_OVERLAP", 100)
TRANSCRIPT_LANGS = [c.strip() for c in
                    os.getenv("TRANSCRIPT_LANGS", "en,en-US,en-GB").split(",") if c.strip()]

# --- Fusion (multimodal retrieval) ---------------------------------------------
# RRF: rank-based fusion across branches (score-agnostic). rrf = 1/(K + rank).
RRF_K = _int("RRF_K", 60)
# Hits from either branch within this many seconds are the SAME moment.
FUSION_WINDOW_S = _float("FUSION_WINDOW_S", 15.0)
# When a window has BOTH a frame and a transcript hit, multiply its score —
# two independent modalities agreeing is the strongest relevance signal.
CROSS_MODAL_BOOST = _float("CROSS_MODAL_BOOST", 1.5)
# Per-branch candidates fetched before fusion.
BRANCH_TOP_K = _int("BRANCH_TOP_K", 20)
# Citation-list diversity: at most this many moments from any ONE source before
# other sources get a turn. Overflow is demoted, never dropped, so a question
# only one source can answer still returns everything it has. 0 disables.
MAX_PER_SOURCE = _int("MAX_PER_SOURCE", 2)
# A moment whose only evidence is a video frame - nothing was said about it -
# must clear this to earn a citation slot. Higher than CONFIDENCE_THRESHOLD by
# design: a frame corroborated by the transcript is still admitted at the lower
# bar, because two independent signals agreeing IS the evidence.
# Calibrated by measurement on this corpus, not taste: an off-corpus question
# scores CLIP 0.230 at best, on-topic questions reach 0.266-0.341.
VISUAL_ONLY_THRESHOLD = _float("VISUAL_ONLY_THRESHOLD", 0.30)

# --- YouTube download hardening ---------------------------------------------------
# YouTube increasingly answers yt-dlp's default web client with "Sign in to
# confirm you're not a bot". Mitigations, in order of reliability:
#   YT_COOKIES_FILE  path to a Netscape cookies.txt exported from a logged-in
#                    browser (yt-dlp wiki: "Exporting YouTube cookies").
#                    In docker-compose, drop it at ./data/cookies.txt and set
#                    YT_COOKIES_FILE=/app/data/cookies.txt
#   YT_PROXY_URL     route YouTube traffic through a (residential) proxy,
#                    e.g. http://user:pass@host:port
#   YT_RETRY_CLIENTS on a bot-check error, automatically retry once with these
#                    alternate YouTube player clients (comma-separated).
# Cookies make yt-dlp an authenticated client — the one fix that works from
# ANY IP (home OR datacenter). Two ways to supply them, so the same code works
# local and deployed:
#   YT_COOKIES_FILE  path to a mounted cookies.txt   (easy locally)
#   YT_COOKIES_B64   base64 of cookies.txt as a secret (Fly/cloud: no file mount
#                    needed — the worker writes it to a temp file at runtime)
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "").strip()
YT_COOKIES_B64 = os.getenv("YT_COOKIES_B64", "").strip()
YT_PROXY_URL = os.getenv("YT_PROXY_URL", "").strip()
# Player clients. Default EMPTY = let yt-dlp pick (best, once a JS runtime is
# present — see below). Forcing tv/android used to help pre-JS-runtime, but now
# those clients hand back media URLs that 403 on download, so we only fall back
# to them if the default attempt fails outright.
YT_PLAYER_CLIENTS = [c.strip() for c in
                     os.getenv("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
YT_FALLBACK_CLIENTS = [c.strip() for c in
                       os.getenv("YT_FALLBACK_CLIENTS", "tv,android,ios").split(",") if c.strip()]
# yt-dlp 2025+ needs a JavaScript runtime to compute YouTube signatures, plus
# its EJS challenge-solver component — WITHOUT these every video fails with
# "This video is not available" / "Requested format is not available". The
# Dockerfile installs Node; these tell yt-dlp to use it + fetch the solver.
# (For bare-process dev, install node or deno yourself.)
YT_JS_RUNTIMES = [c.strip() for c in
                  os.getenv("YT_JS_RUNTIMES", "node").split(",") if c.strip()]
YT_REMOTE_COMPONENTS = [c.strip() for c in
                        os.getenv("YT_REMOTE_COMPONENTS", "ejs:github").split(",") if c.strip()]

# --- Sample corpus ------------------------------------------------------------
# On boot the worker auto-ingests the four "Deep Dive into LLMs" sample talks
# (src/samples.py) if they aren't indexed yet — a fresh clone is queryable on
# the / page without running anything by hand. Set false to skip.
SEED_SAMPLE_VIDEOS = _envbool("SEED_SAMPLE_VIDEOS", True)

# --- Work orchestration (Prefect Cloud) ----------------------------------------
# The SDK reads PREFECT_API_URL / PREFECT_API_KEY from the environment directly.
# WORKER_CONCURRENCY is read by worker.py; retries live on the flow's tasks.

# --- Qdrant ----------------------------------------------------------------------
# One shared multi-tenant collection: every point carries user_id (tenant payload
# index) and every search/upsert/delete is user_id-filtered.
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or os.getenv("QDRANT_TOKEN", "").strip()
QDRANT_LOCAL_PATH = os.getenv("QDRANT_LOCAL_PATH", str(DATA / "qdrant"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "moments")
# Low-RAM profile: original vectors on disk, int8-quantized copies pinned in
# RAM (~4x smaller), HNSW graph on disk; queries rescore against the originals.
# Frames balloon vector counts fast, so these default ON.
QDRANT_QUANTIZATION = _envbool("QDRANT_QUANTIZATION", True)
QDRANT_ON_DISK = _envbool("QDRANT_ON_DISK", True)
QDRANT_HNSW_ON_DISK = _envbool("QDRANT_HNSW_ON_DISK", True)

# --- Retrieval / faithfulness ------------------------------------------------------
TOP_K = _int("TOP_K", 6)                 # frames fed to the multimodal LLM (3-8)
KNN_K = _int("KNN_K", 24)                # candidates fetched before trimming to TOP_K
# Gate 1: abstain WITHOUT calling the LLM if BOTH branches' best raw score is
# below their threshold. Fusion scores are RRF (tiny), so the gate uses each
# branch's own raw cosine. CLIP text->image cosines run low (~0.2-0.35); bge
# text-text cosines run higher (~0.5-0.7 for real matches). 0 disables.
# Calibrated against 18 answerable + 14 off-corpus questions run through the
# live index (benchmark/calibrate_thresholds.py, results in _calibration.json).
# The shipped defaults were 0.2 / 0.35, both BELOW where an off-corpus question
# scores - "recipe for sourdough bread" hit CLIP 0.230 / bge 0.567 - so the
# abstention gate could never fire and the system answered anything.
#
# Text: negatives top out at 0.7055. Most positives sit at 0.7375+, but the
# graded deck probe ("the slide about one index for every source") scores
# 0.7162 - it is phrased in the assignment's vocabulary, not the corpus's.
# 0.71 separates all 34 labelled queries, but the margin either side is only
# ~0.006. That is THIN: expect it to need re-running as the corpus grows, and
# treat Stage 7's labelled query set as the real calibration.
# Visual OVERLAPS (negatives reach 0.282, positives start 0.2655) so it cannot
# separate alone; 0.29 is chosen because the gate is OR, so a positive the
# visual branch misses is still answered by the text branch. Together they
# score 0/32 errors on the labelled set.
# Re-run the calibration after the corpus changes - these are corpus-specific.
CONFIDENCE_THRESHOLD = _float("CONFIDENCE_THRESHOLD", 0.29)              # visual (CLIP)
TEXT_CONFIDENCE_THRESHOLD = _float("TEXT_CONFIDENCE_THRESHOLD", 0.71)  # transcript (bge)

# --- Multimodal LLM (answer synthesis only — retrieval works without it) -----------
# LLM_PROVIDER: openai | nvidia | anthropic ("openai" also covers any
# OpenAI-compatible server via LLM_BASE_URL: Ollama, vLLM, OpenRouter, ...).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 1024)
LLM_IMAGE_MAX_PX = _int("LLM_IMAGE_MAX_PX", 512)  # frames are downscaled again before the LLM


def llm_configured() -> bool:
    # Local OpenAI-compatible servers often need no key, so a base_url alone counts.
    return bool(LLM_API_KEY or LLM_BASE_URL)
