# Reading AA Stocks Charts

How to visually read an AA Stocks chart image. The app fetches these charts from AA Stocks (see
`download_aa_stocks_chart` in app.py and the `url_template_*` settings) and renders them on the
Tracker and Training pages. Patterns are detected by reading the chart **visually** — exact prices
are inferred from the chart, not from a numeric data feed — so this reference is the basis for any
pattern recognition (see [technical-patterns.md](technical-patterns.md)).

## Scheme=3 (the scheme this app uses)

- **Price scale is the right-hand axis.** The numbers down the right edge (e.g. 11.770 … 16.570)
  are the price levels. To read or infer any candle's price, project it horizontally across to the
  right axis: the top/bottom of the body give open & close, the wick tips give the high & low.
  Levels between the printed gridlines are interpolated (e.g. a body sitting midway between 15.370
  and 15.970 ≈ 15.67).
- **Exact OHLC of the latest/selected candle** is printed in the header row as `O: H: L: C:`
  (open, high, low, close) — use it to calibrate your read of the axis.
- **Each candle is an OHLC bar** for one trading period (daily on the 3-month view): the body spans
  **open ↔ close**, and the wicks reach the **high** (top tip) and **low** (bottom tip). Always read
  all four values — open, high, low, close — when analysing a bar.
- **Identifying O/H/L/C on the bar:** **High** = top tip of the upper wick, **Low** = bottom tip of
  the lower wick, and **Open/Close** are the two body edges — the **fill** says which is which:
  hollow → open = bottom edge, close = top edge; solid → open = top edge, close = bottom edge. (A
  side with no wick means that body edge is also the high or low.)
- **Fill = close vs open (within the same bar):**
  - **Hollow / empty body** → close is **higher than the open** (the bar finished above where it
    started).
  - **Solid / filled body** → close is **lower than the open** (the bar finished below where it
    started).
- **Colour = close vs the *previous* bar's close (day-over-day direction):**
  - **Blue** (whether hollow or solid) → this bar's close is **higher than the previous bar's close**.
  - **Green** (whether hollow or solid) → this bar's close is **lower than the previous bar's close**.
- **Fill and colour are independent — every bar carries both.** Examples of the mixed cases:
  - **Solid blue:** closed below its own open, yet still above the prior close (e.g. gapped up, faded
    intraday, but held above yesterday).
  - **Hollow green:** closed above its own open, yet still below the prior close (e.g. gapped down,
    recovered part of the way).
  - Strongest up-bar = **hollow + blue** (up on the day *and* up vs the previous close); weakest =
    **solid + green**.
- NOTE: this colour convention is the **opposite of the US** (green ≠ up here), and colour is
  measured against the **previous close**, not against the open.
- **Volume = the grey solid bars along the bottom.** Bar height = shares traded that period. Tall
  bars mean heavy participation / conviction behind that day's price move; short bars mean a quiet,
  low-conviction move.
- **Moving averages** are the colored lines (e.g. red SMA20, plus SMA50/100/150). Price rising above
  upward-sloping MAs is a sign of an established uptrend.
- **Time scale is the bottom (x) axis.** The markers along the bottom label the **months** spanned
  by the chart, left (oldest) to right (latest). Use them to judge how much history a chart actually
  covers and to date any candle by projecting it down to the axis.
  - **Spotting a recent IPO / newly-listed stock:**
    - **Primary tell (works on *any* chart period): long moving averages read 0.00.** In the header,
      **`SMA(100): 0.00` and `SMA(150): 0.00`** (and sometimes a partial/zero SMA50) mean the stock
      has **fewer than 100/150 trading days of history in total**. AA Stocks computes each SMA from
      the stock's full history, not just the candles visible in the window — so these zeros indicate
      a young listing regardless of whether you're viewing the 3-month, 6-month, or 1-year chart.
    - **Extra confirmation on the 1-year view: few month markers.** A fully-listed stock shows ~12
      month markers along the bottom axis. If the 1-year chart instead shows only a handful
      (**roughly ≤5**) — candles not filling the full width, data starting partway across — that
      corroborates **less than ~5 months of history**. (This count tell is 1-year-specific; a
      3-month view naturally shows only ~2–3 markers for *any* stock, so don't use marker count
      there.)
    - Treat a recent IPO's "historical high" as very young (little price history to lean on) when
      judging breakout patterns.

## Reading as relative, not absolute

Because price is inferred by eye, pattern rules are expressed in **relative/visual** terms — this
candle vs the recent ones, this volume bar vs the surrounding bars, price vs the prior cluster —
rather than exact numeric thresholds. Exact price is not required for the graphical patterns.
