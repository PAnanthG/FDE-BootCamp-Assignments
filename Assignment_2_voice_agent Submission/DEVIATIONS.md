# Deviations from the upstream reference implementation

Scratch tracking file — not one of the three graded deliverables. Points here get folded
into the real docs when each is drafted (see the target section noted per entry), then this
file can be deleted.

## 1. Groq provider removed (OpenAI is now the only live provider)

**What changed:** `pipeline/providers.py`'s `PRESETS` dict originally had two live backends,
`groq` and `openai`, sharing one code path because both speak the OpenAI SDK dialect. At the
user's explicit request, the `groq` preset and all Groq references (code comments, `.env` /
`config.example.env` keys and comments, `README.md`, `pipeline/README.md`, `livekit/README.md`)
were removed. `PROVIDER` now defaults to `openai`; `PROVIDER=groq` raises
`ValueError: Unknown PROVIDER 'groq'; use one of ['openai']`.

**Why it happened (real operational reasons, not just preference):**
- Groq's free-tier daily token quota (100,000 TPD) was exhausted mid-session during Stage 2
  live testing, and repeated retries against a 429 appeared to push the reset further out
  rather than recover — a real production-relevant finding in its own right (see below).
- Separately, live testing against Groq surfaced a reproducible bug: `llama-3.3-70b-versatile`
  reliably emitted the `guests` tool argument as a quoted string (`"3"`) instead of an integer,
  which Groq's strict server-side schema validation rejected outright (400 `tool_use_failed`),
  with no retry/catch anywhere in the reference code. This was fixed at the schema level
  (`guests` now declared `"type": "string"` in `TOOLS`, with a defensive `_coerce_guests()` in
  `run_tool()`) — the fix itself is provider-agnostic and stays regardless of the Groq removal.

**What this costs the submission:** the reference implementation's deliberate teaching point —
"one interface, two backends, provider-agnostic by design" (`providers.py`'s original docstring:
"Groq speaks the OpenAI API dialect, so a single code path covers both") — no longer has a
second live backend to demonstrate it with. This is a genuine deviation from the graded
upstream repo, not an oversight.

**Where this needs to land in the real deliverables:**
- `SETUP-AND-REVIEW.md` §2 (Prerequisites) — currently planned to say "Provider choice: mock
  (zero setup) vs Groq free tier." Must be corrected to describe OpenAI as the only live
  provider, and should explain *why* (this note), not just state it as fact.
- `SETUP-AND-REVIEW.md` §6 (providers.py review) — the "one interface, three backends" framing
  in the fixed outline's own bullet list needs updating to reflect two backends (mock + openai),
  with the historical Groq-parity design decision still worth noting as a "what was true
  upstream" aside.
- `EVAL_REPORT.md` §5 (Live-provider delta) — this section's premise was "mock vs Groq." Needs
  reframing as "mock vs OpenAI," while still reporting the Groq findings (quota exhaustion
  behavior under retry, the guests-type-string bug) as historical evidence gathered before the
  provider was removed — that evidence is real and still valuable, it just describes a provider
  no longer in the codebase.
- `FDE-ANALYSIS.md` §1 or §7 — worth a short explicit callout that this is a deliberate,
  user-directed deviation from the reference implementation's multi-provider design, made for
  operational reasons (quota exhaustion blocking iteration) rather than a technical flaw in the
  original two-backend approach.

## 2. Aurora persona rename

