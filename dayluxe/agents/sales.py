"""Sales & lead management: extract service/suburb/urgency, score, store in CRM, draft replies.

Extraction is deterministic (keywords + suburb list), so it works with no AI provider
and never invents details: anything not found is left blank.
"""
from __future__ import annotations

import re
from typing import Any

from ..context import Context

SERVICES: dict[str, tuple[str, ...]] = {
    "Roof restoration": ("restor", "re-coat", "recoat", "roof make over", "makeover"),
    "Roof painting": ("roof paint", "paint the roof", "paint my roof", "roof coating", "spray the roof"),
    "Roof repairs": ("leak", "leaking", "roof repair", "broken tile", "cracked tile", "repoint", "rebed",
                     "ridge cap", "flashing", "storm damage", "water coming"),
    "Roof cleaning": ("roof clean", "pressure clean", "high pressure", "moss", "lichen"),
    "Gutters": ("gutter", "downpipe", "down pipe", "gutter guard", "leaf guard"),
    "Fascia": ("fascia",),
    "Painting": ("interior paint", "exterior paint", "house paint", "painting", "ceiling", "fence paint",
                 "paint the house", "walls"),
    "Pergola / carport": ("pergola", "carport"),
}

SERVICE_AR = {
    "Roof restoration": "ترميم سقف",
    "Roof painting": "صبغ سقف",
    "Roof repairs": "تصليح سقف",
    "Roof cleaning": "تنظيف سقف",
    "Gutters": "مزاريب (gutters)",
    "Fascia": "فاشيا",
    "Painting": "صبغ",
    "Pergola / carport": "برگولا / كراج",
    "": "خدمة غير واضحة",
}

# Sydney & Western Sydney suburbs (extend freely; used only to *recognise* a suburb in text).
SUBURBS = (
    "Parramatta", "Penrith", "Blacktown", "Liverpool", "Campbelltown", "Castle Hill", "Rouse Hill",
    "Fairfield", "Bankstown", "Auburn", "Granville", "Merrylands", "Guildford", "Westmead", "Seven Hills",
    "Baulkham Hills", "Kellyville", "Bella Vista", "Glenwood", "Quakers Hill", "Riverstone", "Schofields",
    "Marsden Park", "Mount Druitt", "Rooty Hill", "St Marys", "Kingswood", "Emu Plains", "Glenmore Park",
    "Jamisontown", "Cranebrook", "Werrington", "St Clair", "Erskine Park", "Prospect", "Wentworthville",
    "Toongabbie", "Winston Hills", "Northmead", "North Rocks", "Carlingford", "Epping", "Ryde", "Eastwood",
    "Strathfield", "Burwood", "Lidcombe", "Berala", "Regents Park", "Yagoona", "Bass Hill", "Chester Hill",
    "Villawood", "Cabramatta", "Canley Vale", "Canley Heights", "Bonnyrigg", "Wetherill Park", "Smithfield",
    "Greystanes", "Pendle Hill", "Hinchinbrook", "Hoxton Park", "Prestons", "Casula", "Moorebank",
    "Edmondson Park", "Leppington", "Oran Park", "Gregory Hills", "Narellan", "Camden", "Ingleburn",
    "Minto", "Leumeah", "Macquarie Fields", "Glenfield", "Austral", "Kemps Creek", "Horsley Park",
    "Cecil Hills", "Green Valley", "Hurstville", "Kogarah", "Sutherland", "Cronulla", "Hornsby",
    "Chatswood", "Lane Cove", "Hills District", "Windsor", "Richmond", "Bligh Park", "Box Hill",
    "The Ponds", "Stanhope Gardens", "Doonside", "Plumpton", "Oakhurst", "Hebersham", "Dean Park",
    "Kings Langley", "Lalor Park", "Girraween", "Harris Park", "Rydalmere", "Ermington", "Dundas",
    "Sydney", "Western Sydney", "Inner West", "Marrickville", "Ashfield", "Campsie", "Lakemba",
    "Punchbowl", "Revesby", "Padstow", "Panania", "Picnic Point", "Milperra", "Condell Park",
)
_SUBURB_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(SUBURBS, key=len, reverse=True)) + r")\b",
                        re.IGNORECASE)
