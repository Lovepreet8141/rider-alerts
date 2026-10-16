"""
Rider queries — what the bot does when a rider writes to us in Intercom (v9: the Quickzi training pack).

How it works
  1. A rider's messages that arrive within a few seconds are read together (a burst): "Tkbt6q", "Hlo",
     "Brother, he is not giving the code" is ONE question.
  2. Understanding: Claude Haiku (brain.py) reads the burst + the order data and names the situation; keyword rules
     are the fallback when the AI is off, unsure or unreachable. Workflow buttons are taken as they are.
  3. The answer is always one of the approved texts below (Intercom page → texts, editable), translated into the
     rider's language. The AI never writes its own answers, never cancels, never sends customer data.
  4. What the bot can't solve goes to a teammate with a one-line English summary and the order context, and the
     rider is told a teammate has it.

Rules from the training pack
  * Customer phone: first the call button in the app, then "check your own number in the app profile". Only when
    calling through the app does not work, the bot sends the customer's number, street + house number and the
    customer's delivery notes (never the customer's name) — and only to
    the rider the order is assigned to. A customer who simply doesn't answer: keep calling for 10 minutes, then a
    teammate takes over.
  * Restaurant late: ask how many more minutes; up to 10 more → please wait; more, or 20 minutes waited in total →
    a teammate takes over. The bot never says "the order will be cancelled".
  * Waiting messages ("?", "hello", "reply me bro") get one short answer, not one per message.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# ----------------------------------------------------------------------------- texts (editable on the Intercom page)
# key, label, default text — written in simple English; the bot translates them into the rider's language.
QUERIES = [
    ("q:not_ready", "Food not ready → ask how many more minutes",
     "Got it, the food isn't ready yet. How many more minutes does the restaurant say it needs?"),
    ("q:not_ready_wait", "Restaurant needs 10 minutes or less → please wait",
     "OK, {n} more minutes. Please wait, we're keeping an eye on it."),
    ("q:not_ready_check", "Waiting time is over → is it ready now?",
     "Is the food ready now? Reply YES, or tell me how many more minutes the restaurant needs."),
    ("q:handover_restaurant", "Long wait / rider wants to cancel → a teammate decides",
     "Thanks for waiting. A teammate is checking it now and will tell you what to do. Please stay at the restaurant for a moment."),
    ("q:no_order", "Order not at the restaurant / another rider took it",
     "Thanks, understood: the restaurant doesn't have this order for you. A teammate is checking it now and will free you from it. Please wait a moment."),
    ("q:item_missing", "Item not available at the restaurant",
     "Thanks for telling us. Please wait at the restaurant, a teammate is checking with the restaurant what happens with the order now."),
    ("q:closed", "Restaurant closed → photo, a teammate checks",
     "Thanks. Please send us a photo of the entrance, with the opening hours if you can see them. A teammate is checking it now."),
    ("q:cancel_reason", "Rider wants to cancel / remove the order → ask the reason first",
     "Okay. Why do you want to cancel this order? For example: food not ready, restaurant closed, customer not answering, or a problem with your bike or car."),
    ("q:no_new_orders", "Rider gets no new orders → check online / zone, a teammate checks the account",
     "Thanks for telling us. Please check that you are online in the app and inside your delivery zone. A teammate is checking your account now."),
    ("q:compensation", "Rider asks for compensation / extra pay (detour, extra km, long wait)",
     "Thanks for letting us know. A teammate will check it and get back to you here."),
    ("q:photo_what", "Photo without text → what is it about? (order not known yet)",
     "Thanks for the photo. What is it about? Please tell us in one sentence, plus the order number."),
    ("q:photo_what_known", "Photo without text → what is it about? (we already know his order)",
     "Thanks for the photo. What is it about? Please tell us in one sentence."),
    ("q:cant_go_online", "Rider can't go online in the app → screenshot, a teammate checks",
     "Thanks for telling us. Please send a screenshot of what the app shows when you try to go online. A teammate will check your account right away."),
    ("q:returned_photo", "Rider gave the order back to the restaurant → photo first",
     "Thanks for telling us. Please send a photo of the order at the restaurant, so we can see it was handed back. Then a teammate takes care of it."),
    ("q:returned_thanks", "Photo of the returned order received",
     "Thanks, got the photo. A teammate is checking it now and will confirm here."),
    ("q:remove_order", "Reason given → a teammate decides about the order",
     "Thanks. A teammate is checking order {ref} now and will tell you what to do. Please wait a moment."),
    ("q:phone_app", "Rider asks for the customer's number → call from the app",
     "Please call the customer with the call button on the customer stop in your app. If the call doesn't go through, check that your own phone number in the app profile is correct, then try again."),
    ("q:phone_number", "Calling through the app doesn't work → number, house number and notes (only to the assigned rider)",
     "Customer's number: {phone}\nAddress: {address}\nNotes: {notes}\nCall, ring the bell, ask the neighbours. Wait there 10 min.\n⚠️ Don't mark it delivered without handing the food to the customer. No answer? Write us here."),
    ("q:phone_number_find", "Rider can't FIND the customer → number + how to find him (only to the assigned rider)",
     "Customer's number: {phone}\nAddress: {address}\nNotes: {notes}\nCheck all entrances and the names on the doorbells, ask the neighbours. Call and wait there 10 min.\n⚠️ Don't mark it delivered without handing the food to the customer. Can't find them? Write us here."),
    ("q:keep_trying", "Number already sent, rider writes again → keep trying for the minutes left (never the number twice)",
     "Please keep trying {left} more min: call, ring the bell, ask the neighbours.\n⚠️ Don't mark it delivered without handing the food over. No answer by then? A teammate will tell you what to do."),
    ("q:keep_trying_find", "Can't find the customer, number already sent → keep looking for the minutes left",
     "Please keep looking {left} more min: all entrances, the doorbell names, the neighbours, and keep calling.\n⚠️ Don't mark it delivered without handing the food over. Still not found? A teammate will tell you what to do."),
    ("q:proof_photo", "Order marked delivered after we sent the customer's number → ask for a handover photo (off by default)",
     "We see order {ref} is marked as delivered. Please send us a photo of the food handover to the customer now."),
    ("q:proof_thanks", "Handover photo received",
     "Thanks, got the photo."),
    ("q:unreachable", "Customer doesn't answer / open → keep calling 10 minutes",
     "Thanks for trying. Please keep calling from the app and wait at the door for up to 10 minutes. If there's still no answer, write here how long you waited and we'll tell you what to do."),
    ("q:find_address", "Rider can't find the address",
     "Thanks. Please check the delivery notes and the map pin in your app, check the entrance and the building, look for the customer's name from your app on the doorbells and ask the neighbours if you can. Call the customer from the app. Don't mark the order as delivered without handing it over — write here if you can't find the customer."),
    ("q:liefercode", "Customer doesn't give the Liefercode",
     "Please ask the customer to open the Lieferando app or the order email, the code is shown there. If they still can't give it, send us a photo of the handover and the order number."),
    ("q:forgot_finish", "Forgot to finish the order in the app",
     "No problem. Send the order number, a photo of the delivered order and the Liefercode if you have it. We'll finish it, you can take your next order."),
    ("q:forgot_done", "…but the order is already finished on our side",
     "This order is already finished on our side, all good. You can take your next order."),
    ("q:photo_thanks", "Photo received for a finish / Liefercode case",
     "Thanks, got the photo. A teammate is finishing it now, you can take your next order."),
    ("q:customer_refuses", "Customer refuses the order",
     "Please don't leave the food yet and stay with the customer for a moment. A teammate is checking it now and will tell you what to do in a few minutes."),
    ("q:damaged", "Order damaged → photo",
     "Please send us a photo of the order. A teammate will reply in a few minutes."),
    ("q:cant_deliver", "Rider can't continue → ask the reason first",
     "Okay, thanks for telling us. What's the reason? For example: food not ready, restaurant closed, customer not answering, or a problem with your bike or car."),
    ("q:urgent", "Accident, injury, police, danger",
     "If anyone is hurt or in danger, call 112 now. A teammate is taking over your chat right now."),
    ("q:combine", "Rider has two orders at the same restaurant and wants both → a teammate decides",
     "Thanks. A teammate is checking if you can take both orders and will reply here."),
    ("q:handover", "Anything a teammate has to decide",
     "A teammate has your case and will reply here in a few minutes. Please stay where you are."),
    ("q:holding", "Rider is waiting and writes '?', 'hello' … (once every few minutes)",
     "Still with you, a teammate is on it. Thanks for waiting."),
    ("q:other", "Greeting / 'Other' button / not clear → how can I help?",
     "Hi! How can I help you? Please tell us in one sentence what happened, plus the order number."),
    ("q:other_known", "Greeting / 'Other' button → how can I help? (we already know his order — no order number asked)",
     "Hi! How can I help you? Please tell us in one sentence what happened."),
    ("q:ask_order", "Rider we can't identify yet (no MotionTools link) → how can I help + order number",
     "Hi! How can I help you? Please send the order number (for example 7DDP8F) and tell us what happened."),
    ("q:order_ref", "Rider sends only an order number",
     "Thanks, I found order {ref} ({status}). What's the problem?"),
    ("q:ack", "Rider confirms an automatic message (ok / arrived / on the way)",
     "Thanks!"),
    ("q:noted", "Rider explains a delay (traffic, lost, …)",
     "Thanks, noted."),
]
QUERY_DEFAULT = {k: t for k, _, t in QUERIES}
TEXTS_VERSION = "9"          # app.py drops texts customised for the old flows (v8) once, so these become active

# ----------------------------------------------------------------------------- classification (keywords: the fallback)
# ----------------------------------------------------------------------------- classification
YES = ("ok", "okay", "oke", "ja", "yes", "yeah", "yep", "sure", "fine", "alles klar", "klar", "gut", "passt", "mach ich", "will do",
       "theek", "thik", "haan", "han", "ha ji", "نعم", "تمام", "حسنا", "اوك", "evet", "tamam", "👍", "👌", "✅")
NO = ("nein", "no ", "nope", "nicht warten", "not wait", "can't wait", "cant wait", "cannot wait", "kann nicht warten", "cancel", "stornier",
      "zurückgeben", "zurueckgeben", "hand back", "give back", "leave", "gehe jetzt", "i go", "ich gehe", "nahi", "nai", "لا ", "لا أستطيع", "hayır", "hayir", "yok")
URGENT = ("unfall", "accident", "verletzt", "injur", "hurt", "polizei", "police", "crash", "ambulance", "krankenwagen", "hospital", "krankenhaus")
CLOSED = ("لا يوجد احد في المطعم", "لا يوجد أحد في المطعم", "nobody at the restaurant", "no one at the restaurant", "niemand im restaurant", "geschlossen", "closed", "zugemacht", "macht zu", "locked", "nobody there", "niemand da", "niemand hier", "not open", "nicht offen", "nicht geöffnet", "shut",
          "مغلق", "kapalı", "kapali", "band hai", "closed restaurant", "restaurant closed")
NO_ORDER = ("does not have this order", "doesn't have this order", "does not have the order", "doesn't have the order", "not have this order",
            "hat diese bestellung nicht", "hat die bestellung nicht", "hat keine bestellung", "kennt die bestellung nicht", "ليس لديهم الطلب", "ما عندهم الطلب",
            "no order", "no such", "kein auftrag", "keine bestellung", "nicht da", "already taken", "already picked", "schon abgeholt", "schon weg",
            "another driver", "anderer fahrer", "andere fahrer", "wrong order", "falsche bestellung", "don't have", "dont have", "haben nicht", "haben keine",
            "not found", "nicht gefunden", "no booking", "they say no", "not giving me this order", "not giving me the order", "not giving the order",
            "won't give", "not in restaurant", "not in the restaurant", "another person took", "other person took", "someone else took", "took this order",
            "took the order", "already took")
NOT_READY = ("مش جاهز", "مو جاهز", "ما جاهز", "ما خلص", "لسا", "لسه", "not ready", "nicht fertig", "noch nicht", "not prepared", "preparing", "still cooking", "being prepared", "wird noch", "dauert", "takes time",
             "will take", "take time", "not done", "kitchen", "küche", "kueche", "wait", "warte", "warten", "late", "spät", "spaet", "ready nahi",
             "hazır değil", "hazir degil", "غير جاهز", "ليس جاهز", "لم يجهز", "order is not", "bestellung ist nicht", "still waiting", "noch warten",
             "already been", "its been", "it's been", "telling 10", "telling 5", "they said 10", "said 10 min", "10 more min", "sagen 10", "schon 10 min", "schon 20 min")
READY = ("ready now", "jetzt fertig", "ist fertig", "is ready", "order ready", "bestellung fertig", "picked up", "abgeholt", "habs", "hab es", "got it", "have it", "got the order", "habe die bestellung", "on my way now", " ready", " fertig")
NOT_READY_GUARD = ("not ready", "nicht fertig", "isn't ready", "isnt ready", "no ready", "not yet ready", "noch nicht fertig", "ready nahi")
FORGOT_CTX = ("order", "bestellung", "auftrag", "app", "finish", "complete", "abschließ", "abschliess", "finaliz", "deliver", "liefer", "mark", "photo", "foto", "bild", "upload", "الطلب", "الطلبية", "التطبيق", "إكمال", "اكمال", "إنهاء", "انهاء", "sipariş", "uygulama")
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
           "does not respond", "doesn't respond", "not respond", "no response", "did not answer", "didn't answer", "not answered", "nobody answered", "no one answered", "did not respond", "didn't respond", "not picking up", "not on site", "not at home", "not home", "not there", "nobody home", "no one home",
           "nobody there", "no one there", "kunde nicht da", "customer not there", "التواصل", "تواصل", "الاتصال", "اتصل", "اتصال", "يرد", "يجاوب", "يرد على", "communicat", "not reachable",
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
REFUSED = ("didn't order", "did not order", "didnt order", "never ordered", "not ordered", "nicht bestellt", "nichts bestellt", "keine bestellung aufgegeben",
           "refuse", "verweiger", "ablehn", "doesn't accept", "does not accept", "won't accept", "nicht annehmen", "nimmt nicht an",
           "will die bestellung nicht", "doesn't want the order", "does not want the order", "لم يطلب", "ما طلب", "مو طالب", "رفض", "sipariş vermedim",
           "sipariş vermemiş", "kabul etmiyor")
ACK = ("arrived", "angekommen", "at location", "im here", "i'm here", "i am here", "bin da", "bin hier", "on the way", "on my way", "unterwegs", "coming",
       "komme", "moving", "fahre", "fahre los", "thanks", "thank you", "danke", "done", "delivered", "geliefert", "ausgeliefert", "started", "gestartet",
       "all good", "alles gut", "no problem", "kein problem", "im moving", "i'm moving", "going", "jetzt")
EXCUSE = ("traffic", "verkehr", "stau", "verfahren", "lost", "wrong way", "falsch gefahren", "rain", "regen", "umleitung", "detour", "road closed", "straße gesperrt",
          "red light", "ampel", "slow", "langsam")
COMBINE = ("zusammenfassen", "zusammenlegen", "zusammen legen", "beide bestellungen", "beide aufträge", "zwei bestellungen", "zwei aufträge",
           "both orders", "two orders", "combine", "merge the orders", "both the orders", "طلبين", "الطلبين", "iki sipariş")
HELLO = ("hello", "hallo", "hi", "hey", "salam", "selam", "good evening", "guten abend", "guten tag", "help", "hilfe", "problem", "question", "frage", "مرحبا", "السلام")



CALL_FAILED = ("can't call", "cannot call", "cant call", "can not call", "call button", "no call button", "call is not going", "call not going",
               "call doesn't work", "call does not work", "call not working", "calling not working", "call failed", "call fails", "call drops",
               "call cut", "app call", "call from the app", "call in the app", "anruf geht nicht", "kann nicht anrufen", "kann ihn nicht anrufen",
               "kann sie nicht anrufen", "anrufen geht nicht", "anruf funktioniert nicht", "anruftaste", "call nahi ho", "call nahi lag", "call nhi",
               "لا أستطيع الاتصال", "ما بقدر اتصل", "ما عم يزبط الاتصال", "الاتصال لا يعمل", "arayamıyorum", "arama olmuyor")
LIEFERCODE = ("liefercode", "liefer code", "delivery code", "the code", "code", "kodu", "كود", "الرمز", "رمز", "pin", "pincode", "pin code")
CANT_WAIT = ("can't wait", "cant wait", "cannot wait", "can not wait", "kann nicht warten", "kann nicht mehr warten", "no more waiting", "too long", "zu lange",
             "long wait", "waiting too long", "warte schon", "ما بقدر انتظر", "لا أستطيع الانتظار", "intezaar nahi", "wait nahi")
CANCEL = ("cancel", "stornier", "الغاء", "إلغاء", "الغي", "iptal")
LIEFER_CTX = ("give", "giving", "gibt", "sagt", "tell", "wrong", "falsch", "not", "nicht", "no ", "doesn't", "won't", "kein", "nahi", "لا", "ما ",
              "missing", "fehlt", "customer", "kunde", "he ", "she ", "er ", "sie ")
ITEM_MISSING = ("not available", "no more", "sold out", "out of stock", "ausverkauft", "nicht verfügbar", "nicht verfuegbar", "nicht mehr da",
                "haben kein", "haben keine", "missing item", "item missing", "fehlt ein", "fehlt die", "fehlen", "غير متوفر", "خلص", "kalmadı", "yok artık")
BUTTONS = {   # workflow buttons: the label arrives as the rider's message
    "food not ready / restaurant late": "not_ready", "food not ready": "not_ready", "restaurant late": "not_ready",
    "order not at the restaurant": "no_order", "item missing": "item_missing", "customer not answering": "customer_unreachable",
    "i need to call the customer": "customer_phone", "liefercode problem": "liefercode", "i forgot to finish the order": "forgot_finish",
    "i forgot to finish the order on the app": "forgot_finish", "customer refuses the order": "customer_problem",
    "customer doesn't accept the order": "customer_problem", "other": "other", "i can't do the delivery": "cant_deliver",
    "order is damaged": "damaged", "restaurant closed": "closed",
    "i cannot make the delivery": "cant_deliver", "i can't make the delivery": "cant_deliver",
    "i cannot communicate with the client": "customer_unreachable", "i can't communicate with the client": "customer_unreachable",
    # the "Quickzii (Copy)" workflow buttons, exactly as riders see them
    "i can't go online": "cant_go_online", "the order is not in restaurant": "no_order", "order is not ready yet": "not_ready",
    "i can't reach out to the customer": "customer_unreachable", "forgot to complete the order on the app": "forgot_finish",
}
NUDGE_VOCAB = {"hello", "helo", "hlo", "hallo", "hi", "hey", "bro", "brother", "bhai", "bhaiya", "sir", "boss", "reply", "me", "pls", "plz",
               "please", "bitte", "anyone", "there", "answer", "antwort", "und", "and", "jetzt", "now", "still", "waiting", "wait", "warte",
               "ok", "yes", "yaar", "habibi", "ya", "akhi", "merhaba", "abi", "selam", "salam", "update", "news", "noch", "hallooo"}

NUM_RE = re.compile(r"^\s*(\d{1,3})\s*(min|mins|minutes|minuten|m|')?\s*\.?\s*$", re.I)
MIN_IN_TEXT = re.compile(r"(\d{1,3})\s*(?:min|mins|minute|minutes|minuten|m\b|')", re.I)


def _has(low: str, words) -> bool:
    return any(w in low for w in words)


# German "zu" = closed only as "(das Restaurant) ist zu / hat zu / sind schon zu" — not "zu einer Bestellung", "ist zu spät"
_ZU_CLOSED = re.compile(r"(?<![a-zäöüß])(ist|hat|sind|haben|schon|noch|laden|restaurant|tür|tuer)\s+(schon\s+|noch\s+)?zu(?![a-zäöüß])"
                        r"(?!\s+(spät|spaet|weit|lang|lange|viel|wenig|früh|frueh|klein|groß|gross|teuer|schwer|kalt|heiß|heiss|voll|einer|einem|den|dem|der|die|das)\b)")


def _closed(low: str) -> bool:
    return _has(low, CLOSED) or bool(_ZU_CLOSED.search(low))


_WORD = re.compile(r"[a-zäöüß]+", re.I)


def _has_word(low: str, words) -> bool:
    """Short tokens (ok, ja, no, han) match whole words only — 'thanks' must not match 'han'."""
    tokens = None
    for w in words:
        w = w.strip()
        if _WORD.fullmatch(w):
            if re.search(rf"(?<![a-zäöüß]){re.escape(w)}(?![a-zäöüß])", low):
                return True
        elif re.fullmatch(r"[^\W\d_]+", w):                # one word in another script (لا, نعم): a whole word, "السلام" is no "لا"
            tokens = tokens if tokens is not None else set(re.findall(r"[^\W\d_]+", low))
            if w in tokens:
                return True
        elif w in low:
            return True
    return False


TAG_INTENTS = {
    "not_ready": "not_ready", "order_not_ready": "not_ready", "notready": "not_ready", "order not ready": "not_ready", "not ready": "not_ready",
    "closed": "closed", "restaurant_closed": "closed", "restaurant closed": "closed",
    "no_order": "no_order", "already_taken": "no_order", "no order": "no_order",
    "item_missing": "item_missing", "item missing": "item_missing",
    "cant_deliver": "cant_deliver", "cannot_deliver": "cant_deliver", "can't do the delivery": "cant_deliver", "cant deliver": "cant_deliver", "i can't do the delivery": "cant_deliver",
    "customer_unreachable": "customer_unreachable", "customer unreachable": "customer_unreachable", "cannot contact customer": "customer_unreachable",
    "i cannot get in touch with the customer": "customer_unreachable", "customer not reachable": "customer_unreachable", "cant reach customer": "customer_unreachable",
    "customer_phone": "customer_phone", "customer number": "customer_phone", "phone": "customer_phone",
    "call_failed": "call_failed", "call failed": "call_failed",
    "customer_find": "customer_find", "wrong address": "customer_find", "cant find": "customer_find",
    "liefercode": "liefercode", "delivery code": "liefercode",
    "customer_problem": "customer_problem", "customer doesn't accept the order": "customer_problem", "customer refuses": "customer_problem", "customer problem": "customer_problem",
    "forgot_finish": "forgot_finish", "forgot": "forgot_finish", "i forgot to finish the order on the app": "forgot_finish", "forgot to complete": "forgot_finish",
    "damaged": "damaged", "order_damaged": "damaged", "order is damaged": "damaged",
    "other": "other", "returned": "returned", "order_returned": "returned", "cant_go_online": "cant_go_online", "offline": "cant_go_online",
    "no_new_orders": "no_new_orders", "compensation": "compensation", "wrong_number": "wrong_number",
}


NO_NEW = ("not receiving any order", "not receiving orders", "not receiving order", "no orders coming", "not getting any order",
          "not getting orders", "don't get any order", "dont get any order", "don't get orders", "dont get orders", "i get no orders",
          "no new orders", "why no orders", "why am i not getting", "keine aufträge", "keine auftraege", "bekomme keine aufträge",
          "bekomme keine bestellungen", "kriege keine aufträge", "kriege keine bestellungen", "keine neuen bestellungen",
          "ما عم يجيني طلبات", "ما عم يجيني ولا طلب", "ما في طلبات", "لا تصلني طلبات", "لا يصلني طلبات", "لا أستلم طلبات",
          "ما يجيني طلب", "sipariş gelmiyor", "siparis gelmiyor", "order nahi aa raha", "order nahi aa rahe", "order nhi aa raha")
WRONG_NUMBER = ("wrong number", "number is wrong", "number wrong", "number is not correct", "not the right number", "number doesn't exist",
                "number does not exist", "not in service", "out of service", "someone else answers", "another person answers",
                "falsche nummer", "nummer ist falsch", "nummer existiert nicht", "nicht vergeben", "kein anschluss", "nummer stimmt nicht",
                "الرقم غلط", "الرقم خطأ", "الرقم خاطئ", "الرقم غير صحيح", "الرقم مو صحيح", "الرقم غير موجود", "numara yanlış", "numara yanlis",
                "galat number", "number galat")
COMPENSATION = ("compensation", "compensate", "kompensation", "entschädigung", "entschaedigung", "vergütung", "verguetung",
                "extra km", "extra kilometer", "km more", "more km", "kilometers more", "more kilometers", "mehr kilometer",
                "zusätzliche kilometer", "extra pay", "extra money", "pay me", "bezahlung", "extra geld", "تعويض", "مسافة إضافية",
                "كيلومتر إضافي", "ekstra ödeme", "ek ödeme", "tazminat", "extra paise")
ROAD = ("road", "street", "straße", "strasse", "weg", "route", "bridge", "brücke", "bruecke", "gesperrt", "festival", "baustelle",
        "construction", "الطريق", "الشارع", "yol", "sokak")
OFFLINE = ("can't go online", "cant go online", "cannot go online", "can not go online", "not going online", "unable to go online",
           "can't get online", "cannot get online", "can't log in", "cant log in", "cannot log in", "can't login", "cannot login", "app not working",
           "kann nicht online", "nicht online gehen", "komme nicht online", "kann mich nicht einloggen", "einloggen geht nicht", "login geht nicht",
           "لا أستطيع الدخول", "ما بقدر ادخل", "ما عم يفتح التطبيق", "لا أستطيع أن أكون متصل", "لا أستطيع الاتصال بالإنترنت", "اونلاين",
           "online olamıyorum", "online olamiyorum", "giriş yapamıyorum", "online nahi ho", "online nhi ho")
RETURNED = ("returned the order", "returned it", "returned to the restaurant", "return the order", "gave it back", "given it back", "gave back",
            "given back", "brought it back", "brought back", "back to the restaurant", "zurückgegeben", "zurueckgegeben", "zurück gebracht",
            "zurückgebracht", "zurueckgebracht", "zurück ins restaurant", "zurück zum restaurant", "abgegeben im restaurant",
            "رجعت الطلب", "رجعت الطلبية", "ارجعت الطلب", "أرجعت الطلب", "رجعته للمطعم", "رجعتها للمطعم", "سلمته للمطعم",
            "geri verdim", "geri götürdüm", "iade ettim", "wapas kar diya", "wapas de diya", "wapis kar diya", "wapis de diya")
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


def button_intent(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("‘", "'").strip().lower()).rstrip(".!")
    return BUTTONS.get(t, "")


NUDGE_PHRASES = ("is anyone there", "anyone there", "anyone here", "is someone there", "jemand da", "ist jemand da", "hallo?", "reply me",
                 "هل يوجد احد", "هل يوجد أحد", "حدا هون", "في حدا", "فيه احد", "يا جماعة", "kimse var mı", "koi hai")


def _squash(word: str) -> str:
    return re.sub(r"(.)\1+", r"\1", word)


GREET_WORDS = {"hi", "hii", "hello", "helo", "hlo", "hallo", "hey", "salam", "salaam", "salem", "assalam", "assalamu", "alaikum", "aleikum",
               "alaykum", "marhaba", "merhaba", "selam", "namaste", "sat", "sri", "akal", "good", "morning", "evening", "afternoon", "guten",
               "tag", "morgen", "abend", "moin", "servus", "how", "are", "you", "u", "r", "sir", "bro", "brother", "bhai", "boss", "team",
               "support", "please", "pls", "plz", "i", "need", "help", "hilfe", "kaise", "ho", "kya", "haal", "hai", "ya", "habibi", "akhi",
               "abi", "yardım", "lütfen", "مرحبا", "السلام", "عليكم", "اهلا", "أهلا", "مساء", "صباح", "الخير", "مساعدة", "ساعدني"}


def is_greeting(text: str) -> bool:
    """'Hi', 'Salam alaikum brother', 'Hello, I need help' — an opener, nothing to act on yet."""
    t = (text or "").strip().lower()
    if not t or is_nudge(t):
        return bool(t)
    words = re.findall(r"[^\W\d_]+", t)
    return 0 < len(words) <= 8 and all(_squash(w) in {_squash(g) for g in GREET_WORDS} or w in GREET_WORDS for w in words)


def is_nudge(text: str) -> bool:
    """'?', '??', 'Hlooo', 'Reply me bro', 'Brother' … — the rider is waiting for an answer, nothing new."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if re.fullmatch(r"[\s?!.¿؟…]+", t):
        return True
    if any(ph in t for ph in NUDGE_PHRASES) and len(t.split()) <= 5 and not any(w in t for w in ("restaurant", "مطعم", "المطعم", "kunde", "customer", "العميل", "الزبون")):
        return True
    words = re.findall(r"[a-zäöüß]+", t)
    if not words or len(words) > 4 or re.search(r"\d", t):
        return False
    vocab = {_squash(w) for w in NUDGE_VOCAB}
    return all(_squash(w) in vocab for w in words) and not (len(words) == 1 and words[0] in ("ok", "yes"))


