"""
Rider queries — what the dashboard does when a rider writes to us in Intercom.

Every rider message (any conversation, any language) is classified against the situations we know
(restaurant not ready, closed, customer unreachable, can't deliver, …). Where the answer comes from data
we have, the dashboard answers and keeps the rider waiting in the right place; everything else is
forwarded to the main inbox with an internal note that carries the order context — so nobody has to
ask the rider for the order number again.

Flows keep a small state per rider (what we asked, since when) so a reply like "10", "ok" or "no"
is understood in context. Timers run from the automation loop once a minute.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# ----------------------------------------------------------------------------- texts (editable in Settings)
# key, label, default text  (German / English — the UI shows them as two fields, joined with " / ")
QUERIES = [
    ("q:not_ready", "Order not ready → ask to wait and for the minutes",
     "Danke für die Info. Bitte bleib dort und frag das Personal, wie lange es noch dauert – die Wartezeit wird dem Restaurant zugerechnet, nicht dir. Schreib mir die Minuten, die sie dir nennen (z. B. \"10\")."
     " / Thanks. Please stay there and ask the staff how long it will take – the waiting time is counted for the restaurant, not for you. Write me the minutes they tell you (e.g. \"10\")."),
    ("q:not_ready_minutes", "Rider named the minutes",
     "Okay, {n} Minuten – bitte warten, ich melde mich. / Okay, {n} minutes – please wait, I'll check back."),
    ("q:not_ready_ok", "Rider agrees to wait",
     "Super, danke fürs Warten. / Great, thanks for waiting."),
    ("q:not_ready_check", "Timer ended → is it ready now?",
     "Ist die Bestellung jetzt fertig? Antworte JA oder schreib nochmal die Minuten. / Is the order ready now? Reply YES or write the minutes again."),
    ("q:persuade", "Rider wants to hand back → ask for 10 more minutes (once)",
     "Ich verstehe. Wenn du die Bestellung zurückgibst, muss ein anderer Fahrer von vorne anfangen und der Kunde wartet noch länger – das Warten zählt für das Restaurant, nicht gegen dich. Kannst du noch 10 Minuten geben? Antworte JA, dann ist alles gut, oder NEIN, dann übernimmt ein Kollege."
     " / I understand. If you hand it back, another rider starts from zero and the customer waits even longer – waiting is counted for the restaurant, not against you. Can you give it 10 more minutes? Reply YES and we're fine, or NO and a colleague takes over."),
    ("q:closed", "Restaurant closed → ask for a photo",
     "Bitte schick mir ein Foto vom Eingang (mit Öffnungszeiten, wenn sichtbar) und bleib noch kurz dort. / Please send me a photo of the entrance (opening hours if visible) and stay there a moment."),
    ("q:no_order", "Restaurant has no such order / already taken by another rider",
     "Danke – ich prüfe das sofort, bitte bleib dort. / Thanks – I'm checking right now, please stay there."),
    ("q:cant_deliver", "Can't do the delivery → ask why",
     "Bitte sag uns kurz, warum. / Please tell us briefly why."),
    ("q:customer_card", "Customer card (sent when the rider asks for the number / can't reach / can't find)",
     "📦 {ref} · {customer_name}📞 {phone} · 📍 {address}\n{notes_line}{map_line}"),
    ("q:customer_card_call", "…closing line when the rider asked for the number",
     "Ruf den Kunden an. Wenn er nicht reagiert, schreib uns hier. / Call the customer. If they don't respond, message us here."),
    ("q:customer_card_wait", "…closing line when the rider can't reach the customer",
     "Bitte bleib vor Ort und ruf den Kunden weiter an, bis er reagiert. Klingle auch nochmal. Wenn der Kunde nach 5 Minuten nicht reagiert, schreib uns hier nochmal."
     " / Please stay there and keep calling the customer until they respond. Ring the bell again too. If the customer doesn't respond after 5 minutes, message us here again."),
    ("q:customer_card_find", "…closing line when the rider can't find the address",
     "Prüfe die Notizen und den Kartenpunkt. Wenn du es in 3 Minuten nicht findest, ruf den Kunden an und frag nach dem Eingang."
     " / Check the notes and the map pin. If you can't find it within 3 minutes, call the customer and ask for the entrance."),
    ("q:customer_problem", "Customer doesn't accept / problem at the customer → ask what exactly",
     "Was genau ist das Problem? / What exactly is the problem?"),
    ("q:forgot_finish", "Forgot to finish the order in the app → deliver + photo",
     "Bitte liefere das Essen aus und schick mir hier ein Foto der Übergabe, dann schließen wir die Bestellung ab. / Please deliver the food and send me a photo of the handover here; then we finalize the order."),
    ("q:forgot_done", "…but the order is already completed on our side",
     "Die Bestellung ist bei uns schon abgeschlossen – alles gut. / The order is already completed on our side – all good."),
    ("q:damaged", "Order damaged → ask for a photo",
     "Bitte schick mir ein Foto der Bestellung. / Please send me a photo of the order."),
    ("q:other", "Anything unclear → one question (never the order number)",
     "Wie können wir helfen? / How can we help?"),
    ("q:ack", "Rider confirms an automatic message (ok / arrived / on the way)",
     "Danke! / Thanks!"),
    ("q:noted", "Rider explains a delay (traffic, lost, …)",
     "Danke, notiert. / Thanks, noted."),
]
QUERY_DEFAULT = {k: t for k, _, t in QUERIES}

# ----------------------------------------------------------------------------- classification
YES = ("ok", "okay", "oke", "ja", "yes", "yeah", "yep", "sure", "fine", "alles klar", "klar", "gut", "passt", "mach ich", "will do",
       "theek", "thik", "haan", "han", "ha ji", "نعم", "تمام", "حسنا", "اوك", "evet", "tamam", "👍", "👌", "✅")
NO = ("nein", "no ", "nope", "nicht warten", "not wait", "can't wait", "cant wait", "cannot wait", "kann nicht warten", "cancel", "stornier",
      "zurückgeben", "zurueckgeben", "hand back", "give back", "leave", "gehe jetzt", "i go", "ich gehe", "nahi", "nai", "لا ", "لا أستطيع", "hayır", "hayir", "yok")
URGENT = ("unfall", "accident", "verletzt", "injur", "hurt", "polizei", "police", "crash", "ambulance", "krankenwagen", "hospital", "krankenhaus")
CLOSED = ("geschlossen", "closed", " zu.", " zu ", "locked", "nobody there", "niemand da", "niemand hier", "not open", "nicht offen", "nicht geöffnet", "shut",
          "مغلق", "kapalı", "kapali", "band hai", "closed restaurant", "restaurant closed")
NO_ORDER = ("no order", "no such", "kein auftrag", "keine bestellung", "nicht da", "already taken", "already picked", "schon abgeholt", "schon weg",
            "another driver", "anderer fahrer", "andere fahrer", "wrong order", "falsche bestellung", "don't have", "dont have", "haben nicht", "haben keine",
            "not found", "nicht gefunden", "no booking", "they say no")
NOT_READY = ("not ready", "nicht fertig", "noch nicht", "not prepared", "preparing", "still cooking", "being prepared", "wird noch", "dauert", "takes time",
             "will take", "take time", "not done", "kitchen", "küche", "kueche", "wait", "warte", "warten", "late", "spät", "spaet", "ready nahi",
             "hazır değil", "hazir degil", "غير جاهز", "ليس جاهز", "لم يجهز", "order is not", "bestellung ist nicht", "still waiting", "noch warten")
READY = ("ready now", "jetzt fertig", "ist fertig", "is ready", "order ready", "bestellung fertig", "picked up", "abgeholt", "habs", "hab es", "got it", "have it", "got the order", "habe die bestellung", "on my way now", " ready", " fertig")
NOT_READY_GUARD = ("not ready", "nicht fertig", "isn't ready", "isnt ready", "no ready", "not yet ready", "noch nicht fertig", "ready nahi")
FORGOT_CTX = ("order", "bestellung", "app", "finish", "complete", "abschließ", "abschliess", "finaliz", "deliver", "liefer", "mark")
FORGOT = ("forgot", "vergessen", "finish the order", "complete the order", "abschließen", "abschliessen", "close the order", "mark delivered", "als geliefert",
          "finalize", "finalise", "bhool", "نسيت", "unuttum", "not completed", "nicht abgeschlossen", "cannot complete", "can't complete")
DAMAGED = ("damaged", "beschädigt", "beschaedigt", "kaputt", "spilled", "verschüttet", "verschuettet", "ausgelaufen", "leaking", "leak", "broken", "zerbrochen",
           "تالف", "hasarlı", "hasarli", "fell", "runtergefallen", "squashed")
CANT = ("can't do", "cant do", "cannot do", "can't deliver", "cant deliver", "cannot deliver", "kann nicht liefern", "kann nicht ausliefern", "nicht liefern",
        "kann nicht machen", "can't continue", "cannot continue", "nicht weiter", "unable", "bike", "fahrrad", "panne", "flat", "platten", "reifen", "tire", "tyre",
        "sick", "krank", "going home", "nach hause", "feierabend", "too far", "zu weit", "too big", "zu groß", "zu gross", "nahi kar sakta", "لا أستطيع التوصيل",
        "teslim edemiyorum", "i can't", "ich kann nicht")
UNREACH = ("cannot contact", "can't contact", "cant contact", "not contact", "nicht kontaktieren", "no contact", "not answering", "doesn't answer", "does not answer", "no answer", "keine antwort", "geht nicht ran", "nicht erreich", "can't reach", "cant reach",
           "cannot reach", "not reach", "not picking", "not responding", "doesn't respond", "doesnt respond", "nobody opens", "macht nicht auf", "not opening",
           "door", "tür", "tuer", "klingel", "bell", "nicht erreichbar", "unreachable", "switched off", "ausgeschaltet", "mailbox", "voicemail",
           "لا يرد", "cevap vermiyor", "phone nahi utha", "utha nahi")
FIND = ("can't find", "cant find", "cannot find", "finde nicht", "find nicht", "nicht finden", "where is", "wo ist", "entrance", "eingang", "which floor",
        "welcher stock", "stock", "address", "adresse", "hausnummer", "house number", "wrong address", "falsche adresse", "عنوان", "adres", "location of customer",
        "not find", "nicht gefunden", "where exactly")
PHONE = ("number", "nummer", "phone", "telefon", "handy", "contact", "kontakt", "رقم", "numara", "telefon numarası", "call", "anrufen", "mobile")
CUSTOMER = ("customer", "kunde", "kundin", "client", "refuse", "ablehn", "doesn't accept", "does not accept", "not accept", "nicht annehmen", "nimmt nicht",
            "wants to cancel", "will nicht", "didn't order", "did not order", "nicht bestellt", "العميل", "الزبون", "müşteri", "musteri", "grahak")
ACK = ("arrived", "angekommen", "at location", "im here", "i'm here", "i am here", "bin da", "bin hier", "on the way", "on my way", "unterwegs", "coming",
       "komme", "moving", "fahre", "fahre los", "thanks", "thank you", "danke", "done", "delivered", "geliefert", "ausgeliefert", "started", "gestartet",
       "all good", "alles gut", "no problem", "kein problem", "im moving", "i'm moving", "going", "jetzt")
EXCUSE = ("traffic", "stau", "verfahren", "lost", "wrong way", "falsch gefahren", "rain", "regen", "umleitung", "detour", "road closed", "straße gesperrt",
          "red light", "ampel", "slow", "langsam")
HELLO = ("hello", "hallo", "hi", "hey", "salam", "selam", "good evening", "guten abend", "guten tag", "help", "hilfe", "problem", "question", "frage", "مرحبا", "السلام")

NUM_RE = re.compile(r"^\s*(\d{1,3})\s*(min|minutes|minuten|m|')?\s*\.?\s*$", re.I)


def _has(low: str, words) -> bool:
    return any(w in low for w in words)


_WORD = re.compile(r"[a-zäöüß]+", re.I)


def _has_word(low: str, words) -> bool:
    """Short tokens (ok, ja, no, han) match whole words only — 'thanks' must not match 'han'."""
    for w in words:
        w = w.strip()
        if _WORD.fullmatch(w):
            if re.search(rf"(?<![a-zäöüß]){re.escape(w)}(?![a-zäöüß])", low):
                return True
        elif w in low:
            return True
    return False


def classify(text: str, has_photo: bool = False) -> str:
    low = f" {(text or '').strip().lower()} "
    if has_photo and len(low.strip()) < 3:
        return "photo"
    if _has(low, URGENT):
        return "urgent"
    if NUM_RE.match(text or ""):
        return "minutes"
    if _has(low, NO_ORDER):
        return "no_order"
    if _has(low, CLOSED) and not _has(low, ("door", "tür", "tuer")):
        return "closed"
    if _has(low, READY) and not _has(low, NOT_READY_GUARD):
        return "ready"
    if _has(low, FORGOT) and _has(low, FORGOT_CTX):
        return "forgot_finish"
    if _has(low, DAMAGED):
        return "damaged"
    if _has(low, UNREACH) and not _has(low, ("restaurant", "staff", "personal")):
        return "customer_unreachable"
    if _has(low, FIND) and not _has(low, ("restaurant",)):
        return "customer_find"
    if _has(low, PHONE) and (_has(low, CUSTOMER) or not _has(low, ("restaurant",))):
        return "customer_phone"
    if _has(low, CANT):
        return "cant_deliver"
    if _has_word(low, NO) and len(low.strip()) <= 40:
        return "no"
    if _has(low, NOT_READY) and not _has(low, CUSTOMER):
        return "not_ready"
    if _has(low, CUSTOMER):
        return "customer_problem"
    if _has_word(low, YES) and len(low.strip()) <= 30:
        return "yes"
    if _has(low, EXCUSE):
        return "excuse"
    if _has_word(low, ACK):
        return "ack"
    if has_photo:
        return "photo"
    if _has(low, HELLO) or len(low.strip()) <= 2:
        return "other"
    return "other"


INTENT_LABEL = {"not_ready": "order not ready", "closed": "restaurant closed", "no_order": "restaurant: no such order / already taken",
                "cant_deliver": "can't do the delivery", "customer_unreachable": "customer not reachable", "customer_find": "can't find the address",
                "customer_phone": "asked for the customer's number", "customer_problem": "problem at the customer", "forgot_finish": "forgot to finish in the app",
                "damaged": "order damaged", "other": "unclear", "ack": "confirmed", "excuse": "delay explained", "urgent": "URGENT", "photo": "photo",
                "yes": "yes", "no": "no", "minutes": "minutes", "ready": "ready"}


# ----------------------------------------------------------------------------- the flow engine
class RiderFlows:
    """One small state per rider: which situation we are in and what we are waiting for.

    deps (set by app.py):
      order_for(rid)                -> live order dict or None
      send(rid, conv, key, o, **fmt)-> sends the query text (settings override / default) into the conversation, logs it
      forward(rid, conv, note, urgent=False, o=None) -> hands the conversation to a person with a note
      log(rid, o, key, text)        -> log line without sending
      riders_on(o)                  -> list of (name, when) for an order (who accepted it, in order)
      customer_card(o)              -> dict with ref, customer_name, phone, address, notes_line, map_line
      settings()                    -> dict
    """

    def __init__(self, store):
        self.store = store
        self.state: dict = {}
        self.deps: dict = {}
        self._load()

    # ---- persistence (survives a redeploy)
    def _load(self):
        try:
            raw = self.store.get_settings().get("rider_flows") or "{}"
            self.state = json.loads(raw)
        except Exception:
            self.state = {}

    def _save(self):
        try:
            self.store.set_settings({"rider_flows": json.dumps(self.state)[:60000]})
        except Exception:
            pass

    def set(self, rid: str, flow: str, step: str = "", minutes: float = 0, **extra):
        now = time.time()
        st = {"flow": flow, "step": step, "since": now, "until": now + minutes * 60 if minutes else 0}
        st.update(extra)
        self.state[rid] = st
        self._save()
        return st

    def clear(self, rid: str):
        if rid in self.state:
            del self.state[rid]
            self._save()

    def active(self) -> list:
        """For the Intercom page: who is in which flow, waiting for what."""
        out, now = [], time.time()
        for rid, st in self.state.items():
            o = self.deps["order_for"](rid) if self.deps.get("order_for") else None
            out.append({"rider_id": rid, "flow": st.get("flow"), "step": st.get("step"), "since_min": round((now - st.get("since", now)) / 60),
                        "until_min": round((st.get("until", 0) - now) / 60) if st.get("until") else None, "ref": (o or {}).get("ref") or st.get("ref", ""),
                        "conv": st.get("conv", ""), "last": st.get("last", "")})
        return out

    def on(self, key: str) -> bool:
        return self.deps["settings"]().get(f"auto:{key}", "1") == "1"

    # ---- a rider wrote to us
    async def on_message(self, rid: str, text: str, conv: str, in_auto: bool = False, has_photo: bool = False, last_ops_auto: bool = False) -> str:
        d = self.deps
        o = d["order_for"](rid)
        intent = classify(text, has_photo)
        st = self.state.get(rid)
        if st:
            st["conv"], st["last"] = conv, (text or "")[:120]
        ctx = f"{INTENT_LABEL.get(intent, intent)}"
        d["log"](rid, o, f"reply:{intent}", (text or "📷 photo")[:200])

        if intent == "urgent":
            await d["forward"](rid, conv, f"🔴 URGENT — {text[:300]}", urgent=True, o=o)
            self.clear(rid)
            return "forwarded urgent"

        # ---------------- inside a flow: context answers
        if st:
            flow, step = st.get("flow"), st.get("step")
            if flow == "not_ready":
                if intent == "minutes":
                    n = int(NUM_RE.match(text).group(1))
                    n = max(1, min(n, 60))
                    await d["send"](rid, conv, "q:not_ready_minutes", o, n=n)
                    self.set(rid, "not_ready", "wait", n, conv=conv, asked=0, persuaded=st.get("persuaded", False), ref=(o or {}).get("ref", ""))
                    return f"waiting {n} min"
                if intent in ("yes", "ack") and step != "persuade":
                    await d["send"](rid, conv, "q:not_ready_ok", o)
                    self.set(rid, "not_ready", "wait", 10, conv=conv, asked=st.get("asked", 0), persuaded=st.get("persuaded", False), ref=(o or {}).get("ref", ""))
                    return "rider waits"
                if intent == "yes" and step == "persuade":
                    await d["send"](rid, conv, "q:not_ready_ok", o)
                    self.set(rid, "not_ready", "wait", 10, conv=conv, asked=0, persuaded=True, ref=(o or {}).get("ref", ""))
                    return "rider gives 10 more minutes"
                if intent == "ready":
                    self.clear(rid)
                    return "ready — flow closed"
                if intent in ("no", "cant_deliver"):
                    if not st.get("persuaded") and self.on("q:persuade"):
                        await d["send"](rid, conv, "q:persuade", o)
                        self.set(rid, "not_ready", "persuade", 3, conv=conv, persuaded=True, asked=st.get("asked", 0), ref=(o or {}).get("ref", ""))
                        return "asked for 10 more minutes"
                    await d["forward"](rid, conv, f"⚠ wants to hand back {(o or {}).get('ref', '')} — waiting at {(o or {}).get('restaurant', 'the restaurant')} for {self._wait_min(o)} min.\nRider: {text[:300]}", urgent=True, o=o)
                    self.clear(rid)
                    return "forwarded: wants to hand back"
                if intent in ("not_ready", "excuse", "other", "photo"):
                    if step == "persuade":
                        await d["forward"](rid, conv, f"⚠ reply to our 'can you wait 10 more minutes' — needs a person.\nRider: {text[:300]}", urgent=True, o=o)
                        self.clear(rid)
                        return "forwarded"
                    return "noted (still waiting)"
                # closed / no_order / customer… → fall through to a new flow
            elif flow == "await_reason":
                await d["forward"](rid, conv, f"⚠ can't do the delivery — reason: {text[:300]}\nOther orders: {self._others(rid, o)}", urgent=intent == "urgent", o=o)
                self.clear(rid)
                return "forwarded with reason"
            elif flow == "await_photo":
                if intent == "photo" or has_photo:
                    kind = st.get("kind", "")
                    if o is not None and kind:
                        o["query_flag"] = {"closed": "restaurant closed", "damaged": "order damaged", "forgot": "handover photo — finalize"}.get(kind, kind)
                    await d["forward"](rid, conv, f"📷 photo received — {o['query_flag'] if o else kind}. " + ("Please finalize the order in MotionTools." if kind == "forgot" else "Please decide."), o=o)
                    self.clear(rid)
                    return "photo forwarded"
                if intent in ("yes", "ack", "minutes"):
                    return "waiting for the photo"
                # anything else → new flow below
            elif flow == "await_problem":
                if intent in ("customer_unreachable", "customer_phone", "customer_find"):
                    self.clear(rid)
                    return await self._card(rid, conv, o, intent)
                await d["forward"](rid, conv, f"⚠ problem at the customer: {text[:300]}", o=o)
                self.clear(rid)
                return "forwarded"
            elif flow == "customer_wait":
                mins = round((time.time() - st.get("since", time.time())) / 60)
                if o is not None:
                    o["query_flag"] = "customer unreachable" if intent in ("customer_unreachable", "no", "other", "customer_phone") else "address problem"
                await d["forward"](rid, conv, f"⚠ {o['query_flag'] if o else 'customer problem'} — rider at the customer for {mins} min after our card.\nRider: {text[:300]}", o=o)
                self.clear(rid)
                return "forwarded"
            elif flow == "other_wait":
                self.clear(rid)
                if intent in ("other", "ack", "yes", "no", "excuse", "ready", "minutes"):
                    await d["forward"](rid, conv, f"✉ {text[:300]}", o=o)
                    return "forwarded"
                # a clear intent → new flow below

        # ---------------- new flow
        if intent == "not_ready":
            if o is not None:
                o["kitchen_reported"] = True
            if not self.on("q:not_ready"):
                await d["forward"](rid, conv, f"order not ready — {text[:300]}", o=o); return "forwarded"
            await d["send"](rid, conv, "q:not_ready", o)
            self.set(rid, "not_ready", "wait", 10, conv=conv, asked=0, persuaded=False, ref=(o or {}).get("ref", ""))
            return "asked to wait"
        if intent == "closed":
            if o is not None:
                o["query_flag"] = "restaurant closed?"
            if self.on("q:closed"):
                await d["send"](rid, conv, "q:closed", o)
                self.set(rid, "await_photo", "", 5, kind="closed", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for a photo"
            await d["forward"](rid, conv, f"restaurant closed — {text[:300]}", o=o); return "forwarded"
        if intent == "no_order":
            if self.on("q:no_order"):
                await d["send"](rid, conv, "q:no_order", o)
            await d["forward"](rid, conv, f"⚠ restaurant says no such order / already taken.\nRider: {text[:300]}\nRiders on this order: {self._riders(o)}", urgent=True, o=o)
            return "forwarded"
        if intent == "cant_deliver":
            if self.on("q:cant_deliver"):
                await d["send"](rid, conv, "q:cant_deliver", o)
                self.set(rid, "await_reason", "", 3, conv=conv, ref=(o or {}).get("ref", ""))
                return "asked why"
            await d["forward"](rid, conv, f"⚠ can't do the delivery — {text[:300]}\nOther orders: {self._others(rid, o)}", urgent=True, o=o); return "forwarded"
        if intent in ("customer_unreachable", "customer_phone", "customer_find"):
            return await self._card(rid, conv, o, intent)
        if intent == "customer_problem":
            if self.on("q:customer_problem"):
                await d["send"](rid, conv, "q:customer_problem", o)
                self.set(rid, "await_problem", "", 10, conv=conv, ref=(o or {}).get("ref", ""))
                return "asked what exactly"
            await d["forward"](rid, conv, f"problem at the customer — {text[:300]}", o=o); return "forwarded"
        if intent == "forgot_finish":
            done = o is None or o.get("phase") in ("delivered", "closed")
            if done and self.on("q:forgot_done"):
                await d["send"](rid, conv, "q:forgot_done", o)
                return "already completed"
            if self.on("q:forgot_finish"):
                await d["send"](rid, conv, "q:forgot_finish", o)
                self.set(rid, "await_photo", "", 0, kind="forgot", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for the handover photo"
            await d["forward"](rid, conv, f"forgot to finish in the app — {text[:300]}", o=o); return "forwarded"
        if intent == "damaged":
            if o is not None:
                o["query_flag"] = "order damaged?"
            if self.on("q:damaged"):
                await d["send"](rid, conv, "q:damaged", o)
                self.set(rid, "await_photo", "", 5, kind="damaged", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for a photo"
            await d["forward"](rid, conv, f"order damaged — {text[:300]}", o=o); return "forwarded"
        if intent == "photo":
            await d["forward"](rid, conv, "📷 photo received without text", o=o)
            return "photo forwarded"
        if intent in ("ack", "yes", "ready"):
            if in_auto or last_ops_auto:
                if self.on("q:ack"):
                    await d["send"](rid, conv, "q:ack", o)
                return "acknowledged"
            return "ignored (reply to a person)"
        if intent == "excuse":
            if o is not None:
                o["excuse"] = text[:120]
            if in_auto or last_ops_auto:
                if self.on("q:noted"):
                    await d["send"](rid, conv, "q:noted", o)
                return "noted"
            return "ignored (reply to a person)"
        if intent == "minutes" and o is not None and o.get("phase") in ("at_restaurant", "to_restaurant", "accepted"):
            n = max(1, min(int(NUM_RE.match(text).group(1)), 60))
            o["kitchen_reported"] = True
            await d["send"](rid, conv, "q:not_ready_minutes", o, n=n)
            self.set(rid, "not_ready", "wait", n, conv=conv, asked=0, persuaded=False, ref=o.get("ref", ""))
            return f"waiting {n} min"
        # other / no / unclear
        if in_auto:
            await d["forward"](rid, conv, f"✉ reply to an automatic message — needs a person.\nRider: {text[:300]}", o=o)
            return "forwarded"
        if self.on("q:other") and not st:
            await d["send"](rid, conv, "q:other", o)
            self.set(rid, "other_wait", "", 30, conv=conv, ref=(o or {}).get("ref", ""))
            return "asked how we can help"
        await d["forward"](rid, conv, f"✉ {text[:300]}", o=o)
        return "forwarded"

    async def _card(self, rid: str, conv: str, o, intent: str) -> str:
        d = self.deps
        if o is None:
            await d["forward"](rid, conv, "asked for customer details but has no live order", o=None)
            return "forwarded (no order)"
        card = d["customer_card"](o)
        closer = {"customer_phone": "q:customer_card_call", "customer_unreachable": "q:customer_card_wait", "customer_find": "q:customer_card_find"}[intent]
        if self.on("q:customer_card"):
            await d["send"](rid, conv, "q:customer_card", o, closer_key=closer, **card)
        if not card.get("phone_known"):
            await d["forward"](rid, conv, f"ℹ rider needs the customer's number for {o.get('ref')} — not in MotionTools data, please look it up.", o=o)
        self.set(rid, "customer_wait", "", 15, conv=conv, ref=o.get("ref", ""), kind=intent)
        return "customer card sent"

    # ---- timers (once a minute from the automation loop)
    async def tick(self, now: datetime):
        d = self.deps
        t = now.timestamp()
        for rid, st in list(self.state.items()):
            o = d["order_for"](rid)
            flow, until, conv = st.get("flow"), st.get("until") or 0, st.get("conv", "")
            if t - st.get("since", t) > 2 * 3600:
                self.clear(rid); continue
            if flow == "not_ready":
                if o is None or o.get("picked_up_at") or o.get("phase") not in ("at_restaurant", "to_restaurant", "accepted"):
                    self.clear(rid); continue
                if self._wait_min(o) >= 20 and not st.get("decided"):
                    await d["forward"](rid, conv, f"⏱ {o.get('ref')}: rider waiting {self._wait_min(o)} min at {o.get('restaurant')} — decide: keep waiting or reassign.\nLast from rider: {st.get('last', '')}", o=o)
                    st["decided"] = True; self._save(); continue
                if st.get("step") == "persuade" and until and t >= until:
                    await d["forward"](rid, conv, f"⚠ no answer to 'can you wait 10 more minutes' — rider may hand back {o.get('ref')} ({self._wait_min(o)} min at {o.get('restaurant')}).", urgent=True, o=o)
                    self.clear(rid); continue
                if st.get("step") == "wait" and until and t >= until:
                    if not st.get("asked"):
                        await d["send"](rid, conv, "q:not_ready_check", o)
                        st["asked"], st["until"] = 1, t + 5 * 60; self._save()
                    else:
                        await d["forward"](rid, conv, f"⏱ {o.get('ref')}: no answer after the check, {self._wait_min(o)} min at {o.get('restaurant')}.", o=o)
                        self.clear(rid)
            elif flow in ("await_photo", "await_reason") and until and t >= until:
                what = "photo" if flow == "await_photo" else "reason"
                kind = st.get("kind") or "cannot deliver"
                await d["forward"](rid, conv, f"⏳ no {what} received for '{kind}' — please follow up.", o=o)
                self.clear(rid)
            elif flow == "customer_wait":
                if o is None or o.get("phase") in ("delivered", "closed", "cancelled"):
                    self.clear(rid); continue
                if until and t >= until:
                    await d["forward"](rid, conv, f"⏱ {o.get('ref')}: 15 min since the customer card, no news from the rider.", o=o)
                    self.clear(rid)
            elif flow in ("await_problem", "other_wait") and until and t >= until:
                self.clear(rid)

    # ---- helpers
    def _wait_min(self, o) -> int:
        if not o or not o.get("at_restaurant_at"):
            return 0
        return int((datetime.now(UTC) - o["at_restaurant_at"]).total_seconds() // 60)

    def _riders(self, o) -> str:
        try:
            return ", ".join(f"{n} ({w})" for n, w in self.deps["riders_on"](o)) or "–"
        except Exception:
            return "–"

    def _others(self, rid, o) -> str:
        try:
            return self.deps["others"](rid, o)
        except Exception:
            return "–"


def render_query(template: str, **fmt) -> str:
    """Fill a query text; missing values become empty; tidy double spaces and empty lines."""
    class _D(dict):
        def __missing__(self, k):
            return ""
    out = template.format_map(_D(**fmt))
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    out = re.sub(r" ·\s*(?=\n|$)", "", out)
    return out.strip()
