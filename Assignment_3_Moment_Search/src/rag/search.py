"""Read path: question -> retrieve -> gate -> cited answer (or honest abstain).

Retrieval is milliseconds; the multimodal LLM call is seconds and dominates
cost. So the shape is a confidence funnel: fetch KNN_K candidates, collapse
temporal near-duplicates, trim to TOP_K, and — Gate 1 — if even the best
score is below CONFIDENCE_THRESHOLD, abstain WITHOUT calling the LLM. That
one free check kills most hallucination risk. Generated answers get their
[n] citations validated; invented references are stripped.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import config, db, llm, storage
from ..config import (BRANCH_TOP_K, CONFIDENCE_THRESHOLD, CROSS_MODAL_BOOST,
                      FUSION_WINDOW_S, MAX_PER_SOURCE, RRF_K,
                      TEXT_CONFIDENCE_THRESHOLD, TOP_K,
                      VISUAL_ONLY_THRESHOLD)
from . import vector_store
from .embeddings import embed_query, embed_text

ABSTAIN = ("I couldn't find that in your videos — nothing indexed looks "
           "related to the question (neither what's on screen nor what's said).")


def _seconds(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _fuse(visual_hits: list[dict], text_hits: list[dict]) -> list[dict]:
    """Reciprocal-Rank-Fusion of the two branches into time windows.

    Raw scores are incomparable (CLIP ~0.3 vs bge ~0.7), so we rank each branch
    on its own and score by rank: rrf = 1/(RRF_K + rank). Then we bucket hits
    within FUSION_WINDOW_S seconds of each other (same video) into one 'moment',
    sum their rrf, and boost windows where BOTH modalities agree — two
    independent signals pointing at the same instant is the strongest evidence.
    """
    def ranked(hits, modality, threshold):
        """Rank a branch, dropping hits that are not actually relevant.

        RRF scores by RANK ONLY, so without an admission filter the visual
        branch's best hit scores 1/(RRF_K+0) no matter how weak it is - exactly
        tying the text branch's best hit. With 20 candidates per branch that
        handed video half of every result list by construction, regardless of
        whether anything was on screen.

        The thresholds already exist and are already calibrated per branch
        (CLIP text->image cosines run ~0.2-0.35, bge text-text ~0.5-0.7); they
        were only being consulted for abstention. Using them for admission too
        means a branch that has nothing relevant contributes nothing, instead
        of contributing its least-bad guesses.

        `margin` is how far above its own threshold a hit sits, normalised so
        the two branches are comparable. It breaks RRF ties on evidence rather
        than on which list happened to be concatenated first - which is what
        made this systematically favour video.
        """
        out = []
        for rank, h in enumerate(hits):
            score = float(h.get("score", 0.0))
            if threshold and score < threshold:
                continue
            t = float(h.get("t_start", h.get("ms", 0) / 1000.0))
            margin = (score - threshold) / max(1e-6, 1.0 - threshold)
            out.append({**h, "modality": modality, "rrf": 1.0 / (RRF_K + rank),
                        "t": t, "kind": h.get("kind") or "video",
                        "margin": max(0.0, margin)})
        return out

    def same_window(w, h) -> bool:
        """Does hit `h` belong in window `w`?

        Video groups by TIME: hits within FUSION_WINDOW_S seconds are one
        moment, because a frame and the words spoken over it are the same
        event. Documents have no timeline - `t` would be 0.0 for every chunk,
        so time-bucketing would collapse an entire 21-page paper into ONE
        citation. They group by locator instead: one window per page or slide,
        which is also the unit a reader can actually be sent to.
        """
        if w["source_id"] != h["video_id"] or w["kind"] != h["kind"]:
            return False
        if h["kind"] == "video":
            return abs(w["t"] - h["t"]) <= FUSION_WINDOW_S
        return w["loc"] == h.get("loc")

    windows: list[dict] = []
    # Hits arrive best-first (rrf desc), so the first hit landing in a window for
    # a given modality is that modality's best hit there.
    admitted = (ranked(visual_hits, "frame", CONFIDENCE_THRESHOLD)
                + ranked(text_hits, "text", TEXT_CONFIDENCE_THRESHOLD))
    for h in sorted(admitted, key=lambda x: (x["rrf"], x["margin"]), reverse=True):
        w = next((w for w in windows if same_window(w, h)), None)
        if w is None:
            w = {"source_id": h["video_id"], "video_id": h["video_id"],
                 "kind": h["kind"], "loc": h.get("loc"), "t": h["t"], "rrf": 0.0,
                 "margin": 0.0, "modalities": set(), "frame": None, "text": None}
            windows.append(w)
        w["margin"] = max(w["margin"], h["margin"])
        w["modalities"].add(h["modality"])
        slot = "frame" if h["modality"] == "frame" else "text"
        # Keep only the BEST hit per modality. Summing every hit would let a
        # burst of near-identical frames clustered in one 15s window inflate its
        # score past a genuine frame+transcript match — the bug that ranked a
        # silent frame-burst above the moment that actually answered.
        if w[slot] is None:
            w[slot] = h
    for w in windows:
        # Score = best frame + best transcript hit; ×boost when BOTH modalities
        # agree at this instant (two independent signals = strongest evidence).
        w["rrf"] = (w["frame"]["rrf"] if w["frame"] else 0.0) + \
                   (w["text"]["rrf"] if w["text"] else 0.0)
        if {"frame", "text"} <= w["modalities"]:
            w["rrf"] *= CROSS_MODAL_BOOST

    # A window whose ONLY evidence is a frame has to clear a higher bar than one
    # corroborated by speech. Measured on this corpus: an off-corpus question
    # ("recipe for sourdough bread") still scores CLIP 0.230 at best, while
    # on-topic questions reach 0.266-0.341 - so CONFIDENCE_THRESHOLD=0.2 admits
    # literal noise, and a frame nobody was talking about was filling citation
    # slots with "Visual frame at 17:41" and no quotable text.
    #
    # This is deliberately asymmetric rather than just raising
    # CONFIDENCE_THRESHOLD: a frame BACKED by a transcript hit is still good
    # evidence at a lower score, because two independent signals agree. Only
    # uncorroborated visual guesses are held to the stricter bar.
    windows = [w for w in windows
               if w["text"] is not None
               or float((w["frame"] or {}).get("score", 0.0)) >= VISUAL_ONLY_THRESHOLD]

    windows.sort(key=lambda w: (w["rrf"], w["margin"]), reverse=True)
    return _cap_per_source(windows)


def _cap_per_source(windows: list[dict]) -> list[dict]:
    """Stop one source monopolising the citation list.

    Even with fair scoring, a single 60-minute video contributes far more
    windows than a 21-page paper, so a question it answers well can fill every
    slot. Six citations from one source is a worse answer than four from that
    source plus two corroborating ones elsewhere - and the assignment is
    explicitly about answering ACROSS sources.

    Overflow is not discarded, just demoted: if there is nothing else to show,
    the capped windows still appear, in their original order. So a genuinely
    single-source question degrades to exactly the old behaviour.
    """
    if MAX_PER_SOURCE <= 0:
        return windows
    kept: list[dict] = []
    overflow: list[dict] = []
    seen: dict[str, int] = {}
    for w in windows:
        sid = w["source_id"]
        seen[sid] = seen.get(sid, 0) + 1
        (kept if seen[sid] <= MAX_PER_SOURCE else overflow).append(w)
    return kept + overflow


def _deeplink(video: dict | None, video_id: str, ms: int) -> str:
    secs = ms // 1000
    if video and video.get("source") == "youtube" and video.get("url"):
        sep = "&" if "?" in video["url"] else "?"
        return f"{video['url']}{sep}t={secs}"
    return f"/api/video/{video_id}#t={secs}"


def _doc_deeplink(meta: dict | None, kind: str, loc: int | None) -> str | None:
    """Deep-link into a document at its page or slide.

    `#page=N` is the PDF Open Parameters fragment, honoured by Chrome, Safari,
    Firefox and Acrobat, so a paper citation opens on the cited page rather
    than at the front of a 21-page PDF. Decks are PDFs too in the common case,
    and a slide number is a page number, so the same fragment applies.
    """
    url = (meta or {}).get("url")
    if not url or loc is None:
        return url
    return f"{url}#page={loc}"


def _thumb_url(user_id: str, video_id: str, idx: int) -> str:
    """Browser-facing thumbnail URL. Presigned GET straight to the bucket when
    the provider supports it (an <img> tag can't send auth headers); the API
    serves the bytes itself only in local-dev mode."""
    if storage.presign_capable():
        return storage.presign_get(storage.frame_key(user_id, video_id, idx))
    return f"/api/frame/{video_id}/{idx:06d}.jpg?u={user_id}"


def _media_url(video: dict | None, user_id: str, video_id: str) -> str | None:
    """Playback URL for uploaded videos (YouTube plays via its own URL)."""
    if not video or video.get("source") != "upload" or not video.get("storage_key"):
        return None
    if storage.presign_capable():
        return storage.presign_get(video["storage_key"])
    return f"/api/video/{video_id}?u={user_id}"


def retrieve(question: str, user_id: str, *, top_k: int | None = None,
             video_id: str | None = None,
             video_ids: list[str] | None = None) -> dict[str, Any]:
    """Multimodal retrieve: query BOTH branches (CLIP frames + transcript text),
    fuse by RRF into time windows, and return numbered moment-citations.

    Returns {citations, best_visual, best_text} — the two raw bests feed the
    confidence gate (RRF scores are too small to threshold on). video_ids scopes
    the search to chosen videos (UI select/unselect)."""
    k = top_k or TOP_K

    # Visual branch — CLIP text→image.
    vhits = vector_store.search(embed_text(question), user_id, top_k=BRANCH_TOP_K,
                                video_id=video_id, video_ids=video_ids)
    best_visual = vhits[0]["score"] if vhits else 0.0

    # Text branch — bge query→transcript-chunk (only if transcript is enabled).
    thits: list[dict] = []
    best_text = 0.0
    if config.ENABLE_TRANSCRIPT:
        thits = vector_store.search_text(embed_query(question), user_id,
                                         top_k=BRANCH_TOP_K, video_id=video_id,
                                         video_ids=video_ids)
        best_text = thits[0]["score"] if thits else 0.0

    windows = _fuse(vhits, thits)[:k]
    videos = db.videos_by_ids(sorted({w["video_id"] for w in windows}))
    citations = []
    for i, w in enumerate(windows, 1):
        vid = w["video_id"]
        meta = videos.get(vid)
        fr, tx = w["frame"], w["text"]
        # Anchor on the frame's exact timestamp when there is one (precise visual
        # seek); otherwise the transcript chunk's start.
        ms = int(fr["ms"]) if fr else int(w["t"] * 1000)
        idx = int(fr["idx"]) if fr else None
        kind = w.get("kind") or "video"

        # Locator: what a reader is actually sent to. One shape per kind, which
        # is what eval.py asserts on (locator.page / locator.slide) and what the
        # UI branches on to decide "seek the player" vs "open to page".
        if kind == "paper":
            locator = {"page": w["loc"]}
        elif kind == "deck":
            locator = {"slide": w["loc"]}
        else:
            locator = {"ms": ms, "timestamp": _seconds(ms)}

        # `text` must be non-empty for every citation - the grounding check
        # requires it, and a citation with no quotable content is not evidence.
        # A frame-only video moment has no transcript; rather than emit null (or
        # invent a quote, which would be fabrication) it gets a factual
        # descriptor of what was actually retrieved.
        body = (tx or {}).get("text")
        if not body:
            body = (f"Visual frame at {_seconds(ms)}"
                    if kind == "video" else "")

        citations.append({
            "n": i,
            "kind": kind,
            "locator": locator,
            "text": body,
            "source_id": vid,
            "video_id": vid,
            "title": (meta or {}).get("title") or vid,
            "url": (meta or {}).get("url"),
            "source": (meta or {}).get("source"),
            # Video-shaped fields. Kept for every kind so the existing UI keeps
            # working unchanged (ui/index.html reads c.timestamp and c.deeplink
            # directly); they are simply null for documents, which have no
            # timeline, no frame and no player.
            "ms": ms if kind == "video" else None,
            "timestamp": _seconds(ms) if kind == "video" else None,
            "idx": idx,
            "thumbnail": (_thumb_url(user_id, vid, idx)
                          if kind == "video" and idx is not None else None),
            "media_url": _media_url(meta, user_id, vid) if kind == "video" else None,
            "deeplink": (_deeplink(meta, vid, ms) if kind == "video"
                         else _doc_deeplink(meta, kind, w["loc"])),
            "score": round(w["rrf"], 4),
            "transcript": (tx or {}).get("text"),
            "modalities": sorted(w["modalities"]),
        })
    return {"citations": citations, "best_visual": best_visual, "best_text": best_text}


def _fallback_answer(citations: list[dict[str, Any]]) -> str:
    """No-LLM summary: rank the visually-closest moments. Honest about being
    similarity, not synthesis."""
    top = citations[0]
    where = f"{top['title']} at {top['timestamp']}" if top.get("title") else top["timestamp"]
    others = ", ".join(f"{c['timestamp']} [{c['n']}]" for c in citations[1:4])
    msg = f"Closest visual match: {where} [{top['n']}] (similarity {top['score']})."
    if others:
        msg += f" Other relevant moments: {others}."
    return msg


_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _validate_citations(answer: str, n_frames: int) -> str:
    """Strip invented [n] references the model has no frame for."""
    def fix(m: re.Match) -> str:
        nums = [int(x) for x in re.split(r"\s*,\s*", m.group(1))]
        valid = [str(x) for x in nums if 1 <= x <= n_frames]
        return f"[{', '.join(valid)}]" if valid else ""
    return _CITE_RE.sub(fix, answer)


def _build_moments(user_id: str, citations: list[dict[str, Any]]) -> list[dict]:
    """Turn citations into what the LLM sees: each moment carries its frame
    image (if any) and/or its transcript excerpt (if any), numbered to match."""
    def frame_bytes(c):
        if c.get("idx") is None:
            return None
        try:
            return storage.get_bytes(storage.frame_key(user_id, c["video_id"], c["idx"]))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        images = list(ex.map(frame_bytes, citations))
    return [{"image": img, "transcript": c.get("transcript"),
             "timestamp": c["timestamp"]} for img, c in zip(images, citations)]


def resolve_llm(user_id: str) -> tuple[llm.LLMConfig | None, str]:
    """Which model answers for this tenant: their own hosted endpoint
    (ms_user_llms — e.g. a vLLM server) first, the server-wide LLM_* env
    config as fallback. Returns (config, source) with source in
    {"user", "server", "none"}."""
    row = db.get_user_llm(user_id)
    if row and row.get("model"):
        return llm.from_row(row), "user"
    cfg = llm.env_config()
    return (cfg, "server") if cfg else (None, "none")


def ask(question: str, user_id: str, *, top_k: int | None = None,
        video_id: str | None = None,
        video_ids: list[str] | None = None,
        retrieved: dict[str, Any] | None = None) -> dict[str, Any]:
    """`retrieved` lets a caller that has already run `retrieve()` reuse it.

    /ask_stream emits citations before synthesis, so without this it would run
    the whole retrieval twice per request - doubling the work on the exact
    endpoint bench.py measures search latency against.
    """
    r = retrieved or retrieve(question, user_id, top_k=top_k, video_id=video_id,
                              video_ids=video_ids)
    citations = r["citations"]
    result: dict[str, Any] = {"question": question, "citations": citations}

    if not citations:
        result.update(answer="No relevant moments were found. Try ingesting a video first.",
                      llm_used=False, abstained=True)
        return result

    # Gate 1 — confidence on the RAW per-branch bests (not the RRF score).
    # Abstain only if NEITHER what's on screen nor what's said looks relevant.
    visual_ok = r["best_visual"] >= CONFIDENCE_THRESHOLD
    text_ok = r["best_text"] >= TEXT_CONFIDENCE_THRESHOLD
    if CONFIDENCE_THRESHOLD and not visual_ok and not text_ok:
        result.update(answer=ABSTAIN, llm_used=False, abstained=True)
        return result

    cfg, source = resolve_llm(user_id)
    if cfg is None:
        # No generative model — summarize the best matches instead of inventing.
        result.update(answer=_fallback_answer(citations), llm_used=False,
                      note=("Retrieval-only results. Connect your own model "
                            "(vLLM/Ollama/API) in settings, or set LLM_API_KEY "
                            "on the server, for a synthesized, grounded answer."))
        return result

    moments = _build_moments(user_id, citations)
    result["answer"] = _validate_citations(llm.answer(question, moments, cfg),
                                           len(citations))
    result["llm_used"] = True
    result["llm_source"] = source          # "user" = their own hosted model
    result["llm_model"] = cfg.model
    return result