def classify(text: str, has_photo: bool = False, tags=None) -> str:
    tagged = intent_from_tags(tags) or button_intent(text)
    if tagged:
        return tagged
    low = f" {(text or '').strip().lower()} "
    if has_photo and len(low.strip()) < 3:
        return "photo"
    if _has(low, URGENT):
        return "urgent"
    if NUM_RE.match(text or ""):
        return "minutes"
    if is_nudge(text):
        return "nudge"
    if _has(low, CALL_FAILED):
        return "call_failed"
    if _has(low, CANT_WAIT):
        return "not_ready"                                   # "I can't wait" at the restaurant = food late, wants out
    if _has(low, WRONG_NUMBER):
        return "wrong_number"                               # the number we sent doesn't work — calling it again won't help
    if _has(low, COMPENSATION):
        return "compensation"                               # "road closed, 2.5 km more, please compensate" — pay, not a closed restaurant
    if _has(low, NO_NEW):
        return "no_new_orders"                              # "why am I not receiving any orders?" — account / zone, not a missing order
    if _has(low, OFFLINE):
        return "cant_go_online"                             # an app / account problem, not a delivery
    if _has(low, RETURNED):
        return "returned"                                   # "I have returned the order. Please cancel" → photo first
    if _has(low, REMOVE):
        return "remove_order"
    if _has(low, ITEM_MISSING) and not _has(low, CUSTOMER):
        return "item_missing"
    if _has(low, NO_ORDER) and not (_has(low, CUSTOMER) and _has(low, UNREACH)) and not _has(low, PHONE):
        return "no_order"
    if _has(low, COMBINE):
        return "combine"                                    # "two orders at the same restaurant — please combine them"
    if _closed(low) and not _has(low, ("door", "tür", "tuer")) and not _has_word(low, ROAD):
        return "closed"
    if _has(low, READY) and not _has(low, NOT_READY_GUARD):
        return "ready"
    if _has(low, FORGOT) and _has(low, FORGOT_CTX):
        return "forgot_finish"
    if _has_word(low, LIEFERCODE) and (_has(low, LIEFER_CTX) or "liefer" in low) \
            and not _has(low, ("map pin", "pin on the map", "location pin", "pin location", "standort", "karte")):
        return "liefercode"
    if _has(low, DAMAGED) and not _has_word(low, VEHICLE):
        return "damaged"                                # "my bike is broken" is the rider's vehicle, not the food
    if _has(low, REFUSED) and not _has(low, ("restaurant", "مطعم", "المطعم")):
        return "customer_problem"                       # "I called, he says he ordered nothing" — not a phone question
    if _has(low, FIND) and not _has(low, ("restaurant",)):
        return "customer_find"
    if _has(low, UNREACH) and not _has(low, ("restaurant", "staff", "personal")):
        return "customer_unreachable"
    if _has(low, PHONE) and (_has(low, CUSTOMER) or not _has(low, ("restaurant",))):
        return "customer_phone"
    if _has(low, CANT):
        return "cant_deliver"
    if _has_word(low, ("traffic", "verkehr", "stau", "زحمة", "trafik")):
        return "excuse"                                     # "takes half an hour because of heavy traffic" = delay on the road
    if _has(low, NOT_READY) and not _has(low, CUSTOMER) and not _has_word(low, ("no", "nein", "nope")):
        return "not_ready"
    if _has(low, CANCEL) and not _has(low, CUSTOMER):
        return "remove_order"                               # inside a "food not ready" chat this means: he wants out
    if _has_word(low, NO) and len(low.strip()) <= 40 and not _has(low, CUSTOMER) and not _has(low, ("restaurant", "مطعم", "المطعم")):
        return "no"
    if _has(low, NOT_READY) and not _has(low, CUSTOMER):
        return "not_ready"
    if _has(low, CUSTOMER):
        return "customer_problem"
    if _has_word(low, YES) and len(low.strip()) <= 30:
        return "yes"
    if _has_word(low, EXCUSE):                                # whole words: "stau" must not match "restaurant"
        return "excuse"
    if _has_word(low, ACK):
        return "ack"
    if has_photo:
        return "photo"
    return "other"


