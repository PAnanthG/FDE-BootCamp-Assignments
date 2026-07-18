"""
smoke_llm.py — live check of lib/llm.py against the real Anthropic API.

Run from backend/ai-service-python/ with your own .env in place:

    cp .env.example .env      # then put your real ANTHROPIC_API_KEY in it
    pip install -r requirements.txt
    python smoke_llm.py

Prints English -> Dutch for a spread of tricky inputs so you can eyeball
quality: UI labels, prices, product codes, URLs, HTML, and a bare number.
"""
import asyncio

from dotenv import load_dotenv

load_dotenv()  # must run BEFORE the first translate call (lazy client reads the key)

from lib.llm import translate_text  # noqa: E402

SAMPLES = [
    "Add to cart",                                   # UI label -> expect "Voeg toe aan winkelwagen"
    "Best sellers",                                  # UI label, idiomatic not literal
    "Free shipping on orders over $50",              # price must stay $50
    "Your order #A1B2-9931 has shipped.",            # order code unchanged
    "The SKU-4471 laptop stand costs $1,299.00.",    # code + formatted price unchanged
    "Visit https://example.com/help or email us at support@example.com",  # URL/email unchanged
    "<strong>Sale</strong> ends <em>tonight</em>",   # HTML tags preserved
    "42",                                            # nothing to translate
    "Sign in to continue to your account settings.", # sentence, friendly 'je' register
]


async def main():
    for text in SAMPLES:
        out = await translate_text(text, target="es-MX")  # widget's target; Dutch override applies
        print(f"\nEN: {text}\nNL: {out}")


if __name__ == "__main__":
    asyncio.run(main())
