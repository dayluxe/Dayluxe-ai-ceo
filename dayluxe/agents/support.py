"""Customer support: detect matters that must be escalated, answer FAQs from verified info only."""
from __future__ import annotations

from ..context import Context

ESCALATION_WORDS = {
    "complaint": ("complain", "complaint", "not happy", "unhappy", "disappointed", "poor job", "bad job",
                  "terrible", "still leaking after", "you guys did", "redo", "fix your work"),
    "safety": ("injur", "hurt", "fell off", "unsafe", "asbestos", "electric shock", "live wire", "collapse"),
    "payment dispute": ("refund", "chargeback", "overcharged", "dispute", "won't pay", "not paying", "invoice is wrong"),
    "legal": ("lawyer", "solicitor", "legal action", "fair trading", "tribunal", "ncat", "sue ", "court"),
}


def escalation_reason(text: str) -> str:
    t = text.lower()
    for reason, words in ESCALATION_WORDS.items():
        if any(w in t for w in words):
            return reason
    return ""


def faq_answer(ctx: Context, question: str) -> str:
    """Answers using only facts in config. Unknown → honest 'we'll confirm'."""
    s = ctx.settings
    q = question.lower()
    if any(w in q for w in ("area", "suburb", "where do you", "service area", "come to")):
        return f"We work across {s.service_area}. Let us know your suburb and we'll confirm."
    if any(w in q for w in ("service", "what do you do", "offer")):
        return ("Roof restoration, repairs, painting, cleaning and sealing; gutters, gutter guards and downpipes; "
                "fascia; interior and exterior painting; and pergola/carport repairs.")
    if any(w in q for w in ("quote", "inspection", "how does it work", "next step")):
        return ("Send us your address, a few photos and what you need. We'll arrange a time to inspect, then send "
                "a written quote for you to review.")
    if any(w in q for w in ("price", "cost", "how much", "warranty", "insur", "licen", "available", "when can")):
        return "We'll confirm that for your job once we've had a look — we don't want to guess and get it wrong."
    contact = f" or call {s.business_phone}" if s.business_phone else ""
    return f"Good question — we'll get back to you on that shortly{contact}."