_POSTCODE_RE = re.compile(r"\bNSW\s*,?\s*(2[0-9]{3})\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?61\s?|0)[2-478](?:[\s-]?\d){8}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

URGENT_WORDS = ("urgent", "asap", "emergency", "leak", "leaking", "water coming", "storm", "flood",
                "collapsed", "dripping", "today", "tonight")
SPAM_WORDS = ("seo services", "rank your website", "backlink", "guest post", "crypto", "loan offer",
              "web design services", "increase your traffic", "marketing agency", "unsubscribe from this list",
              "casino", "bitcoin")


def detect_service(text: str) -> str:
    t = text.lower()
    for name, words in SERVICES.items():
        if any(w in t for w in words):
            return name
    if "roof" in t:
        return "Roof repairs" if "repair" in t else "Roof restoration" if "restor" in t else "Roof (unspecified)"
    return ""


def detect_suburb(text: str) -> str:
    m = _SUBURB_RE.search(text)
    if m:
        # Return canonical capitalisation.
        found = m.group(1).lower()
        for s in SUBURBS:
            if s.lower() == found:
                return s
    pc = _POSTCODE_RE.search(text)
    return f"NSW {pc.group(1)}" if pc else ""


def detect_phone(text: str) -> str:
    m = _PHONE_RE.search(text)
    return m.group(0).strip() if m else ""


def detect_email(text: str) -> str:
    m = _EMAIL_RE.search(text)
    return m.group(0).lower() if m else ""


def is_urgent(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in URGENT_WORDS)


def spam_score(text: str) -> int:
    t = text.lower()
    hits = sum(1 for w in SPAM_WORDS if w in t)
    links = t.count("http://") + t.count("https://")
    return hits * 2 + (1 if links > 3 else 0)


def score_lead(service: str, suburb: str, urgent: bool, phone: str, email: str, body: str) -> int:
    score = 0
    if service:
        score += 40 if service != "Roof (unspecified)" else 30
    if suburb:
        score += 20
    if urgent:
        score += 20
    if phone:
        score += 10
    if email:
        score += 5
    if len(body) > 80:
        score += 5  # detailed enquiries convert better
    return min(score, 100)


def analyse(text: str, name: str = "", email: str = "", phone: str = "", suburb: str = "",
            service: str = "") -> dict[str, Any]:
    """Build a lead record. Explicit form fields win over text extraction."""
    service = service or detect_service(text)
    suburb = suburb or detect_suburb(text)
    phone = phone or detect_phone(text)
    email = (email or "").lower()
    urgent = is_urgent(text)
    spam = spam_score(text) >= 2
    score = 0 if spam else score_lead(service, suburb, urgent, phone, email, text)
    if spam:
        next_action = "Ignore — looks like spam/marketing"
    elif urgent:
        next_action = "Call today — urgent job (possible leak/damage)"
    elif not suburb:
        next_action = "Reply and ask for the property suburb/address and photos"
    else:
        next_action = "Reply and book a free inspection/quote visit"
    return {
        "name": name.strip(),
        "email": email,
        "phone": phone,
        "suburb": suburb,
        "service": service,
        "urgency": "urgent" if urgent else "normal",
        "score": score,
        "status": "spam" if spam else "new",
        "next_action": next_action,
    }


def owner_summary_ar(lead: dict[str, Any]) -> str:
    """Short lead summary for the owner, in simple Iraqi Arabic."""
    name = lead.get("name") or "زبون"
    where = lead.get("suburb") or "منطقة مو معروفة"
    svc = SERVICE_AR.get(lead.get("service", ""), lead.get("service", "") or SERVICE_AR[""])
    urgent = "مستعجل 🔴 — اتصل بي اليوم" if lead.get("urgency") == "urgent" else "عادي"
    phone = lead.get("phone") or "ماكو رقم"
    return f"{name} من {where} يريد {svc}. الأولوية: {urgent}. التلفون: {phone}. التقييم: {lead.get('score', 0)}/100"


def reply_fallback(ctx: Context, lead: dict[str, Any]) -> str:
    s = ctx.settings
    first = (lead.get("name") or "").split(" ")[0] or "there"
    svc = lead.get("service") or "your enquiry"
    if svc == "Roof (unspecified)":
        svc = "your roof"
    ask = []
    if not lead.get("suburb"):
        ask.append("the suburb or address of the property")
    ask.append("a few photos of the area if you can")
    if not lead.get("phone"):
        ask.append("the best number to reach you on")
    asks = "; ".join(ask)
    urgent = (
        "\n\nIf water is coming in right now, give us a call and we'll do our best to get someone out as soon as we can."
        if lead.get("urgency") == "urgent" and s.business_phone else ""
    )
    phone_line = f"\nPh: {s.business_phone}" if s.business_phone else ""
    return (
        f"Hi {first},\n\nThanks for getting in touch about {svc.lower() if svc != 'your enquiry' else svc}. "
        f"Happy to help.\n\nSo we can sort out a quote, could you send through {asks}? "
        f"Then we'll lock in a time to come and have a look.{urgent}\n\n"
        f"Cheers,\n{s.email_signature}{phone_line}"
    )


def draft_reply(ctx: Context, lead: dict[str, Any], enquiry_text: str) -> tuple[str, str]:
    """Return (reply_text, provider)."""
    fallback = reply_fallback(ctx, lead)
    prompt = (
        "Write a short email reply (under 120 words) to this customer enquiry. Thank them, show you understood "
        "what they need, and ask only for what's missing to book an inspection/quote (address/suburb, photos, "
        "best contact time). Do NOT state prices, availability dates or warranties. "
        f"End with this exact signature:\n{ctx.settings.email_signature}"
        + (f"\nPh: {ctx.settings.business_phone}" if ctx.settings.business_phone else "")
        + f"\n\nKnown details: name={lead.get('name') or 'unknown'}, service={lead.get('service') or 'unknown'}, "
        f"suburb={lead.get('suburb') or 'unknown'}, urgent={lead.get('urgency') == 'urgent'}.\n\n"
        f"Customer message:\n\"\"\"\n{enquiry_text[:3000]}\n\"\"\""
    )
    res = ctx.llm.generate(prompt, fallback)
    return res.text, res.provider
