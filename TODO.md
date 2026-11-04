# Deal Finder - to do

Running list. Update it with every change (done items move to the bottom with the date).
Live site: https://deals.alerolabs.ai · code on .76 at /home/jon/dev/deals · `./deploy.sh` (tests, then swap, rollback on failure).

## Needs Jon

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

- [ ] Compare layout for the shortlist (today: the "Starred / shortlist" filter + the All-listings table).
- [ ] Digest: include listings whose price dropped into deal territory yesterday, not only new ones.
- [ ] Typical price from private sellers only (or dealer asks down-weighted); the buy box is private-only.
- [ ] Ask the model for the price written in the ad text, to recover "$1" / "$123" placeholder listings.
- [ ] Dealer websites as a new-price source for trailers (Marketplace only shows dealers who post there).
- [ ] A proper "trip" switch in Setup (search area + auto-return) instead of hand-set settings; Craigslist site per area.
- [ ] Cap starred page re-checks (every full scan today) once the shortlist grows.

## Known small issues (from the 10/9 review, not fixed)

- [ ] `usage_adjusted` is inferred from the note text (minor double count on tiny adjustments).
- [ ] `rescore_all` runs one long write transaction (~5 s on 16k listings); fine today, watch it.
- [ ] Craigslist `detail` never returns None, so `record_miss` for Craigslist is dead code.
- [ ] Facebook budget (220/h since 10/11, long lanes stop at 175) cuts the full scan's item pages; the leftovers
      wait for the night window. Blocked twice: 10/01 at ~410 loads/h, 10/11 14:23 right at the 300/h cap (rolling hour; 2 h back-off, Craigslist
      kept going). If it blocks again at 220: drop to ~170, or enter the TorGuard proxy in Setup -> Facebook route.

## Done

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