WEAK = ("other", "nudge", "ack", "yes", "no", "excuse")
NUMBER_INTENTS = ("call_failed", "customer_phone", "customer_unreachable")      # (once per chat — after that: keep trying)
SOLVABLE = ("returned", "not_ready", "closed", "no_order", "item_missing", "customer_unreachable", "customer_find", "call_failed", "customer_phone",
            "liefercode", "customer_problem", "damaged", "forgot_finish")       # a reason the bot has its own steps for


PRIORITY = ("urgent", "wrong_number", "call_failed", "returned", "cant_go_online", "no_order", "item_missing", "liefercode", "forgot_finish", "customer_find", "customer_unreachable",
            "customer_phone", "closed", "damaged", "customer_problem", "not_ready", "remove_order", "cant_deliver")


def classify_burst(msgs: list, has_photo: bool = False, tags=None) -> str:
    """Keyword view of several messages: the most specific situation named anywhere wins ("please cancel … another
    person took this order … waiting 20 min" = order taken by someone else), else the newest message."""
    tagged = intent_from_tags(tags)
    if tagged:
        return tagged
    seen = [classify(m, has_photo and i == len(msgs) - 1) for i, m in enumerate(msgs)]
    for p in PRIORITY:
        if p in seen:
            return p
    for i in reversed(seen):
        if i not in WEAK:
            return i
    if seen and all(i == "nudge" for i in seen):
        return "nudge"
    return seen[-1] if seen else "other"


