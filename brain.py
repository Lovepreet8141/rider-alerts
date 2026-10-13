"""The bot's understanding — Claude Haiku reads what a rider wrote, in any language, and says which situation it is.

What the AI does (and what it never does):
  * understand(): reads ALL the rider's unanswered messages together ("Tkbt6q", "Hlo", "Brother, he is not giving the
    code" = order TKBT6Q, Liefercode problem), plus the order data we have, and returns a small JSON: situation, order
    number, minutes waited / minutes the restaurant still needs, whether he wants to cancel, whether his app call fails,
    the language he reads best, and a one-line English summary for the team.
  * translate(): puts an APPROVED reply text (Intercom page → texts) into that language — same meaning, nothing added.
  * It never writes its own answers, never decides to cancel, never sees or sends customer data. The flow engine
    (replies.py) decides what happens; when the AI is unsure or unreachable, the keyword rules decide.

Cost (Claude Haiku 4.5: $1 per million input tokens, $5 per million output tokens): about 0.2–0.3 cents per rider
message burst including the translated reply. Today's calls and cost are on /health → "ai".
Needs ANTHROPIC_API_KEY in Railway; without it the bot runs on keywords and answers in English.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

log = logging.getLogger("brain")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("BOT_MODEL", "claude-haiku-4-5-20251001").strip()
PRICE_IN, PRICE_OUT, PRICE_CACHE = 1.0, 5.0, 0.10          # USD per million tokens (Haiku 4.5)
MIN_CONFIDENCE = 0.55                                        # below this the keyword rules / a person decide

SITUATIONS = {
    "not_ready": "food not ready, restaurant late, long wait at the restaurant — also when the rider wants to cancel BECAUSE of the wait",
    "no_order": "restaurant does not have this order, another rider already took it, restaurant won't hand it over",
    "item_missing": "an item of the order is not available, restaurant cancelled or changed part of the order",
    "closed": "the RESTAURANT is closed, nobody there (a closed road is not this)",
    "wrong_number": "the customer's phone number (that we sent) is wrong, not in service, or a stranger answers",
    "no_new_orders": "rider is not receiving / not being offered new orders in the app (account, zone or online question — no specific order)",
    "compensation": "rider asks for compensation, extra pay or money (detour, extra kilometres, long wait)",
    "cant_go_online": "rider cannot go online / log in / start his shift in the rider app (app or account problem, no order involved)",
    "returned": "rider has given / brought the order back to the restaurant (usually wants it cancelled)",
    "remove_order": "rider wants the order removed / unassigned / cancelled — with no reason, or a reason that is NOT waiting (wrong location, too far, wrong order assigned)",
    "customer_phone": "rider asks for the customer's phone number (and has not said the app call fails)",
    "call_failed": "rider cannot call the customer through the app: no call button, call fails or drops, cannot hear, number wrong, any calling problem",
    "customer_unreachable": "customer does not answer the phone or does not open the door — the call itself works",
    "customer_find": "rider cannot find the customer's address, entrance or house number",
    "liefercode": "delivery code (Liefercode) problem: customer does not give it, code wrong, code missing",
    "forgot_finish": "rider forgot to finish the order in the app, photo upload fails, order still open after delivering",
    "customer_problem": "customer refuses the order, complains, says wrong order, did not order",
    "damaged": "food damaged or spilled",
    "cant_deliver": "rider cannot continue / cannot make the delivery (with or without a reason: vehicle broken, sick, too far) — not an emergency",
    "urgent": "accident, injury, police, threat, any danger",
    "nudge": "rider only asks for an answer: '?', 'hello', 'reply me', 'brother', 'anyone there'",
    "ack": "ok / thanks / arrived / on my way / done",
    "excuse": "explains his own delay: traffic, lost, rain",
    "other": "anything else, or not clear",
}

EXAMPLES = [
    (["The restaurant is very late", "I want to cancel the order"],
     {"situation": "not_ready", "wants_cancel": True, "summary": "Restaurant very late, rider wants to cancel."}),
    (["Because the order is not ready", "Cancel the order", "I can't wait", "?"],
     {"situation": "not_ready", "wants_cancel": True, "summary": "Order not ready, rider can't wait and wants to cancel."}),
    (["Long wait time, I want to cancel"],
     {"situation": "not_ready", "wants_cancel": True, "summary": "Long wait at the restaurant, rider wants to cancel."}),
    (["Please cancel", "It's not in restaurant", "The another person took this order", "Restaurant is not giving me this order",
      "Am waiting here last 20 min"],
     {"situation": "no_order", "wants_cancel": True, "minutes_waited": 20,
      "summary": "Restaurant says another rider took the order; rider waited 20 min."}),
    (["Hello, you canceled the order because there are no more chicken wings available."],
     {"situation": "item_missing", "summary": "Restaurant has no chicken wings left for this order."}),
    (["Customer number, please"], {"situation": "customer_phone", "summary": "Rider asks for the customer's number."}),
    (["Number snd kro"], {"situation": "customer_phone", "reply_language": "English", "summary": "Rider asks for the customer's number."}),
    (["The call button doesn't work", "I can't call him from the app"],
     {"situation": "call_failed", "summary": "Rider cannot call the customer through the app."}),
    (["He did not respond to him", "Pressed the bell more than 5 times, no response", "I tried to call him more than three times, but he did not answer."],
     {"situation": "customer_unreachable", "summary": "Customer doesn't open or answer; rider rang 5 times, called 3 times."}),
    (["Hello, I couldn't find this customer's address", "I called him, but the call was not answered"],
     {"situation": "customer_find", "summary": "Rider can't find the address; customer didn't answer the call."}),
    (["Tkbt6q", "Hlo", "Brother, he is not giving the code"],
     {"situation": "liefercode", "order_ref": "TKBT6Q", "reply_language": "English", "summary": "Customer won't give the Liefercode (order TKBT6Q)."}),
    (["I forgot to finish the order on the app"], {"situation": "forgot_finish", "summary": "Rider forgot to finish the order in the app."}),
    (["Customer doesn't accept the Order"], {"situation": "customer_problem", "summary": "Customer refuses the order."}),
    (["Ich habe die Nummer angerufen, und mir wurde gesagt, dass ich nichts bestellt habe."],
     {"situation": "customer_problem", "reply_language": "German", "summary": "Rider called the customer; the person says they ordered nothing."}),
    (["I have returned the order. Please cancel the order."],
     {"situation": "returned", "wants_cancel": True, "reason": "order given back to the restaurant", "summary": "Rider gave the order back to the restaurant and wants it cancelled."}),
    (["Hello, why am I not receiving any orders?"], {"situation": "no_new_orders", "reply_language": "German", "summary": "Rider gets no new orders."}),
    (["I delivered the order, but the road to the customer was closed due to a festival, so I changed the route, which increased the distance by 2.5 kilometers. Please provide compensation, thank you."],
     {"situation": "compensation", "reply_language": "Arabic", "summary": "Road closed for a festival; detour of 2.5 km; rider asks for compensation."}),
    (["He can't find a PIN", "I have been waiting for a long time"], {"situation": "liefercode", "summary": "Customer can't find the delivery PIN; rider waiting long."}),
    (["Please send me the phone number of this customer."], {"situation": "customer_phone", "summary": "Rider asks for the customer's number."}),
    (["And what if it is not there?"], {"situation": "customer_unreachable", "summary": "Rider asks what to do if the customer is not there."}),
    (["The number is wrong, someone else answers"], {"situation": "wrong_number", "summary": "The customer's number reaches someone else."}),
    (["I cannot make the delivery"], {"situation": "cant_deliver", "reason": "", "summary": "Rider can't make the delivery, no reason given yet."}),
    (["Please cancel"], {"situation": "remove_order", "wants_cancel": True, "reason": "", "summary": "Rider wants the order cancelled, no reason given yet."}),
    (["I can't deliver, my bike has a flat tire"], {"situation": "cant_deliver", "reason": "flat tire", "summary": "Rider can't continue: flat tire."}),
    (["I cannot communicate with the client"], {"situation": "customer_unreachable", "summary": "Rider can't reach the customer."}),
    (["Hello", "Hlooo", "Reply me bro", "Brother"], {"situation": "nudge", "reply_language": "English", "summary": "Rider is waiting for an answer."}),
    (["المطعم ما جهز الطلب لسا"], {"situation": "not_ready", "reply_language": "Arabic", "summary": "Food not ready yet."}),
    (["العميل لا يرد على الهاتف"], {"situation": "customer_unreachable", "reply_language": "Arabic", "summary": "Customer doesn't answer the phone."}),
    (["Bhai order abhi ready nahi hai, 15 min bol rahe hai"],
     {"situation": "not_ready", "minutes_needed": 15, "reply_language": "English", "summary": "Food not ready; restaurant says 15 more minutes."}),
    (["Kunde macht nicht auf und geht nicht ans Handy"],
     {"situation": "customer_unreachable", "reply_language": "German", "summary": "Customer doesn't open or answer."}),
]

SYSTEM = (
    "You read messages that food-delivery riders (Lieferando, Quickzi fleet) send to rider support in Intercom, and you "
    "classify them. Riders write from the road, in Arabic, Hindi or Urdu (often in Latin letters), Turkish, German, English "
    "and other languages, with typos and several short messages in a row. Read ALL the messages together — the newest one "
    "matters most — and use the order data when it helps.\n\n"
    "Situations (pick exactly one):\n" + "\n".join(f"- {k}: {v}" for k, v in SITUATIONS.items()) + "\n\n"
    "Rules:\n"
    "- A wish to cancel because the food is late is not_ready with wants_cancel=true, not remove_order.\n"
    "- call_failed only when the rider says calling itself does not work; a customer who doesn't pick up is customer_unreachable.\n"
    "- reason: when the rider can't continue or wants to cancel: WHY, in a few English words ('bike broken', 'too far', "
    "'food not ready'); \"\" when he gives no reason ('Please cancel', 'I cannot make the delivery' alone = no reason).\n"
    "- order_ref: an order number the rider wrote (5-9 letters and digits, like TKBT6Q), in capitals; else \"\".\n"
    "- minutes_waited: minutes the rider says he has ALREADY waited; minutes_needed: minutes the restaurant says it still needs; else null.\n"
    "- reply_language: the language the rider should get the answer in, in English (\"Arabic\", \"German\", \"Turkish\", \"English\"...). "
    "Riders who write Hindi or Urdu in Latin letters get \"English\".\n"
    "- summary: one short English sentence for the support team: what happened and what the rider wants. No customer data.\n"
    "- confidence: 0.0-1.0, how sure you are about the situation. Unclear or mixed = below 0.5.\n\n"
    "Answer with ONE JSON object only, no other text:\n"
    "{\"situation\": \"...\", \"confidence\": 0.0, \"order_ref\": \"\", \"minutes_waited\": null, \"minutes_needed\": null, "
    "\"wants_cancel\": false, \"reason\": \"\", \"reply_language\": \"English\", \"summary\": \"...\"}\n\n"
    "Examples (rider messages → answer):\n" +
    "\n".join(f"{json.dumps(m, ensure_ascii=False)} → {json.dumps({**{'confidence': 0.9}, **a}, ensure_ascii=False)}" for m, a in EXAMPLES)
)

STATS = {"day": "", "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0, "usd": 0.0,
         "translations": 0, "last_error": ""}
_CACHE: dict = {}
_TCACHE: dict = {}
_client = None


def enabled() -> bool:
    return bool(API_KEY)


def stats() -> dict:
    _roll()
    return {"on": enabled(), "model": MODEL, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in STATS.items()}}


def _roll():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if STATS["day"] != today:
        STATS.update(day=today, calls=0, failed=0, input_tokens=0, output_tokens=0, cache_tokens=0, usd=0.0, translations=0)


async def _call(system, user: str, max_tokens: int) -> str:
    global _client
    import httpx
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    _roll()
    STATS["calls"] += 1
    try:
        r = await _client.post("https://api.anthropic.com/v1/messages",
                               headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                               json={"model": MODEL, "max_tokens": max_tokens, "temperature": 0, "system": system,
                                     "messages": [{"role": "user", "content": user}]})
        data = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}: {str(data)[:160]}")
        u = data.get("usage") or {}
        i, o, c = u.get("input_tokens") or 0, u.get("output_tokens") or 0, (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        STATS["input_tokens"] += i
        STATS["output_tokens"] += o
        STATS["cache_tokens"] += c
        STATS["usd"] += (i * PRICE_IN + o * PRICE_OUT + (u.get("cache_read_input_tokens") or 0) * PRICE_CACHE
                         + (u.get("cache_creation_input_tokens") or 0) * PRICE_IN * 1.25) / 1e6
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text").strip()
    except Exception as e:
        STATS["failed"] += 1
        STATS["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        log.warning("AI call failed: %s", e)
        return ""


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    sit = str(d.get("situation") or "").strip().lower()
    if sit not in SITUATIONS:
        return {}

    def _int(v):
        try:
            n = int(float(v))
            return n if 0 <= n <= 180 else None
        except Exception:
            return None
    ref = re.sub(r"[^A-Z0-9]", "", str(d.get("order_ref") or "").upper())
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence"))))
    except Exception:
        conf = 0.5
    lang = re.sub(r"[^A-Za-z ()-]", "", str(d.get("reply_language") or "English")).strip()[:30] or "English"
    return {"situation": sit, "confidence": conf, "order_ref": ref if 5 <= len(ref) <= 9 else "",
            "minutes_waited": _int(d.get("minutes_waited")), "minutes_needed": _int(d.get("minutes_needed")),
            "wants_cancel": bool(d.get("wants_cancel")), "reply_language": lang.title() if lang.islower() else lang,
            "reason": re.sub(r"\s+", " ", str(d.get("reason") or "")).strip()[:120],
            "summary": str(d.get("summary") or "").strip()[:240]}


async def understand(messages: list, order_ctx: str = "", asked: str = "", extra: str = "") -> dict:
    """messages = the rider's unanswered messages, oldest first. Returns {} when the AI is off, failed or unsure."""
    msgs = [str(m).strip()[:400] for m in messages if str(m or "").strip()][-8:]
    if not enabled() or not msgs:
        return {}
    key = (tuple(m.lower() for m in msgs), order_ctx[:60], asked[:60])
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    user = (f"Order data: {order_ctx or 'no order found for this rider'}\n"
            + (f"Our last message to the rider: \"{asked[:300]}\"\n" if asked else "")
            + f"Rider messages (oldest first): {json.dumps(msgs, ensure_ascii=False)}")
    system = [{"type": "text", "text": SYSTEM + (f"\n\nTeam notes:\n{extra[:1500]}" if extra.strip() else ""),
               "cache_control": {"type": "ephemeral"}}]
    res = _parse(await _call(system, user, 220))
    if res:                                        # a failed call is not remembered: the next message tries again
        _CACHE[key] = (time.time(), res)
        if len(_CACHE) > 800:
            _CACHE.pop(next(iter(_CACHE)))
    return res


TRANSLATE_SYSTEM = (
    "You translate short messages from a delivery company's rider support to its riders. Translate the message into the "
    "requested language: simple, friendly, short sentences a rider understands on a phone. Keep the meaning exactly — add "
    "nothing, leave nothing out. Keep order numbers, phone numbers, minutes, the words 'Lieferando', 'Liefercode' and app "
    "button names unchanged. Keep markers like [[0]] or [[1]] exactly as they are, in the right place. Answer with the "
    "translated message only."
)


async def translate(text: str, language: str) -> str:
    """The approved reply in the rider's language; the original English text if that is not possible."""
    lang = (language or "").strip()
    if not enabled() or not text.strip() or not lang or lang.lower() in ("english", "en"):
        return text
    key = (text, lang.lower())
    if key in _TCACHE:
        return _TCACHE[key]
    out = await _call(TRANSLATE_SYSTEM, f"Language: {lang}\nMessage:\n{text}", 400)
    if not out or len(out) > 4 * len(text) + 200:
        return text
    _roll()
    STATS["translations"] += 1
    _TCACHE[key] = out
    if len(_TCACHE) > 1500:
        _TCACHE.pop(next(iter(_TCACHE)))
    return out
