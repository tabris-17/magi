/*
 * SymbolField — a reusable, context-aware instrument-symbol input.
 *
 * Point it at any <input> and it provides per-market behaviour:
 *   - live caret-safe upper-casing as you type
 *   - canonical normalization on blur / Enter (HK -> 00700.HK, JP -> 7203.T, ...)
 *   - a hint line under the box: a live "-> normalized" preview, or an amber
 *     warning (e.g. crypto: "expect BTC, not bitcoin")
 *   - a market-aware placeholder
 *
 * It is deliberately behaviour-agnostic: callers wire what *happens* via options
 * (onResolve = e.g. name lookup; onEnter = e.g. run a search). The same component
 * backs both the Portfolio add-form Symbol box and the Tracker search box.
 *
 * Globals exposed: window.SYMBOL_RULES, window.SymbolField.
 */
(function () {
    'use strict';

    // Per-market rules. `live` MUST preserve length (caret-safe). `normalize` is the
    // canonical form (mirrors normalize_symbol() on the backend). `hint` returns
    // {type:'info'|'warn', text} or null.
    const SYMBOL_RULES = {
        hk: {
            placeholder: '0700  →  00700.HK',
            live: v => v.toUpperCase(),
            normalize: v => {
                let s = v.trim().toUpperCase();
                if (!s) return '';
                if (s.endsWith('.HK')) s = s.slice(0, -3);   // don't double-suffix
                s = s.replace(/\.+$/, '');
                if (/^\d+$/.test(s)) s = s.padStart(5, '0');  // 5-digit zero-pad
                return s + '.HK';
            },
            hint: v => v.trim() ? { type: 'info', text: '→ ' + SYMBOL_RULES.hk.normalize(v) } : null
        },
        jp: {
            placeholder: '7203  →  7203.T',
            live: v => v.toUpperCase(),
            normalize: v => {
                // Like HK's suffix rule but the code is ALPHANUMERIC (no zero-pad). Always XXXX.T.
                let s = v.trim().toUpperCase();
                if (!s) return '';
                if (s.endsWith('.T')) s = s.slice(0, -2);   // don't double-suffix
                s = s.replace(/\.+$/, '');
                return s + '.T';
            },
            hint: v => v.trim() ? { type: 'info', text: '→ ' + SYMBOL_RULES.jp.normalize(v) } : null
        },
        us: {
            placeholder: 'AAPL  →  AAPL.NASDAQ',
            live: v => v.toUpperCase(),
            normalize: v => v.trim().toUpperCase(),
            hint: () => null,
            // Async: look the ticker up across US boards and offer "<SYMBOL>.<BOARD>"
            // suggestions (the board is the extension). Multiple listings → all shown.
            suggest: async v => {
                const q = v.trim().toUpperCase();
                if (!q) return [];
                try {
                    const res = await fetch(`/betelgeuse/api/lookup/us?symbol=${encodeURIComponent(q)}`);
                    if (!res.ok) return [];
                    const d = await res.json();
                    return (d.matches || []).map(m => ({
                        value: `${m.symbol}.${m.board}`,
                        label: `${m.symbol}.${m.board}${m.name ? ' · ' + m.name : ''}`
                    }));
                } catch (e) { return []; }
            }
        },
        crypto: {
            placeholder: 'BTC  (ticker, not "bitcoin")',
            live: v => v.toUpperCase(),
            normalize: v => v.trim().toUpperCase(),
            hint: v => {
                const s = v.trim();
                if (s.length > 3) {
                    return { type: 'warn', text: `Use the ticker, not the name — expected something like “BTC”, not “${s.toLowerCase()}”.` };
                }
                return null;
            }
        }
    };

    // Inject the component's styles once. Class names are namespaced (sf-*) so they
    // don't collide with page CSS and stay consistent across pages.
    let stylesInjected = false;
    function injectStyles() {
        if (stylesInjected) return;
        stylesInjected = true;
        const css = `
            .sf-hint {
                min-height: 1.05rem;
                margin-top: 0.4rem;
                font-size: 0.75rem;
                line-height: 1.05rem;
                color: #6e7681;
                word-break: break-word;
            }
            .sf-hint-info { color: #6e7681; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
            .sf-hint-warn { color: #d29922; }
            input.sf-input-warn { border-color: rgba(210,153,34, 0.55); }
            input.sf-input-warn:focus {
                border-color: rgba(210,153,34, 0.8);
                box-shadow: 0 0 0 3px rgba(210,153,34, 0.12);
            }
            .sf-suggest { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.4rem; }
            .sf-suggest:empty { margin-top: 0; }
            .sf-suggest-label { flex: 1 0 100%; font-size: 0.72rem; color: #6e7681; }
            .sf-suggest-chip {
                font-size: 0.72rem; padding: 0.22rem 0.55rem; border-radius: 6px; cursor: pointer;
                background: rgba(47,129,247, 0.12); border: 1px solid rgba(88,166,255, 0.3);
                color: #79c0ff; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; font-family: inherit;
            }
            .sf-suggest-chip:hover { background: rgba(47,129,247, 0.25); border-color: rgba(88,166,255, 0.55); }
        `;
        const style = document.createElement('style');
        style.setAttribute('data-symbol-field', '');
        style.textContent = css;
        document.head.appendChild(style);
    }

    class SymbolField {
        /**
         * @param {HTMLInputElement} input
         * @param {object} opts
         * @param {() => string}  opts.getMarket        required — current market key
         * @param {(sym,mkt)=>void} [opts.onResolve]    debounced + on-blur (e.g. name lookup)
         * @param {(sym,mkt)=>void} [opts.onEnter]      on Enter (e.g. run a search)
         * @param {(mkt,rule)=>string} [opts.placeholder] override the placeholder text
         * @param {number} [opts.resolveDelay=500]      debounce for onResolve (ms)
         * @param {boolean} [opts.normalizeOnBlur=true]  rewrite the box to canonical on blur
         * @param {boolean} [opts.selectOnFocus=false]   select-all when the box (re)gains
         *        focus AND right after Enter, so you can immediately retype a new symbol
         *        (address-bar style). Intended for search boxes, not fill-once form fields.
         */
        constructor(input, opts = {}) {
            injectStyles();
            this.input = input;
            this.getMarket = opts.getMarket || (() => '');
            this.onResolve = opts.onResolve || null;
            this.onEnter = opts.onEnter || null;
            this.placeholderFn = opts.placeholder || null;
            this.resolveDelay = opts.resolveDelay != null ? opts.resolveDelay : 500;
            this.normalizeOnBlur = opts.normalizeOnBlur !== false;
            this.selectOnFocus = opts.selectOnFocus === true;
            this._selectGuard = false;
            this._debounce = null;
            this._suggestTimer = null;
            this._suggestToken = 0;
            this._lastResolveKey = '';

            input.setAttribute('autocomplete', 'off');
            input.setAttribute('spellcheck', 'false');

            // Auto-create the hint line + suggestion row right after the input.
            this.hintEl = document.createElement('div');
            this.hintEl.className = 'sf-hint';
            this.hintEl.setAttribute('role', 'status');
            this.hintEl.setAttribute('aria-live', 'polite');
            input.insertAdjacentElement('afterend', this.hintEl);

            this.suggestEl = document.createElement('div');
            this.suggestEl.className = 'sf-suggest';
            this.hintEl.insertAdjacentElement('afterend', this.suggestEl);

            this._onInput = this._onInput.bind(this);
            this._onBlur = this._onBlur.bind(this);
            this._onKeydown = this._onKeydown.bind(this);
            this._onFocus = this._onFocus.bind(this);
            this._onMouseup = this._onMouseup.bind(this);
            input.addEventListener('input', this._onInput);
            input.addEventListener('blur', this._onBlur);
            input.addEventListener('keydown', this._onKeydown);
            input.addEventListener('focus', this._onFocus);
            input.addEventListener('mouseup', this._onMouseup);

            this.refresh();
        }

        rule() { return SYMBOL_RULES[this.getMarket()] || null; }

        normalizedValue() {
            const r = this.rule();
            return r ? r.normalize(this.input.value) : this.input.value.trim().toUpperCase();
        }

        _applyLive() {
            const r = this.rule();
            if (!r) return;
            const start = this.input.selectionStart, end = this.input.selectionEnd;
            const before = this.input.value, after = r.live(before);
            if (after !== before) {
                this.input.value = after;
                try { this.input.setSelectionRange(start, end); } catch (e) { /* unsupported */ }
            }
        }

        _renderHint() {
            const r = this.rule();
            const h = r ? r.hint(this.input.value) : null;
            if (h) {
                this.hintEl.textContent = h.text;
                this.hintEl.className = 'sf-hint ' + (h.type === 'warn' ? 'sf-hint-warn' : 'sf-hint-info');
                this.input.classList.toggle('sf-input-warn', h.type === 'warn');
            } else {
                this.hintEl.textContent = '';
                this.hintEl.className = 'sf-hint';
                this.input.classList.remove('sf-input-warn');
            }
        }

        _applyPlaceholder() {
            const r = this.rule();
            if (this.placeholderFn) {
                this.input.placeholder = this.placeholderFn(this.getMarket(), r) || '';
            } else {
                this.input.placeholder = r ? r.placeholder : '';
            }
        }

        _fireResolve() {
            if (!this.onResolve) return;
            const sym = this.normalizedValue();
            const mkt = this.getMarket();
            if (!sym || !mkt) return;
            const key = mkt + '|' + sym.toLowerCase();
            if (key === this._lastResolveKey) return;   // skip duplicate (blur after debounce)
            this._lastResolveKey = key;
            this.onResolve(sym, mkt);
        }

        _clearSuggest() { this.suggestEl.innerHTML = ''; }

        _renderSuggest(list) {
            this.suggestEl.innerHTML = '';
            if (!list || !list.length) return;
            if (list.length > 1) {
                const lbl = document.createElement('span');
                lbl.className = 'sf-suggest-label';
                lbl.textContent = 'Multiple boards — pick one:';
                this.suggestEl.appendChild(lbl);
            }
            list.forEach(item => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'sf-suggest-chip';
                chip.textContent = item.label;
                chip.title = item.value;
                // mousedown (not click) so we act before the input's blur fires.
                chip.addEventListener('mousedown', e => {
                    e.preventDefault();
                    this._selectSuggestion(item.value);
                });
                this.suggestEl.appendChild(chip);
            });
        }

        async _fireSuggest() {
            const r = this.rule();
            if (!r || !r.suggest) { this._clearSuggest(); return; }
            const token = ++this._suggestToken;
            let list = [];
            try { list = (await r.suggest(this.input.value)) || []; } catch (e) { list = []; }
            if (token !== this._suggestToken) return;   // a newer keystroke superseded this
            this._renderSuggest(list);
        }

        _selectSuggestion(value) {
            this.input.value = value;
            this._applyLive();
            this._renderHint();
            this._clearSuggest();
            this.input.focus();
            this._fireResolve();
        }

        _onInput() {
            const r = this.rule();
            this._applyLive();
            this._renderHint();
            if (this.onResolve) {
                clearTimeout(this._debounce);
                this._debounce = setTimeout(() => this._fireResolve(), this.resolveDelay);
            }
            if (r && r.suggest) {
                clearTimeout(this._suggestTimer);
                this._suggestTimer = setTimeout(() => this._fireSuggest(), this.resolveDelay);
            } else {
                this._clearSuggest();
            }
        }

        _onBlur() {
            clearTimeout(this._debounce);
            if (this.normalizeOnBlur) {
                const norm = this.normalizedValue();
                if (norm !== this.input.value) this.input.value = norm;
            }
            this._renderHint();
            this._fireResolve();
            // Delay so a suggestion-chip mousedown can still register first.
            setTimeout(() => this._clearSuggest(), 150);
        }

        _onKeydown(e) {
            if (e.key !== 'Enter') return;
            const norm = this.normalizedValue();
            if (norm !== this.input.value) this.input.value = norm;
            this._renderHint();
            if (this.onEnter) {
                e.preventDefault();
                if (!norm) return;
                this.onEnter(norm, this.getMarket());
                // Leave the just-searched symbol selected so the next keystroke replaces it.
                if (this.selectOnFocus) this.input.select();
            }
        }

        _onFocus() {
            if (!this.selectOnFocus) return;
            // Select-all on (re)focus. Guard the click's upcoming mouseup so it doesn't
            // collapse the selection to a caret — address-bar behaviour.
            this._selectGuard = true;
            this.input.select();
        }

        _onMouseup(e) {
            if (this.selectOnFocus && this._selectGuard) {
                e.preventDefault();     // keep the focus-time select-all (don't collapse to caret)
                this._selectGuard = false;
            }
        }

        /** Re-apply placeholder + live transform + hint + suggestions (call after the market changes). */
        refresh() {
            this._applyPlaceholder();
            this._applyLive();
            this._renderHint();
            const r = this.rule();
            if (r && r.suggest && this.input.value.trim()) this._fireSuggest();
            else this._clearSuggest();
        }

        /** Force an immediate onResolve (e.g. after a market switch). */
        resolveNow() {
            this._lastResolveKey = '';
            this._fireResolve();
        }

        destroy() {
            clearTimeout(this._debounce);
            clearTimeout(this._suggestTimer);
            this.input.removeEventListener('input', this._onInput);
            this.input.removeEventListener('blur', this._onBlur);
            this.input.removeEventListener('keydown', this._onKeydown);
            this.input.removeEventListener('focus', this._onFocus);
            this.input.removeEventListener('mouseup', this._onMouseup);
            if (this.hintEl && this.hintEl.parentNode) this.hintEl.parentNode.removeChild(this.hintEl);
            if (this.suggestEl && this.suggestEl.parentNode) this.suggestEl.parentNode.removeChild(this.suggestEl);
        }
    }

    window.SYMBOL_RULES = SYMBOL_RULES;
    window.SymbolField = SymbolField;
})();