`agent.py`'s `SYSTEM_PROMPT` and `voice_loop.py`'s greeting were changed from framing the agent
as "for Aurora Hotel" to introducing itself as "Aurora, a hotel reservations assistant" —
persona-only, at the user's request. Tool schemas, mock room data, `knowledge/hotel_policies.md`,
and evals were deliberately left untouched (the underlying single mock property still exists,
just not used as the agent's caller-facing identity). Verified via smoke_test/unittest/evals
after the change (all green). Belongs in `SETUP-AND-REVIEW.md` §6 (agent.py review) as a noted
customization from the stock reference prompt.

### 2a. Correction: three more stale greetings found during component review

While reading `livekit/talk_server.py` for the Stage 3 component review, found its own hardcoded
`GREETING` constant still said "Thanks for calling Aurora Hotel reservations." — missed during
the original rename because that pass only grepped/edited `agent.py` and `voice_loop.py`. A
follow-up repo-wide search for the literal phrase "aurora hotel" found three more: `livekit/
talk_server.py`, `livekit/web/talk.js` (browser TTS fallback), and `mocks/demo_call.py` /
`mocks/ivr_menu_mock.py`. All four updated to match the renamed persona greeting. Deliberately
left alone (confirmed out of scope): `README.md`'s title (the assignment's own given name),
`RUNBOOK.md` prose, `pipeline/providers.py`'s `DEFAULT_STT_PROMPT` (an STT vocabulary hint, never
spoken), `agent.py`'s tool-schema description (already a deliberate earlier decision), and
`MockProvider`'s scripted reply text that names the property (consistent with "mock data stays
untouched" — the property can still be named Aurora Hotel, only the agent's own self-introduction
changed). Re-verified: smoke_test/unittest/evals all green after the four fixes.

## 3. Native Spanish voice for system TTS

**What changed:** `agent.current_locale` (e.g. `es-ES` after a language switch) was tracked in
session state but never actually passed to `provider.synthesize()` — `voice_loop.py`'s `speak()`
and both `Provider.synthesize()`/`MockProvider.synthesize()` in `providers.py` ignored it
entirely. With `TTS_BACKEND=system`, macOS `say` always used the OS default voice regardless of
response language, so Spanish replies were spoken with an English voice's phonetic engine —
audibly non-native, per the user's report.

**Fix:** threaded `locale` through the whole call chain (`speak(provider, text, locale=...)` in
both `pipeline/voice_loop.py` and `web_demo/server.py` → `synthesize(text, locale=...)` in both
`Provider` and `MockProvider`). Added `SYSTEM_TTS_VOICES = {"es-ES": "Mónica"}` in `providers.py`
plus a `SYSTEM_TTS_VOICE_<LOCALE>` env override, and a `_system_tts_args()` helper that only adds
`-v <voice>` when a mapping exists for the current locale — English is untouched (no `-v` flag,
same as before, zero regression risk there).

**A real gotcha hit along the way:** the first attempt used `"Monica"` (no accent) as the voice
name, matched via `say -v '?'`'s output by eye. It silently failed — `say -v <bad-name>` does
**not** error, it exit-code-0s and silently falls back to the default voice, so the bug was
invisible until manually listening. The actual installed voice name is `"Mónica"` (with the
accented ó); confirmed via `subprocess.run(['say','-v','?'])` decoded as UTF-8 rather than by-eye
terminal grep, which is what surfaced the mismatch. Worth a callout in `FDE-ANALYSIS.md`'s
operational-fallbacks or observability section as a real example of a failure mode that fails
silently rather than loudly — the same category of risk as the codebase's broader "no retry/catch
around provider calls" gap, just in a different subsystem (local OS command invocation instead of
a network API call).

**Verified:** user listened to before/after and confirmed the post-fix Spanish audio sounds
native. `smoke_test.py`, all 16 unit tests, and the 12/12 eval suite stayed green throughout.
Belongs in `SETUP-AND-REVIEW.md` §6 (voice_loop.py / providers.py review) as a noted
customization, and the silent-fallback gotcha belongs in `FDE-ANALYSIS.md`.

## 4. New `web_demo/` front end (not part of the graded reference implementation)

A stdlib-only local web server + browser chat UI was built at the user's request as an
alternative to the Docker-dependent LiveKit demo (Docker/Homebrew were not installed on this
machine). Browser mic → `POST /turn` → Whisper STT → the same `Agent`/`Provider` code
`voice_loop.py` uses, unmodified → TTS plays server-side (same machine). Lives entirely outside
`pipeline/`, `evals/`, `livekit/`, `mocks/` so it's clearly separable from the reference
implementation. Two real bugs were found and fixed during manual testing (a connection-corrupting
early-return-without-draining-body bug, and a client-side state-sync issue on call-already-ended).
Belongs in `SETUP-AND-REVIEW.md` §4d (documented-only becomes "documented + a custom alternative
was built and used") and as supplementary evidence in `EVAL_REPORT.md`/`FDE-ANALYSIS.md` where
relevant (e.g. the connection bug is a good real example for the Operational Fallbacks section).
