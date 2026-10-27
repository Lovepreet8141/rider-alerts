# Quickzi — fleet ops platform, all cities (v6.4)

A 24/7 control room for Quickzi's delivery operation in every city — one server, one webhook stream, one database. Everything is written to SQLite on the Railway
volume, so nothing depends on anyone watching: while you sleep it keeps recording, and a few minutes after midnight
(Berlin) it freezes the daily report of the day that just ended.

## Network edition (6.1): every city in one tool
- **A city = a MotionTools service area.** Events only carry the area id, so each area is named once in Settings → Cities
  (names coming from the booking API are taken automatically). Until named, an area shows as the first 8 characters of its id.
- **City selector** in the header: every tab — City (live board), Orders, Riders, Insights, Daily report, CSV exports,
  staffing plan, delete-day — works for the selected city or for all cities. The choice is remembered per browser.
- **Overview tab**: the whole network — delivered, on time, PTOD, live, waiting, riders on orders, alerts — then one tile per
  city sorted by status (critical → strained → ok) with live/waiting/riders, on time, avg, orders-per-hour bars and the top
  issue in words; a per-city watchdog ("silent 18′") when a city stops sending events while it has live orders; the 14-day
  on-time trend; the list of cities that need the network team now. Tap a city → its live board.
- **Fleets = MotionTools organizations.** Every rider has an *Organization* in MotionTools; the server reads it through the
  rider-detail endpoint (open on the restricted token) after each start, one rider every 2 s, and uses it as the fleet.
  A fleet typed in Settings wins over the API value. The Riders tab then shows a Fleets table — riders, deliveries, per rider, within target, on time, PTOD, accept, hand-backs — same metrics for every fleet.
- **Built for 170 000 orders a month**: city/day/hour and the result numbers are stored as columns, the staffing plan and
  the network view aggregate in SQL, alerts are indexed, the event replay streams the daily files, and orders older than
  90 days keep their numbers but drop their details. Measured with 25 cities, 1 000 live orders, 600 riders: a city page
  answers in ~10 ms, the network overview in ~2 ms (cached 10 s), alert evaluation of 1 000 orders in ~30 ms.
- **Deployment change**: remove the `MUNICH_SERVICE_AREA_ID` variable in Railway (or leave it empty) — with it set, events
  of other cities are ignored. Make sure the MotionTools webhook is not limited to one service area.

## 9.7.1 — Morning report: long restaurant waits in Slack with @here + the sheet to download (new file: gsheet.py · requirements: openpyxl)

* **Every day at 10:00 (Berlin)** — the time can be changed in Settings — a message starting with **@here** goes to
  your team's channel: orders where the rider waited 10+ min yesterday, % of delivered, rider-hours lost, the 3 cities
  with the most, the worst restaurant, and a **download link to the sheet** (Excel — opens in Google Sheets:
  Drive → New → File upload). Mondays the file also has last week, and the Monday waiting report follows.
  A restart up to 8 hours after the time still sends what is due; a deploy at night waits for the next morning.
* **The sheet**: *Yesterday* (key numbers, by city, top 10 restaurants) · *Last week* (by city + call list per city,
  Mondays) · *Yesterday orders* · *Last week orders* (every order with its timeline, wait, PTOD, hand-back note and a
  MotionTools link) · *How it works*. Sorted by city A–Z; waits coloured 10–14 yellow, 15–19 orange, 20+ red.
  No customer data. Download any day or week: /export/long-waits.xlsx?day=YYYY-MM-DD or ?week=YYYY-MM-DD.
* **No Google account needed.** For the Quickzii ops channel add an incoming webhook for it in Railway as
  `SLACK_REPORT_WEBHOOK_URL` (without it the message goes to the `SLACK_WEBHOOK_URL` channel).
* Settings → Morning report: on/off, time, preview, send now, download yesterday's / last week's sheet.
* (9.7.0 wrote into a Google Sheet with a Google service account — replaced by the file, nothing to set up.)

## 9.6.0 — PTOD under 30 (new page, key 4)

* **Its own section in the sidebar, right after Performance.** It shows how to keep PTOD under 30′, from your own orders:
  * **Right now** — live orders that will go over 30′ unless someone acts, with the step they're stuck in and what to
    do ("No rider after 7′ — assign one now", "Food not ready after 13′ — call the restaurant"). Orders that are
    going normally are left out. Refreshes every 20 s.
  * **Where the minutes go** — the 30′ budget (accept 2′ · to the restaurant 8′ · food 5′ · ride 12′ · hand-over 3′)
    next to the on-time and the late orders, step by step.
  * **Biggest fixes** — for each step, how many points of "within 30′" you'd gain if only that step had its normal
    time, ranked. Each fix has its own card with where to make it: slow kitchens (with "send riders X′ later" per
    restaurant and hour), long rides (restaurants to ask Lieferando about), hours short of riders, riders who accept
    slowly, riders slow at the door.
  * **City by city, hour by hour** — % within 30′ per city and hour, with the fix that matters most in each hour.
* Periods: last 7 days (default), today, yesterday, last week, month or any range.
* The Monday Slack report now ends with the 3 biggest fixes of the week (no rider names in Slack).
* Number keys: 1 Now · 2 City board · 3 Performance · 4 PTOD under 30 · 5 Restaurants · 6 Riders & fleets ·
  7 Staffing · 8 Evidence pack · 9 Orders · 0 Intercom bot.
* Only shown in the dashboard — nothing is sent to riders, restaurants or MotionTools.

## 9.5.0 — Staffing forecast · Lieferando evidence pack · Monday waiting report in Slack

* **Staffing (key 6)** — orders and riders needed per city and hour for tomorrow (or any of the next 7 days, ← →).
  Orders = the last 4 same weekdays (40 · 30 · 20 · 10 %) × the city's trend of the last 7 days (max ±15 %) × the
  weather in that hour. **Riders needed are worked out per city from its own history**: orders in the hour × the
  minutes a delivery really takes a rider in that city at that hour (accept → restaurant → waiting → ride → hand-over,
  same weekdays; orders a rider carries together count once) ÷ 48 (a rider is busy ~80 % of an hour). Cities are
  planned one by one and then added up. The grid "Riders needed per city, hour by hour" shows every city at a glance;
  with a shift sheet it marks hours with fewer riders planned than needed. Only shown in the dashboard — no rider is
  messaged. Weather: Deutscher Wetterdienst via Bright Sky (free, no key), refreshed every hour.
* **Evidence pack (key 7)** — per week, every late order where the rider was at the restaurant within 20 min and the
  kitchen made him wait 10+ min, and the order would have been on time without that waiting. Timeline per order
  (dispatched, accepted, at restaurant, picked up, delivered), per-restaurant totals, "within 30′ would have been X %".
  PDF and CSV to send to Lieferando. Order numbers, restaurants and times only — no customer data, no rider names.
* **Monday report in Slack** — Mondays from 08:30 (Tuesday if missed), once per week, to your own Slack channel:
  average restaurant wait, rider-hours lost, the 5 restaurants that cost the most rider time (with their slowest
  hour), getting worse / better, cities with the longest waits, the evidence summary and 3 actions. Settings → Slack:
  "Monday report" on/off, preview and "send now". No customer data.

## 9.4.1 — Restaurants: "All restaurants"

* The ranking starts with **All restaurants** (selected when the page opens): every long-wait order of the period and
  city with its restaurant, wait by hour and long waits per week for all restaurants together, PDF and CSV of all.
  Click a restaurant to see only that one; ↑ ↓ move between "All" and the restaurants.
* New download: /export/waits.csv — every long-wait order (all restaurants or one) for Excel.
* File names keep umlauts readable (muenchen instead of m-nchen).

## 9.4.0 — one dashboard, one design

* Every page now has the premium look of /v2: the same left sidebar everywhere (Now · City board · Performance ·
  Restaurants · Riders & fleets · Orders · Intercom bot · Reports · Settings, and under More: Shifts & scores ·
  Fleet scorecard · Insights), keys 1–9 switch pages from anywhere, page titles, cards, buttons, tables, dialogs
  and the messages drawer in the same colours and type.
* The city you pick travels with you between the live pages and Performance / Restaurants / Riders.
* Phones: a bottom bar (Now · City · Performance · Orders · Menu) and a menu with every page.
* Nothing changed in what the pages do — every button, filter, export and setting works as before (all tests pass).
* Always dark (the light/dark switch is gone).

