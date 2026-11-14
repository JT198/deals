# Deal Finder - to do

Running list. Update it with every change (done items move to the bottom with the date).
Live site: https://deals.alerolabs.ai · code on .76 at /home/jon/dev/deals · `./deploy.sh` (tests, then swap, rollback on failure).

## Needs Jon

- [ ] One-off Telegram reminders on .82: `deals-reminder-1119` (what a bar of 70 sent that week) and
      `deals-reminder-1126` (scoring check due). Delete each timer, service and /opt/deals/data/reminder_*.py after it fires.
- [ ] **Buy box**: set a max price for 4-seat UTVs and zero-turns (Setup -> Buy box); pick models once decided.
      Instant alerts are only as good as the box.
- [ ] **Use the signals**: Star / Fix / Hide on the dashboard, 👍 / 👎 under Telegram alerts. The 10/12 tuning
      works off these; as of 10/10 there are 3 stars and nothing else.
- [ ] Cloudflare Access: deals app Session Duration 24 h -> 1 month (and Global session timeout); optionally add
      Google login so it's one tap instead of an emailed code.
- [ ] Facebook app: saved-search notifications for the top 2-3 models (instant, free, within Facebook's rules).

## Photo-check pilot (decision held at the 10/12 check-in)

- [ ] Decide: full-size photos at fetch time, yes/no questions (dealer/promo graphic, cab/doors, plow, trailer,
      odometer/hour-meter reading), tags only, never score damage (the model hallucinated "collision damage" twice).

## Next scoring check (~10/26)

- [ ] Re-run the outcome study (fresh private listings, sold/pending within 3 days by score band) on two more weeks.
- [ ] Mowers: off-season in October, so cheap ones sit; re-check in spring before trusting mower scores.
- [ ] Sea-Doo touring (GTX / Explorer / FishPro in one family) and 2026 Ski-Doo price newer-below-older; split families?
- [ ] Toro LX500 (a lawn tractor) was filed as a TimeCutter zero-turn - check mower family rules.

## Build ideas (ordered)

- [ ] Mowers: 54% of ads give no model year, so they are compared across all years (score already damped).
      Series matching (11/11) cut false "Great"s 61 -> 32; a year guess from the model number would be next.
- [ ] RZR Pro S 4 sits in the Turbo R 4 family (4 same-age comps); give it its own family once ~15 are listed.
- [ ] Newly promoted listings (bigger engines that used to look overpriced) showed no faster selling in the
      11/11 outcome check (n=28) - re-check with the next scoring review.
- [ ] Ask the model for the price written in the ad text, to recover "$1" / "$123" placeholder listings.
- [ ] Dealer websites as a new-price source for trailers (Marketplace only shows dealers who post there).
- [ ] Cap starred page re-checks (every full scan today) once the shortlist grows.

## Known small issues (from the 10/9 review, not fixed)

- [ ] `usage_adjusted` is inferred from the note text (minor double count on tiny adjustments).
- [ ] `rescore_all` runs one long write transaction (~5 s on 16k listings); fine today, watch it.
- [ ] Craigslist `detail` never returns None, so `record_miss` for Craigslist is dead code.
- [ ] Facebook budget (220/h since 10/11, long lanes stop at 175) cuts the full scan's item pages; the leftovers
      wait for the night window. Blocked twice: 10/01 at ~410 loads/h, 10/11 14:23 right at the 300/h cap (rolling hour; 2 h back-off, Craigslist
      kept going). If it blocks again at 220: drop to ~170, or enter the TorGuard proxy in Setup -> Facebook route.

## Done

- 11/14 ROOT CAUSE of the "Facebook blocked" pauses (walls.jsonl showed it): the control check had tried ZERO
  listings every time. Reading a page stamps a listing as just seen, so the most recently seen listings were always
  the batch itself, all skipped; "nothing to compare against" was then read as a block. A batch that merely ended
  on 3+ deleted listings paused Facebook for 2 h. The 11/06, 11/12 and 11/13 fixes all sat on top of this. Now
  the controls come from outside the batch (up to a day back) and "no control available" never pauses. The session
  renewal and 5-minute cool-off stay as safety nets. Still unknown whether a real item-page block ever happens:
  walls.jsonl will now show it with the controls it tried. Tests 85.

- 11/13 The 11/12 "walled session" theory was wrong: four more pauses overnight (15:19, 23:54, 03:23, 08:29) with
  session renewal in place. Each time the tail of an item-page batch and three known-live listings failed, yet an
  hour after the 08:29 pause a probe read 7 of 8 listings (the miss was a deleted one). Now: when the controls
  fail, wait 5 minutes, renew the session and test again; pause only if still unreadable (`COOL_OFF_SECS`). What the
  failing pages looked like is appended to /opt/deals/data/audit/walls.jsonl (final URL, title, size, login
  redirect) and the log gets a ". item pages unreadable ..." line. **Next step depends on that file**: if
  cool-offs recover, it is a short throttle and the page pace should drop; if not, the block is real. Tests 84.

- 11/12 15:17 Login-token check switched to blocking: Jon's real request verified, audience tag pinned
  (`CF_ACCESS_AUD` + `CF_ACCESS_VERIFY=enforce` in /opt/deals/.env; previous file kept as .env.bak-20261112).
  A request from the tunnel with only the e-mail header now gets 403. If the Access app is ever re-created in
  Cloudflare its tag changes: set `CF_ACCESS_VERIFY=log`, restart deals-web, read the new tag from the log.

- 11/12 14:27 second Facebook pause of the day, also not an address block: the last 8 item pages of an 85-page scan
  and three known-live listings all failed, yet a new browser read just-seen listings four minutes later (and the
  fast lane's searches never stopped). Working theory: Facebook walls a browser *session* after a long run of item
  pages. Now the session is renewed every 40 item pages, after 3 failures in a row, and before the control check
  (`Facebook.fresh`, `scan._renew`); only failures in a clean session pause Facebook. Watch the log for a few days:
  if pauses still happen it is something else. Tests 83.

- 11/12 Hardening + follow-ups. Every service runs as the unprivileged `deals` user (read-only system, writes only
  under /opt/deals/data, cannot read .env, Chromium sandbox on; browsers in /opt/deals/pw-browsers; installer sets
  it up and re-owns data on every deploy). **Manual commands on .82: `runuser -u deals -- ...`** (a root run can
  leave root-owned database files; the units re-own them at start). Cloudflare Access token verified in the gate
  (signature, issuer, expiry, e-mail; `CF_ACCESS_TEAM=systemgo`, log-only until the audience tag is pinned; needs
  PyJWT[crypto] in the venv - installed by hand, in requirements.txt). Nightly backup can run twice a day.
  4-seat UTV alert bar 75 -> 70: the week before had 0 at 75+ and 3 at 70-74, one of them (2022 RZR XP 4 Ultimate,
  $11,500 vs $15,705) already sold. Checked and left alone: database backups (whole container to PBS nightly at
  04:00 + live DB in the OneDrive offsite set); trailer weight-class guessing (made the affected trailers worse:
  middle-half spread 61 -> 70 pts) - and trailer discounts already predict sales (30%+ under sells 18.3% in 3 days
  vs 6.8% over typical), so trailer pricing stays as is. Tests 82.

- 11/12 False "Facebook blocked" pause 08:20-10:19 (the 11/06 fix did not cover it): a full scan's item-page batch
  had 28 of 52 pages fail, the single control page failed too, and Facebook was paused 2 h - while the fast lane's
  searches at 08:10 worked and everything loaded the moment the pause ended. Now a wall needs an unbroken run of
  3+ failures to the end of the batch (a page that loads after failures proves those were removals) AND three
  known-live listings failing (was one). Walled pages are no longer counted toward "removed". Tests 81.

- 11/11 21:21 outage ("database is locked", every scan lane crashing, ~15 min): clearing a buying stage wrote NULL
  into `starred` (NOT NULL), the request failed mid-write and the dashboard process kept SQLite's write lock until
  it was restarted. Fixed both: clearing a stage keeps the star, and every connection a dashboard request opens is
  rolled back and closed when the request ends, however it ends (`web.connect` + the gate middleware). Tests 81.

- 11/11 Scoring round 2 (Jon: "fix the others, get it production ready"). Reposts (same year + exact miles + town)
  count once at the latest price and never price themselves (~750 UTV/ATV comps removed). Defender Limited = cab +
  heat + A/C even when unstated, A/C implies a cab, Lone Star = top trim (Limited went from +20% to +2% vs typical).
  Sub-model matching extended to jet skis (engine class), mowers (series: Scag Liberty vs Cheetah, Bad Boy MZ vs
  Rogue...) and off-brand machines in the "Other" families. `categories.fix_family`: Maverick R families, Pro S 4,
  pre-2022 "Turbo" 4-seaters, families the model year rules out get no typical (one-off pass over old listings via
  `family_fix_version`). Trailers with no size in the ad get no typical. "Why $X?" shows lined-up values for
  trend-line comps. Re-score 28 s -> 20 s (`usage.doubt` patterns compiled once, comps as namedtuples).
  Checked and dropped: "typical from private sellers only" (dealer +1% vs private +2% for UTVs - no difference).
  Outcome check (2,661 fresh private listings): 15-25% under now sells 12.0% in 3 days (was 9.6%); the 65 listings
  that dropped out of "20%+ under" sold 3.1% (average 7.6%) - they were not deals. "Great" counts start of day ->
  now: utv4 30 -> 18, utv2 128 -> 84, ATV 133 -> 62, jet skis 47 -> 25, mowers 61 -> 32. Tests 80.

- 11/11 Scoring review (Jon: comps looked off on older models, miles, trims). Year and miles were fine on average;
  the cause was sub-models pooled in one family (Defender MAX HD9 -19% vs "typical", Ranger 1000 non-XP -14 to -20%,
  Sportsman 1000 +58%, Outlander 450 -16%), so most "Great" scores were just the cheaper version. Now UTV / ATV comps
  are narrowed to the same sub-model when 8+ exist (`app/variant.py`: engine class, Ranger 1000 XP vs non-XP,
  Defender HD number, diesel); a known sub-model with too few of its own is compared with the family, says so, and
  its score is pulled toward the middle. Trend-line estimates now count only same-age comps, so the thin-comp
  damping applies to them. Buy box before/after: 4-seat UTVs at the alert bar 4 -> 3, ATVs 29 -> 6. Tests 79.

- 11/11 Morning digest: starred listings that sold / were removed drop off the "Watching" list once their
  watch alert has gone out (they are not shown on the site, so the star could not be taken off).
- 11/06 Morning "Facebook blocked, pausing 2 h" alerts were false: every one came from the item-page wall check
  (searches in the same run worked). Deleted listings redirect to login exactly like a block, and the re-checks
  target long-unseen listings (live test: 6/6 long-unseen pages failed, 3/3 listings seen within the hour loaded).
  The check now loads one listing a search showed in the last 3 h before calling it a wall
  (`scan.control_readable`); if that loads, the failures are recorded as removals. 5 false pauses 11/01-11/06.

- 11/04 Enhancement round (Jon's picks): "🎯 My buy box" view is the default (instant-alert categories within their
  limits, same rules as alerts); per-category Scan speed in Setup (`scan_every`; daily = no fast lane, swept only at
  night) - mowers, jet skis, 2-seat UTVs, 3-wheelers set to daily; card buttons wrap on narrow screens; ranking
  tapers past 40% under ("too good to be true?" flag) and discounts no lower than the sold price count 30% less;
  digest "Price cuts into deal range"; opened listings fade + "Hide ones I've opened"; Compare shortlist table;
  Trip panel in Setup (POST/DELETE /api/trip, auto-return at the next scan after the date); Min $ and Max year
  filters; API errors show the server's reason.

- 11/04 Review of the recurring issues. Measured: top 30 dashboard listings 29 active + 1 pending (none deleted);
  random 30 shown 4-seat UTVs: 25 active, 2 pending, 3 deleted (10%). Fixes: page checks re-aimed at listings
  still shown but unseen 2+ days, instant-alert categories first (`fading_candidates`, 14/full scan + 10/night;
  long-hidden "stale" checks 12 -> 4). Photo saver bug: a SQL LIMIT over top scores filled with expired links so
  working links further down were never saved - now filters all candidates, 1,000/full scan. Facebook photo
  links die ~4.4 d after a sighting; the 4.5k photo-less shown cards were all last seen 5-8 d ago and will clear
  as they are re-seen or age out. Feedback poller: Telegram 5xx/timeouts no longer dump tracebacks (153 in the
  log, with the bot token in the URL - token redacted from all deals logs). Cards say "last confirmed up Nd ago".

- 11/03 Dashboard hides Facebook listings unseen by any search/sweep/page check for a sweep cycle (~8.6 d; 14.8k were
  unseen 7 d+ while page checks run ~1,600/day over 35.6k live) - records unchanged, page check still decides
  "ended". Craigslist gone after 3 days unseen (was the sweep-based 8.6 d).

- 11/03 Photos saved on our own disk (Facebook links expire in days; 16k of 30k had gone blank): images.py caches
  each photo while its link works (every full scan + one-off backfill), served at /img/...; item pages supply a
  fresh og:image; 6 top-scoring listings with expired photos get their page re-read per full scan (+10 at night).
  Slim feed: /api/listings carries only filter/table/map fields (13.1 -> 3.1 MB); cards fetch details for the
  ~120 on screen from /api/cards. Live listings 38.6k.

- 10/17 Offer notes load when the Offer box opens; dashboard hides listings past 250 mi (Setup "Show listings within",
  starred always shown; still used as comps). Feed 11.3 -> 8.75 MB. Trip mode for Appleton, WI: search area moved
  to Appleton (FB slug `appleton`; Craigslist followed via the ZIP), restored to Plymouth 10/18 06:00 as planned,
  one-off timer + files removed.

- 10/15 Best-deal order breaks score ties by comps (confidence), then distance, then newest (was newest only).

- 10/12 Per-category "Min $ saved" (buy box; blank = global $1,500): mowers/trailers/ATVs $500, PWC/sleds $750, trikes $300.
- 10/12 Check-in: score bonuses for price cut / below-sold / low use dropped (shown, no points; none predicted a
  3-day sale within its discount band) -> 75+ / 60-74 / 45-59 / <45 sell 18 / 15 / 12 / 11% in 3 days; Great 1,273 -> 708.
  Scorecard = fresh private listings, sold/pending within 3 days (was every listing incl. backlog, removals counted).
  Trim values learned per family (cap 30%); Commander XT no longer "lower trim". Per-category alert bar
  (buy box "Alert at score"): 4-seat UTVs 75, rest 80. Year trend re-checked: 7 of 323 modern adjacent years inverted.
  Reminder timer deleted.
- 10/12 Sold pull waits out the hourly Facebook budget (holding the lock, fast lane still first, 3 h cap)
  instead of quitting - on 10/12 the overnight sweep had spent the hour and the 04:30 pull got 0.
- 10/11 Facebook blocked at the 300/h cap (back-off worked) -> hourly budget 300 -> 220.
- 10/10 Telegram 👍/👎 + seller opener in alerts; feedback poller; TODO.md.
- 10/09 Why-this-price panel, Fix (corrections), buying stages, alert activity log; Codex round 3 (real rollback
  with release check + deploy tests, feed revision, Gone survives page checks, route-aware hand-offs).
- 10/09 Full code review (3 reviewers, 57 findings): page checks decide gone, hourly Facebook budget, price-cut
  re-alerts, title-only re-reads, trailer ceiling by axles, "$X each", dashboard perf, ~30 smaller fixes.
- 10/09 Fast lane gets its turn during long scans; listings feed gzipped/cached/304 (35 MB -> 6 MB, 0 B on refresh).
- 10/08 Buy box (per category: models, year, price, miles/hours, distance, instant vs digest); towns-not-counties
  geocoding; reposts merge; stricter red flags.
- 10/05 Deep sweep (price-band searches reach the older listings a Facebook search never shows); Codex rounds 1-2.
