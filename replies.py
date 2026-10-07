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
    ("q:phone_lookup", "Rider needs the customer's number but MotionTools didn't send it → a dispatcher sends it",
     "Einen Moment – ein Disponent schickt dir gleich die Nummer des Kunden. Bitte bleib vor Ort, klingel nochmal und schau, ob jemand kommt."
     " / One moment – a dispatcher will send you the customer's number right away. Please stay there, ring the bell again and see if someone comes."),
    ("q:customer_problem", "Customer doesn't accept / problem at the customer → ask what exactly",
     "Was genau ist das Problem? / What exactly is the problem?"),
    ("q:forgot_finish", "Forgot to finish the order in the app → deliver + photo",
     "Bitte liefere das Essen aus und schick mir hier ein Foto der Übergabe, dann schließen wir die Bestellung ab. / Please deliver the food and send me a photo of the handover here; then we finalize the order."),
    ("q:forgot_done", "…but the order is already completed on our side",
     "Die Bestellung ist bei uns schon abgeschlossen – alles gut. / The order is already completed on our side – all good."),
    ("q:damaged", "Order damaged → ask for a photo",
     "Bitte schick mir ein Foto der Bestellung. / Please send me a photo of the order."),
    ("q:order_ref", "Rider sends only an order number → confirm the order, ask what's wrong",
     "Danke, Bestellung {ref} gefunden ({status}, {restaurant} → {address}). Was ist das Problem? / Thanks, found order {ref} ({status}, {restaurant} → {address}). What is the problem?"),
    ("q:remove_order", "Rider wants the order removed / taken back → acknowledge, a dispatcher decides",
     "Verstanden – ein Disponent prüft {ref} sofort und nimmt die Bestellung ggf. raus. Bitte kurz warten, wir melden uns in wenigen Minuten. / Understood – a dispatcher is checking {ref} right now and will remove it if needed. Please wait a moment, we'll get back to you within a few minutes."),
    ("q:holding", "Rider waits for a person and nobody has answered yet",
     "Danke für deine Geduld – ein Disponent ist informiert und meldet sich gleich bei dir. / Thanks for your patience – a dispatcher has been alerted and will reply to you shortly."),
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
CLOSED = ("لا يوجد احد في المطعم", "لا يوجد أحد في المطعم", "nobody at the restaurant", "no one at the restaurant", "niemand im restaurant", "geschlossen", "closed", " zu.", " zu ", "locked", "nobody there", "niemand da", "niemand hier", "not open", "nicht offen", "nicht geöffnet", "shut",
          "مغلق", "kapalı", "kapali", "band hai", "closed restaurant", "restaurant closed")
NO_ORDER = ("does not have this order", "doesn't have this order", "does not have the order", "doesn't have the order", "not have this order",
            "hat diese bestellung nicht", "hat die bestellung nicht", "hat keine bestellung", "kennt die bestellung nicht", "ليس لديهم الطلب", "ما عندهم الطلب",
            "no order", "no such", "kein auftrag", "keine bestellung", "nicht da", "already taken", "already picked", "schon abgeholt", "schon weg",
            "another driver", "anderer fahrer", "andere fahrer", "wrong order", "falsche bestellung", "don't have", "dont have", "haben nicht", "haben keine",
            "not found", "nicht gefunden", "no booking", "they say no")
NOT_READY = ("مش جاهز", "مو جاهز", "ما جاهز", "ما خلص", "لسا", "لسه", "not ready", "nicht fertig", "noch nicht", "not prepared", "preparing", "still cooking", "being prepared", "wird noch", "dauert", "takes time",
             "will take", "take time", "not done", "kitchen", "küche", "kueche", "wait", "warte", "warten", "late", "spät", "spaet", "ready nahi",
             "hazır değil", "hazir degil", "غير جاهز", "ليس جاهز", "لم يجهز", "order is not", "bestellung ist nicht", "still waiting", "noch warten")
READY = ("ready now", "jetzt fertig", "ist fertig", "is ready", "order ready", "bestellung fertig", "picked up", "abgeholt", "habs", "hab es", "got it", "have it", "got the order", "habe die bestellung", "on my way now", " ready", " fertig")
NOT_READY_GUARD = ("not ready", "nicht fertig", "isn't ready", "isnt ready", "no ready", "not yet ready", "noch nicht fertig", "ready nahi")
FORGOT_CTX = ("order", "bestellung", "auftrag", "app", "finish", "complete", "abschließ", "abschliess", "finaliz", "deliver", "liefer", "mark", "photo", "foto", "bild", "upload")
FORGOT = ("forgot", "vergessen", "finish the order", "complete the order", "complete this order", "complete order", "complete my order", "finish this order",
          "finish order", "close this order", "end the order", "order complete", "photo not upload", "not getting upload", "not uploading", "can't upload", "cant upload",
          "cannot upload", "upload nahi", "foto geht nicht", "foto lädt nicht", "bild lädt nicht", "auftrag abschließen", "beenden", "abschluss", "abschließen", "abschliessen", "close the order", "mark delivered", "als geliefert",
          "finalize", "finalise", "bhool", "نسيت", "unuttum", "not completed", "nicht abgeschlossen", "cannot complete", "can't complete")