INTENT_LABEL = {"nudge": "waiting for an answer ('?')", "not_ready": "food not ready", "closed": "restaurant closed", "no_order": "restaurant: no such order / already taken",
                "item_missing": "item not available", "cant_deliver": "can't do the delivery", "customer_unreachable": "customer doesn't answer",
                "customer_find": "can't find the address", "customer_phone": "asked for the customer's number", "call_failed": "can't call through the app",
                "liefercode": "Liefercode problem", "customer_problem": "customer refuses / problem", "forgot_finish": "forgot to finish in the app",
                "damaged": "order damaged", "combine": "wants two orders combined", "other": "unclear", "ack": "confirmed", "excuse": "delay explained", "urgent": "URGENT", "photo": "photo",
                "yes": "yes", "no": "no", "minutes": "minutes", "ready": "ready", "order_ref": "order number only", "remove_order": "wants the order removed",
                "returned": "gave the order back to the restaurant", "cant_go_online": "can't go online in the app",
                "no_new_orders": "gets no new orders", "compensation": "asks for compensation / extra pay",
                "wrong_number": "the customer's number is wrong / not in service"}


# ----------------------------------------------------------------------------- the flow engine
HANDOVER_KEYS = ("q:combine", "q:no_new_orders", "q:compensation", "q:handover", "q:handover_restaurant", "q:holding", "q:no_order", "q:item_missing", "q:remove_order", "q:urgent",
                 "q:customer_refuses", "q:photo_thanks", "q:not_ready_check")