## 9.3.1 — /v2 fast: every finished day is counted once

* The new dashboard was slow because every period or city you clicked re-read the last 30 days of orders (~150 000)
  for the trend line and the heatmap. Now each finished day is aggregated ONCE (per city × hour) and kept in memory and
  on disk (table day_cube); a month, a city or a comparison is a sum of ~30 small tables. Only today is read live.
  Measured on 156 000 orders: Performance month 2.2 s → 0.03 s, Restaurants month 10 s → 0.04 s, any city ~5 ms.
* After a deploy the last 9 weeks are prepared in the background (first time ~1–2 min, then loaded from disk).
* One restaurant's orders use a new index (created in the background after the start).
* ⌘K searches restaurants in memory; riders' "usual" is read once an hour; week/month rider tables refresh every 2 min.
* Lighter page: no background blur (it repainted the page on every scroll); the other pages are fetched in advance,
  so switching between Performance, Restaurants and Riders is instant.
* /health shows v2_cube_days.

## 9.3.0 — the new premium dashboard at /v2 (new files: app.html, analytics.py)

* Open **/v2** (same login). Three pages on your real data, next to the current dashboard (which stays at /):
  * **Performance** — delivered, avg delivery time, within 30′, riders (each with a 14-day sparkline and a fair
    comparison: today is compared with the same weekday last week *up to the same time*); live ribbon (live orders,
    waiting, cities in trouble); orders vs rider capacity per hour; where the minutes go; delivery time by city;
    weekday × hour late heatmap; why orders were late; 30-day trend (within 30′ / avg time / delivered).
  * **Restaurants** — every restaurant ranked by long waits; the selected one with wait by hour, long waits per week,
    every long-wait order (click = full story, Open ↗ = MotionTools), its own PDF and CSV; long waits by hour and city.
  * **Riders & fleets** — riders right now, who needs a check (not moving, no GPS, waiting long, accepted but not at the
    restaurant, 2+ hand-backs) with a Message button (Intercom chat in a side panel), deliveries-per-rider histogram,
    speed vs workload scatter, sortable rider table, fleets side by side.
* Periods: Today · Yesterday · This week · Last week · Month · any custom from–to. ← → move a day back/forward.
* ⌘K (or /) searches orders (any day by number), riders, restaurants and cities. 1–7 switch pages, E export.
* Hover any bar, cell, dot or line for exact numbers. Everything is computed in SQL and cached; pages answer in
  milliseconds and refresh by themselves every 30 s while the period includes today.
* The long-waits PDF now also works for one restaurant and for custom from–to ranges.
* Now / Orders / Intercom / Settings still open the current dashboard; it got a "✦ New dashboard" link.

## 9.2.2 — the long-waits PDF lists EVERY order (new files: DejaVuSansCondensed.ttf, DejaVuSansCondensed-Bold.ttf)

* No more "2 000 longest" limit: every order that waited 10+ min is in the PDF, grouped per restaurant (worst first),
  each with its timeline: dispatched · accepted · arrived · picked up · waited · delivered · delivery time · note.
* All letters print (Phở Việt, Şükrü, ł …): the PDF uses the bundled DejaVu font — the two .ttf files must be
  uploaded next to app.py.
* The PDF is drawn in its own short-lived process, so a 400-page week never slows the live board (tested: 13 800 orders,
  488 pages, 13 s, board answering in ~70 ms meanwhile).
* Period label shows the real days: "Last week (Mon 19 Oct – Sun 25 Oct 2026)".

## 9.2.1 — "Last week" (Monday–Sunday) on the Orders page · clock-change fix

* Orders page: Today · Yesterday · This week · **Last week** (Monday 00:00 – Sunday 24:00) · Month. The CSV and the
  long-waits PDF follow the chosen period.
* Fix: "Last week" and "Yesterday" are counted on Berlin's wall clock. Across the clock change (last Sunday of March /
  October) they had lost or gained an hour (last week started Monday 01:00 instead of 00:00).

## 9.2.0 — PDF of long restaurant waits (new file: pdf_report.py · requirements: fpdf2)

* Orders page → "waited ≥ [10] min" + "⬇ PDF long waits" (Today / Yesterday / Week / Month / any day, current city).
  Daily report → "⬇ PDF long waits" for that day.
* The PDF: summary (how many orders waited, share of all orders that reached a restaurant, average, longest), a ranking
  of restaurants (long waits, share of their orders, average, longest) and the order list (date, arrival, order,
  restaurant, city, rider, pickup, minutes waited, note: handed back / cancelled / still waiting).
* The wait is the kitchen's: from the FIRST rider's arrival to pickup — a rider who gave up and handed the order back
  still counts. No customer data. Very long periods list the 2 000 longest waits (the CSV has every order).
* Railway installs fpdf2 by itself from requirements.txt (the first build takes a minute longer).

## 9.1.5 — Slack: rider accepted but not at the restaurant

* New Slack message for priority restaurants: 🕐 a rider ACCEPTED the order but is still not at the restaurant
  10 min later (rider, when he accepted, his ETA at the restaurant, the order's timeline). Once per rider: if another
  rider takes the order over, he gets his own 10 minutes. Orders nobody accepted stay with ⚠️ no rider.
* Settings → Slack: "🕐 rider accepted but not at the restaurant after [10] min" (on; minutes adjustable).

## 9.1.4 — short number message · no "marked as delivered" message · 3 misreads fixed

* The customer's-number message is short now (number, street + house number, the customer's notes, then):
  "Call, ring the bell, ask the neighbours. Wait there 10 min. ⚠️ Don't mark it delivered without handing the food to
  the customer. No answer? Write us here." The keep-trying follow-ups are shorter too.
* "We see order X is marked as delivered. Please send us a photo…" is switched OFF (once, also where it was saved as on).
  Settings → Intercom → answers → "q:proof_photo" switches it back on.
* A photo with "Delivered" / "code 02" (or of an order that is already delivered) → "Thanks, got the photo." instead of
  "What is it about?" (or no answer at all).
* "Two orders at the same restaurant — please combine them" → "A teammate is checking if you can take both orders"
  (was read as "restaurant closed" because of the German word "zu").
* "The order takes half an hour because of traffic" after pickup → "Thanks, noted." (was read as "food not ready").

## 9.1.3 — Slack 🆕 right when the order arrives

* 🆕 no longer waits for MotionTools to dispatch the order (that made it 2–4 min late, e.g. TW343W: food preparation,
  on hold 16:47 → auto-dispatch 16:57). It is posted within ~20 seconds of arrival, as soon as MotionTools' first ETAs
  are in (never later than 1 min).
* The message says "Dispatched HH:MM" when the order was dispatched at once, otherwise "On hold (not dispatched yet)"
  with the pickup time (food ready). ⚠️ no rider still carries the dispatch time.

## 9.1.2 — no more yellow notes in Intercom

* The bot no longer writes internal notes into rider chats (🔴 "rider waiting … — please reply now", ⚠ hand-overs,
  ℹ information). The team reads the chat itself. Hand-overs work as before: the chat is assigned to you and re-opened,
  so it still shows up in your inbox — just without the note.
* Settings → Intercom → Bot brain → "internal notes in Intercom" switches them back on (off by default).

## 9.1.1 — Slack messages carry the order's timeline

* Every Slack message now shows: Received (when the order arrived in our system) · Dispatched (offered to riders) ·
  Pickup ~ / Picked up · Planned delivery (in N min), then the rider with his acceptance time, or "Waiting for a rider",
  and the distance when MotionTools sends it.
* 🆕 waits until MotionTools dispatches the order, so the dispatch time is in it. That is usually seconds,
  at most ~4 min. Pre-orders are posted at once as "🆕 Pre-order … On hold (not dispatched yet)".
* ❌ shows the cancel time, the timeline and the rider. ⚠️ shows how many minutes are left until the planned delivery.

## 9.1.0 — Slack messages for priority restaurants (new file: slack_notify.py)

* Settings → "Slack — priority restaurants": one restaurant per line (part of the name is enough, ®/™ ignored).
  For their orders the dashboard posts to ONE Slack channel of your own team:
  🆕 new order (ref, restaurant, city, planned delivery / pre-order time, MotionTools ↗ link) ·
  ⚠️ no rider after N minutes (default 5) · ❌ cancelled (with the reason). Each once per order, also after a restart.
  No customer data. Checked every 20 seconds.