DAMAGED = ("damaged", "beschädigt", "beschaedigt", "kaputt", "spilled", "verschüttet", "verschuettet", "ausgelaufen", "leaking", "leak", "broken", "zerbrochen",
           "تالف", "hasarlı", "hasarli", "fell", "runtergefallen", "squashed")
CANT = ("مشكلة بالسيارة", "مشكلة في السيارة", "السيارة تعطلت", "الدراجة", "car broke", "car problem", "problem with my car", "auto kaputt", "panne", "reifen", "flat tire", "platten", "akku leer", "battery empty", "bike broke", "fahrrad kaputt", "unfall", "accident", "حادث", "لا أستطيع القيام بالتوصيل", "لا أستطيع التوصيل", "ما بقدر وصل", "ما اقدر اوصل", "القيام بالتوصيل", "cannot make the delivery", "can't make the delivery",
        "can't do", "cant do", "cannot do", "can't deliver", "cant deliver", "cannot deliver", "kann nicht liefern", "kann nicht ausliefern", "nicht liefern",
        "kann nicht machen", "can't continue", "cannot continue", "nicht weiter", "unable", "bike", "fahrrad", "panne", "flat", "platten", "reifen", "tire", "tyre",
        "sick", "krank", "going home", "nach hause", "feierabend", "too far", "zu weit", "too big", "zu groß", "zu gross", "nahi kar sakta", "لا أستطيع التوصيل",
        "teslim edemiyorum", "i can't", "ich kann nicht")
UNREACH = ("الباب لا يفتح", "ما بيفتح", "ما حدا فتح", "لا أحد يفتح", "door does not open", "door doesn't open", "door not open", "nobody opens the door",
           "tür geht nicht auf", "öffnet nicht", "antwortet nicht", "reagiert nicht", "meldet sich nicht", "nicht vor ort", "nicht zu hause", "nicht zuhause", "niemand zu hause", "macht nicht auf",
           "does not respond", "doesn't respond", "not respond", "no response", "not on site", "not at home", "not home", "not there", "nobody home", "no one home",
           "nobody there", "no one there", "kunde nicht da", "customer not there", "التواصل", "تواصل", "الاتصال", "اتصل", "اتصال", "يرد", "يجاوب", "يرد على", "communicat", "can't call", "cannot call", "not reachable",
           "erreichen", "erreiche", "nicht erreichbar", "ulaşamıyorum", "ulasamiyorum", "cevap vermiyor", "ne javlja", "ne odgovara", "ne mogu kontaktirati",
           "contact nahi", "baat nahi", "ما يرد", "ما بيرد", "مش بيرد", "مابيرد", "ما يجاوب", "لا يجيب", "ما يرد على", "مش عم يرد", "ما حدا بيفتح", "cannot contact", "can't contact", "cant contact", "not contact", "nicht kontaktieren", "no contact", "not answering", "doesn't answer", "does not answer", "no answer", "keine antwort", "geht nicht ran", "nicht erreich", "can't reach", "cant reach",
           "cannot reach", "not reach", "not picking", "not responding", "doesn't respond", "doesnt respond", "nobody opens", "macht nicht auf", "not opening",
           "door", "tür", "tuer", "klingel", "bell", "nicht erreichbar", "unreachable", "switched off", "ausgeschaltet", "mailbox", "voicemail",
           "لا يرد", "cevap vermiyor", "phone nahi utha", "utha nahi")
FIND = ("can't find", "cant find", "cannot find", "finde nicht", "find nicht", "nicht finden", "where is", "wo ist", "entrance", "eingang", "which floor",
        "welcher stock", "stock", "address", "adresse", "hausnummer", "house number", "wrong address", "falsche adresse", "عنوان", "adres", "location of customer",
        "not find", "nicht gefunden", "where exactly")
PHONE = ("تلفون", "تليفون", "هاتف", "الهاتف", "موبايل", "جوال", "الرقم", "number", "nummer", "phone", "telefon", "handy", "contact", "kontakt", "رقم", "numara", "telefon numarası", "call", "anrufen", "mobile")
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


TAG_INTENTS = {
    "not_ready": "not_ready", "order_not_ready": "not_ready", "notready": "not_ready", "order not ready": "not_ready", "not ready": "not_ready",
    "closed": "closed", "restaurant_closed": "closed", "restaurant closed": "closed",
    "no_order": "no_order", "already_taken": "no_order", "no order": "no_order",
    "cant_deliver": "cant_deliver", "cannot_deliver": "cant_deliver", "can't do the delivery": "cant_deliver", "cant deliver": "cant_deliver", "i can't do the delivery": "cant_deliver",
    "customer_unreachable": "customer_unreachable", "customer unreachable": "customer_unreachable", "cannot contact customer": "customer_unreachable",
    "i cannot get in touch with the customer": "customer_unreachable", "customer not reachable": "customer_unreachable", "cant reach customer": "customer_unreachable",
    "customer_phone": "customer_phone", "customer number": "customer_phone", "phone": "customer_phone",
    "customer_find": "customer_find", "wrong address": "customer_find", "cant find": "customer_find",
    "customer_problem": "customer_problem", "customer doesn't accept the order": "customer_problem", "customer refuses": "customer_problem", "customer problem": "customer_problem",
    "forgot_finish": "forgot_finish", "forgot": "forgot_finish", "i forgot to finish the order on the app": "forgot_finish", "forgot to complete": "forgot_finish",
    "damaged": "damaged", "order_damaged": "damaged", "order is damaged": "damaged",
    "other": "other",
}