class RiderFlows:
    """One small state per rider: which situation we are in and what we are waiting for.

    deps (set by app.py):
      order_for(rid)                -> the rider's live / just-finished order or None
      order_by_ref(text)            -> an order named in the text (known today) or None
      understand(msgs, ctx, asked)  -> AI reading of the burst ({} when off / unsure)            (brain.understand)
      send(rid, conv, key, o, lang=…, **fmt) -> sends the approved text (translated) into the conversation, logs it
      forward(rid, conv, note, urgent=False, o=None) -> hands the conversation to a person with an internal note
      log(rid, o, key, text)        -> log line without sending
      customer_phone(o)             -> the customer's number from MotionTools data, "" if unknown
      order_ctx(o)                  -> one line about the order for the AI (status, minutes waited, restaurant — no customer data)
      riders_on(o), others(rid, o), status_of(o), settings(), rider_waiting(conv, since), human_replied_since(conv, since)
    """

    HANDOVER_H = 3                      # hours the bot keeps out of a conversation a person is answering
    GRACE_MIN = 5                       # after a forward: minutes a person gets to answer before the bot steps back in
    TEAMMATE_MIN = 15                   # a teammate wrote in the chat within these minutes → the bot doesn't answer
    HOLD_GAP = 5 * 60                   # "still with you" at most every 5 minutes per conversation
    TELL_GAP = 10 * 60                  # "a teammate has your case" at most every 10 minutes per conversation

    def __init__(self, store):
        self.store = store
        self.state: dict = {}
        self.handover: dict = {}        # conversation id -> time it was handed to a person
        self.asked_other: dict = {}     # "rider|conversation" -> time we last asked "how can I help you?"
        self.sent_keys: dict = {}       # rider id -> {query key: time sent}
        self._last: dict = {}           # rider id -> his last message(s)
        self.named_ref: dict = {}       # rider id -> (order ref he wrote himself, when)
        self.last_rider_at: dict = {}   # conversation id -> when the rider started waiting
        self.conv_rider: dict = {}      # conversation id -> rider id
        self.last_intent: dict = {}     # rider id -> what the bot finally understood
        self.last_ai: dict = {}         # rider id -> the AI's reading of his last burst
        self.last_bot: dict = {}        # conversation id -> (key, time) of our last answer — context for the AI
        self.lang: dict = {}            # rider id -> language he gets answers in
        self.told: dict = {}            # conversation id -> when the rider was last told "a teammate has it" / "still with you"
        self.number_sent: dict = {}     # "conversation|order id" -> when the customer's number went to the rider (once per chat)
        self.kept: dict = {}            # "conversation|order id" -> when we last said "keep trying"
        self.num_info: dict = {}        # "conversation|order id" -> {rid, kind (unreachable/find), proof_asked, proof_done}
        self.deps: dict = {}
        self._load()

    # ---- persistence (survives a redeploy)
    def _load(self):
        try:
            s = self.store.get_settings()
            self.state = json.loads(s.get("rider_flows") or "{}")
            self.handover = json.loads(s.get("rider_handover") or "{}")
            self.lang = json.loads(s.get("rider_lang") or "{}")
            nums = json.loads(s.get("rider_numbers") or "{}")
            if "sent" in nums and isinstance(nums.get("sent"), dict):
                self.number_sent, self.num_info = nums.get("sent") or {}, nums.get("info") or {}
            else:
                self.number_sent = {k: v for k, v in nums.items() if isinstance(v, (int, float))}   # 9.0.9 format
        except Exception:
            self.state = {}

    def _save(self):
        try:
            self.handover = {c: t for c, t in self.handover.items() if time.time() - t < self.HANDOVER_H * 3600}
            self.number_sent = {k: t for k, t in self.number_sent.items() if time.time() - t < 4 * 3600}
            self.num_info = {k: v for k, v in self.num_info.items() if k in self.number_sent}
            self.store.set_settings({"rider_flows": json.dumps(self.state)[:60000], "rider_handover": json.dumps(self.handover)[:30000],
                                     "rider_lang": json.dumps(dict(list(self.lang.items())[-3000:]))[:60000],
                                     "rider_numbers": json.dumps({"sent": self.number_sent, "info": self.num_info})[:120000]})
        except Exception:
            pass

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

    async def teammate_on_it(self, conv: str) -> bool:
        """A teammate wrote in this chat in the last TEAMMATE_MIN minutes (not via a forward of ours): it's his chat now.
        Intercom unreachable → False (the bot answers as usual)."""
        hr = self.deps.get("human_replied_since")
        if not hr or not conv:
            return False
        try:
            return bool(await hr(conv, time.time() - self.TEAMMATE_MIN * 60))
        except Exception:
            return False

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
        return self.deps["settings"]().get(f"auto:{key}", "0" if key in DEFAULT_OFF else "1") == "1"

    def _cfg(self, key: str, default: int) -> int:
        try:
            return int(self.deps["settings"]().get(key) or default)
        except Exception:
            return default

    # ---- sending
    async def _send(self, rid, conv, key, o, **fmt) -> bool:
        """Never say the same thing twice: if this answer already went to the rider in the last 30 min, he is clearly
        not helped by it — a person takes over instead of the bot repeating itself."""
        if not self.on(key):
            return False
        last = self.sent_keys.setdefault(f"{rid}|{conv}", {})
        if key in last and time.time() - last[key] < 30 * 60 and key not in HANDOVER_KEYS and key not in ("q:phone_number", "q:phone_number_find", "q:keep_trying", "q:keep_trying_find"):
            await self._fwd(rid, conv, f"🔁 rider is not helped by our automatic answer ({key[2:].replace('_', ' ')}) — please reply personally.", o=o)
            return False
        last[key] = time.time()
        await self.deps["send"](rid, conv, key, o, lang=self.lang.get(rid, ""), **fmt)
        self.last_bot[conv] = (key, time.time())
        if key in ("q:handover", "q:handover_restaurant", "q:holding", "q:urgent", "q:no_order", "q:item_missing", "q:remove_order",
                   "q:no_new_orders", "q:compensation"):
            self.told[conv] = time.time()
        if key != "q:holding":
            self.last_rider_at.pop(conv, None)          # the rider got an answer — nobody is waiting any more
        return True

    def _note(self, rid: str, head: str) -> str:
        ai = self.last_ai.get(rid) or {}
        said = self._last.get(rid, "")
        line = f"{head}"
        if ai.get("summary"):
            line += f"\n🤖 {ai['summary']}"
        if said:
            line += f"\nRider: {said[:400]}"
        return line

    async def _fwd(self, rid, conv, note, urgent=False, o=None):
        """Forward to a person and step back: from now on that person owns the conversation."""
        await self.deps["forward"](rid, conv, note, urgent=urgent, o=o)
        if conv:
            self.handover[conv] = time.time()
            self._save()

    async def _hand_over(self, rid, conv, o, head, urgent=False, key="q:handover", **fmt):
        """Tell the rider a teammate has it (not again within 10 min), then hand the conversation over with the facts."""
        if key and time.time() - self.told.get(conv, 0) > self.TELL_GAP:
            await self._send(rid, conv, key, o, **fmt)
        await self._fwd(rid, conv, self._note(rid, head), urgent=urgent, o=o)
        self.clear(rid)

    # ---- a rider wrote to us
    async def on_message(self, rid: str, text: str, conv: str, in_auto: bool = False, has_photo: bool = False, last_ops_auto: bool = False,
                         tags=None, burst=None) -> str:
        d = self.deps
        msgs = [m.strip() for m in (burst or [text]) if isinstance(m, str) and m.strip()] or [text if isinstance(text, str) else ""]
        joined = "\n".join(msgs)
        now = time.time()
        o = d["order_for"](rid)
        named = d["order_by_ref"](joined) if d.get("order_by_ref") else None
        if named is not None:
            o = named                                    # the rider named the order himself — that's the one he means
            self.named_ref[rid] = (named.get("ref"), now)
        elif o is None and rid in self.named_ref and now - self.named_ref[rid][1] < 2 * 3600 and d.get("order_by_ref"):
            o = d["order_by_ref"](self.named_ref[rid][0])   # he told us the order earlier in this chat
        st = self.state.get(rid)
        if st and st.get("conv") and conv and st["conv"] != conv and (intent_from_tags(tags) or button_intent(msgs[0])
                                                                      or all(is_greeting(m) for m in msgs)):
            self.clear(rid)                              # a new chat opened with a button / "hi": a new topic, not the old chat's
            st = None
        self._last[rid] = " / ".join(msgs)[:400]
        self.last_rider_at.setdefault(conv, now)
        self.conv_rider[conv] = rid
        if st:
            st["conv"], st["last"] = conv, msgs[-1][:120]

        # ---- the handover photo we asked for: counts even when a teammate is in the chat
        if has_photo and st and st.get("flow") == "await_photo" and st.get("kind") == "proof":
            return await self._in_flow(rid, conv, o, st, "photo", {}, joined, True)

        # ---- understanding: buttons as they are; obvious one-word cases by keywords; everything else by the AI
        kw = classify_burst(msgs, has_photo, tags)
        tagged = intent_from_tags(tags) or button_intent(msgs[-1])
        in_flow_answer = bool(st and st.get("flow") == "not_ready" and kw in ("minutes", "yes", "no", "ready"))
        obvious = bool(tagged) or kw in ("urgent", "photo", "minutes") or in_flow_answer or all(is_nudge(m) for m in msgs)
        intent, ai = (tagged or kw), {}
        if not obvious and d.get("understand"):
            try:
                key, at = self.last_bot.get(conv, ("", 0))
                asked = QUERY_DEFAULT.get(key, "") if now - at < 3600 else ""
                ai = await d["understand"](msgs, d["order_ctx"](o) if d.get("order_ctx") else "", asked) or {}
            except Exception:
                ai = {}
            if ai.get("reply_language"):
                self.lang[rid] = ai["reply_language"]
            if ai.get("order_ref") and d.get("order_by_ref") and named is None:
                hit = d["order_by_ref"](ai["order_ref"])
                if hit is not None:
                    o = named = hit
                    self.named_ref[rid] = (hit.get("ref"), now)
            if ai.get("situation") and ai.get("confidence", 0) >= 0.55:
                intent = ai["situation"]
                if intent == "other" and kw not in WEAK and ai.get("confidence", 0) < 0.8:
                    intent = kw                          # the AI shrugs, the keywords found something specific
        self.last_ai[rid] = ai
        if named is not None and intent in ("other", "nudge") and len(joined.split()) <= 3:
            intent = "order_ref"
        # a photo with "Delivered" / "code 02" (or of an order that is already delivered) = his delivery photo → "thanks"
        if has_photo and intent in ("ack", "yes", "other") and o is not None and (
                o.get("phase") in ("delivered", "closed") or any(w in (joined or "").lower() for w in DELIVERED_WORDS)):
            intent = "photo"
        self.last_intent[rid] = intent
        d["log"](rid, o, f"reply:{intent}", joined[:300] or "📷 photo")

        # ---- a teammate is already talking to this rider (he took the chat himself): the bot stays out
        if intent != "urgent" and not self.handed_over(conv) and await self.teammate_on_it(conv):
            if intent in NUMBER_INTENTS:                 # the rider is at the door: the number can't wait for a teammate
                r = await self._number_while_person(rid, conv, o)
                self.clear(rid)
                return r
            self.clear(rid)
            return "silent — a teammate is already answering this chat"

        # ---- nudges: one short answer, not one per message
        if intent == "nudge":
            recent_bot = now - self.last_bot.get(conv, ("", 0))[1] < 45 * 60
            if st or self.handed_over(conv) or recent_bot:
                if now - self.told.get(conv, 0) > self.HOLD_GAP:
                    await self._send(rid, conv, "q:holding", o)
                await self._fwd(rid, conv, self._note(rid, "⏰ rider is waiting for an answer"), urgent=True, o=o)
                return "rider waiting ('?') → holding reply + urgent forward"
            intent = "other"

        # ---- a person is on this conversation: stay out of it
        if self.handed_over(conv):
            if await self.person_engaged(conv):
                if intent == "urgent":
                    await self._fwd(rid, conv, self._note(rid, "🔴 URGENT"), urgent=True, o=o)
                    return "urgent note added (person already on it)"
                if intent in NUMBER_INTENTS:
                    return await self._number_while_person(rid, conv, o)
                self.clear(rid)
                return "silent — a person is answering this conversation"
            self.handover.pop(conv, None)               # forwarded, but nobody answered: the bot takes it back
            self._save()
            if intent in ("other", "no", "yes", "ack"):
                if now - self.told.get(conv, 0) > self.HOLD_GAP:
                    await self._send(rid, conv, "q:holding", o)
                waited = round((now - self.last_rider_at.get(conv, now)) / 60)
                await self._fwd(rid, conv, self._note(rid, f"⏰ rider still waiting for an answer{f' ({waited} min)' if waited else ''} — please reply"), urgent=True, o=o)
                return "holding reply + urgent re-forward"

        if intent == "urgent":
            await self._hand_over(rid, conv, o, "🔴 URGENT", urgent=True, key="q:urgent")
            return "urgent: told to call 112 if needed + forwarded"

        # ---- answers inside a running situation
        if st:
            r = await self._in_flow(rid, conv, o, st, intent, ai, joined, has_photo)
            if r:
                return r

        # ---- a new situation
        return await self._new(rid, conv, o, intent, ai, joined, has_photo, in_auto, last_ops_auto)

    async def _in_flow(self, rid, conv, o, st, intent, ai, text, has_photo):
        flow, step = st.get("flow"), st.get("step")
        if flow == "not_ready":
            last = text.split("\n")[-1]
            if intent == "minutes" or (intent == "not_ready" and ai.get("minutes_needed") is not None):
                m = NUM_RE.match(last)
                n = int(m.group(1)) if (intent == "minutes" and m) else ai.get("minutes_needed")
                return await self._restaurant(rid, conv, o, n, ai)
            if intent == "ready":
                self.clear(rid)
                if step in ("wait", "check"):
                    await self._send(rid, conv, "q:ack", o)
                return "food ready — flow closed"
            if intent in ("yes", "ack") and step in ("wait", "check"):
                self.set(rid, "not_ready", "wait", 5, conv=conv, ref=(o or {}).get("ref", ""))
                return "rider keeps waiting"
            if intent in ("no", "cant_deliver", "remove_order") or ai.get("wants_cancel"):
                return await self._restaurant(rid, conv, o, ai.get("minutes_needed"), ai, insist=True)
            if intent == "not_ready":
                if step == "minutes":
                    return "noted (waiting for the minutes)"
                return await self._restaurant(rid, conv, o, None, ai, insist=True)   # waited and still not ready → a teammate
            # anything else → a new situation
        elif flow == "phone":
            if intent in ("call_failed", "customer_phone"):
                return await self._number(rid, conv, o)    # asked again after "call from the app" = calling doesn't work for him
            if intent in ("yes", "ack"):
                return "noted (calling from the app)"
        elif flow in ("unreachable", "find"):
            if intent in ("call_failed", "customer_phone", "customer_unreachable", "customer_find") and not st.get("number"):
                return await self._number(rid, conv, o, flow=flow)   # tips first (older chats), now the number
            if intent in ("ack", "yes", "ready"):
                return "noted"
            if intent == "wrong_number":
                await self._hand_over(rid, conv, o, f"📞 {(o or {}).get('ref', '')}: the customer's number seems wrong / not in service — please check", urgent=True)
                return "wrong number → teammate"
            if st.get("number") and intent in ("customer_unreachable", "customer_find", "call_failed", "customer_phone", "other", "no", "excuse"):
                return await self._after_number(rid, conv, o, self._sent_at(conv, o) or st.get("since", time.time()))
            if intent in ("customer_unreachable", "customer_find", "other", "no", "customer_phone", "nudge", "photo", "excuse"):
                mins = round((time.time() - st.get("since", time.time())) / 60)
                what = "customer doesn't answer" if flow == "unreachable" else "rider can't find the address"
                await self._hand_over(rid, conv, o, f"⚠ {what} — {mins} min since {'we sent the number' if st.get('number') else 'our tips'}")
                return f"{flow}: handed over after {mins} min"
        elif flow == "await_photo":
            kind = st.get("kind", "")
            if intent == "photo" or has_photo:
                if kind == "proof":
                    await self._send(rid, conv, "q:proof_thanks", o)
                    info = self._note(rid, f"📷 handover photo received for {st.get('ref') or (o or {}).get('ref', '')} (marked delivered after we sent the number) — please check it")
                    await (self.deps["note"](rid, conv, info, o=o) if self.deps.get("note") else self._fwd(rid, conv, info, o=o))
                    for k, v in self.num_info.items():
                        if k.startswith(f"{conv}|") and v.get("proof_asked"):
                            v["proof_done"] = time.time()
                    self.clear(rid)
                    return "handover photo received → team note"
                if kind in ("forgot", "liefercode"):
                    await self._send(rid, conv, "q:photo_thanks", o)
                    await self._fwd(rid, conv, self._note(rid, f"📷 photo received — {'forgot to finish' if kind == 'forgot' else 'Liefercode missing'}: please finish the order in MotionTools"), o=o)
                    self.clear(rid)
                    return "photo received → team finishes the order"
                if kind == "online":
                    await self._hand_over(rid, conv, o, "📷 screenshot received — rider can't go online: please check his account / shift in MotionTools", urgent=True)
                    return "screenshot (can't go online) → teammate"
                if kind == "returned":
                    await self._send(rid, conv, "q:returned_thanks", o)
                    await self._fwd(rid, conv, self._note(rid, f"📷 photo received — order {(o or {}).get('ref', '')} given back to the restaurant: please cancel / decide in MotionTools"), urgent=True, o=o)
                    self.clear(rid)
                    return "photo of the returned order → teammate"
                await self._hand_over(rid, conv, o, f"📷 photo received — {'restaurant closed?' if kind == 'closed' else 'order damaged'}: please decide")
                return "photo received → handed over"
            if intent in ("yes", "ack", "minutes", "order_ref") or (kind == "proof" and intent in ("other", "no", "excuse", "nudge")):
                return "waiting for the photo"
        elif flow == "await_reason":
            kind = st.get("kind") or "cant_deliver"
            if intent in SOLVABLE:                       # "food not ready", "customer doesn't answer" … → that situation's own steps
                self.clear(rid)
                return await self._new(rid, conv, o, intent, {**ai, "wants_cancel": True}, text, has_photo, False, False)
            reason = (ai.get("reason") or "").strip() or kw_reason(text) or text.strip().replace("\n", " / ")[:120]
            return await self._reason_given(rid, conv, o, kind, reason)
        elif flow == "other_wait":
            if intent in ("other", "ack", "yes", "no", "excuse", "ready", "minutes"):
                await self._hand_over(rid, conv, o, "✉ rider explained the problem")
                return "explained → handed over"
            self.clear(rid)                                  # a clear situation → handled as new below
        return ""

    async def _restaurant(self, rid, conv, o, needed, ai, insist=False) -> str:
        """Food not ready: up to 10 more minutes → wait; more, 20 minutes waited, or he insists → a teammate decides."""
        max_more, max_total = self._cfg("bot_wait_more", 10), self._cfg("bot_wait_total", 20)
        waited = max(self._wait_min(o), ai.get("minutes_waited") or 0)
        ref = (o or {}).get("ref", "")
        if o is not None:
            o["kitchen_reported"] = True
        if waited >= max_total or (needed is not None and needed > max_more) or insist:
            why = (f"waited {waited} min" if waited else "") + (f", restaurant needs {needed} more" if needed is not None else "")
            why += ", rider wants to cancel" if ai.get("wants_cancel") or insist else ""
            await self._hand_over(rid, conv, o, f"⚠ food not ready at {(o or {}).get('restaurant') or 'the restaurant'} {ref} — {why.strip(', ') or 'please decide'}: keep waiting or take it off him",
                                  urgent=waited >= max_total, key="q:handover_restaurant")
            return f"restaurant: handed over ({why.strip(', ')})"
        if needed is not None:
            n = max(1, min(int(needed), max_more))
            await self._send(rid, conv, "q:not_ready_wait", o, n=n)
            self.set(rid, "not_ready", "wait", n + 2, conv=conv, ref=ref)
            return f"asked to wait {n} min"
        await self._send(rid, conv, "q:not_ready", o)
        self.set(rid, "not_ready", "minutes", 6, conv=conv, ref=ref)
        return "asked how many more minutes"

    async def _number(self, rid, conv, o, flow: str = "unreachable", tips_key: str = "") -> str:
        """Calling through the app doesn't work: the customer's number + street/house number + delivery notes — never the
        name, and only for his own live order."""
        d = self.deps
        sent = self._sent_at(conv, o)
        if sent:                                         # the number is already in this chat — never twice
            return await self._after_number(rid, conv, o, sent)
        mine = o is not None and o.get("rider_id") == rid and o.get("phase") not in ("delivered", "cancelled", "closed")
        phone = d["customer_phone"](o) if mine and d.get("customer_phone") else ""
        if mine and not phone and d.get("fetch_phone"):
            try:
                phone = await d["fetch_phone"](o)        # one booking read through the API, if it is open
            except Exception:
                phone = ""
        if mine and phone and self.on("q:phone_number"):
            await self._send(rid, conv, "q:phone_number_find" if flow == "find" else "q:phone_number", o, phone=phone, **delivery_details(o))
            self._mark_sent(conv, o, rid, flow)
            d["log"](rid, o, "reply:number_sent", f"customer number sent for {o.get('ref')} (app call not working)")
            self.set(rid, flow, "", self._cfg("bot_unreach_min", 10), conv=conv, ref=o.get("ref", ""), number=True)
            return "customer's number + house number + notes sent (his own order)"
        why = self._why_no_number(rid, o, mine)
        if tips_key:                                     # no number from the bot: tips now, a teammate sends the number
            await self._send(rid, conv, tips_key, o)
            await self._fwd(rid, conv, self._note(rid, f"📞 rider can't reach the customer — number NOT sent automatically ({why}). Please send it."), urgent=True, o=o)
            self.clear(rid)
            return f"tips sent + teammate asked to send the number ({why})"
        await self._hand_over(rid, conv, o, f"📞 rider can't reach the customer — please send the customer's number ({why})", urgent=True)
        return f"teammate sends the number ({why})"

    async def _number_while_person(self, rid, conv, o) -> str:
        """A teammate has the chat, but the rider can't reach the customer now: the bot sends the number (his own live
        order) and tells the team; if it can't, the team gets the note with the why."""
        d = self.deps
        sent = self._sent_at(conv, o)
        if sent:
            return await self._after_number(rid, conv, o, sent, person=True)
        mine = o is not None and o.get("rider_id") == rid and o.get("phase") not in ("delivered", "cancelled", "closed")
        phone = d["customer_phone"](o) if mine and d.get("customer_phone") else ""
        if mine and not phone and d.get("fetch_phone"):
            try:
                phone = await d["fetch_phone"](o)
            except Exception:
                phone = ""
        if mine and phone and self.on("q:phone_number"):
            await self._send(rid, conv, "q:phone_number", o, phone=phone, **delivery_details(o))
            self._mark_sent(conv, o, rid, "unreachable")
            d["log"](rid, o, "reply:number_sent", f"customer number sent for {o.get('ref')} (teammate on the chat)")
            info = self._note(rid, f"📞 the bot sent the customer's number for {o.get('ref')} — rider couldn't reach the customer")
            await (d["note"](rid, conv, info, o=o) if d.get("note") else d["forward"](rid, conv, info, o=o))   # a note — the chat stays where it is
            return "number sent while a teammate has the chat (team told)"
        await d["forward"](rid, conv, self._note(rid, f"📞 rider needs the customer's number — {self._why_no_number(rid, o, mine)}. Please send it."), urgent=True, o=o)
        return "note: rider needs the number"

    def _sent_at(self, conv, o) -> float:
        return self.number_sent.get(f"{conv}|{(o or {}).get('id', '')}", 0) if o else 0

    def _mark_sent(self, conv, o, rid="", kind="unreachable"):
        k = f"{conv}|{o.get('id', '')}"
        self.number_sent[k] = time.time()
        self.num_info[k] = {"rid": rid, "kind": kind, "ref": o.get("ref", "")}
        self._save()

    async def _after_number(self, rid, conv, o, sent_at: float, person: bool = False) -> str:
        """The number is already in this chat and the rider still can't reach the customer. Never the number again:
        keep trying until 10 minutes after the number (call, bell, neighbours, entrance), then a teammate decides.
        person=True: a teammate has the chat — "keep trying" at most, the decision stays with him."""
        wait = self._cfg("bot_unreach_min", 10)
        mins = (time.time() - sent_at) / 60
        ref = (o or {}).get("ref", "")
        if mins >= wait:
            if person:
                return f"silent — number sent {round(mins)} min ago, the teammate decides"
            await self._hand_over(rid, conv, o, f"⏱ {ref}: customer still unreachable — number sent {round(mins)} min ago, rider kept trying. Please decide",
                                  urgent=True)
            return f"customer unreachable {round(mins)} min after the number → teammate"
        key = f"{conv}|{(o or {}).get('id', '')}"
        if time.time() - self.kept.get(key, 0) < 3 * 60:
            return f"noted — rider keeps trying ({math.ceil(wait - mins)} min left)"
        self.kept[key] = time.time()
        left = max(1, math.ceil(wait - mins))
        find = (self.num_info.get(key) or {}).get("kind") == "find"
        await self._send(rid, conv, "q:keep_trying_find" if find else "q:keep_trying", o, left=left)
        if not person:                                   # the timer hands over exactly 10 minutes after the number
            st = self.state.get(rid)
            if not st or st.get("flow") not in ("unreachable", "find"):
                st = self.set(rid, "find" if find else "unreachable", "", wait - mins, conv=conv, ref=ref, number=True)
            st["since"], st["number"] = sent_at, True
            self._save()
        return f"number already sent → keep trying ({left} min left)"

    @staticmethod
    def _why_no_number(rid, o, mine) -> str:
        if o is None:
            return "no live order found for this rider — which order is it?"
        ref = o.get("ref") or "the order"
        if o.get("rider_id") != rid:
            return f"{ref} is not assigned to this rider in MotionTools"
        if not mine:
            return f"{ref} is already {o.get('phase')}"
        return f"no customer number for {ref} in MotionTools"

    async def _reason_given(self, rid, conv, o, kind, reason) -> str:
        """The rider told us why he can't continue / wants the order cancelled: a teammate decides, with the reason."""
        ref = (o or {}).get("ref", "")
        if kind == "remove_order":
            await self._hand_over(rid, conv, o, f"🔴 wants {ref or 'the order'} cancelled — reason: {reason}. Please decide in MotionTools",
                                  urgent=True, key="q:remove_order")
            return f"wants the order cancelled ({reason}) → handed over"
        await self._hand_over(rid, conv, o, f"⚠ can't continue the delivery — reason: {reason}. Other orders: {self._others(rid, o)}", urgent=True)
        return f"can't continue ({reason}) → handed over"

    async def _new(self, rid, conv, o, intent, ai, text, has_photo, in_auto, last_ops_auto) -> str:
        d = self.deps
        ref = (o or {}).get("ref", "")
        if intent == "not_ready" and o is not None and o.get("phase") in ("to_customer", "at_customer"):
            intent = "excuse"                          # he already has the food: "it takes 30 min" is a delay on the road
        if intent == "not_ready":
            return await self._restaurant(rid, conv, o, ai.get("minutes_needed"), ai, insist=False)
        if intent == "combine":
            await self._hand_over(rid, conv, o, f"🔁 two orders at {(o or {}).get('restaurant') or 'the same restaurant'} — wants both combined", key="q:combine")
            return "wants two orders combined → teammate"
        if intent == "minutes" and o is not None and o.get("phase") in ("at_restaurant", "to_restaurant", "accepted"):
            m = NUM_RE.match(text.split("\n")[-1])
            return await self._restaurant(rid, conv, o, int(m.group(1)) if m else None, ai)
        if intent == "no_order":
            await self._hand_over(rid, conv, o, f"⚠ restaurant says no such order / already taken. Riders on this order: {self._riders(o)}", urgent=True, key="q:no_order")
            return "order not at the restaurant → handed over"
        if intent == "item_missing":
            await self._hand_over(rid, conv, o, "⚠ item not available at the restaurant — check with the restaurant", urgent=True, key="q:item_missing")
            return "item missing → handed over"
        if intent == "closed":
            if o is not None:
                o["query_flag"] = "restaurant closed?"
            await self._send(rid, conv, "q:closed", o)
            await self._fwd(rid, conv, self._note(rid, "⚠ restaurant closed? — photo of the entrance asked"), urgent=True, o=o)
            self.set(rid, "await_photo", "", 10, kind="closed", conv=conv, ref=ref)
            return "restaurant closed → photo asked + team informed"
        if intent == "cant_go_online":
            if has_photo:
                await self._hand_over(rid, conv, o, "📷 rider can't go online (screenshot sent): please check his account / shift in MotionTools", urgent=True)
                return "can't go online with screenshot → teammate"
            await self._send(rid, conv, "q:cant_go_online", o)
            self.set(rid, "await_photo", "", 10, kind="online", conv=conv, ref=ref)
            return "can't go online → screenshot asked"
        if intent == "returned":
            if has_photo:                               # photo came with the message
                await self._send(rid, conv, "q:returned_thanks", o)
                await self._fwd(rid, conv, self._note(rid, f"📷 order {ref} given back to the restaurant (photo sent): please cancel / decide in MotionTools"), urgent=True, o=o)
                return "returned order with photo → teammate"
            await self._send(rid, conv, "q:returned_photo", o)
            self.set(rid, "await_photo", "", 10, kind="returned", conv=conv, ref=ref)
            return "returned order → photo asked"
        if intent in ("remove_order", "cant_deliver"):
            reason = (ai.get("reason") or "").strip() if ai.get("situation") else kw_reason(text)
            if not reason:
                await self._send(rid, conv, "q:cancel_reason" if intent == "remove_order" else "q:cant_deliver", o)
                self.set(rid, "await_reason", "", 5, conv=conv, ref=ref, kind=intent)
                return "asked the reason"
            return await self._reason_given(rid, conv, o, intent, reason)
        if intent == "customer_phone":                 # asks for the number → number + house number + notes (his own order)
            return await self._number(rid, conv, o, flow="unreachable", tips_key="q:phone_app")
        if intent == "call_failed":
            return await self._number(rid, conv, o)
        if intent == "customer_unreachable":           # can't reach the customer → number + house number + notes
            return await self._number(rid, conv, o, flow="unreachable", tips_key="q:unreachable")
        if intent == "customer_find":                  # can't find the address → number + house number + notes
            return await self._number(rid, conv, o, flow="find", tips_key="q:find_address")
        if intent == "liefercode":
            await self._send(rid, conv, "q:liefercode", o)
            self.set(rid, "await_photo", "", 15, kind="liefercode", conv=conv, ref=ref)
            return "Liefercode tips sent, photo asked"
        if intent == "forgot_finish":
            done = o is None or o.get("phase") in ("delivered", "closed")
            if done and o is not None:
                await self._send(rid, conv, "q:forgot_done", o)
                return "already finished"
            await self._send(rid, conv, "q:forgot_finish", o)
            self.set(rid, "await_photo", "", 15, kind="forgot", conv=conv, ref=ref)
            return "asked for order number + photo"
        if intent == "customer_problem":
            await self._hand_over(rid, conv, o, "⚠ customer refuses the order / problem at the customer", key="q:customer_refuses")
            return "customer refuses → asked what happened + handed over"
        if intent == "damaged":
            if o is not None:
                o["query_flag"] = "order damaged?"
            await self._send(rid, conv, "q:damaged", o)
            self.set(rid, "await_photo", "", 10, kind="damaged", conv=conv, ref=ref)
            return "photo of the damage asked"
        if intent == "photo":                          # a photo alone says nothing yet — ask, don't park the chat with the team
            low = (text or "").lower()
            if o is not None and (o.get("phase") in ("delivered", "closed") or any(w in low for w in DELIVERED_WORDS)):
                await self._send(rid, conv, "q:proof_thanks", o)             # delivery / handover photo: just thank him
                o["query_flag"] = o.get("query_flag") or "delivery photo sent by the rider"
                return "delivery photo → thanks"
            await self._send(rid, conv, "q:photo_what_known" if o is not None else "q:photo_what", o)   # known order: don't ask for it
            self.set(rid, "other_wait", "", 30, conv=conv, ref=ref)
            return "photo without text → asked what it is about"
        if intent == "wrong_number":
            await self._hand_over(rid, conv, o, f"📞 {ref}: the customer's number seems wrong / not in service — please check", urgent=True)
            return "wrong number → teammate"
        if intent == "no_new_orders":
            await self._hand_over(rid, conv, o, "📵 rider gets no new orders — please check his account / zone / online status in MotionTools", key="q:no_new_orders")
            return "no new orders → teammate checks the account"
        if intent == "compensation":
            await self._hand_over(rid, conv, o, "💶 rider asks for compensation (extra distance / pay) — please check", key="q:compensation")
            return "compensation request → teammate"
        if intent == "order_ref" and o is not None:
            await self._send(rid, conv, "q:order_ref", o, status=d["status_of"](o) if d.get("status_of") else o.get("phase", ""))
            self.set(rid, "other_wait", "", 30, conv=conv, ref=ref)
            return "order recognised, asked what's wrong"
        if intent in ("ack", "yes", "ready"):
            if in_auto or last_ops_auto:
                await self._send(rid, conv, "q:ack", o)
                return "acknowledged"
            return "ignored (reply to a person)"
        if intent == "excuse":
            if o is not None:
                o["excuse"] = text[:120]
            if in_auto or last_ops_auto or (o is not None and o.get("phase") in ("to_customer", "at_customer")):
                await self._send(rid, conv, "q:noted", o)
                return "noted"
            return "ignored (reply to a person)"
        # other / no / unclear
        if in_auto:
            await self._hand_over(rid, conv, o, "✉ reply to an automatic message — needs a person")
            return "handed over"
        lines = [m for m in text.split("\n") if m.strip()] or [""]
        greeting = all(is_greeting(m) or button_intent(m) == "other" for m in lines)
        substantive = not greeting and len(text.split()) >= 5
        akey = f"{rid}|{conv}"
        if not substantive and time.time() - self.asked_other.get(akey, 0) > 2 * 3600:
            self.last_bot.pop(conv, None)                # a fresh question: the "said twice" guard is for answers, not for this
            for k in ("q:other", "q:other_known"):
                self.sent_keys.get(f"{rid}|{conv}", {}).pop(k, None)
            await self._send(rid, conv, "q:other_known" if o is not None else "q:other", o)   # known order: don't ask for it
            self.asked_other[akey] = time.time()
            self.set(rid, "other_wait", "", 30, conv=conv, ref=ref)
            return "asked what happened"
        await self._hand_over(rid, conv, o, "✉ needs a person")
        return "handed over"

    # ---- timers (once a minute from the automation loop)
    async def tick(self, now: datetime):
        d = self.deps
        t = now.timestamp()
        await self._proof_check(t)
        # riders left waiting in a forwarded conversation: nobody answered for 7 min → "still with you" + urgent note
        for conv, since in list(self.last_rider_at.items()):
            if t - since > 3 * 3600:
                self.last_rider_at.pop(conv, None); continue
            if t - since < 7 * 60 or not self.handed_over(conv):
                continue
            rid = self.conv_rider.get(conv)
            if not rid or t - self.told.get(conv, 0) < 15 * 60:
                continue
            try:
                rw = d.get("rider_waiting")
                if rw and not await rw(conv, since):
                    self.last_rider_at.pop(conv, None); continue   # a teammate answered, closed or snoozed it
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
            try:
                if flow == "not_ready":
                    if o is None or o.get("picked_up_at") or o.get("phase") not in ("at_restaurant", "to_restaurant", "accepted"):
                        self.clear(rid); continue
                    if self._wait_min(o) >= self._cfg("bot_wait_total", 20):
                        if not await self._closed_by_team(rid, conv, st):
                            self.last_ai.pop(rid, None)
                            await self._hand_over(rid, conv, o, f"⏱ {o.get('ref')}: rider waiting {self._wait_min(o)} min at {o.get('restaurant')} — keep waiting or take it off him",
                                                  urgent=True, key="q:handover_restaurant")
                        continue
                    if until and t >= until:
                        if st.get("step") == "wait":
                            if not await self._closed_by_team(rid, conv, st):
                                await self._send(rid, conv, "q:not_ready_check", o)
                                st["step"], st["until"] = "check", t + 5 * 60; self._save()
                        elif st.get("step") == "check":
                            if not await self._closed_by_team(rid, conv, st):
                                await self._fwd(rid, conv, f"⏱ {o.get('ref')}: no answer after 'is it ready now?', {self._wait_min(o)} min at {o.get('restaurant')}.", o=o)
                            self.clear(rid)
                        else:
                            self.clear(rid)                       # never told us the minutes — he probably got the food
                elif flow in ("unreachable", "find"):
                    if o is None or o.get("phase") in ("delivered", "closed", "cancelled"):
                        self.clear(rid); continue
                    if until and t >= until and not await self._closed_by_team(rid, conv, st):
                        what = "customer still unreachable" if flow == "unreachable" else "rider still can't find the address"
                        self.last_ai.pop(rid, None)
                        self._last.pop(rid, None)
                        await self._hand_over(rid, conv, o, f"⏱ {o.get('ref')}: {what} after {round((t - st.get('since', t)) / 60)} min — please decide", urgent=True)
                elif flow == "await_photo" and st.get("kind") == "proof" and until and t >= until:
                    if not await self._closed_by_team(rid, conv, st):
                        po = d["order_by_id"](st.get("oid")) if d.get("order_by_id") and st.get("oid") else o
                        if po is not None:
                            po["query_flag"] = "no handover photo"
                        await self._fwd(rid, conv, self._note(rid, f"🔴 {st.get('ref')}: NO handover photo 10 min after it was marked delivered (we had sent the customer's number) — please check this order"),
                                        urgent=True, o=po)
                    self.clear(rid)
                elif flow in ("await_photo", "await_reason") and until and t >= until:
                    if not await self._closed_by_team(rid, conv, st):
                        what = "photo" if flow == "await_photo" else "reason"
                        await self._fwd(rid, conv, f"⏳ no {what} received for '{st.get('kind') or 'cannot continue'}' — please follow up.", o=o)
                    self.clear(rid)
                elif flow in ("phone", "other_wait") and until and t >= until:
                    self.clear(rid)
            except Exception:
                pass

    async def _proof_check(self, t: float):
        """The Liefercode is the last 2 digits of the customer's number: an order marked delivered after we sent that
        number gets a handover-photo request at once, a note for the team, and a red flag if no photo comes."""
        d = self.deps
        if not d.get("order_by_id"):
            return
        for key, sent in list(self.number_sent.items()):
            info = self.num_info.get(key)
            if not info or info.get("proof_asked") or not info.get("rid") or t - sent > 3 * 3600:
                continue
            conv, oid = key.split("|", 1)
            try:
                o = d["order_by_id"](oid)
            except Exception:
                o = None
            if not o or o.get("phase") != "delivered":
                continue
            dl = o.get("delivered_at")
            dl_ts = dl.timestamp() if hasattr(dl, "timestamp") else t
            info["proof_asked"] = t
            if dl_ts < sent - 60:
                continue                                 # delivered before the number went out — nothing to check
            rid = info["rid"]
            mins = max(0, round((dl_ts - sent) / 60))
            closed = False
            if d.get("conv_closed"):
                try:
                    closed = await d["conv_closed"](conv)    # the team closed the chat: don't re-open it with a message
                except Exception:
                    closed = False
            ask = self.on("q:proof_photo") and not closed
            o["query_flag"] = "delivered after the customer's number — " + ("handover photo asked" if ask else "please check")
            try:
                if ask:
                    await self._send(rid, conv, "q:proof_photo", o)
                    self.set(rid, "await_photo", "", 10, kind="proof", conv=conv, ref=o.get("ref", ""), oid=oid)
                why = "handover photo asked" if ask else ("chat closed by the team — no message to the rider" if closed else "please check")
                note = self._note(rid, f"⚠ {o.get('ref')} marked as delivered {mins} min after we sent the customer's number — {why}")
                await (d["note"](rid, conv, note, o=o) if d.get("note") else d["forward"](rid, conv, note, o=o))
            except Exception:
                pass
            self._save()

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

    # ---- helpers
    def _wait_min(self, o) -> int:
        if not o or not o.get("at_restaurant_at") or o.get("picked_up_at"):
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


