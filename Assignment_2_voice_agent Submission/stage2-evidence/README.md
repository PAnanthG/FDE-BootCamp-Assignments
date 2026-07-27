# Stage 2 evidence — live OpenAI (gpt-4o-mini) runs

Captured against the final code state (post guests-schema fix, post Groq removal, post
Spanish-voice fix). `PROVIDER=openai`, `TTS_BACKEND=system` (macOS `say`/`afplay`, real spoken
audio, so `tts` timings reflect full utterance duration, not first-audio latency).

## Files

- `01_full_sequence.log` / `01_full_sequence_telemetry.jsonl` — 12-turn session: booking
  availability attempt (ambiguous "book it" correctly triggers a clarifying question rather
  than a guess) → guardrail probes (weather, FIFA) → cancellation policy (RAG) → EN→ES switch →
  Spanish availability restatement → pet policy in Spanish (RAG) → ES→EN switch → "¡Gracias!"
  (correctly does NOT switch language back) → check-in question (RAG) → natural hangup.
- `02_booking_completion.log` / `02_booking_completion_telemetry.jsonl` — 3-turn session with an
  explicit room type, completing a real `create_booking` call: confirmation `AH-4827`.
- `03_transfer.log` / `03_transfer_telemetry.jsonl` — single-turn "speak to a person" request.
  **This specific captured run exhibits the compound-action bug below** (recorded action:
  `hangup`, not `transfer`) — kept as-is because it's the interesting case; see finding #1 for
  the full reproducibility data across repeated attempts.
- `04_real_mic.log` / `04_real_mic_telemetry.jsonl` — **real microphone session, captured live by
  the user**, not scripted text input. `python voice_loop.py` (no `--text`). 3 turns: "I'd like to
  book a room" (correctly asks for missing slots rather than guessing) → "Could you speak in
  Spanish?" (live mid-flow language switch, question correctly re-asked in Spanish, booking-flow
  state preserved) → "Stop, end call." (hangup, reply correctly still in Spanish). First and only
  real-mic evidence in this review — see Finding 3 below for why its timings need careful reading.

## Aggregate latency (01_full_sequence, 12 turns)

| Stage | min | max | avg |
|---|---|---|---|
| llm | 663ms | 3654ms | 1539ms |
| tts (spoken, system backend) | 5074ms | 23948ms | 10721ms |
| total | 6809ms | 27604ms | 12261ms |

Per-turn breakdown (llm / tts / total / language / action / grounding sources):

```
turn  1: llm= 3654ms tts= 23948ms total= 27604ms lang=en action=-       sources=-
turn  2: llm=  866ms tts= 10487ms total= 11355ms lang=en action=-       sources=-
turn  3: llm=  742ms tts=  6781ms total=  7524ms lang=en action=-       sources=-
turn  4: llm=  908ms tts=  6599ms total=  7508ms lang=en action=-       sources=-
turn  5: llm= 1652ms tts= 17345ms total= 18998ms lang=en action=-       sources=[hotel_policies.md#Cancellation]
turn  6: llm= 1733ms tts=  5074ms total=  6809ms lang=es action=-       sources=-
turn  7: llm= 1290ms tts= 12571ms total= 13862ms lang=es action=-       sources=-
turn  8: llm= 1697ms tts= 16586ms total= 18284ms lang=es action=-       sources=[hotel_policies.md#Pets]
turn  9: llm= 2114ms tts=  7872ms total=  9987ms lang=en action=-       sources=-
turn 10: llm=  663ms tts=  6588ms total=  7253ms lang=en action=-       sources=-
turn 11: llm= 1477ms tts=  9146ms total= 10625ms lang=en action=-       sources=[hotel_policies.md#Check-In And Check-Out, hotel_policies.md#Cancellation]
turn 12: llm= 1674ms tts=  5654ms total=  7328ms lang=en action=hangup  sources=-
```

Note turn 11 retrieves two sources (check-in question also pulls the Cancellation section) —
worth checking `knowledge.py`'s retrieval scoring in the component review.

## Key findings from these runs

### Finding 1: Transfer requests sometimes silently become hangups (compound tool-call bug)

**Reproducibility test** (`"Can I speak to a person please?"`, run 5 times against live
`gpt-4o-mini`, temperature=0.3):

| Run | Result |
|---|---|
| Original capture (`03_transfer.log`) | **BUG**: chained `transfer_to_human` -> `end_call`, final action=`hangup` |
| Retry 1 | Correct: `transfer` only |
| Retry 2 | **BUG**: `hangup` |
| Retry 3 | Correct: `transfer` only |
| Retry 4 | Correct: `transfer` only |