REMOVE = ("remove this order", "remove the order", "remove order", "remove it", "remove esko", "remove isko", "remove krna", "remove krdo", "krdo remove", "remove kar",
          "remove karo", "krdo bhai remove", "kar do remove", "krdo remove", "bhai remove", "hatao", "hata do", "order hata", "take this order", "take the order back", "unassign", "auftrag entfernen", "bestellung entfernen",
          "nimm den auftrag", "auftrag rausnehmen", "storniere", "احذف الطلب", "شيل الطلب", "الغي الطلب", "siparişi kaldır")


def intent_from_tags(tags) -> str:
    """Intercom tags set by the workflow buttons (language-independent): 'q:not_ready', 'customer_unreachable', …"""
    for t in tags or []:
        name = str(t.get("name") if isinstance(t, dict) else t).strip().lower()
        name = name[2:] if name.startswith("q:") else name
        name = name.replace("-", "_") if "_" in name.replace("-", "_") and " " not in name else name
        if name in TAG_INTENTS:
            return TAG_INTENTS[name]
        key = name.replace("_", " ")
        if key in TAG_INTENTS:
            return TAG_INTENTS[key]
    return ""


NUDGE_PHRASES = ("is anyone there", "anyone there", "anyone here", "is someone there", "jemand da", "ist jemand da", "hallo?",
                 "هل يوجد احد", "هل يوجد أحد", "حدا هون", "في حدا", "فيه احد", "يا جماعة", "kimse var mı", "koi hai")
NUDGE_WORDS = {"und", "and", "hello", "hallo", "hi", "hey", "anyone", "jemand", "update", "news", "still", "noch", "wait", "waiting",
               "und jetzt", "what now", "bitte", "please", "pls", "plz", "any update", "any news", "still waiting", "warte noch", "ich warte"}


