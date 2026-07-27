"""
web_demo/server.py  -  a dummy browser front end for the Aurora pipeline.

Not part of the graded reference implementation (pipeline/, evals/, livekit/,
mocks/) -- a separate, minimal harness that swaps voice_loop.py's terminal
I/O for a browser chat window, while reusing the exact same Agent/Provider
code from pipeline/ unmodified.

    browser mic (MediaRecorder) -> POST /turn -> Whisper STT -> Agent.respond()
        -> TTS played locally via provider.synthesize() (same say/afplay path
           voice_loop.py already uses) -> JSON reply -> chat bubble

Stdlib only, no new dependencies. Because the browser and the server are the
same machine in this demo, TTS output is not streamed back over HTTP -- it
plays through this computer's speakers exactly as it does in --text/mic mode.

Run from this directory so the pipeline/.env-relative TELEMETRY_JSONL path
resolves the same way it does for voice_loop.py:

    cd web_demo
    python server.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(PIPELINE_DIR / ".env")

from agent import Agent  # noqa: E402
from providers import make_provider  # noqa: E402
from telemetry import TurnTrace, write_trace  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("WEB_DEMO_PORT", "8765"))
GREETING = "Thanks for calling. This is Aurora — how can I help with your reservation?"

_EXT_BY_CONTENT_TYPE = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
}


class Session:
    """One dummy 'call' -- mirrors what run() in voice_loop.py holds per session."""

    def __init__(self) -> None:
        self.provider = make_provider()
        self.agent = Agent(self.provider)
        self.session_id = f"web-{uuid.uuid4().hex[:10]}"
        self.ended = False


session = Session()


def transcribe_blob(provider, audio_bytes: bytes, content_type: str) -> str:
    """Send a browser-recorded clip straight to Whisper (skips providers.py's
    transcribe(), which wraps *raw PCM* into a WAV -- MediaRecorder output is
    already a compressed container Whisper accepts natively)."""
    if provider.name == "mock":
        # MockProvider has no .client -- it ignores audio and returns the next
        # scripted phrase, same as voice_loop.py's mic mode does.
        return provider.transcribe(audio_bytes, 16000)
    ext = _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip(), "webm")
    clip = io.BytesIO(audio_bytes)
    clip.name = f"turn.{ext}"
    kwargs = {
        "model": provider.stt_model,
        "file": clip,
        "response_format": "text",
    }
    if provider.stt_prompt:
        kwargs["prompt"] = provider.stt_prompt
    resp = provider.client.audio.transcriptions.create(**kwargs)
    return (resp if isinstance(resp, str) else resp.text).strip()


def speak(provider, text: str, locale: str | None = None) -> None:
    """Same contract as voice_loop.speak(): system backend plays locally and
    returns None; provider backend would return WAV bytes we simply discard
    here since browser and server share speakers in this demo."""
    provider.synthesize(text, locale=locale)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("[web_demo] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            html = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/turn":
            return self._handle_turn()
        if self.path == "/reset":
            return self._handle_reset()
        if self.path == "/greeting":
            return self._handle_greeting()
        self.send_response(404)
        self.end_headers()

    def _handle_reset(self):
        global session
        session = Session()
        self._send_json(200, {"ok": True, "sessionId": session.session_id})

    def _handle_greeting(self):
        # Speaks the greeting the same way voice_loop.py does at call start --
        # index.html previously only added a text bubble for this, with no
        # audio ever triggered, unlike the CLI's speak() call before its loop.
        trace = TurnTrace(session_id=session.session_id, turn_id="greeting")
        trace.event("greeting.requested")
        with trace.span("tts", backend=session.provider.tts_backend):
            speak(session.provider, GREETING, locale=session.agent.current_locale)
        payload = trace.finish(action=None, sources=[])
        write_trace(payload)
        self._send_json(200, {"greeting": GREETING})

    def _handle_turn(self):
        global session
        # Always drain the request body first, even on an early-return path --
        # http.server keeps the connection alive between requests, and leaving
        # an unread body in the socket corrupts whatever request comes next.
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "audio/webm")
        audio_bytes = self.rfile.read(length) if length else b""

        if session.ended:
            return self._send_json(409, {"error": "Call has ended. POST /reset to start a new one."})
        if not audio_bytes:
            return self._send_json(400, {"error": "No audio received."})

        trace = TurnTrace(session_id=session.session_id)
        trace.event("input.browser_audio", bytes=len(audio_bytes))
        provider = session.provider
        agent = session.agent

        try:
            with trace.span("stt", model=provider.stt_model):
                user_text = transcribe_blob(provider, audio_bytes, content_type)

            if not user_text.strip():
                payload = trace.finish(action=None, sources=[])
                write_trace(payload)
                return self._send_json(200, {
                    "you": "", "reply": "I didn't catch that -- could you repeat it?",
                    "action": None, "language": agent.current_language,
                    "timings": payload["timings"], "totalMs": payload["totalMs"],
                })

            reply, action = agent.respond(user_text, trace=trace)

            with trace.span("tts", backend=provider.tts_backend):
                speak(provider, reply, locale=agent.current_locale)

            payload = trace.finish(action=action, sources=agent.last_sources)
            write_trace(payload)

            if action in ("hangup", "transfer"):
                session.ended = True

            self._send_json(200, {
                "you": user_text,
                "reply": reply,
                "action": action,
                "language": agent.current_language,
                "sources": agent.last_sources,
                "timings": payload["timings"],
                "totalMs": payload["totalMs"],
            })
        except Exception as exc:  # noqa: BLE001 -- demo-server safety net only
            traceback.print_exc()
            self._send_json(500, {
                "error": str(exc),
                "hint": "The live model call failed (see server terminal for the "
                        "full traceback). This is a real gap in the reference "
                        "pipeline, not a web_demo bug -- there is no retry/catch "
                        "around provider calls in agent.py/voice_loop.py.",
            })


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Aurora web demo: http://127.0.0.1:{PORT}  (provider={session.provider.name}, "
          f"llm={session.provider.llm_model}, tts_backend={session.provider.tts_backend})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