**2 out of 5 (40%)** — not a one-off fluke. Full event trace for the buggy case
(`03_transfer_telemetry.jsonl`) shows the model called `transfer_to_human()` (tool result carries
`action: 'transfer'`), then in the *same turn's tool loop* also called `end_call()` (tool result
carries `action: 'hangup'`), then produced the reply text "I'm transferring you to a human agent
now." `agent.py`'s tool loop tracks `action` as "the last control signal seen" (see its own
docstring) with no validation that `transfer` and `hangup` are mutually exclusive, mutually
exclusive SIP-level operations (REFER vs BYE) — so the later `end_call` silently overwrites the
earlier `transfer`. Net effect: **the caller is told they're being transferred but the system
actually disconnects them** — the opposite of the intended escape hatch.

Why mock mode can't catch this: `MockProvider.chat()` is a single-shot rule match per phrase — it
can never chain two tool calls in one turn the way a real model can. The existing
`control.transfer` eval (mock provider, deterministic) is structurally incapable of exercising
this failure mode. Textbook mock-green-does-not-mean-production-green.

**Where this belongs:** `EVAL_REPORT.md` §5 (live-provider delta) and §6 (coverage gaps —
"multi-intent turns" is already a named gap in the fixed outline, this is a concrete instance of
it) and §7 (a new eval case could assert the tool-call sequence never contains both `transfer` and
`hangup` in one turn, though catching an *intermittent* live-model behavior deterministically in
an eval is itself a design question worth discussing). `FDE-ANALYSIS.md` §2 (model boundaries —
new failure-taxonomy entry: "conflicting compound tool calls resolved by last-write-wins") and §3
(operational fallbacks — this directly undermines the "every failure should end in a human, never
a dead line" principle, since the human-escalation path itself can silently fail).

### Finding 2: Real booking flow requires disambiguation when multiple rooms qualify

In `01_full_sequence`, "book it" after being shown all 5 qualifying rooms (2 guests fits every
room type's capacity) correctly produced a clarifying question rather than a guessed room choice.
This is real evidence for `SETUP-AND-REVIEW.md` §5a (reservation workflow) that the live model
handles ambiguity reasonably — contrast with mock mode, where `MockProvider`'s scripted
availability always returns exactly one room (Standard Queen), so this ambiguity path is never
exercised in the deterministic eval suite either.

### Finding 3: real-mic telemetry reveals two latency-interpretation traps

`04_real_mic.log` is the only real-microphone capture in this review (`--text` mode skips
`capture` and `stt` entirely, so this is the first time either was actually measured). Two things
in this data are easy to misread if taken at face value:

**"capture" time is dominated by the caller talking, not by system overhead.** The three captured
turns show `capture` times of 11238ms, 9222ms, and 8955ms — 9-11 *seconds* each. This is not
processing latency: `record_utterance()`'s VAD loop starts a timer the instant recording begins
and only stops after detecting `ENDPOINT_SILENCE_MS` (600ms default) of trailing silence — so this
number is almost entirely "however long the human took to say their sentence," plus a fixed 600ms
tail, not a system bottleneck. Reading `capture: 11238ms` as "the system took 11 seconds to do
something" would be a real misinterpretation of this telemetry field.

**"tts" total duration is not first-audio latency, and this pipeline doesn't measure first-audio
latency at all.** `TTS_BACKEND=system` calls `subprocess.run(["say", "-v", ..., text])`, which
blocks until `say` finishes speaking the *entire* reply — `say` begins producing audible sound
almost immediately once invoked, but the code has no way to observe or log that moment; it only
observes when the whole utterance is done. So `tts: 10370ms` on turn 1 means "the full reply took
10.4 seconds to finish being spoken," not "the caller waited 10.4 seconds before hearing anything."
The caller's actual perceived wait-before-any-response is much closer to
`capture + stt + routing + llm` (turn 1: 11238 + 1678 + 0.1 + 1122 ≈ **14 seconds** before Aurora's
voice starts at all) — still slow for a real phone call, but a materially different number and a
materially different bottleneck (waiting-to-respond vs. time-spent-responding) than the raw `tts`
field suggests on its own. This is a real, concrete gap for `FDE-ANALYSIS.md` §4: the telemetry
schema has no `ttsFirstByteMs`/`ttsFirstAudioMs` field, and given the current synchronous `say`
invocation, it could not compute one even if it wanted to.
