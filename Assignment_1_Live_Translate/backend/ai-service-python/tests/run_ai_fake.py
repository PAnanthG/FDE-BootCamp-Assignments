"""Runs the REAL app.py under uvicorn, with only the provider call faked (no API key)."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["FORCE_TARGET"] = "nl"
os.environ["TRANSLATION_DB_PATH"] = "translations.db"
import app as A

async def fake_translate(text, target="es-MX", model=None):
    await asyncio.sleep(0.05)          # pretend the provider costs 50ms
    if "boom" in text: raise RuntimeError("provider 529 overloaded")
    return "[nl] " + text
A.translate_text = fake_translate

import uvicorn
uvicorn.run(A.app, host="127.0.0.1", port=8000, log_level="warning")
