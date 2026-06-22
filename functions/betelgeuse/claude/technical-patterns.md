# Technical Patterns Knowledge

Domain knowledge for reading AA Stocks charts and identifying the technical patterns this app
tracks. Charts are fetched from AA Stocks (see `download_aa_stocks_chart` in app.py and the
`url_template_*` settings) and rendered on the Tracker and Training pages.

## How to read the charts

See **[reading-aastocks-charts.md](reading-aastocks-charts.md)** for how to read an AA Stocks chart
(price axis, candle colors, volume, moving averages). Patterns below are evaluated visually against
that reference, in relative terms rather than exact prices.

## Patterns

### Breakthrough (volume-confirmed up move)

The core signal: **a substantial recent up-bar, on relatively large volume, emerging from a stable
base.** Evaluated visually against [reading-aastocks-charts.md](reading-aastocks-charts.md) in
relative terms (this bar vs recent bars, this volume vs surrounding bars) — exact prices not required.

#### Rule set

**Core conditions** — all should hold to flag a Breakthrough candidate:

1. **Recent substantial up-bar.** Within the last **N bars** (N is a soft heuristic, typically
   **2–10**), there is a bar showing a substantial rise (a large up-move). A subsequent partial
   retreat/pullback is acceptable and does not disqualify it.
2. **Relatively large volume.** That up-bar (and/or the bars around it) shows a volume bar clearly
   larger than the surrounding/recent bars.
3. **Stable base beforehand.** Leading into that up-bar, the stock has been oscillating up and down
   in a relatively stable range — a slight/mild rise in price is also fine. (I.e. it is breaking out
   of a calm base, not extended after a big run.)

**Strength modifiers** — not required, but each increases the conviction of the signal:

4. **Near the high.** The up-bar from step 1 nearly reaches / tests the recent high visible on the
   chart → stronger signal.
   - **4.1 Pierced the historical high, then retreated (tested / "failed" breakout).** The bar's
     **high broke above the prior historical high** on the chart but it closed back below (an upper
     wick poking above the old high). Rationale: at a fresh high, existing holders take profit and
     recent buyers cut losses, so a retreat is natural — yet the fact the stock reached a new high at
     all signals real demand. Treat this as an **interesting observation point**, bullish-leaning
     *within the base-breakout context of conditions 1–3*, but ambiguous on its own — direction is
     confirmed by what happens next:
     - **Strength-increasing** when it occurs early / straight out of the base, the pierce comes on
       high volume, and the **following bars hold or reclaim** the old high (breakout being tested,
       not rejected).
     - **A warning instead** when it comes after an already-extended run, shows a large upper wick
       closing near the low on the heaviest volume (rejection / blow-off / distribution), or price
       **fails to reclaim** and rolls over (failed breakout that traps buyers).

5. **Recent IPO / newly-listed name.** If the stock is a recent IPO — detected via the chart
   (`SMA(100)/SMA(150) = 0.00` on any period, optionally corroborated by ≤~5 month markers on the
   1-year view; see [reading-aastocks-charts.md](reading-aastocks-charts.md)) — a Breakthrough is
   **stronger**. Rationale: a young listing has little overhead supply and no long-term trapped
   holders to sell into the advance, so almost every breakout pushes into **all-time-high territory
   with no historical resistance above** (closely related to the "near the high" modifier 4). The
   flip side to keep in mind: its "historical high" is only weeks/months old, so there's little
   price history to lean on and moves can be more volatile.

#### Worked example

**002715.HK (埃斯頓), daily / 3-month**: after oscillating in a stable base around 12.4–13, the stock
produced large up-bars (the ~6th-from-last and ~2nd-from-last candles) each on a **tall volume bar**
(clearly above the quiet base), pushing toward ~16.5 near the recent highs — satisfying core
conditions 1–3 with the near-high strength modifier.

### Triangle

Placeholder — a consolidation/coiling pattern where the range narrows between converging
support/resistance before resolving with a directional move. (Detailed rules to be added when defined.)

## Relationship to the app

- `BACKTEST_PATTERNS` in app.py = the patterns selectable on the **Training** page (`Breakthrough`,
  `Triangle`). Saved chart snapshots land in `backtest/training/{market}/{pattern}/`.
- The `TechnicalPattern` group in `GROUP_OPTIONS` is a portfolio classification bucket (not the same
  thing as a detected chart pattern, though related in spirit).