VEHICLE = ("bike", "car", "auto", "fahrrad", "roller", "scooter", "ebike", "motorrad", "moped", "tire", "tyre", "reifen", "السيارة", "الدراجة",
           "سيارة", "دراجة", "araba", "bisiklet", "gaadi", "gadi")
REASON_HINTS = ("car", "auto", "bike", "fahrrad", "roller", "scooter", "ebike", "tire", "tyre", "reifen", "platten", "flat", "panne", "akku",
                "battery", "kaputt", "broke", "broken", "sick", "krank", "ill", "accident", "unfall", "police", "polizei", "far", "weit", "km",
                "home", "hause", "rain", "regen", "storm", "late", "wait", "waiting", "warte", "warten", "ready", "fertig", "closed",
                "geschlossen", "customer", "kunde", "kunden", "address", "adresse", "because", "weil", "wegen", "denn", "phone", "handy",
                "السيارة", "الدراجة", "سيارة", "دراجة", "مريض", "بعيد", "حادث", "المطعم", "الزبون", "العميل", "لأن", "عشان", "بسبب",
                "araba", "bisiklet", "hasta", "uzak", "çünkü", "kyunki", "kyuki", "gaadi", "gadi")


def kw_reason(text: str) -> str:
    """Keyword view: did the rider say WHY he can't continue / wants to cancel? The button label alone is no reason."""
    for line in (text or "").split("\n"):
        low = line.strip().lower()
        if low and _has_word(low, REASON_HINTS) and not button_intent(line):
            return line.strip()[:120]
    return ""