* Setup: Slack → api.slack.com/apps → Create app → Incoming Webhooks → On → Add New Webhook → pick the channel →
  copy the URL → Railway (web) → Variables → SLACK_WEBHOOK_URL. "Send test message" checks it.
* Never use a channel shared with MotionTools (Slack Connect) — the dashboard never contacts MotionTools.
* /health → "slack": webhook set, posted today, failed, last error.

## 9.0.13 — handover-photo request can be switched off · closed chats stay closed

* Intercom page → bot answers → "Order marked delivered after we sent the customer's number → handover photo" → On off:
  the rider gets no second message; the team still gets a note "marked as delivered N min after we sent the number —
  please check" and the order keeps its dashboard flag. No "NO handover photo" alert (no photo was asked).
* If the team already closed the chat, the bot never re-opens it with the photo request — team note only.

## 9.0.12 — no "not moving" without a live GPS feed

* "Not moving" (and "no GPS" / "wrong direction") alerts and their rider messages now need a LIVE GPS feed:
  booking.driver_location_updated / tour.driver_location_updated events. One-off positions (tour started, rider went
  online) are still stored but no longer count as a feed — before, a rider who started a tour looked "not moving"
  a few minutes after the pickup because simply no new position arrived (Göttingen, 13 Oct: pickup 16:54, message 17:00).
* To get "not moving" back: add booking.driver_location_updated to the MotionTools webhook (same customer_id filter).

## 9.0.11 — "don't mark it delivered" + handover photo check

* The Liefercode is the last 2 digits of the customer's phone number, so the number message now says:
  "only mark the order as delivered when you hand the food to the customer. If you can't find or reach the customer,
  do NOT mark it as delivered. Write here … We need a photo of the food handover to the customer."
* Can't FIND the customer → the number message says how to look: entrance and building (more than one entrance),
  the customer's name from the app on the doorbells, ask the neighbours. Follow-ups: "keep looking for N minutes".
* Every "keep trying / keep looking" message repeats: don't mark it delivered without handing it over.
* Check: an order marked delivered AFTER we sent its customer's number → within a minute the rider is asked for a
  photo of the handover, the team gets a note (the chat is not moved), the order is flagged on the dashboard. Photo
  received → thanks + note. No photo in 10 minutes → red team alert + flag "no handover photo". The photo counts
  even while a teammate has the chat.

## 9.0.10 — no second question for a known order · cities A–Z

* When the bot already knows the rider's order, "Hi / Other / photo" get "How can I help you?" / "What is it about?"
  WITHOUT "plus the order number" (new texts q:other_known, q:photo_what_known).
* City lists sorted alphabetically (German order, Ä/Ö/Ü in place): the city menu at the top, the Intercom page
  "Cities for the rules", Settings → city names. New cities slot in at the right place.

## 9.0.9 — the customer's number only once per chat

* The number (+ house number + notes) goes to a rider **once per order per chat** — on every path, also while a
  teammate has the chat, and also after a restart (remembered 4 hours).
* The number message now says: wait there at least 10 minutes, keep calling, ring the bell, ask the neighbours, check
  you are at the right address (some buildings have more than one entrance).
* Rider writes again ("can't call", "what if he's not there?") → "Please keep trying for N more minutes: call, bell,
  neighbours, entrance" — at most once every 3 minutes, never the number again.
* 10 minutes after the number → a teammate decides (also when the rider stays silent).
* "The number is wrong / someone else answers" → a teammate at once.
* Information notes ("the bot sent the number") no longer re-assign or re-open the chat.

## 9.0.8 — six real chats from 13 Oct answered right

