/*
 * InstrumentDetail — a shared, market-aware instrument detail modal.
 *
 * The single front-end entry point for "show me everything about this instrument":
 * a polished detail view (header + reference facts + notes + charts) that replaces
 * the old bare "multiple charts" popup. Used by BOTH the Tracker (index.html) and
 * the Overview page (test_overview.html) — like static/symbol-field.js, it is a
 * deliberate shared asset: it injects its own modal DOM + styles once and exposes
 * a tiny API so call sites never re-implement the layout.
 *
 *   InstrumentDetail.open(market, symbol)   // fetch + show
 *   InstrumentDetail.PROVIDERS              // {hk:'aastocks', jp:null, us:null, crypto:'coingecko'}
 *   InstrumentDetail.MARKET_META            // per-market flag + accent + name
 *
 * Data comes from GET /api/instrument/<market>/<symbol> (the single source of truth
 * for the reference facts). Charts dispatch on the market's provider: aastocks (HK)
 * renders the four AA-Stocks period images with Copy URL; coingecko (crypto) renders
 * the 7/30/90/365-day candlestick grid (no shareable URL, so Copy URL is disabled);
 * markets with no provider (JP/US) show a styled "charting not available yet" panel.
 */
(function () {
    'use strict';

    const MARKET_META = {
        hk:     { name: 'Hong Kong',      flag: '🇭🇰', accent: '#2f81f7' },
        jp:     { name: 'Japan',          flag: '🇯🇵', accent: '#da3633' },
        us:     { name: 'United States',  flag: '🇺🇸', accent: '#2ea043' },
        crypto: { name: 'Cryptocurrency', flag: '🪙', accent: '#bb8009' }
    };
    // Chart provider per market — mirrors MARKET_CHART_PROVIDER in index.html.
    // These are DEFAULT fallbacks: the detail bundle's own `provider` field (from
    // /api/instrument) wins (see line ~325), so HK honours the runtime-configurable
    // hk_chart_provider setting ('aastocks' | 'yfinance') without changing this map.
    const PROVIDERS = { hk: 'aastocks', jp: 'yfinance', us: 'yfinance', crypto: 'coingecko' };

    const CRYPTO_PERIODS = [
        { period: '7',   label: '7 Days' },
        { period: '30',  label: '30 Days' },
        { period: '90',  label: '90 Days' },
        { period: '365', label: '1 Year' }
    ];
    // Performance lookback windows shown in the detail panel (key matches the API's
    // performance map: GET /api/instrument/<market>/<symbol>/performance).
    const PERF_PERIODS = [
        { key: '1',   label: '1 Day' },
        { key: '7',   label: '7 Days' },
        { key: '30',  label: '30 Days' },
        { key: '365', label: '1 Year' }
    ];
    const STOCK_PERIODS = [
        { period: '30',  label: '1 Month' },
        { period: '90',  label: '3 Months' },
        { period: '180', label: '6 Months' },
        { period: '365', label: '1 Year' }
    ];
    // AA-Stocks period image templates, pulled from /api/settings (cached per session).
    const AASTOCKS_PERIODS = [
        { key: 'url_template_3m',         label: '3 Months' },
        { key: 'url_template_6m',         label: '6 Months' },
        { key: 'url_template_1y',         label: '1 Year' },
        { key: 'url_template_1y_monthly', label: '1 Year (Monthly)' }
    ];

    let stylesInjected = false;
    let overlayEl = null;
    let settingsCache = null;
    let openToken = 0;   // guards stale async chart loads against a newer open()

    // ── helpers ──
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    function hexSoft(hex, a) {
        const n = parseInt(hex.slice(1), 16);
        return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    function relativeDate(dateStr) {
        if (!dateStr) return null;
        const t = Date.parse(String(dateStr).replace(' ', 'T'));
        if (isNaN(t)) return null;
        const days = Math.floor((Date.now() - t) / 86400000);
        if (days < 0) return 'in the future';
        if (days === 0) return 'today';
        if (days === 1) return 'yesterday';
        if (days < 30) return days + ' days ago';
        if (days < 365) { const m = Math.floor(days / 30); return m + (m === 1 ? ' month ago' : ' months ago'); }
        const y = Math.floor(days / 365); return y + (y === 1 ? ' year ago' : ' years ago');
    }
    // Relative time from an epoch-ms timestamp (price "as of" hint).
    function relTs(ms) {
        const n = Number(ms);
        if (!Number.isFinite(n) || n <= 0) return null;
        const sec = Math.floor((Date.now() - n) / 1000);
        if (sec < 0) return 'just now';
        if (sec < 60) return sec + 's ago';
        const min = Math.floor(sec / 60); if (min < 60) return min + 'm ago';
        const hr = Math.floor(min / 60); if (hr < 24) return hr + 'h ago';
        const days = Math.floor(hr / 24); if (days < 30) return days + 'd ago';
        const mo = Math.floor(days / 30); return mo < 12 ? mo + 'mo ago' : Math.floor(mo / 12) + 'y ago';
    }
    // Magnitude-aware price formatting (crypto spans $100k → $0.0000001). USD only today.
    function fmtPrice(v, currency) {
        if (v == null || isNaN(v)) return null;
        const abs = Math.abs(v);
        let opts;
        if (abs >= 1)        opts = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
        else if (abs >= 0.01) opts = { minimumFractionDigits: 2, maximumFractionDigits: 4 };
        else                  opts = { maximumSignificantDigits: 4 };
        const sym = (currency || 'usd').toLowerCase() === 'usd' ? '$' : '';
        return sym + v.toLocaleString('en-US', opts);
    }
    // Display name of the market-data provider that sourced the price, per market.
    const PRICE_SOURCE = { crypto: 'CoinGecko' };

    function injectStyles() {
        if (stylesInjected) return;
        stylesInjected = true;
        const css = `
        .idv-overlay {
            display: none; position: fixed; inset: 0; z-index: 2000;
            background: rgba(2,6,23,0.78); backdrop-filter: blur(3px);
            align-items: flex-start; justify-content: center; padding: 3vh 1rem;
            overflow-y: auto;
        }
        .idv-overlay.active { display: flex; }
        .idv-modal {
            width: 1100px; max-width: 100%;
            background: linear-gradient(135deg, rgba(22,27,34, 0.97) 0%, rgba(13,17,23, 0.97) 100%);
            border: 1px solid rgba(125,133,144, 0.18);
            border-left: 3px solid var(--accent, #2f81f7);
            border-radius: 16px; box-shadow: 0 24px 70px rgba(0,0,0,0.55);
            animation: idvIn 0.22s ease; overflow: hidden;
        }
        @keyframes idvIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

        .idv-head { display: flex; align-items: flex-start; gap: 0.9rem; padding: 1.25rem 1.4rem 1.1rem; border-bottom: 1px solid rgba(125,133,144, 0.1); }
        .idv-flag { font-size: 2rem; line-height: 1; margin-top: 0.1rem; }
        .idv-titles { flex: 1; min-width: 0; }
        .idv-symbol { font-family: 'SF Mono','Roboto Mono',Menlo,monospace; font-size: 1.55rem; font-weight: 700; color: #f0f6fc; letter-spacing: -0.01em; word-break: break-all; }
        .idv-name { color: #7d8590; font-size: 0.95rem; margin-top: 0.15rem; }
        .idv-head-meta { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.55rem; }
        .idv-market-tag { font-size: 0.72rem; font-weight: 600; color: var(--accent,#58a6ff); background: var(--accent-soft, rgba(47,129,247, 0.15)); border-radius: 999px; padding: 0.2rem 0.65rem; }
        .idv-group { font-size: 0.72rem; font-weight: 600; color: #c9d1d9; background: rgba(125,133,144, 0.12); border: 1px solid rgba(125,133,144, 0.15); border-radius: 6px; padding: 0.2rem 0.6rem; }
        .idv-watch { font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #d29922; background: rgba(210,153,34, 0.12); border: 1px solid rgba(210,153,34, 0.3); border-radius: 999px; padding: 0.18rem 0.6rem; }
        .idv-close { background: none; border: none; color: #7d8590; font-size: 1.6rem; line-height: 1; cursor: pointer; padding: 0 0.2rem; transition: color 0.2s; align-self: flex-start; }
        .idv-close:hover { color: #f85149; }

        .idv-body { padding: 1.2rem 1.4rem 1.5rem; }

        /* quote hero — the headline last price + 24h move, finance-terminal style */
        .idv-quote {
            display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.55rem 0.85rem;
            padding: 1rem 1.15rem; margin-bottom: 1.35rem;
            background: linear-gradient(135deg, rgba(13,17,23, 0.65) 0%, rgba(13,17,23, 0.3) 100%);
            border: 1px solid rgba(125,133,144, 0.12);
            border-left: 3px solid var(--accent, #2f81f7);
            border-radius: 14px;
        }
        .idv-quote-label { width: 100%; font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.07em; color: #6e7681; font-weight: 700; margin-bottom: 0.1rem; }
        .idv-quote-price { font-family: 'SF Mono','Roboto Mono',Menlo,monospace; font-size: 2rem; font-weight: 700; color: #f0f6fc; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
        .idv-quote-ccy { font-size: 0.78rem; font-weight: 600; color: #6e7681; text-transform: uppercase; letter-spacing: 0.06em; }
        .idv-quote-delta { display: inline-flex; align-items: center; gap: 0.32rem; font-size: 0.88rem; font-weight: 700; padding: 0.25rem 0.65rem; border-radius: 999px; font-variant-numeric: tabular-nums; }
        .idv-quote-delta.up { color: #3fb950; background: rgba(46,160,67, 0.13); }
        .idv-quote-delta.down { color: #f85149; background: rgba(218,54,51, 0.13); }
        .idv-quote-delta.flat { color: #c9d1d9; background: rgba(125,133,144, 0.12); }
        .idv-quote-delta-tag { font-size: 0.6rem; font-weight: 700; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.05em; }
        .idv-quote-sub { width: 100%; font-size: 0.74rem; color: #6e7681; margin-top: 0.2rem; }
        .idv-quote.muted .idv-quote-price { color: #484f58; font-style: italic; font-size: 1.4rem; font-family: inherit; font-weight: 600; }

        .idv-facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.7rem; margin-bottom: 1.3rem; }
        .idv-fact { background: rgba(13,17,23, 0.5); border: 1px solid rgba(125,133,144, 0.1); border-radius: 12px; padding: 0.7rem 0.85rem; position: relative; overflow: hidden; }
        .idv-fact::before { content:''; position:absolute; left:0; top:0; bottom:0; width:2px; background: var(--accent,#2f81f7); opacity: 0.7; }
        .idv-fact-label { font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.06em; color: #6e7681; font-weight: 600; }
        .idv-fact-value { font-size: 1.05rem; font-weight: 700; color: #e6edf3; margin-top: 0.25rem; font-variant-numeric: tabular-nums; word-break: break-word; }
        .idv-fact-value.muted { color: #484f58; font-weight: 500; font-style: italic; font-size: 0.9rem; }
        .idv-fact-sub { font-size: 0.7rem; color: #6e7681; margin-top: 0.2rem; }

        .idv-perf { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.7rem; margin-bottom: 1.4rem; }
        .idv-perf-card { background: rgba(13,17,23, 0.5); border: 1px solid rgba(125,133,144, 0.1); border-radius: 12px; padding: 0.7rem 0.85rem; position: relative; overflow: hidden; }
        .idv-perf-card::before { content:''; position:absolute; left:0; top:0; bottom:0; width:2px; background: #484f58; opacity: 0.7; }
        .idv-perf-card.up::before { background: #2ea043; }
        .idv-perf-card.down::before { background: #da3633; }
        .idv-perf-value { font-size: 1.2rem; font-weight: 700; margin-top: 0.28rem; font-variant-numeric: tabular-nums; color: #c9d1d9; }
        .idv-perf-card.up .idv-perf-value { color: #3fb950; }
        .idv-perf-card.down .idv-perf-value { color: #f85149; }
        .idv-perf-card.muted .idv-perf-value { color: #484f58; font-weight: 500; font-style: italic; font-size: 1rem; }
        .idv-perf-note { font-size: 0.76rem; color: #6e7681; margin: -0.8rem 0 1.4rem; font-style: italic; }
        .idv-perf-note:empty { display: none; }
        @media (max-width: 760px) { .idv-perf { grid-template-columns: repeat(2, 1fr); } }

        .idv-section-title { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: #6e7681; font-weight: 700; margin: 0 0 0.65rem; display: flex; align-items: center; gap: 0.4rem; }

        .idv-notes { background: rgba(13,17,23, 0.5); border: 1px solid rgba(125,133,144, 0.1); border-radius: 12px; padding: 0.85rem 1rem; margin-bottom: 1.4rem; }
        .idv-notes-body { color: #c9d1d9; font-size: 0.9rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
        .idv-notes-body.na { color: #484f58; font-style: italic; }

        .idv-charts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.4rem; }
        .idv-chart-item { display: flex; flex-direction: column; }
        .idv-chart-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; gap: 0.8rem; }
        .idv-chart-label { font-weight: 600; color: var(--accent,#58a6ff); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.05em; }
        .idv-copy { background: rgba(47,129,247, 0.1); border: 1px solid rgba(125,133,144, 0.2); color: #58a6ff; border-radius: 6px; padding: 0.3rem 0.55rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.2s; }
        .idv-copy:hover { background: rgba(47,129,247, 0.2); border-color: rgba(88,166,255, 0.4); }
        .idv-copy:disabled { background: rgba(110,118,129, 0.08); border-color: rgba(125,133,144, 0.15); color: #484f58; cursor: not-allowed; opacity: 0.6; }
        .idv-chart-img { width: 100%; height: auto; border-radius: 8px; border: 1px solid rgba(125,133,144, 0.2); }
        .idv-cell { background: #010409; border: 1px solid rgba(88,166,255, 0.1); border-radius: 8px; overflow: hidden; min-height: 300px; display: flex; align-items: center; justify-content: center; }
        .idv-cell img { width: 100%; height: auto; display: block; }
        .idv-loading { color: #484f58; font-size: 0.85rem; display: flex; flex-direction: column; align-items: center; gap: 0.6rem; padding: 2rem; text-align: center; }
        .idv-spinner { width: 26px; height: 26px; border: 3px solid rgba(88,166,255, 0.15); border-top-color: #2f81f7; border-radius: 50%; animation: idvSpin 0.8s linear infinite; }
        @keyframes idvSpin { to { transform: rotate(360deg); } }

        .idv-placeholder { grid-column: 1 / -1; text-align: center; padding: 3rem 1rem; color: #6e7681; background: rgba(13,17,23, 0.4); border: 1px dashed rgba(125,133,144, 0.18); border-radius: 12px; }
        .idv-placeholder .big { font-size: 2.4rem; opacity: 0.5; margin-bottom: 0.5rem; }
        .idv-placeholder .msg { font-size: 0.95rem; color: #7d8590; }
        .idv-placeholder .hint { font-size: 0.8rem; margin-top: 0.35rem; color: #484f58; }

        @media (max-width: 760px) {
            .idv-charts { grid-template-columns: 1fr; }
            .idv-symbol { font-size: 1.25rem; }
        }`;
        const tag = document.createElement('style');
        tag.id = 'idv-styles';
        tag.textContent = css;
        document.head.appendChild(tag);
    }

    function ensureOverlay() {
        if (overlayEl) return overlayEl;
        injectStyles();
        overlayEl = document.createElement('div');
        overlayEl.className = 'idv-overlay';
        overlayEl.innerHTML = `<div class="idv-modal" role="dialog" aria-modal="true"></div>`;
        document.body.appendChild(overlayEl);
        // backdrop click closes
        overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) close(); });
        // ESC closes
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlayEl.classList.contains('active')) close();
        });
        return overlayEl;
    }

    function modalNode() { return ensureOverlay().querySelector('.idv-modal'); }

    function openOverlay() {
        ensureOverlay().classList.add('active');
        document.body.style.overflow = 'hidden';
    }
    function close() {
        if (!overlayEl) return;
        overlayEl.classList.remove('active');
        document.body.style.overflow = '';
        openToken++;   // cancel any in-flight chart loads
    }

    // ── public entry point ──
    async function open(market, symbol) {
        market = (market || '').toLowerCase();
        const meta = MARKET_META[market] || { name: market, flag: '•', accent: '#2f81f7' };
        const node = modalNode();
        node.style.setProperty('--accent', meta.accent);
        node.style.setProperty('--accent-soft', hexSoft(meta.accent, 0.15));
        const token = ++openToken;

        node.innerHTML = `
            <div class="idv-head">
                <span class="idv-flag">${meta.flag}</span>
                <div class="idv-titles">
                    <div class="idv-symbol">${esc(symbol)}</div>
                    <div class="idv-name">Loading…</div>
                </div>
                <button class="idv-close" aria-label="Close">&times;</button>
            </div>
            <div class="idv-body"><div class="idv-loading"><div class="idv-spinner"></div><span>Loading instrument…</span></div></div>`;
        node.querySelector('.idv-close').addEventListener('click', close);
        openOverlay();

        let detail;
        try {
            const r = await fetch(`/betelgeuse/api/instrument/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`);
            detail = await r.json();
        } catch (e) {
            detail = { market, symbol, name: symbol, provider: PROVIDERS[market] || null, in_portfolio: false, portfolio: null, reference: {} };
        }
        if (token !== openToken) return;   // a newer open() superseded this one
        render(detail, meta, token);
    }

    function factCard(label, value, opts = {}) {
        const muted = value == null || value === '';
        const v = muted ? (opts.empty || '—') : value;
        return `<div class="idv-fact">
            <div class="idv-fact-label">${esc(label)}</div>
            <div class="idv-fact-value${muted ? ' muted' : ''}">${esc(v)}</div>
            ${opts.sub ? `<div class="idv-fact-sub">${esc(opts.sub)}</div>` : ''}
        </div>`;
    }

    function buildFacts(d) {
        const ref = d.reference || {};
        const p = d.portfolio;
        const cards = [];
        // Portfolio-side facts (only when tracked)
        if (p) {
            cards.push(factCard('Group', p.group || 'Default'));
            cards.push(factCard('Added', p.added_date || null, { sub: relativeDate(p.added_date) || '', empty: 'Unknown' }));
        }
        // Market-specific reference facts
        if (d.market === 'hk') {
            cards.push(factCard('Lot Size', ref.lot_size != null && ref.lot_size !== '' ? ref.lot_size : null, { sub: 'shares / board lot', empty: 'N/A' }));
            cards.push(factCard('Category', ref.category || null, { empty: 'N/A' }));
            if (ref.sub_category) cards.push(factCard('Sub-category', ref.sub_category));
        } else if (d.market === 'us') {
            cards.push(factCard('Board', (ref.boards && ref.boards.length) ? ref.boards.join(', ') : null, { empty: 'N/A' }));
            cards.push(factCard('Type', ref.boards && ref.boards.length ? (ref.etf ? 'ETF' : 'Equity') : null, { empty: 'N/A' }));
        } else if (d.market === 'crypto') {
            cards.push(factCard('CoinGecko ID', ref.coin_id || null, { empty: 'Unmapped' }));
            cards.push(factCard('Market-cap Rank', ref.market_cap_rank != null ? ('#' + ref.market_cap_rank) : null, { empty: 'Unranked' }));
        } else if (d.market === 'jp') {
            cards.push(factCard('Reference Data', null, { empty: 'None yet' }));
        }
        return cards.join('');
    }

    function render(d, meta, token) {
        const node = modalNode();
        const provider = d.provider || PROVIDERS[d.market] || null;
        const hasNotes = d.portfolio && d.portfolio.comment && d.portfolio.comment.trim();
        const notesBody = hasNotes ? d.portfolio.comment : 'N/A';

        node.innerHTML = `
            <div class="idv-head">
                <span class="idv-flag">${meta.flag}</span>
                <div class="idv-titles">
                    <div class="idv-symbol">${esc(d.symbol)}</div>
                    <div class="idv-name">${esc(d.name || d.symbol)}</div>
                    <div class="idv-head-meta">
                        <span class="idv-market-tag">${meta.flag} ${esc(meta.name)}</span>
                        ${d.portfolio ? `<span class="idv-group">${esc(d.portfolio.group || 'Default')}</span>`
                                      : `<span class="idv-watch">Not in portfolio</span>`}
                    </div>
                </div>
                <button class="idv-close" aria-label="Close">&times;</button>
            </div>
            <div class="idv-body">
                <div class="idv-quote muted" id="idv-quote">${quoteHtml(null, true, d.market)}</div>

                <div class="idv-facts">${buildFacts(d)}</div>

                <div class="idv-section">
                    <div class="idv-section-title">📊 Performance</div>
                    <div class="idv-perf" id="idv-perf">${perfGridHtml(null, true)}</div>
                    <div class="idv-perf-note" id="idv-perf-note"></div>
                </div>

                <div class="idv-section">
                    <div class="idv-section-title">📝 Notes</div>
                    <div class="idv-notes"><div class="idv-notes-body${hasNotes ? '' : ' na'}">${esc(notesBody)}</div></div>
                </div>

                <div class="idv-section">
                    <div class="idv-section-title">📈 Charts</div>
                    <div class="idv-charts" id="idv-charts"></div>
                </div>
            </div>`;
        node.querySelector('.idv-close').addEventListener('click', close);

        loadPerformance(d, token);

        const charts = node.querySelector('#idv-charts');
        if (provider === 'aastocks') renderAastocks(charts, d, token);
        else if (provider === 'coingecko') renderCrypto(charts, d, token);
        else if (provider === 'yfinance') renderStock(charts, d, token);
        else renderNoCharts(charts, d, meta);
    }

    // Quote hero inner HTML — headline last price + 24h delta + "as of · source".
    // `snap` is the snapshot payload (or null); markets/instruments with no price show
    // a muted state so the block stays present and the layout is consistent.
    function quoteHtml(snap, loading, market) {
        const price = snap ? fmtPrice(snap.price, snap.currency) : null;
        if (price == null) {
            const txt = loading ? 'Loading…' : '—';
            const sub = loading ? 'Fetching latest price…'
                : (snap && snap.available === false
                    ? 'No market-data provider for this market yet'
                    : 'Market data isn’t available for this instrument yet');
            return `<div class="idv-quote-label">Last Price</div>
                <span class="idv-quote-price">${esc(txt)}</span>
                <span class="idv-quote-sub">${esc(sub)}</span>`;
        }
        const d1 = snap.performance ? snap.performance['1'] : null;
        let delta = '';
        if (d1 != null) {
            const cls = d1 > 0 ? 'up' : (d1 < 0 ? 'down' : 'flat');
            const arrow = d1 > 0 ? '▲' : (d1 < 0 ? '▼' : '■');
            delta = `<span class="idv-quote-delta ${cls}">${arrow} ${(d1 > 0 ? '+' : '') + d1.toFixed(2)}%<span class="idv-quote-delta-tag">24h</span></span>`;
        }
        const ago = snap.as_of ? relTs(snap.as_of) : null;
        const src = PRICE_SOURCE[market];
        const subParts = [ago ? 'as of ' + ago : null, src].filter(Boolean);
        return `<div class="idv-quote-label">Last Price</div>
            <span class="idv-quote-price">${esc(price)}</span>
            ${snap.currency ? `<span class="idv-quote-ccy">${esc((snap.currency || '').toUpperCase())}</span>` : ''}
            ${delta}
            ${subParts.length ? `<span class="idv-quote-sub">${esc(subParts.join(' · '))}</span>` : ''}`;
    }

    // Performance grid (1D / 7D / 30D / 1Y). Rendered first as loading placeholders so
    // the section keeps a consistent shape, then filled by loadPerformance(). Values
    // that aren't available (market has no provider, or nothing cached) stay N/A.
    function perfGridHtml(perf, loading) {
        return PERF_PERIODS.map(p => {
            const v = perf ? perf[p.key] : undefined;
            let cls = 'muted', text = loading ? '…' : 'N/A';
            if (v != null) {
                cls = v > 0 ? 'up' : (v < 0 ? 'down' : 'flat');
                text = (v > 0 ? '+' : '') + v.toFixed(2) + '%';
            }
            return `<div class="idv-perf-card ${cls}">
                <div class="idv-fact-label">${esc(p.label)}</div>
                <div class="idv-perf-value">${esc(text)}</div>
            </div>`;
        }).join('');
    }

    async function loadPerformance(d, token) {
        const grid = document.getElementById('idv-perf');
        if (!grid) return;
        let data = null, perf = null;
        try {
            const r = await fetch(`/betelgeuse/api/instrument/${encodeURIComponent(d.market)}/${encodeURIComponent(d.symbol)}/performance`);
            data = await r.json();
            perf = data && data.performance ? data.performance : null;
        } catch (e) { data = null; perf = null; }
        if (token !== openToken || !grid.isConnected) return;   // a newer open() superseded this

        // Quote hero (last price + 24h delta)
        const quote = document.getElementById('idv-quote');
        if (quote) {
            const hasPrice = data && fmtPrice(data.price, data.currency) != null;
            quote.className = 'idv-quote' + (hasPrice ? '' : ' muted');
            quote.innerHTML = quoteHtml(data, false, d.market);
        }

        grid.innerHTML = perfGridHtml(perf, false);

        // Explain N/A when there's nothing to show, mirroring the Overview footnote.
        const note = document.getElementById('idv-perf-note');
        if (note) {
            const hasAny = perf && Object.values(perf).some(v => v != null);
            if (hasAny) note.textContent = '';
            else if (data && data.available === false)
                note.textContent = 'No market-data provider for this market yet.';
            else
                note.textContent = 'Market data isn’t available for this instrument yet.';
        }
    }

    // ── chart providers ──
    async function renderAastocks(host, d, token) {
        host.innerHTML = `<div class="idv-loading" style="grid-column:1/-1"><div class="idv-spinner"></div><span>Loading charts…</span></div>`;
        try {
            if (!settingsCache) {
                const res = await fetch('/betelgeuse/api/settings');
                settingsCache = await res.json();
            }
        } catch (e) { settingsCache = settingsCache || {}; }
        if (token !== openToken) return;

        const cells = AASTOCKS_PERIODS.map(p => {
            const tpl = settingsCache[p.key];
            if (!tpl) return '';
            const url = tpl.replace('{stockid}', encodeURIComponent(d.symbol));
            return `<div class="idv-chart-item">
                <div class="idv-chart-head">
                    <span class="idv-chart-label">${esc(p.label)}</span>
                    <button class="idv-copy" title="Copy image URL">📋 Copy URL</button>
                </div>
                <img class="idv-chart-img" src="${esc(url)}" alt="${esc(d.symbol)} ${esc(p.label)} chart" loading="lazy">
            </div>`;
        }).filter(Boolean).join('');

        host.innerHTML = cells || `<div class="idv-placeholder"><div class="big">⚙️</div><div class="msg">No chart URL templates configured.</div><div class="hint">Set them in Settings → Tracker → HK.</div></div>`;
        host.querySelectorAll('.idv-copy').forEach(btn => btn.addEventListener('click', () => {
            const img = btn.closest('.idv-chart-item').querySelector('.idv-chart-img');
            copyText(img.src, btn);
        }));
    }

    function renderCrypto(host, d, token) {
        const coinId = (d.reference && d.reference.coin_id) || (d.symbol || '').toLowerCase();
        host.innerHTML = CRYPTO_PERIODS.map(p => `
            <div class="idv-chart-item">
                <div class="idv-chart-head">
                    <span class="idv-chart-label">${esc(p.label)}</span>
                    <button class="idv-copy" disabled title="CoinGecko charts have no shareable URL">📋 Copy URL</button>
                </div>
                <div class="idv-cell" id="idv-cell-${p.period}"><div class="idv-loading"><div class="idv-spinner"></div><span>Loading…</span></div></div>
            </div>`).join('');
        CRYPTO_PERIODS.forEach(p => loadCryptoCell(coinId, p.period, token));
    }

    async function loadCryptoCell(coinId, period, token) {
        const cell = document.getElementById(`idv-cell-${period}`);
        if (!cell) return;
        try {
            const res = await fetch(`/betelgeuse/api/crypto/${encodeURIComponent(coinId)}/chart/${period}`);
            if (token !== openToken || !cell.isConnected) return;
            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                cell.innerHTML = `<div class="idv-loading"><span>✗ ${esc(e.error || ('No chart for "' + coinId + '"'))}</span></div>`;
                return;
            }
            const data = await res.json();
            if (token !== openToken) return;
            cell.innerHTML = `<img src="${esc(data.url)}?t=${Date.now()}" alt="${esc(coinId)} ${esc(period)}d chart">`;
        } catch (e) {
            if (token === openToken && cell.isConnected) cell.innerHTML = `<div class="idv-loading"><span>✗ ${esc(e.message)}</span></div>`;
        }
    }

    function renderStock(host, d, token) {
        host.innerHTML = STOCK_PERIODS.map(p => `
            <div class="idv-chart-item">
                <div class="idv-chart-head">
                    <span class="idv-chart-label">${esc(p.label)}</span>
                    <button class="idv-copy" disabled title="No shareable URL for these charts">📋 Copy URL</button>
                </div>
                <div class="idv-cell" id="idv-cell-${p.period}"><div class="idv-loading"><div class="idv-spinner"></div><span>Loading…</span></div></div>
            </div>`).join('');
        STOCK_PERIODS.forEach(p => loadStockCell(d.market, d.symbol, p.period, token));
    }

    async function loadStockCell(market, symbol, period, token) {
        const cell = document.getElementById(`idv-cell-${period}`);
        if (!cell) return;
        try {
            const res = await fetch(`/betelgeuse/api/stock/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/chart/${period}`);
            if (token !== openToken || !cell.isConnected) return;
            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                cell.innerHTML = `<div class="idv-loading"><span>✗ ${esc(e.error || ('No chart for "' + symbol + '"'))}</span></div>`;
                return;
            }
            const data = await res.json();
            if (token !== openToken) return;
            cell.innerHTML = `<img src="${esc(data.url)}?t=${Date.now()}" alt="${esc(symbol)} ${esc(period)}d chart">`;
        } catch (e) {
            if (token === openToken && cell.isConnected) cell.innerHTML = `<div class="idv-loading"><span>✗ ${esc(e.message)}</span></div>`;
        }
    }

    function renderNoCharts(host, d, meta) {
        host.innerHTML = `<div class="idv-placeholder">
            <div class="big">📊</div>
            <div class="msg">Charting isn't wired up for ${esc(meta.name)} yet.</div>
            <div class="hint">When a chart provider is added for this market, it will appear here automatically.</div>
        </div>`;
    }

    async function copyText(text, btn) {
        const original = btn.textContent;
        try { await navigator.clipboard.writeText(text); }
        catch (e) {
            const ta = document.createElement('textarea');
            ta.value = text; document.body.appendChild(ta); ta.select();
            try { document.execCommand('copy'); } catch (_) {}
            document.body.removeChild(ta);
        }
        btn.textContent = '✓ Copied';
        setTimeout(() => { btn.textContent = original; }, 1500);
    }

    window.InstrumentDetail = { open, close, PROVIDERS, MARKET_META };
})();
