"""
lib/llm.py — the LLM translation call  (Anthropic Claude)
=========================================================
Turns an English string into DUTCH.

NOTE: This is a personal / learning build. The assignment as shipped is
English -> Mexican Spanish (es-MX); here we output Dutch (nl-NL) instead.
We keep the `target` field in the API contract untouched so the provided
widget and the cache keying keep working — we just override the *effective*
output language below.

Provider: Anthropic Claude via the async SDK. The API key is read from
ANTHROPIC_API_KEY in your .env.

FAIL LOUD: there is deliberately no try/except that returns `text` on error.
If the provider call fails, the exception propagates so the caller returns a
502. Silently returning the untranslated English would ship English while
looking healthy — a real production bug (and an auto-fail on the assignment).
"""
import os

from anthropic import AsyncAnthropic

MODEL_DEFAULT = os.getenv("MODEL", "claude-sonnet-4-6")

# Human-readable language names, keyed by the `target` code the contract uses.
# The provided widget always sends "es-MX". For this Dutch build we override the
# effective output language with FORCE_TARGET (default "nl"). Set FORCE_TARGET=""
# in .env to instead honour whatever `target` the caller sends — handy for the
# es-MX / es-ES / pt-BR "language picker" stretch goal.
LANGUAGES = {
    "nl": "Dutch (Netherlands, nl-NL)",
    "es-MX": "Mexican Spanish (es-MX)",
    "es-ES": "Castilian Spanish (es-ES)",
    "pt-BR": "Brazilian Portuguese (pt-BR)",
}
# Different spellings of the same language must not become different cache keys.
ALIASES = {"nl-NL": "nl", "nl_NL": "nl", "dutch": "nl"}
DEFAULT_CODE = "nl"

# Lazily-constructed async client. Built on first use (after app startup has run
# load_dotenv), NOT at import time — so the key from .env is available and a
# missing key surfaces as a translate-time error (-> 502), not an import crash.
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def resolve_target(target: str) -> str:
    """Canonical code for the language we will ACTUALLY output.

    This is the single source of truth for "what language is this?", used by both
    the prompt here and the cache key in app.py — if they disagreed, the cache
    could serve Dutch for a Spanish request. Env is read at call time because
    app.py imports this module before load_dotenv() runs.
    """
    code = os.getenv("FORCE_TARGET", DEFAULT_CODE) or target or DEFAULT_CODE
    code = ALIASES.get(code, code)
    return code if code in LANGUAGES else DEFAULT_CODE


def _language_for(target: str) -> str:
    return LANGUAGES[resolve_target(target)]


def _system_prompt(language: str) -> str:
    return (
        f"You are a professional translator. Translate the user's English text into "
        f"natural, idiomatic {language}, in the friendly register a modern consumer "
        f"website uses (address the reader informally with 'je', not the formal 'u'). "
        f"Use wording a native speaker actually encounters online — for UI labels and "
        f"buttons, use the conventional local phrasing rather than a literal "
        f"word-for-word rendering.\n\n"
        f"Rules:\n"
        f"- Output ONLY the translation: no preamble, no explanation, no notes, and no "
        f"surrounding quotation marks.\n"
        f"- Leave these UNCHANGED: numbers, prices and currency (e.g. $1,299.00), "
        f"product and model codes (e.g. SKU-4471), URLs, email addresses, and code.\n"
        f"- Preserve any HTML tags, markup, or placeholders exactly as they appear.\n"
        f"- If there is nothing to translate (a bare number, code, or symbol), return "
        f"the input unchanged."
    )


def _clean(s: str) -> str:
    s = (s or "").strip()
    # strip a single pair of wrapping quotes the model might add
    if len(s) >= 2 and s[0] in "\"'\u201c\u201d" and s[-1] in "\"'\u201c\u201d":
        s = s[1:-1].strip()
    return s


async def translate_text(text: str, target: str = "es-MX", model: str = MODEL_DEFAULT) -> str:
    """Return `text` translated into the resolved target language (Dutch here)."""
    language = _language_for(target)
    msg = await _get_client().messages.create(
        model=model,
        max_tokens=1024,
        temperature=0,
        system=_system_prompt(language),
        messages=[{"role": "user", "content": text}],
    )
    # A response may contain several content blocks; keep the text ones.
    out = "".join(
        block.text for block in msg.content if getattr(block, "type", None) == "text"
    )
    return _clean(out)