DEFAULT_OFF = {"q:proof_photo"}      # answers that stay off until switched on in Settings
# a photo sent with one of these words is a delivery / handover photo (rider's own order) → just "thanks"
DELIVERED_WORDS = ("delivered", "geliefert", "zugestellt", "übergeben", "abgegeben", "liefercode", "code", "تم التوصيل", "تم التسليم",
                   "سلمت", "teslim edildi", "teslim ettim", "entregado", "consegnato", "livré", "доставлен", "डिलीवर")

_LABEL_LINE = re.compile(r"^[^{}\n]*:\s*\{(\w+)\}[ \t.]*$")


def delivery_details(o) -> dict:
    """For a rider who can't reach the customer through the app: street + house number (no city, no zip) and the
    customer's delivery notes (floor, bell, entrance …). Never the customer's name."""
    o = o or {}
    street = str(o.get("customer_addr") or "").split(",")[0].strip()
    notes = re.sub(r"\s+", " ", str(o.get("customer_notes") or "")).strip()[:300]
    name = str(o.get("customer_name") or "").strip()
    if name and notes == name:
        notes = ""                                       # a "note" that is only the name is not a delivery note
    return {"address": street, "notes": notes}


def render_query(template: str, **fmt) -> str:
    """Fill a query text; missing values become empty; tidy double spaces and empty lines. A line that is only
    'Label: {value}' is left out when that value is empty ("Customer's notes:" with no notes)."""
    class _D(dict):
        def __missing__(self, k):
            return ""
    template = "\n".join(ln for ln in template.split("\n")
                         if not ((m := _LABEL_LINE.match(ln)) and not str(fmt.get(m.group(1)) or "").strip()))
    out = template.format_map(_D(**fmt))
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    out = re.sub(r" ·\s*(?=\n|$)", "", out)
    return out.strip()