def is_nudge(text: str) -> bool:
    """'?', '??', 'und?', 'hello?' … — the rider is waiting for an answer to what he already asked."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if re.fullmatch(r"[\s?!.¿؟…]+", t):
        return True
    core = re.sub(r"[?!.¿؟…]+", "", t).strip()
    if any(ph in t for ph in NUDGE_PHRASES) and len(t.split()) <= 5 and not any(w in t for w in ("restaurant", "مطعم", "المطعم", "kunde", "customer", "العميل", "الزبون")):
        return True
    return ("?" in t or "؟" in t) and core in NUDGE_WORDS


def classify(text: str, has_photo: bool = False, tags=None) -> str:
    tagged = intent_from_tags(tags)
    if tagged:
        return tagged
    low = f" {(text or '').strip().lower()} "
    if has_photo and len(low.strip()) < 3:
        return "photo"
    if is_nudge(text):
        return "nudge"
    if _has(low, URGENT):
        return "urgent"
    if NUM_RE.match(text or ""):
        return "minutes"
    if _has(low, REMOVE):
        return "remove_order"
    if _has(low, NO_ORDER) and not (_has(low, CUSTOMER) and _has(low, UNREACH)) and not _has(low, PHONE):
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
    if _has_word(low, NO) and len(low.strip()) <= 40 and not _has(low, CUSTOMER) and not _has(low, ("restaurant", "مطعم", "المطعم")):
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


INTENT_LABEL = {"nudge": "waiting for an answer ('?')", "not_ready": "order not ready", "closed": "restaurant closed", "no_order": "restaurant: no such order / already taken",
                "cant_deliver": "can't do the delivery", "customer_unreachable": "customer not reachable", "customer_find": "can't find the address",
                "customer_phone": "asked for the customer's number", "customer_problem": "problem at the customer", "forgot_finish": "forgot to finish in the app",
                "damaged": "order damaged", "other": "unclear", "ack": "confirmed", "excuse": "delay explained", "urgent": "URGENT", "photo": "photo",
                "yes": "yes", "no": "no", "minutes": "minutes", "ready": "ready", "order_ref": "order number only", "remove_order": "wants the order removed"}


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
        self.handover: dict = {}        # conversation id -> time it was handed to a person: the bot stays silent there
        self.asked_other: dict = {}     # rider id -> time we last asked "how can we help?"
        self.sent_keys: dict = {}       # rider id -> {query key: time sent}
        self._last: dict = {}           # rider id -> his last message
        self.named_ref: dict = {}       # rider id -> (order ref he wrote himself, when)
        self._prev: dict = {}           # rider id -> his previous message (context for Claude)
        self.last_rider_at: dict = {}   # conversation id -> when the rider started waiting
        self.conv_rider: dict = {}      # conversation id -> rider id
        self.card_conv: dict = {}       # conversation id -> when the customer card was sent into it
        self.last_intent: dict = {}     # rider id -> what the bot finally understood (Claude or keywords)
        self.deps: dict = {}
        self._load()

    # ---- persistence (survives a redeploy)
    def _load(self):
        try:
            raw = self.store.get_settings().get("rider_flows") or "{}"
            self.state = json.loads(raw)
            self.handover = json.loads(self.store.get_settings().get("rider_handover") or "{}")
        except Exception:
            self.state = {}

    HANDOVER_H = 3                      # hours the bot keeps out of a conversation a person is answering
    GRACE_MIN = 5                       # after a forward: minutes a person gets to answer before the bot steps back in

    def handed_over(self, conv: str) -> bool:
        t = self.handover.get(conv or "")
        return bool(t and time.time() - t < self.HANDOVER_H * 3600)

    async def person_engaged(self, conv: str) -> bool:
        """Silent only while a person is really on it: within the grace minutes after a forward, or when a teammate
        has actually replied since. A forward nobody answers must not leave the rider talking to a wall."""
        t = self.handover.get(conv or "")
        if not t or time.time() - t > self.HANDOVER_H * 3600:
            return False
        if time.time() - t < self.GRACE_MIN * 60:
            return True
        hr = self.deps.get("human_replied_since")
        if not hr:
            return True
        try:
            return await hr(conv, t)
        except Exception:
            return True

    async def _send(self, rid, conv, key, o, **fmt):
        """Never say the same thing twice: if this answer already went to the rider in the last 30 min, he is
        clearly not helped by it — a person takes over instead of the bot repeating itself."""
        last = self.sent_keys.setdefault(rid, {})
        if key in last and time.time() - last[key] < 30 * 60 and key not in ("q:not_ready_check", "q:holding"):
            await self._fwd(rid, conv, f"🔁 rider is not helped by our automatic answer ({key[2:].replace('_', ' ')}) — please reply personally.\nRider: {self.state.get(rid, {}).get('last') or self._last.get(rid, '')}", o=o)
            return False
        last[key] = time.time()
        await self.deps["send"](rid, conv, key, o, **fmt)
        if key != "q:holding":
            self.last_rider_at.pop(conv, None)          # the rider got an answer — nobody is waiting any more
        return True

    async def _fwd(self, rid, conv, note, urgent=False, o=None):
        """Forward to a person and step back: from now on that person owns the conversation."""
        await self.deps["forward"](rid, conv, note, urgent=urgent, o=o)
        if conv:
            self.handover[conv] = time.time()
            self._save()

    def _save(self):
        try:
            self.handover = {c: t for c, t in self.handover.items() if time.time() - t < self.HANDOVER_H * 3600}
            self.store.set_settings({"rider_flows": json.dumps(self.state)[:60000], "rider_handover": json.dumps(self.handover)[:30000]})
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
        for rid, st in list(self.state.items()):              # snapshot: may run in a worker thread
            o = self.deps["order_for"](rid) if self.deps.get("order_for") else None
            out.append({"rider_id": rid, "flow": st.get("flow"), "step": st.get("step"), "since_min": round((now - st.get("since", now)) / 60),
                        "until_min": round((st.get("until", 0) - now) / 60) if st.get("until") else None, "ref": (o or {}).get("ref") or st.get("ref", ""),
                        "conv": st.get("conv", ""), "last": st.get("last", "")})
        return out

    def on(self, key: str) -> bool:
        return self.deps["settings"]().get(f"auto:{key}", "1") == "1"

    # ---- a rider wrote to us
    async def on_message(self, rid: str, text: str, conv: str, in_auto: bool = False, has_photo: bool = False, last_ops_auto: bool = False, tags=None) -> str:
        d = self.deps
        o = d["order_for"](rid)
        named = d["order_by_ref"](text) if d.get("order_by_ref") else None
        if named is not None:
            o = named                                    # the rider named the order himself — that's the one he means
            self.named_ref[rid] = (named.get("ref"), time.time())
        elif o is None and rid in self.named_ref and time.time() - self.named_ref[rid][1] < 2 * 3600 and d.get("order_by_ref"):
            o = d["order_by_ref"](self.named_ref[rid][0])   # he told us the order earlier in this chat
        intent = classify(text, has_photo, tags)
        if named is not None and intent == "other" and len((text or "").split()) <= 3:
            intent = "order_ref"
        st = self.state.get(rid)
        self._last[rid] = (text or "")[:200]
        self.last_rider_at.setdefault(conv, time.time())
        self.conv_rider[conv] = rid
        if st:
            st["conv"], st["last"] = conv, (text or "")[:120]
        ctx = f"{INTENT_LABEL.get(intent, intent)}"
        d["log"](rid, o, f"reply:{intent}", (text or "📷 photo")[:200])

        if intent == "nudge":
            prev = self._prev.get(rid, "")
            last_bot = max(self.sent_keys.get(rid, {}).values(), default=0)
            if st or self.handed_over(conv) or time.time() - last_bot < 45 * 60 or conv in self.card_conv:
                if time.time() - self.sent_keys.get(rid, {}).get("q:holding", 0) > 10 * 60:
                    await self._send(rid, conv, "q:holding", o)
                await self._fwd(rid, conv, f"⏰ rider is waiting for an answer (wrote '{text.strip()[:20]}').\nHis question: {prev[:300] or '—'}", urgent=True, o=o)
                return "rider waiting ('?') → holding reply + urgent forward"
            intent = "other"
        self.last_intent[rid] = intent
        # understanding first: Claude reads every real sentence (any language, slang, typos) — keywords are the fallback
        in_yes_no = bool(st and st.get("flow") == "not_ready" and st.get("step") in ("wait", "persuade"))
        if d.get("smart") and not intent_from_tags(tags) and not in_yes_no and intent not in ("urgent", "minutes", "photo", "order_ref") \
                and len((text or "").split()) >= 3:
            try:
                guess = await d["smart"](text, o, self._prev.get(rid, ""))
                if guess:
                    intent = guess
            except Exception:
                pass
        elif intent == "other" and d.get("smart"):
            try:
                guess = await d["smart"](text, o, self._prev.get(rid, ""))
                if guess:
                    intent = guess
            except Exception:
                pass
        self._prev[rid] = (text or "")[:200]
        self.last_intent[rid] = intent

        if self.handed_over(conv):
            if intent in ("customer_phone", "customer_unreachable", "customer_find") and o is not None \
                    and time.time() - self.card_conv.get(conv, 0) > 30 * 60:
                return await self._card(rid, conv, o, intent)  # the customer card is pure data — useful even while a person handles it
            if await self.person_engaged(conv):
                if intent == "urgent":
                    await self._fwd(rid, conv, f"🔴 URGENT — {text[:300]}", urgent=True, o=o)
                    return "urgent note added (person already on it)"
                self.clear(rid)
                return "silent — a person is answering this conversation"
            # forwarded, but nobody answered: the bot takes the conversation back and handles this message itself
            self.handover.pop(conv, None)
            self._save()
            if intent in ("other", "no", "yes", "ack"):
                waited = round((time.time() - self.last_rider_at.get(conv, time.time())) / 60)
                if time.time() - self.sent_keys.get(rid, {}).get("q:holding", 0) > 20 * 60:
                    await self._send(rid, conv, "q:holding", o)
                await self._fwd(rid, conv, f"⏰ rider still waiting for an answer{f' ({waited} min)' if waited else ''} — please reply.\nRider: {text[:300]}", urgent=True, o=o)
                return "holding reply + urgent re-forward"

        if intent == "urgent":
            await self._fwd(rid, conv, f"🔴 URGENT — {text[:300]}", urgent=True, o=o)
            self.clear(rid)
            return "forwarded urgent"

        # ---------------- inside a flow: context answers
        if st:
            flow, step = st.get("flow"), st.get("step")
            if flow == "not_ready":
                if intent == "minutes":
                    n = int(NUM_RE.match(text).group(1))
                    n = max(1, min(n, 60))
                    await self._send(rid, conv, "q:not_ready_minutes", o, n=n)
                    self.set(rid, "not_ready", "wait", n, conv=conv, asked=0, persuaded=st.get("persuaded", False), ref=(o or {}).get("ref", ""))
                    return f"waiting {n} min"
                if intent in ("yes", "ack") and step != "persuade":
                    await self._send(rid, conv, "q:not_ready_ok", o)
                    self.set(rid, "not_ready", "wait", 10, conv=conv, asked=st.get("asked", 0), persuaded=st.get("persuaded", False), ref=(o or {}).get("ref", ""))
                    return "rider waits"
                if intent == "yes" and step == "persuade":
                    await self._send(rid, conv, "q:not_ready_ok", o)
                    self.set(rid, "not_ready", "wait", 10, conv=conv, asked=0, persuaded=True, ref=(o or {}).get("ref", ""))
                    return "rider gives 10 more minutes"
                if intent == "ready":
                    self.clear(rid)
                    return "ready — flow closed"
                if intent in ("no", "cant_deliver"):
                    if not st.get("persuaded") and self.on("q:persuade"):
                        await self._send(rid, conv, "q:persuade", o)
                        self.set(rid, "not_ready", "persuade", 3, conv=conv, persuaded=True, asked=st.get("asked", 0), ref=(o or {}).get("ref", ""))
                        return "asked for 10 more minutes"
                    await self._fwd(rid, conv, f"⚠ wants to hand back {(o or {}).get('ref', '')} — waiting at {(o or {}).get('restaurant', 'the restaurant')} for {self._wait_min(o)} min.\nRider: {text[:300]}", urgent=True, o=o)
                    self.clear(rid)
                    return "forwarded: wants to hand back"
                if intent in ("not_ready", "excuse", "other", "photo"):
                    if step == "persuade":
                        await self._fwd(rid, conv, f"⚠ reply to our 'can you wait 10 more minutes' — needs a person.\nRider: {text[:300]}", urgent=True, o=o)
                        self.clear(rid)
                        return "forwarded"
                    return "noted (still waiting)"
                # closed / no_order / customer… → fall through to a new flow
            elif flow == "await_reason":
                await self._fwd(rid, conv, f"⚠ can't do the delivery — reason: {text[:300]}\nOther orders: {self._others(rid, o)}", urgent=intent == "urgent", o=o)
                self.clear(rid)
                return "forwarded with reason"
            elif flow == "await_photo":
                if intent == "photo" or has_photo:
                    kind = st.get("kind", "")
                    if o is not None and kind:
                        o["query_flag"] = {"closed": "restaurant closed", "damaged": "order damaged", "forgot": "handover photo — finalize"}.get(kind, kind)
                    await self._fwd(rid, conv, f"📷 photo received — {o['query_flag'] if o else kind}. " + ("Please finalize the order in MotionTools." if kind == "forgot" else "Please decide."), o=o)
                    self.clear(rid)
                    return "photo forwarded"
                if intent in ("yes", "ack", "minutes"):
                    return "waiting for the photo"
                # anything else → new flow below
            elif flow == "await_problem":
                if intent in ("customer_unreachable", "customer_phone", "customer_find"):
                    self.clear(rid)
                    return await self._card(rid, conv, o, intent)
                await self._fwd(rid, conv, f"⚠ problem at the customer: {text[:300]}", o=o)
                self.clear(rid)
                return "forwarded"
            elif flow == "customer_wait" and intent in ("customer_phone", "customer_unreachable", "customer_find") and time.time() - self.card_conv.get(conv, 0) > 30 * 60:
                return await self._card(rid, conv, o, intent)      # asked again in a new chat: the card goes here too
            elif flow == "customer_wait":
                mins = round((time.time() - st.get("since", time.time())) / 60)
                if o is not None:
                    o["query_flag"] = "customer unreachable" if intent in ("customer_unreachable", "no", "other", "customer_phone") else "address problem"
                await self._fwd(rid, conv, f"⚠ {o['query_flag'] if o else 'customer problem'} — rider at the customer for {mins} min after our card.\nRider: {text[:300]}", o=o)
                self.clear(rid)
                return "forwarded"
            elif flow == "other_wait":
                self.clear(rid)
                if intent in ("other", "ack", "yes", "no", "excuse", "ready", "minutes"):
                    await self._fwd(rid, conv, f"✉ {text[:300]}", o=o)
                    return "forwarded"
                # a clear intent → new flow below

        # ---------------- new flow
        if intent == "not_ready":
            if o is not None:
                o["kitchen_reported"] = True
            if not self.on("q:not_ready"):
                await self._fwd(rid, conv, f"order not ready — {text[:300]}", o=o); return "forwarded"
            await self._send(rid, conv, "q:not_ready", o)
            self.set(rid, "not_ready", "wait", 10, conv=conv, asked=0, persuaded=False, ref=(o or {}).get("ref", ""))
            return "asked to wait"
        if intent == "closed":
            if o is not None:
                o["query_flag"] = "restaurant closed?"
            if self.on("q:closed"):
                await self._send(rid, conv, "q:closed", o)
                self.set(rid, "await_photo", "", 5, kind="closed", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for a photo"
            await self._fwd(rid, conv, f"restaurant closed — {text[:300]}", o=o); return "forwarded"
        if intent == "no_order":
            if self.on("q:no_order"):
                await self._send(rid, conv, "q:no_order", o)
            await self._fwd(rid, conv, f"⚠ restaurant says no such order / already taken.\nRider: {text[:300]}\nRiders on this order: {self._riders(o)}", urgent=True, o=o)
            return "forwarded"
        if intent == "cant_deliver":
            if self.on("q:cant_deliver"):
                await self._send(rid, conv, "q:cant_deliver", o)
                self.set(rid, "await_reason", "", 3, conv=conv, ref=(o or {}).get("ref", ""))
                return "asked why"
            await self._fwd(rid, conv, f"⚠ can't do the delivery — {text[:300]}\nOther orders: {self._others(rid, o)}", urgent=True, o=o); return "forwarded"
        if intent in ("customer_unreachable", "customer_phone", "customer_find"):
            return await self._card(rid, conv, o, intent)
        if intent == "customer_problem":
            if self.on("q:customer_problem"):
                await self._send(rid, conv, "q:customer_problem", o)
                self.set(rid, "await_problem", "", 10, conv=conv, ref=(o or {}).get("ref", ""))
                return "asked what exactly"
            await self._fwd(rid, conv, f"problem at the customer — {text[:300]}", o=o); return "forwarded"
        if intent == "forgot_finish":
            done = o is None or o.get("phase") in ("delivered", "closed")
            if done and self.on("q:forgot_done"):
                await self._send(rid, conv, "q:forgot_done", o)
                return "already completed"
            if self.on("q:forgot_finish"):
                await self._send(rid, conv, "q:forgot_finish", o)
                self.set(rid, "await_photo", "", 0, kind="forgot", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for the handover photo"
            await self._fwd(rid, conv, f"forgot to finish in the app — {text[:300]}", o=o); return "forwarded"
        if intent == "damaged":
            if o is not None:
                o["query_flag"] = "order damaged?"
            if self.on("q:damaged"):
                await self._send(rid, conv, "q:damaged", o)
                self.set(rid, "await_photo", "", 5, kind="damaged", conv=conv, ref=(o or {}).get("ref", ""))
                return "asked for a photo"
            await self._fwd(rid, conv, f"order damaged — {text[:300]}", o=o); return "forwarded"
        if intent == "remove_order":
            already = time.time() - self.sent_keys.get(rid, {}).get("q:remove_order", 0) < 30 * 60
            if already:
                if time.time() - self.sent_keys.get(rid, {}).get("q:holding", 0) > 20 * 60:
                    await self._send(rid, conv, "q:holding", o)
                await self._fwd(rid, conv, f"⏰ rider insists: remove {(o or {}).get('ref') or 'the order'} — still no answer.\nRider: {text[:300]}", urgent=True, o=o)
                return "holding reply + urgent re-forward"
            if self.on("q:remove_order"):
                await self._send(rid, conv, "q:remove_order", o)
            await self._fwd(rid, conv, f"🔴 wants {(o or {}).get('ref') or 'the order'} removed / taken back — please decide in MotionTools.\nRider: {text[:300]}", urgent=True, o=o)
            return "acknowledged + forwarded (remove order)"
        if intent == "photo":
            await self._fwd(rid, conv, "📷 photo received without text", o=o)
            return "photo forwarded"
        if intent == "order_ref":
            if self.on("q:order_ref"):
                await self._send(rid, conv, "q:order_ref", o, status=d["status_of"](o) if d.get("status_of") else o.get("phase", ""),
                                 address=f"{o.get('customer_addr') or ''} {o.get('customer_zip') or ''}".strip())
                self.set(rid, "other_wait", "", 30, conv=conv, ref=o.get("ref", ""))
                return "order recognised, asked what's wrong"
            await self._fwd(rid, conv, f"✉ order {o.get('ref')}", o=o)
            return "forwarded"
        if intent in ("ack", "yes", "ready"):
            if in_auto or last_ops_auto:
                if self.on("q:ack"):
                    await self._send(rid, conv, "q:ack", o)
                return "acknowledged"
            return "ignored (reply to a person)"
        if intent == "excuse":
            if o is not None:
                o["excuse"] = text[:120]
            if in_auto or last_ops_auto:
                if self.on("q:noted"):
                    await self._send(rid, conv, "q:noted", o)
                return "noted"
            return "ignored (reply to a person)"
        if intent == "minutes" and o is not None and o.get("phase") in ("at_restaurant", "to_restaurant", "accepted"):
            n = max(1, min(int(NUM_RE.match(text).group(1)), 60))
            o["kitchen_reported"] = True
            await self._send(rid, conv, "q:not_ready_minutes", o, n=n)
            self.set(rid, "not_ready", "wait", n, conv=conv, asked=0, persuaded=False, ref=o.get("ref", ""))
            return f"waiting {n} min"
        # other / no / unclear
        if in_auto:
            await self._fwd(rid, conv, f"✉ reply to an automatic message — needs a person.\nRider: {text[:300]}", o=o)
            return "forwarded"
        recently_asked = time.time() - self.asked_other.get(rid, 0) < 3 * 3600
        substantive = len((text or "").split()) >= 4          # a real sentence = the rider already said what he needs
        if self.on("q:other") and not st and not recently_asked and not substantive:
            await self._send(rid, conv, "q:other", o)
            self.asked_other[rid] = time.time()
            self.set(rid, "other_wait", "", 30, conv=conv, ref=(o or {}).get("ref", ""))
            return "asked how we can help"
        await self._fwd(rid, conv, f"✉ {text[:300]}", o=o)
        return "forwarded"

    async def _card(self, rid: str, conv: str, o, intent: str) -> str:
        d = self.deps
        if o is None:
            await self._fwd(rid, conv, "asked for customer details but has no live order", o=None)
            return "forwarded (no order)"
        card = d["customer_card"](o)
        closer = {"customer_phone": "q:customer_card_call", "customer_unreachable": "q:customer_card_wait", "customer_find": "q:customer_card_find"}[intent]
        if not card.get("phone_known") and intent in ("customer_phone", "customer_unreachable"):
            # an empty card ("see the app") helps nobody: tell him a person sends the number, and get a person on it now
            if time.time() - self.card_conv.get(conv, 0) > 30 * 60:
                await self._send(rid, conv, "q:phone_lookup", o)
                self.card_conv[conv] = time.time()
            await self._fwd(rid, conv, f"📞 rider needs the customer's number for {o.get('ref')} — not in MotionTools data, please send it to him now.", urgent=True, o=o)
            self.set(rid, "customer_wait", "", 15, conv=conv, ref=o.get("ref", ""), kind=intent)
            return "phone unknown → told rider a dispatcher sends it + urgent forward"
        if self.on("q:customer_card"):
            # the card is data, not a phrase: it goes into every conversation the rider asks in (riders often start a new
            # chat per button) — only a repeat inside the same conversation is skipped
            if time.time() - self.card_conv.get(conv, 0) > 30 * 60:
                await d["send"](rid, conv, "q:customer_card", o, closer_key=closer, **card)
                self.card_conv[conv] = time.time()
                self.sent_keys.setdefault(rid, {})["q:customer_card"] = time.time()
                self.last_rider_at.pop(conv, None)
        if not card.get("phone_known"):
            await self._fwd(rid, conv, f"ℹ rider needs the customer's number for {o.get('ref')} — not in MotionTools data, please look it up.", o=o)
        self.set(rid, "customer_wait", "", 15, conv=conv, ref=o.get("ref", ""), kind=intent)
        return "customer card sent"

    # ---- timers (once a minute from the automation loop)
    async def tick(self, now: datetime):
        d = self.deps
        t = now.timestamp()
        # riders left waiting in a forwarded conversation: nobody answered for 7 min → holding reply + urgent note, once
        for conv, since in list(self.last_rider_at.items()):
            if t - since > 3 * 3600:
                self.last_rider_at.pop(conv, None); continue
            if t - since < 7 * 60 or not self.handed_over(conv):
                continue
            rid = self.conv_rider.get(conv)
            if not rid or t - self.sent_keys.get(rid, {}).get("q:holding", 0) < 20 * 60:
                continue
            try:
                rw = d.get("rider_waiting")
                if rw and not await rw(conv, since):
                    # a teammate answered, closed or snoozed the conversation — the rider is not waiting, stay quiet
                    self.last_rider_at.pop(conv, None); continue
                if not rw and await self.person_engaged(conv) and t - self.handover.get(conv, t) > self.GRACE_MIN * 60:
                    hr = d.get("human_replied_since")
                    if hr and await hr(conv, since):
                        self.last_rider_at.pop(conv, None); continue
                o = d["order_for"](rid)
                await self._send(rid, conv, "q:holding", o)
                await d["forward"](rid, conv, f"⏰ rider waiting {round((t - since) / 60)} min without an answer — please reply now.", urgent=True, o=o)
            except Exception:
                pass
        for rid, st in list(self.state.items()):
            o = d["order_for"](rid)
            flow, until, conv = st.get("flow"), st.get("until") or 0, st.get("conv", "")
            if t - st.get("since", t) > 2 * 3600:
                self.clear(rid); continue
            if flow == "not_ready":
                if o is None or o.get("picked_up_at") or o.get("phase") not in ("at_restaurant", "to_restaurant", "accepted"):
                    self.clear(rid); continue
                if self._wait_min(o) >= 20 and not st.get("decided"):
                    await self._tick_fwd(rid, conv, st, f"⏱ {o.get('ref')}: rider waiting {self._wait_min(o)} min at {o.get('restaurant')} — decide: keep waiting or reassign.\nLast from rider: {st.get('last', '')}", o=o)
                    st["decided"] = True; self._save(); continue
                if st.get("step") == "persuade" and until and t >= until:
                    await self._tick_fwd(rid, conv, st, f"⚠ no answer to 'can you wait 10 more minutes' — rider may hand back {o.get('ref')} ({self._wait_min(o)} min at {o.get('restaurant')}).", urgent=True, o=o)
                    self.clear(rid); continue
                if st.get("step") == "wait" and until and t >= until:
                    if not st.get("asked"):
                        await self._tick_send(rid, conv, st, "q:not_ready_check", o)
                        st["asked"], st["until"] = 1, t + 5 * 60; self._save()
                    else:
                        await self._tick_fwd(rid, conv, st, f"⏱ {o.get('ref')}: no answer after the check, {self._wait_min(o)} min at {o.get('restaurant')}.", o=o)
                        self.clear(rid)
            elif flow in ("await_photo", "await_reason") and until and t >= until:
                what = "photo" if flow == "await_photo" else "reason"
                kind = st.get("kind") or "cannot deliver"
                await self._tick_fwd(rid, conv, st, f"⏳ no {what} received for '{kind}' — please follow up.", o=o)
                self.clear(rid)
            elif flow == "customer_wait":
                if o is None or o.get("phase") in ("delivered", "closed", "cancelled"):
                    self.clear(rid); continue
                if until and t >= until:
                    await self._tick_fwd(rid, conv, st, f"⏱ {o.get('ref')}: 15 min since the customer card, no news from the rider.", o=o)
                    self.clear(rid)
            elif flow in ("await_problem", "other_wait") and until and t >= until:
                self.clear(rid)

    # ---- timer actions: never into a conversation a teammate already closed / answered
    async def _closed_by_team(self, rid, conv, st) -> bool:
        rw = self.deps.get("rider_waiting")
        if not rw or not conv:
            return False
        try:
            if not await rw(conv, st.get("since", time.time())):
                self.clear(rid)
                return True
        except Exception:
            pass
        return False

    async def _tick_fwd(self, rid, conv, st, note, **kw):
        if not await self._closed_by_team(rid, conv, st):
            await self._fwd(rid, conv, note, **kw)

    async def _tick_send(self, rid, conv, st, key, o, **kw):
        if not await self._closed_by_team(rid, conv, st):
            await self._send(rid, conv, key, o, **kw)

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
