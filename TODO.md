# Deal Finder - to do

Running list. Update it with every change (done items move to the bottom with the date).
Live site: https://deals.alerolabs.ai · code on .76 at /home/jon/dev/deals · `./deploy.sh` (tests, then swap, rollback on failure).

## Needs Jon

- [ ] **Buy box**: set a max price for 4-seat UTVs and zero-turns (Setup -> Buy box); pick models once decided.
      Instant alerts are only as good as the box.
- [ ] **Use the signals**: Star / Fix / Hide on the dashboard, 👍 / 👎 under Telegram alerts. The 10/12 tuning
      works off these; as of 10/10 there are 3 stars and nothing else.
- [ ] Facebook app: saved-search notifications for the top 2-3 models (instant, free, within Facebook's rules).

## 10/12 check-in (reminder fires Mon 2026-10-12 08:30 via the deals bot)

Set on 10/4 so the scoring could be tuned once ~2 weeks of data existed. Now with the deep sweep, page-checked
"gone" data and the feedback table, the pass is:
- [ ] Tune the score weights against what actually sold / got a 👍 (Scorecard on the Market tab + `feedback`).
- [ ] Learn trim values per family instead of one shared lower −9% / top +7%.
- [ ] Re-check the year-trend blend (a newer model year priced below an older one).
- [ ] Decide on the photo-check pilot (full-size photos at fetch time: dealer/promo graphics, cab/plow/trailer
      visible, odometer reading; never score damage - the model hallucinated it).
- [ ] Delete `deals-reminder-1012.timer/.service` and `/opt/deals/data/reminder_1012.py` on .82 after it fires.

## Build ideas (ordered)

- [ ] Compare layout for the shortlist (today: the "Starred / shortlist" filter + the All-listings table).
- [ ] Digest: include listings whose price dropped into deal territory yesterday, not only new ones.
- [ ] Typical price from private sellers only (or dealer asks down-weighted); the buy box is private-only.
- [ ] Ask the model for the price written in the ad text, to recover "$1" / "$123" placeholder listings.
- [ ] Dealer websites as a new-price source for trailers (Marketplace only shows dealers who post there).
- [ ] Lazy-load offer notes on the card (20% of the listings feed).
- [ ] Cap starred page re-checks (every full scan today) once the shortlist grows.

## Known small issues (from the 10/9 review, not fixed)

- [ ] `usage_adjusted` is inferred from the note text (minor double count on tiny adjustments).
- [ ] `rescore_all` runs one long write transaction (~5 s on 16k listings); fine today, watch it.
- [ ] Craigslist `detail` never returns None, so `record_miss` for Craigslist is dead code.
- [ ] Facebook budget (220/h since 10/11, long lanes stop at 175) cuts the full scan's item pages; the leftovers
      wait for the night window. Blocked twice: 10/01 at ~410 loads/h, 10/11 14:23 right at the 300/h cap (rolling hour; 2 h back-off, Craigslist
      kept going). If it blocks again at 220: drop to ~170, or enter the TorGuard proxy in Setup -> Facebook route.

## Done

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
