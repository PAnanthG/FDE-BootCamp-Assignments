"""Offline tests for lib/llm.py — fakes the Anthropic client, no API key, no network."""
import asyncio, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.pop("FORCE_TARGET", None)
import lib.llm as llm

CAPTURED = {}

class FakeBlock:
    def __init__(self, text): self.type, self.text = "text", text

class FakeMsg:
    def __init__(self, text): self.content = [FakeBlock(text)]

class FakeMessages:
    def __init__(self, reply=None, boom=False): self.reply, self.boom = reply, boom
    async def create(self, **kw):
        CAPTURED.update(kw)
        if self.boom: raise RuntimeError("provider 529 overloaded")
        return FakeMsg(self.reply)

class FakeClient:
    def __init__(self, reply=None, boom=False): self.messages = FakeMessages(reply, boom)

def use(reply=None, boom=False):
    llm._client = FakeClient(reply, boom)

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

print("\n1. Happy path + call parameters")
use(reply="Voeg toe aan winkelwagen")
out = asyncio.run(llm.translate_text("Add to cart", target="es-MX"))
check("returns the model's text", out == "Voeg toe aan winkelwagen", out)
check("temperature == 0", CAPTURED.get("temperature") == 0, repr(CAPTURED.get("temperature")))
check("max_tokens == 1024", CAPTURED.get("max_tokens") == 1024)
check("model == claude-sonnet-4-6", CAPTURED.get("model") == "claude-sonnet-4-6", CAPTURED.get("model"))
check("user content is the raw text", CAPTURED["messages"] == [{"role": "user", "content": "Add to cart"}])

print("\n2. Dutch override (widget sends es-MX)")
check("system prompt asks for Dutch", "Dutch (Netherlands, nl-NL)" in CAPTURED["system"])
check("system prompt does NOT ask for Spanish", "Spanish" not in CAPTURED["system"])
check("prompt pins friendly 'je' register", "'je'" in CAPTURED["system"])
check("prompt says output ONLY translation", "ONLY the translation" in CAPTURED["system"])
check("prompt preserves codes/URLs", "URLs" in CAPTURED["system"] and "product and model codes" in CAPTURED["system"])

print("\n3. FORCE_TARGET escape hatch (target-driven mode)")
os.environ["FORCE_TARGET"] = ""
use(reply="Agregar al carrito")
asyncio.run(llm.translate_text("Add to cart", target="es-MX"))
check("FORCE_TARGET='' honours target es-MX", "Mexican Spanish (es-MX)" in CAPTURED["system"])
asyncio.run(llm.translate_text("Add to cart", target="pt-BR"))
check("FORCE_TARGET='' honours target pt-BR", "Brazilian Portuguese (pt-BR)" in CAPTURED["system"])
asyncio.run(llm.translate_text("Add to cart", target="klingon"))
check("unknown target falls back to Dutch", "Dutch (Netherlands, nl-NL)" in CAPTURED["system"])
os.environ["FORCE_TARGET"] = "nl"

print("\n4. Output cleaning")
for raw, want in [('"Hallo daar"', "Hallo daar"), ("  Hallo  ", "Hallo"),
                  ("\u201cHallo\u201d", "Hallo"), ("Zeg \"hoi\" tegen hem", 'Zeg "hoi" tegen hem')]:
    use(reply=raw)
    got = asyncio.run(llm.translate_text("x"))
    check(f"clean({raw!r}) -> {want!r}", got == want, got)

print("\n5. FAIL LOUD — provider error must propagate (never return English)")
use(boom=True)
try:
    got = asyncio.run(llm.translate_text("Add to cart"))
    check("raises instead of returning input", False, f"swallowed error, returned {got!r}")
except RuntimeError:
    check("raises instead of returning input", True)

print("\n6. Lazy client (no key needed at import)")
check("module imported without ANTHROPIC_API_KEY set", "ANTHROPIC_API_KEY" not in os.environ)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