* "Why am I not receiving any orders?" → check online / zone + a teammate checks the account (was: "the restaurant
  doesn't have this order").
* "Send me the customer's phone number" → the number + house number + notes straight away (his own live order).
* "Road closed, 2.5 km detour, please compensate" → a teammate checks the compensation (was: "photo of the entrance" —
  a closed road is not a closed restaurant).
* "He can't find a PIN" → Liefercode help ("PIN" = Liefercode; a map pin is not).
* Photo without text → "What is it about?" (was: forwarded, and the rider's next question got no answer).
* A rider at the door who can't reach the customer gets the number even while a teammate has the chat; the teammate
  gets a note. Everything else in that chat stays with the teammate.
* The "said twice → a person takes over" guard counts per chat, so a new chat can get the same question again.

## 9.0.7 — ↗ opens the tour

* With the `tour.created` webhook (no filter, same relay URL) the ↗ opens the order's **tour**; the newest tour wins
  when MotionTools merges orders into one tour, and it also works when the tour event arrives before the booking.
* Tour → booking links are kept in memory (newest 20 000 tours), no longer written to the settings table (that table is
  read on every rider message — thousands of tours a day would slow it down).

## 9.0.6 — ↗ MotionTools link next to every order number

* A small ↗ next to the order number (order list, the order panel, the Orders table) opens the order in the MotionTools
  dashboard in a new tab: the **tour** page when the dashboard knows the tour (from the `tour.created` webhook),
  otherwise the **booking** page. Base URL: `MT_WEB_URL` (default `https://quickzii.motiontools.io`).
* Tour ids come only from MotionTools' `tour.created` event, which carries no customer_id — so it needs its own webhook
  without a filter (same relay URL). Booking events never contain the tour id.

## 9.0.5 — every button and every "hi" gets an answer

* "Hi", "Salam alaikum", "Hello, I need help", the **Other** button → "Hi! How can I help you? Please tell us in one
  sentence what happened, plus the order number." Once per chat (was: once per rider every 3 hours, after that the
  rider only got "a teammate has your case").
* A contact we can't link to a rider yet → "Hi! How can I help you? Please send the order number …", in his language.
* All workflow buttons are known as riders see them: I can't go online · The Order is not in Restaurant · order is not
  ready yet · I can't reach out to the customer · Order is damaged · I can't do the Delivery · I forgot to finish the
  order on the app · Customer doesn't accept the Order · Other.
* New: **I can't go online** → screenshot asked → teammate checks the account / shift.
* A rider who opens a NEW chat with a button or "hi" starts fresh (the old chat's step no longer answers for him).
* A teammate who wrote in the chat in the last 15 minutes (was 30) keeps the bot out.
* Arabic short words match as whole words ("السلام" is no "لا").

## 9.0.4 — the number reaches every rider who can't reach the customer

* A rider who already got the "keep calling" tips and writes again ("he is not responding to calls or the doorbell")
  now gets the number + house number + notes (was: handed over without the number).
* When the bot can't send the number itself, the rider gets the tips AND a teammate gets a note to send the number,
  with the exact why: no live order found for this rider / order not assigned to him / already delivered /
  no customer number in MotionTools.
* Order numbers written with a space ("6dg 7vy") are found (6DG7VY).
* "I have returned the order, please cancel" → the bot asks for a photo of the order at the restaurant; the photo goes
  to a teammate with "please cancel / decide in MotionTools". No photo in 10 minutes → the team is told.

## 9.0.3 — ask the reason first; number when the customer can't be reached

* **"I cannot make the delivery" / "Please cancel"**: the bot first asks WHY (with examples). The reason decides:
  food not ready → "how many more minutes?" (the 10/20 rule); customer not answering / address not found → the
  customer's number; restaurant closed, item missing, damaged … → their own steps; anything else (bike broken, sick,
  too far) → a teammate, with the reason in the note. No reason within 5 minutes → the team is told.
  A reason in the first message ("I can't deliver, my car broke down") goes to a teammate at once.
* **Customer not answering / "I cannot communicate with the client" / address not found**: the bot now sends the
  customer's number + street/house number + notes straight away (his own live order only), then 10 minutes →
  a teammate. Still no answer after the number → a teammate (no second number). No number available → the tips.
* "My bike is broken" is the rider's vehicle, not damaged food.
* The AI returns a `reason` field (why he can't continue / wants to cancel, "" when none given).

## 9.0.2 — number + house number + notes when the app call fails

* When a rider can't call the customer through the app, the bot's answer now carries the customer's **number,
  street + house number and the customer's delivery notes** (floor, bell, entrance). Never the name, never city/zip,
  and still only to the rider the live order is assigned to.
* Number, street, notes and order number are put in **after** translating, so they arrive exactly as in MotionTools
  (a street name is never translated). If a translation loses one of them, the rider gets the English text instead.
* No notes on the order → the "Customer's notes" line is left out (no empty label).
* Check where your bookings carry the notes: dashboard → `/api/debug/booking-shape` (shows field names only).

## 9.0.1 — fix: the bot crashed on every real rider message

* **Crash fixed**: rider messages that came in through the Intercom webhook (riders who already had a chat history)
  failed with `'dict' object has no attribute 'strip'` and were forwarded with an "automation error" note, unanswered.
  The stored chat was passed to the bot instead of the rider's messages. Regression test drives the real webhook path.
* **Teammate already in the chat** (wrote there in the last 30 min): the bot stays silent. Only "can't call through
  the app" adds an internal note, and an accident / danger message still gets the 112 answer.
* **Forwarding no longer takes a chat away from a teammate**: if a teammate has the conversation, the bot only adds
  its note and re-opens it; it re-assigns only chats nobody has.
* The bot's own earlier answers are recognised also after a restart and when Intercom returns them HTML-escaped,
  so they are never mistaken for a teammate.
* "Customer says he ordered nothing" → customer refuses (it was read as a phone question). Answer text: stay with the
  customer, a teammate is checking.

## 9.0 — the smart rider bot (training pack + AI)
**What changed for riders**
- Answers come from the Quickzi training pack: one clear next step, simple words, no username greeting, never "the order
  will be cancelled", never a customer's name or address.
- **Phone rule:** "call from the app" first → "check your own number in the app profile" → only when calling through the
  app does not work, the bot sends the customer's NUMBER — only to the rider the order is assigned to, only while it is
  live. A customer who just doesn't answer: keep calling 10 min, then a teammate takes over automatically.
- **Food not ready:** asks how many more minutes; up to 10 → "please wait" (and asks again when the time is up); more,
  20 min waited in total (from the MotionTools data), or the rider insists → a teammate decides.
- New situations: order not at the restaurant / taken by another rider, item missing, Liefercode refused, address not
  found, app call not working, customer refuses, accident (→ "call 112 if anyone is hurt").
- "?", "Hlooo", "Reply me bro": ONE short "still with you" (max every 5 min), and the team gets an urgent note.

**How it understands**
- Messages within 6 seconds are read together (`BOT_BURST_SECONDS`): "Tkbt6q · Hlo · Brother, he is not giving the code" =
  order TKBT6Q, Liefercode problem → one answer, not three.
- **AI (brain.py):** Claude Haiku 4.5 reads the burst + the order status (no customer data) and returns the situation,
  order number, minutes waited / still needed, cancel wish, the rider's language and a one-line English summary.
  Keyword rules (improved with the real chats) take over when the AI is off, unsure or unreachable.
- **Answers in the rider's language:** the approved text is translated (same meaning, nothing added). Off-switch on the
  Intercom page. Team notes stay in English with the AI summary on top.
- Cost: about 0.3 cents per rider question (Haiku 4.5: $1 / $5 per million tokens) — about $3 a day at 1,000 questions.
  Today's calls and cost: Intercom page → Bot brain, and /health → "ai".

**Intercom page → Bot brain:** wait limits (10 / 20 min), hand-over time for unreachable customers (10 min),
"answer in the rider's language", and free-text team notes the AI reads. All answer texts are editable (one field,
simple English). Texts edited for v8 are reset once (they described the old customer card).

**Safety:** the bot is OFF on a new install until the master switch is turned on. Tested: the real chats of 11 Oct
(with and without AI), 300 riders × 3 messages at once (one answer each, event loop never stalled > 51 ms), smoke,
repeats, webhook fields and the signed chaos test.

## 8.3 — checked against MotionTools' announcements (changelog + all deprecations)
- A booking whose drop-off FAILED now completes automatically in MotionTools (announcement 2026-07-01, "failed stops are
  terminal"); that final `done` no longer counts as a delivery.
- `stop_failed.outcome` (announcement 2026-07-15) drives the result: re-attempt = still live, skip/return = not delivered.
- The deprecated user field `active_hailing_booking_ids` (2026-03-11) is no longer read; `active_tour_id` is used.
- Nothing else in the deprecation list touches this system: no `hailing_booking.*` webhooks (removed 2025), no
  earnings, failure-reasons, preferred-drivers, fleet-management or route-optimization endpoints.

## 8.2 — every webhook field read the way docs.motiontools.io defines it
- **Failed delivery that MotionTools will re-attempt** (`stop_failed` with outcome `reattempt_now/later`) stays live; only
  `skip_recovery` / `return_*` close it as cancelled, with MotionTools' reason ("delivery failed — recipient unavailable").
  A later successful drop-off turns it into delivered. Before, every failed attempt counted as cancelled for good.
- **ETA to the customer** comes only from the drop-off stop — a `task` or `return` stop's ETA no longer overwrites it.
- **`tour.force_unassigned`** (dispatcher takes a tour away from a rider) now removes the rider from the order
  ("taken away by dispatcher" in the order story).
- Endpoint checks use `view=minimal` (docs: "prefer the compact or minimal view to keep load on the backend minimal");
  the booking detail keeps `standard` because only it has the event timeline and the rider's position.
- Restaurant address from the place endpoint's `street / number / zip_code / city`.
- /health → `events_by_type`: how many events of each kind arrived (counts only) — shows at a glance whether MotionTools
  sends rider (driver.*) and tour (tour.*) events to the webhook.

## 8.1 — MotionTools' API and webhook rules, all kept (checked against MotionTools' openapi.json)
- **Repeated deliveries change nothing.** MotionTools delivers webhooks "at least once". Every event id is remembered
  (≈ 1½ days, also across restarts), and a repeat is ignored and counted in /health → `webhook.repeats`. Before, a late
  repeat of "pickable" counted as a hand-back and a repeat of "created" brought a delivered order back as live.
- **Signature check** (relay v3): `X-Mtools-Signature` (Ed25519) is verified with the public key from the MotionTools
  webhook page (relay variable `MT_WEBHOOK_PUBLIC_KEY`). Unsigned or forged calls are answered 200 but never reach the
  dashboard. Safe switch-on: "checking" until 20 real signatures matched, then "enforcing".
- **Only documented, read-only endpoints**: `/api/hailing/bookings`, `/api/hailing/bookings/{id}`, `/api/users`,
  `/api/users/{id}`, `/api/places/{id}`, `/api/user` — GET only, API token without X-Client-Version. The undocumented
  `/api/bookings…` paths are gone.
- **No list polling**: webhooks are the source of truth (MotionTools: subscribe instead of polling lists); the API only
  fills names, phones and addresses within the hourly budget. An endpoint answered "restricted" is asked again once a day
  (before: every 30 min).
- **/health no longer shows rider messages** (name + text were visible to anyone with the link); it shows the count.
  The messages stay on the logged-in Intercom page.
- Tested: chaos test with signatures on (dashboard killed, redeployed, frozen 45 s, killed again): 30,695 calls all 200,
  1,080/1,080 orders, 1,074/1,074 delivered, 218/218 forged calls refused, 0 orders wrongly cancelled; 1,500 live orders
  at 116 events/s: 0 failures, webhook p99 41 ms, ~700 MB.

## 8.0 — final: the webhook never fails, no order event is lost
**Required setup (once):** MotionTools must send to the relay, not to the dashboard. Only then can a dashboard
restart, crash or freeze never reach MotionTools.
1. Railway → New → GitHub repo (this repo) → service settings: Start command `uvicorn relay:app --host 0.0.0.0 --port $PORT`,
   Watch paths `relay.py`, Healthcheck path `/health`, Networking → Generate domain. No variables, no volume.
2. Check `https://<relay-domain>/health` → `"ok": true`.
3. MotionTools → Settings → Webhooks → create a NEW webhook: URL `https://<relay-domain>/mt/<same secret as today>`, the same
   events and the same `customer_id` filter as the current one, Active. Then switch the old webhook off.
4. The dashboard header shows "· via relay ✓"; an amber banner says "Not protected" while MotionTools still sends directly.

What changed:
- **Write-ahead log:** every order event is written to disk before the 200 goes out (relay batches and direct calls);
  a crash a second later loses nothing — the start-up repair rebuilds it, including orders the live board never saw.
- **Clean stop:** on a deploy the server finishes its queue before it exits.
- **Mode remembered:** after a restart, events the relay hands over are applied at once (before: ignored for the first
  seconds until the API probe finished — orders created during the restart could go missing).
- **Always 200 to MotionTools** on the direct URL too: a wrong secret or a broken body is counted, never an error;
  GET/HEAD probes answer 200.
- **Relay:** accepts any body, answers in ~1 ms, hands over what is waiting when it is itself stopped, `/health` shows
  `waiting`, `oldest_waiting_s`, `last_error`.
- **Chaos test passed:** 30,704 calls from "MotionTools" at ~150/s while the dashboard was crashed (25 s down),
  redeployed, frozen for 45 s and crashed again: 30,704 × 200, relay p99 3.9 ms, 1,099 orders created → 1,099 in the
  database, 1,096 finished → 1,096 delivered. 0 lost.

## 7.0.2 — the last start-up pauses from /health
- No forced garbage collection after reports (a full collection on a big heap paused every thread, the event loop
  included, for up to 5 s); long-lived start-up data is frozen out of the collector (`gc.freeze`).
- Settings are kept in memory (read on every rider message and every minute) and refreshed on every change.
- Settings → System "delete day" and the shift-sheet upload run in a worker thread (delete-day held the server 5 s).
- Naming a city relabels its stored orders in the background, in slices.
- Restart test (10 000 orders/day copy): webhook p99 < 0.3 s during the start-up repair, longest pause 0.6 s, 0 failures.

## 7.0.1 — the 74-second freeze found by the watchdog
- The watchdog named it: the Intercom page's "Conversations in progress" looked up each rider's order by parsing
  EVERY order of the day (all cities) — once per rider, on the event loop → server frozen 74 s.
  Now one indexed query per rider (`ix_orders_rider`), 40 riders in 5 ms; the Intercom page is built in a worker thread.
- Same fix for every rider message (the bot looked up the rider's order and any order number the same way) and the
  bot's timers: 2 ms per message.
- Order-number lookup uses the index (was a scan of every order ever stored); rider panel reads only that rider's
  orders; Settings → places counts in SQL; insights shared and cached for all pages.

## 7.0 — built for 10 000+ orders a day
Load-tested on a copy with 25 cities: 10 000 orders today (1 000 live at once, 1 500 riders), 280 000 orders of
history (30 days, 1.2 GB DB), GPS 50/s + new orders 5/s, six dispatchers polling, every report page opened.
- **Readers no longer block the webhook.** Before: one DB connection + one lock — a month report held the lock and
  the event loop (and MotionTools' webhook) waited up to 46 s. Now every thread reads through its own connection (WAL),
  only writes take the lock. Backups copy through their own connection; big deletes/archiving run in 2 000-row slices.
- **Reports are cached** (stale-while-revalidate): first computation once, then instant; an expired report is served
  at once while a fresh one is computed in the background; max two big computations at a time; common reports are
  pre-computed after a deploy. Any change (reasons, fleets, shifts, settings) clears the cache.
- **Today's numbers** on the City page are no longer recomputed on every event (was: 10 000 orders parsed per refresh).
- Faster parsing (orjson, cached timestamps), restaurant waits computed once per order, memory handed back after big reports.
- Fixed: rare 500 on /api/state when an event arrived during a refresh (`KeyError: 'by_city'`).
- Results: webhook p99 47 ms (max 0.3 s) under full load, 0 failed deliveries, longest loop stall 0.14 s (was 46 s);
  City page all cities 0.16 s (was 2.2 s); cached reports 5–25 ms; memory ~650 MB (was 1.1 GB); restart: answering
  after 3.5 s, webhook p99 < 0.6 s during the start-up repair.
- Railway: volume ≥ 20 GB recommended at this volume (one DB backup is kept; raw order details archived after
  `ARCHIVE_DAYS`, default 45). New dependency: `orjson` (requirements.txt).

## 6.9.1 — watchdog
- A watchdog thread writes `QUICKZI EVENT LOOP BLOCKED …s at: <file, line>` to the Railway log (and /health →
  `loop_blocked`) whenever the server stops answering for 3 s+, so a freeze names its exact cause. /health also shows `memory_mb`.
- Intercom thread file is written at most every 10 s (before: the whole file on every message, on the event loop); 100 messages kept per rider.

## 6.9 — master switches, safe city list, relay without variables
- Intercom page → **Intercom automation**: two master switches. *Bot answers rider queries* (off = the dashboard writes
  nothing in rider chats, no timers, no order-number question) and *Rule messages* (off = no automatic rule message to
  any rider). Switching off asks for confirmation.
- Cities for the rules: **nothing ticked = no rule messages** (before: nothing ticked = all cities, so one empty save
  messaged riders in all 25 cities). "All cities" is now an explicit choice (with confirmation).
- relay.py needs **no variables**: it takes the secret from the MotionTools URL and always answers 200 — a wrong or
  missing secret can no longer get the webhook blocked; it shows in the relay's /health instead.

## 6.8.4 — big pages no longer freeze the server
- JSON for the heavy pages (City, Orders, Riders, Fleets, Insights, Daily, Settings, System …) is built and gzipped in a
  worker thread. Before, FastAPI encoded it on the event loop: "Orders · this week" for 25 cities (14 MB) froze the
  server for seconds — pages hung and MotionTools' webhook calls timed out (→ webhook switched off).
- Orders list: newest 2 500 rows for week/month (the count of all is shown; search + CSV export still cover everything).
- Measured with 21 400 orders: Orders·week 3.3 s → 0.4 s; longest webhook stall during it 2.2 s → 0.16 s.

## 6.8.3 — no reminders into closed conversations
- Before any timed message or note (holding reply, "rider waiting X min", photo/reason timeouts, 15-min customer check)
  the dashboard reads the conversation: closed, snoozed, or a teammate wrote/closed after the rider's last message →
  nothing is sent and the flow ends. Only a new message from the rider starts it again.

## 6.8.2
- relay.py also works while the dashboard is still on an older version (falls back to single events).
- "Received from riders" shows what the bot finally understood (Claude), not the first keyword guess.
- "Is anyone there? / هل يوجد احد هنا" = rider waiting → holding reply + urgent forward. Car / bike / battery trouble →
  "can't do the delivery". "Nobody at the restaurant" → restaurant closed (photo).

## 6.8.1 — no empty customer card, "?" means "I'm waiting"
- Number not in MotionTools data → no card with "siehe App": the rider is told a dispatcher sends the number now, and the
  conversation goes to the team as URGENT.
- "?", "??", "und?", "hello?" after the bot answered = the rider is waiting → holding reply + urgent forward (never "How can we help?").
- A rider's messages are handled one after another (a question and a quick "?" no longer race each other).
- "not here … send me his number" is no longer read as "restaurant has no such order".

## 6.8 — webhook relay (25 cities, updates without losing events)
- New file **relay.py**: a second, tiny Railway service that receives the MotionTools webhook, answers instantly and
  forwards events in order (batches of 300) to the dashboard at `/mt/<secret>/batch`. While the dashboard restarts
  or is busy, events wait in the relay and arrive afterwards — MotionTools never sees a failure, so it can no longer
  switch the webhook off.
- Setup: Railway → New → GitHub repo (same repo) → service Settings: Start command `uvicorn relay:app --host 0.0.0.0 --port $PORT`,
  Watch paths `relay.py`, Networking → Generate domain (no variables needed since 6.9). Then in MotionTools change the webhook URL to
  `https://<relay-domain>/mt/<WEBHOOK_PATH_SECRET>`. Check `https://<relay-domain>/health` → `waiting` should be ~0.

## 6.7.3 — webhook blocked warning after 5 min
- With 10+ live orders the "No events from MotionTools" banner appears after 5 silent minutes (was 15).
- **Install rule:** upload all files in ONE commit (GitHub → Add file → Upload files → drop all files → one Commit). Every commit is a Railway redeploy; while the server restarts MotionTools' calls fail, and after 250 failed calls MotionTools switches the webhook off.

## 6.7.2 — "Received from riders"
- Intercom page → **Received from riders**: every rider message Intercom delivered, and what the bot did (identified / NOT identified → asked order number / understood as … → reply / error).
- Workflow button presses (empty text in the webhook) are now read: the button label is used, or the conversation is fetched to get the rider's text.
- `/health` → `intercom_received` shows the last 10.

## 6.7 — no rider is left talking to a wall

- A forwarded conversation stays with the bot's "silence" only while a teammate is really on it: 5 minutes to
  answer, or as long as a teammate has replied. Nobody answered → the bot takes the next message itself, and after
  7 minutes of waiting the rider gets "a dispatcher has been alerted…" and you get an urgent ⏰ note.
- With ANTHROPIC_API_KEY, Claude reads every real sentence first (any language, slang, Hinglish), with the rider's
  previous message as context; the keyword lists are the fallback.
- New situation "remove the order" (remove krna bhai / hatao / احذف الطلب / Auftrag entfernen): acknowledged at once,
  urgent note to you, a repeat gets the holding reply instead of the same text.
- More phrases: "cannot make the delivery" (Arabic), "door does not open", "restaurant does not have this order".

## 6.6.2 — the bot stops talking when a person takes over

- Once a conversation is forwarded to a person, the bot stays silent in it for 3 hours (only accident/injury still adds an urgent note).
- The bot never sends the same answer twice within 30 min — if the rider insists, the conversation goes to a person.
- "How can we help?" at most once per rider per 3 hours, and never when the rider already wrote a full sentence.
- Riders talking about an order they just finished (photo won't upload, "complete this order") are matched to it.
- Optional: set `ANTHROPIC_API_KEY` in Railway and messages the keyword rules can't place (any language) are
  classified by Claude Haiku into one of the known situations. Without the key nothing changes.

## 6.6 — per-rule cities, riders matched automatically, faster with all cities

- Each rule has its own 🏙 city picker on the Intercom page (empty = the default list / all cities). Rider
  queries are answered in every city regardless.
- A rider who writes first (never messaged by us) is recognised through his MotionTools profile link in Intercom and
  answered; new riders on the board are matched with Intercom automatically every 10 minutes.
- Start-up no longer blocks the pages (no housekeeping/VACUUM/backup at start, repair deferred and batched); webhook
  events yield to page requests; `/health` lists slow requests.
- Service areas are named automatically from their coordinates (OpenStreetMap), no more 8-character ids.

## 6.5 — the Intercom page: rider queries answered, cities, DE/EN texts

**One new file: `replies.py`** (GitHub → *Add file → Upload files*). The other 12 are replaced as usual (13 files now).

New header item **Intercom**:

- **Cities** — tick the cities whose riders receive automatic messages. Nothing ticked = all cities. Rules still
  evaluate everywhere (alerts stay on the board); only the message is held back elsewhere.
- **Conversations in progress** — which rider the dashboard is currently handling, in which situation, what it waits
  for (timer), and a *Take over* button that hands the conversation to your inbox.
- **Rider queries** — what the dashboard answers itself when a rider writes (any conversation, any language the
  Intercom workflow buttons are translated into):
  - *order not ready* → asks the rider to stay and to name the minutes; "10" sets a timer, "ok" keeps waiting,
    "no / can't wait" gets one persuasion message (10 more minutes?), a second no or silence goes to your inbox as
    urgent; 20 min at the restaurant always goes to you. The wait counts as the restaurant's (PTOD excused).
  - *restaurant closed* / *order damaged* → asks for a photo, forwards it with a flag on the order.
  - *restaurant has no such order / another rider took it* → forwarded at once with every rider who accepted it.
  - *can't do the delivery* → "why?" → the reason is forwarded with the rider's other orders.
  - *customer number / can't reach / can't find* → the **customer card** (ref, name, phone, address, delivery notes,
    map pin) with a closing line for the situation; the next message from the rider is forwarded with the time at
    the door. No phone in MotionTools data → you get a note to look it up.
  - *customer doesn't accept* → "what exactly?" → forwarded.
  - *forgot to finish in the app* → deliver + handover photo → forwarded "finalize".
  - *hello / unclear* → "How can we help?" → the next message is forwarded with the order attached.
    The rider is **never asked for the order number** — the note to you carries ref, status, restaurant, address.
  - *ok / arrived / on the way* as a reply to an automatic message → "Thanks!", nothing forwarded.
  - accident / injury / police anywhere → forwarded as urgent immediately.
  Every query has an on/off switch and a German + English text; *Reset* restores the default. "Check" shows what
  the dashboard would understand from any message you type.
- **Rules** — the automatic messages as before, texts now as two fields (DE / EN) with Reset and Send test.
- **Log today** — everything sent, received from riders, forwarded or failed, filterable.
- **Riders ↔ Intercom** — matched count, unmatched names, *Match all*.

Forwarding = the conversation is taken out of the Automation team, assigned to the sending teammate, opened, and a
note is added (what we sent, what the rider wrote, order context). Auto-close is off by default (Settings).
Orders carry a chip in the side panel when a query flagged them (restaurant closed?, customer unreachable, …).

## 6.4 — more rider automation, answers to rider replies, month filter
- **Rules added**: at the restaurant by GPS but not marked arrived · left the customer without marking delivered ·
  no GPS for 10′ on an order · order waiting near a free rider (≤ 1.5 km, nearest two) · riding to a restaurant where
  others already wait 15′+ · 10 on-time deliveries in a row · thanks after a hand-back the kitchen caused · end of day
  (60′ after the last delivery, from 20:00) · handed back without going to the restaurant · took over an order that is
  already late · double order in the wrong sequence · waiting 15′ at the restaurant · 10′ at the customer.
  "Behind the planned time" is skipped when the kitchen was slow or the rider reported the restaurant late.
- **Replies from riders** (needs the Intercom webhook): "customer not answering / number?" → the dashboard answers in
  the same conversation with the customer's phone and address (fetched from MotionTools once if unknown);
  "restaurant late / not ready" → the wait is counted as the kitchen's and the rider gets a thank-you. Both are rules
  with their own switch and text.
- **Automation inbox**: Settings → Intercom automation texts → "Inbox for automatic messages" = an Intercom team name;
  rule messages then go into one conversation per rider and day in that team inbox, closed, re-opened by a reply.
  Manual messages stay in the rider's normal chat.
- **Orders tab**: Month period. All default texts are German + English.

## 6.3 — the designed screens, on real data
**One new file this time: `intercom_msg.py`** (GitHub → *Add file → Upload files*). The other 11 are replaced as usual.
Nothing changes for existing data; the two new tables (shift sheet, hand-backs) are created on the first start and the
hand-backs of the last 90 days are indexed in the background.

- **Overview** — the fleet-partner numbers only: delivered today (vs the last same weekday by this hour), on time
  (Lieferando scoring), avg delivery, riders with orders now, deliveries per rider, cancelled, live orders, cities.
  One tile per city (critical → strained → ok), the 14-day on-time line, orders per hour today vs the forecast
  (average of the last 4 same weekdays — bars turn red when today is 10 % under), and the riders table from orders +
  the shift sheet (no-shows, first order late, working less, left early, delivering as usual).
- **City** — the live board in the v2 layout: tiles, the **pipeline** (one card per stage, one dot per order, tap to
  filter), *Needs action* (PTOD clock counting live, order + rider + restaurant, the alert and what to do, **Msg / Call /
  10′ / ✓**), *All live* (MotionTools-style groups: waiting · active · on hold, with "1 of 2 stops · 3 min to last
  stop"), *Done today*. The side panel shows the selected order: PTOD, ETA / plan / vs plan, alerts, rider card with
  Message · Call · WhatsApp · Restaurant · Map, the story, reason chips, Snooze / Mark handled, raw events. Below it the
  **live map** (OpenStreetMap): riders on orders, free riders, orders waiting for a rider, restaurant and customer
  stops; tap a row to highlight its route. The red bar lists the free riders nearest to a waiting order. Riders panel:
  Free now · Needs a check (no event 60′, not moving, in sheet without an order) · On orders, and *Broadcast to riders*.
  Keys: **J / K** move, **M** message, **H** handled, **S** snooze, **Esc** close.
- **Riders** — riders in sheet, delivered, no order yet, first order late, working less, deliveries per rider, fleet on
  time; per rider: status now, shift (sheet), first → last order, deliveries, usual (avg per worked day in the 4 weeks
  before), per working hour, on time, PTOD, accept, handed back (excused ones not counted), double, **score 0–100**;
  filters (no order yet · first order late · working less · score < 60); deliveries-per-day heatmap for the last 7
  days; reliability by city (shift sheet vs orders: fulfilment, no order, first order late); the Intercom automation
  rules with on/off switches and the log of what was (or would have been) sent.
- **Fleets** — one tab per MotionTools organization with its score; riders active/total, deliveries, per rider-day,
  on time, avg delivery, accept, hand-backs, fleet score (all vs the network); on time fleet vs network over 14 days;
  where the fleet loses minutes (phase minutes vs network); rider scores per band; the all-fleets table with
  sparklines; every rider of the fleet with days worked / planned, flags ("no order on 6 planned days", "coach:
  accept + restaurant wait") and the change vs last week. **🖨 Weekly scorecard** prints the page (PDF via the browser).
- **Rider score** (0–100, needs 3 deliveries): *Customer 40* = on time for the customer 25 · PTOD vs target 10 ·
  handover 5. *Productivity 30* = deliveries per working hour (first accept → last delivery, per day) vs the average
  rider of the same period and city. *Reliability 30* = acceptance 12 (≤ 2′ full, −2 per minute) · hand-backs 10
  (−4 each, excused ones not counted) · shift sheet 8 (planned days without an order, first order > 45′ late).
  A rider in the sheet with no order at all on a planned day scores 0.
- **Shift sheet** (Settings → Shift sheet): CSV from Excel — columns *rider* (name as in MotionTools or the id),
  *date*, *start*, *end*, optional *city*, *fleet*; German or English headers; one row per rider and day.
  Names are matched to the riders seen on orders ("Last First" works too); unmatched names are kept and linked
  when the rider appears. Re-uploading the same days replaces them.
- **Tour of the selected order** (City page): the side panel shows 🏍 rider (last position, moving or not) → 🏪 restaurant
  → 🏠 customer with the times; a step turns green when the rider has reached it. The map shows the same three points
  with a dashed line and dims everything else. Restaurant positions come from the place API when it is open and are
  otherwise **learned the first time a rider arrives there** (his GPS position at the arrival event, kept per
  restaurant); customer positions from the booking detail or the rider's position at arrival.
- **Any single day**: Orders, Riders, Insights and Fleets have a date picker next to Today / Yesterday / Week — pick a day and
  every number on the tab is for that day (the daily report has its own date arrows).
- **Light / dark**: the ◐ button in the header; dark is the default.

## Intercom — messaging and automation (6.3)
`intercom_msg.py` (from the Live board v2 package) does the talking: every rider is an Intercom user with
`external_id = rider:<MotionTools id>` (found by that or by phone, created if missing); the first message is an in-app
message that opens a conversation, later ones are replies in it; rider replies arrive through the Intercom webhook
and show in the **Messages** drawer (header button, unread badge). Threads are kept in `DATA_DIR/intercom_threads.json`.

Railway variables: `INTERCOM_TOKEN` (Developer Hub → your app → Authentication) is enough — messages then go out as the
teammate who created the app. Optional: `INTERCOM_ADMIN_ID` to send as another teammate (`GET https://api.intercom.io/admins`
lists them), `INTERCOM_REGION` (`eu` default, `us`, `au`).
Intercom webhook for replies: `https://<railway-url>/intercom/<WEBHOOK_PATH_SECRET>`, topics
`conversation.user.replied` and `conversation.user.created`. Without the token everything still works — the Msg
buttons report "Intercom not configured" and the rules run in **dry-run**.

**The five rules** (every minute; texts editable in Settings → Intercom automation texts; switches on the Riders page):

| Rule | Fires when | Limit |
|---|---|---|
| In sheet, no order 30′ after shift start | shift sheet row, shift started 30′ ago, no order today | once; one reminder after 90′ |
| Late for the customer | live order: ETA > planned time + grace, or plan passed and not delivered | once per order |
| Accepted, not started | accepted ≥ start limit (3′) ago, no tour started | once per order |
| Idle while orders wait | no live order, last delivery ≥ 45′ ago, shift not over, an order in the city waited ≥ 3′ for a rider | once per 45′ |
| Morning scorecard | 10:00–12:00, every rider who delivered yesterday | once a day |
| Alert → message | any open board alert of that kind on an order with a rider: not moving, behind ETA to the restaurant, waiting at the restaurant, wrong direction, at the customer without handover, PTOD at risk (thresholds = Settings → Alert thresholds); nothing when the dispatcher marked it handled | once per order and kind |

Guardrails: quiet hours 23:30–09:00 (only the two live-order rules may send), max 3 messages per rider per day
(scorecard excluded), nothing while MotionTools is silent, every message logged on the order story and the Riders page.
"Test → me" in Settings sends one rule's text to one rider id.

## Two ways to get the truth from MotionTools — chosen automatically
- **API mode** — the token may read bookings: every 30 s all active orders (full timeline) + all riders (online, GPS).
- **Webhook mode** — MotionTools has the account in *restricted API mode* (list endpoints answer 403
  `restricted_endpoint`). Orders are then rebuilt from the events MotionTools pushes to the webhook.
  On top of that the server **probes every endpoint itself** (startup, hourly, and the *Re-check now* button in
  Settings) and uses whatever is still open:
  - `GET /api/bookings/{id}` open → restaurant, address, phones, ETAs and rider GPS are filled in automatically
    (every new order, and every live order once a minute).
  - `GET /api/users/{id}` open → rider names + phone numbers.
  - `GET /api/places/{id}` open → restaurant names.
  - nothing open → the dashboard still works on order ids; restaurant names and rider phones can be typed in Settings.
  Restricted accounts get a small **hourly quota** per endpoint (429 `restricted_rate_limit`): the client stops
  calling that endpoint until the next hour and spends the quota on dispatched orders with open alerts first.
  Paths that answer 404 on this tenant (the documented `/api/bookings…` ones) are never asked again.
  The moment a list endpoint opens the server switches back to API mode on its own — no redeploy.
  On startup, every order of the last 7 days (live and delivered) is rebuilt by replaying the stored raw events —
  dispatch moment, rider history, timestamps — so reports made before an upgrade are correct too.

## Two clocks per order
- **PTOD** (internal): dispatch → delivered, target 30 min (Settings). Starts when the order is released to riders.
- **Plan** (the customer's time): the delivery time MotionTools calculated when the order came in — the first
  ETA for the customer stop; for a pre-order that is the scheduled delivery time. It never moves afterwards (later
  ETA recalculations are the *current* ETA, not the plan). **On time** = delivered no later than plan + grace
  (5 min, Settings). This is the closest thing to what Lieferando scores: was the customer served when promised.
  Shown as a pulse tile, per rider, per hour, per late order, in the daily brief and in the CSV
  (`planned_delivery`, `min_vs_plan`, `on_time_plan`). Orders whose plan is unknown are simply left out of the %.
  Alert **Behind plan**: amber when the current ETA is later than plan + grace, red when the planned time has passed.

## Tabs
- **Live** — city pulse, **PTOD watch** (live orders at/over the warning time as one strip of chips), then the
  live orders in three views: **Needs action** (default — only orders with an alert, one compact row each, red
  first, then by PTOD; the on-track orders are a single line at the bottom with *Show them*), **All live** (every
  order as a row, longest PTOD first), **By stage** (the card board). Above the list, one chip per stage with its
  count and how many of them have an alert — tap a chip to see only that stage, in any view. The *Waiting for
  rider* chip names the riders who are idle right now with their call button. Tap any row/card for the story
  (Call / Map / Snooze / Handled / reason). The view you pick is remembered on that device. Riders: busy first,
  idle and offline capped with *show all*.
- **Orders** — every order of the day, searchable, with filters (Live · PTOD risk · Late · Late for the customer ·
  Late without reason · On hold · Delivered · Cancelled) and a *Plan* column (+/− minutes vs the planned time). Tap → the order's story: minutes per phase, every rider, alerts, route, and a
  **reason box** ("Restaurant late", "No rider available", …) — reasons are counted in Insights and the daily brief.
- **Riders** — leaderboard (best 3 / coach next, riders with ≥3 deliveries), then deliveries, % within target,
  % on time for the customer, avg PTOD, delivery minutes per order, minutes per phase, km/order, double orders,
  hand-backs, alerts — all from the orders themselves. Hours online / busy % / idle are shown only in
  API mode (webhook mode cannot know online time reliably). Tap a rider → numbers vs the team.
- **Insights** — where the minutes go, focus list, **riders needed — next 7 days**: one line per day (orders/day,
  peak hours, riders at the peak, rider-hours) and, per selected day, the hour-by-hour plan. Weekday-aware: a
  Saturday is planned from the previous Saturdays (up to 4 weeks); until two of that weekday are recorded, the
  last 7 days are used. Per hour: orders ÷ capacity (orders one rider really delivers per hour, Settings, default
  1.5); where acceptance was slow with N riders, at least N+1. *Plan CSV* exports the week for the shift sheet.
  Then staffing hour by hour, restaurants by rider wait (each row shows
  its order numbers; tap → every order of that restaurant with wait/PTOD/alerts, and a box to name an unnamed
  MotionTools place — the name is applied to all its past orders too), districts by postcode, late orders, alert log.
- **Daily report** — 14-day trend, frozen report per operating day (midnight → midnight Berlin; Settings →
  *operating day starts at* moves it, e.g. 4 so night orders count for the evening before), team briefing text (includes the
  on-time % and tomorrow's riders for the peak hours), CSV.
- **Settings** — alert thresholds; restaurant names (by MotionTools place id); rider phone numbers;
  system panel: mode, event counts, **which MotionTools endpoints are open**, log, raw samples;
  **delete one day's data** (for a day that was recorded wrongly: check first, then confirm — live orders are kept).

## Riders are counted from orders only
"Riders on orders" = distinct riders holding a live order; "delivered today" = riders with at least one delivery.
There is no "online" count: MotionTools' online/offline events are unreliable for this (no event when the app is just
closed) and arrive for every rider in the account, other fleets included. Riders you never see on a Quickzi order are
never counted. A rider whose live order had no event for 60 min is flagged "no event 60′+" instead of counted as busy.

## Order stages (Live tab board, always in this order)
Waiting for rider · Accepted, not started · Riding to restaurant · At restaurant · Delivering · At customer · **On hold**.
On hold = created by Lieferando but not yet dispatched by MotionTools (pre-orders, often hours ahead): no PTOD, no alerts.
**PTOD starts when the order is released to riders**, not at creation. MotionTools' "pickable" event is the exact
moment; when it is not sent, the release is derived from the planned delivery time (ETA − *release lead*, the
MotionTools automatic-scheduling setting, editable under Settings, default 45 min). ASAP orders are released at creation.
Alerts resolve themselves when the condition ends or the order is delivered/cancelled; a live order without any
MotionTools event for 3 h is closed automatically ("Closed (no events)") so nothing stays stuck on the board.
GPS-based alerts (not moving / no GPS / wrong direction) only fire for riders we actually receive positions for.

**Double orders** (two bookings in one tour): each order keeps its own PTOD clock from its own dispatch. The order
whose next stop has the earliest ETA is the one the rider is doing now; the other is *queued* — shown on the card
("double with X — rider is doing X first") and exempt from riding/waiting alerts until it is the current one.
**Waiting at the restaurant when a rider handed back**: three measures. *Kitchen wait* (Restaurants table) = first
arrival of any rider → pickup, hand-backs included, plus "gave up" = riders who handed back after waiting. *Own wait*
(Riders table) = only that rider's minutes, including before a hand-back. A hand-back after waiting at least the
restaurant threshold (8 min) is *excused* — the kitchen's fault, not counted against the rider. The alert on the live
board always uses the current rider's own clock.
**Redispatched orders**: every order keeps a rider-by-rider history — accepted by A, arrived at restaurant (A),
handed back by A, accepted by B, picked up (B), delivered (B)… The story shows all of it, the card says
"redispatched 2×", the Riders tab counts hand-backs per rider, and the phase minutes belong to the rider who
actually delivered (a hand-back resets accepted/started/arrived for the next rider).

## Alerts (PTOD clock starts at dispatch; thresholds editable in Settings)
No rider (5 min) · accepted but not started (3 min) · not moving / no GPS (4 min) · wrong direction (400 m) ·
late to restaurant / customer (5 min behind ETA) · waiting at restaurant (8 min) · waiting at customer (5 min) ·
PTOD at risk (25 min or ETA projects > 30) / breached (30 min) · behind plan (ETA later than the planned time + 5) /
plan missed · rider offline with an order.

## Webhook watchdog
MotionTools pauses a webhook when our server answered with errors for a while (it happened when the volume was full).
In webhook mode the dashboard therefore shows an orange banner — and the status dot turns red — when **no event has
arrived for 15 minutes while orders are live** (or riders are online between 11:00 and 23:00): "check in MotionTools →
Settings → Webhooks that the webhook is still active". It is logged in Settings → System, and `/health` shows
`silent_min` / `webhook_silent` so an external uptime check can read it too.

## Files
`app.py` server · `mt.py` MotionTools client (multi-path, self-probing) · `events.py` webhook projector ·
`orders.py` phases + alert rules · `store.py` SQLite + analytics · `dashboard.html` UI · `intercom_msg.py` Intercom messaging (6.3) · `replies.py` rider-query flows (6.5) ·
`simulate.py` API-mode evening · `simulate_webhook.py` restricted-mode evening (fake MotionTools server) ·
`requirements.txt` · `Procfile`

## Built for peak load
At peak MotionTools sends several GPS events per second. Each event only touches memory; alerts are re-evaluated at
most every 2 s, alert rows are written only when something changed, GPS events never re-write the order, SQLite runs
in WAL mode (no fsync per commit on the slow volume), today's numbers and the Insights are cached for 20–30 s, and
API responses are gzipped. A restart answers within a second — housekeeping, backup and the event replay run in the
background afterwards. VACUUM never runs between 11:00 and 23:00.

## Disk (Railway volume)
GPS events are processed live but never written to disk; other raw events go to daily files (`events-YYYY-MM-DD.jsonl`,
3 days kept). GPS points are stored at most every 30 s per rider and kept 2 days. Housekeeping runs at startup and
hourly (old files, old points, VACUUM when there is room); Settings → System shows the volume usage.

## Railway variables
`MT_API_TOKEN`, `DASHBOARD_PASSWORD`, `WEBHOOK_PATH_SECRET`, `DATA_DIR=/data`,
`MUNICH_SERVICE_AREA_ID` (optional: comma-separated area ids to keep; **empty = all cities**). Optional: `SYNC_SECONDS` (30), `CITY_NAME`,
`INTERCOM_TOKEN` (optional `INTERCOM_ADMIN_ID`, `INTERCOM_REGION` — see Intercom above).

## MotionTools webhooks (required in webhook mode)
Endpoint `https://<railway-url>/mt/<WEBHOOK_PATH_SECRET>`.
1. "Munich Rider Alerts": booking.created, booking.transition, booking.in_progress, booking.stop_arrived,
   booking.stop_completed, booking.stop_failed, booking.etas_recalculated, driver.online, driver.offline,
   tour.created, tour.transition (+ driver.busy if offered).
2. "Munich GPS": same endpoint, filter customer_id = the Lieferando customer, only booking.driver_location_updated.

## Intercom rider bot — lookup endpoint
`GET /api/intercom/customer?ref=WPC4W7` with header `X-Api-Key: <WEBHOOK_PATH_SECRET>` answers
`{found, ref, phone, address, zip, restaurant, restaurant_phone, rider, status, message}` for that order; `POST` on the
same URL first fetches the booking detail from MotionTools when the number is not known yet (one call of the restricted
hourly quota, only when a rider asks). Used by the Intercom Workflow (Data connector) or an external bot — the dashboard
itself does not talk to Intercom.

## Pages
`/dashboard` (any username + DASHBOARD_PASSWORD) · `/export.csv?period=today|yesterday|week|month|YYYY-MM-DD` ·
`/health` (no login) · `/api/network`, `/api/cities`, `/api/system`, `/api/settings`, `/api/riders`, `/api/places`, `/api/probe`, `/api/staffing`,
`/api/riders-page`, `/api/fleets`, `/api/shifts` (GET status · POST CSV · DELETE), `/api/automations`, `/api/intercom/*`,
`/export-events.jsonl` (last raw webhook events, for debugging) (login).

## Test locally
```
pip install -r requirements.txt
DASHBOARD_PASSWORD=test python3 simulate.py                       # API mode        http://127.0.0.1:8020/dashboard
DASHBOARD_PASSWORD=test SIM_OPEN=detail python3 simulate_webhook.py   # restricted mode http://127.0.0.1:8021/dashboard
#   SIM_OPEN=none (everything restricted) · detail (only detail endpoints open) · all (nothing restricted)
```
