# Copilot Instructions for Betelgeuse

## Project Overview

**Betelgeuse** is a stock and cryptocurrency tracker dashboard with portfolio management. It features a Flask backend serving interactive dashboards for tracking stocks across HK, Japan, and US markets, plus cryptocurrency assets. The app includes real-time chart fetching from AA Stocks and persistent portfolio storage.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server
python3 app.py

# Access the app
# Stock Tracker: http://localhost:8000/
# Portfolio Dashboard: http://localhost:8000/portfolio
```

## Architecture

### Frontend (Client-side)
- **index.html** - Stock tracker dashboard with regional tabs (HK, JP, US)
  - Modern dark theme with gradient accents
  - Regional market data display
  - Chart modal with AA Stocks integration
  - Vanilla JavaScript for interactivity
  
- **portfolio.html** - Portfolio management dashboard
  - Add/edit/delete stock positions
  - Market-specific formatting (e.g., auto-appends .HK for HK stocks)
  - Notes and analysis section per position
  - Persistent storage via SQLite

### Backend (Flask)
- **app.py** - Single Flask application with all routes and logic
  - Market data structure (MARKETS dict with regional breakdowns)
  - SQLite database initialization and operations
  - Chart download and caching from AA Stocks API
  - RESTful API endpoints for portfolio CRUD operations
  - Embedded static folder serving

### Database
- **portfolio.db** - SQLite database
  - `portfolio` table: id, symbol, market, name, added_date, comment, created_at
  - `settings` table: key, value (for configuration storage)
  - Auto-creates on first run via `init_db()` function
  - Markets: 'hk', 'jp', 'us', 'crypto'

⚠️ **CRITICAL: Database Changes Must Be Backward Compatible**
- **NEVER wipe the current database without backup**
- When making schema changes:
  1. **Create a backup first**: `cp portfolio.db portfolio.db.backup`
  2. **Test the changes thoroughly** with existing data
  3. **Verify all features still work** (CRUD operations, data retrieval)
  4. **Only delete the backup** after confirming everything works
- Use `ALTER TABLE` to add new columns when possible instead of recreating tables
- The `init_db()` function includes migration logic to add missing tables/columns to existing databases

### Static Assets
- **static/** - Cached GIF charts from AA Stocks
  - Naming: `chart_{symbol}_{period}.gif`
  - Cached after first fetch to reduce API calls

## Key Conventions

### Market Symbols
- **HK stocks**: Format as `XXXX.HK` (e.g., 0700.HK for Tencent)
- **Japan stocks**: Format as `XXXX.T` (e.g., 9984.T for SoftBank)
- **US stocks**: Any format accepted (e.g., AAPL, MSFT)
- **Crypto**: Any identifier (e.g., BTC, ETH)

Symbol auto-formatting happens in `add_portfolio_item()` route - no manual .HK/.T appending needed from user input.

### AA Stocks Chart Integration
- API endpoint: `https://charts.aastocks.com/servlet/Charts?...`
- `download_aa_stocks_chart()` function handles fetching and caching
- Charts cached as GIFs in `static/` to prevent re-downloading
- Called via `/api/stock/<stockid>/chart` endpoint
- Includes User-Agent header to avoid blocking

### API Routes Pattern
All routes return JSON:
- `GET /api/market/<market>` - Get market data (sample stocks)
- `GET /api/markets` - List all markets
- `GET /api/portfolio` - All portfolio items grouped by market
- `POST /api/portfolio` - Add new position (body: market, symbol, name, added_date, comment)
- `PUT /api/portfolio/<id>` - Update position
- `DELETE /api/portfolio/<id>` - Delete position
- `GET /api/stock/<stockid>/chart` - Get chart URL (downloads if not cached)

### Database Access Pattern
```python
conn = get_db_connection()
c = conn.cursor()
c.execute(...)
conn.commit()
conn.close()
```

Always close connections after use. Use `conn.row_factory = sqlite3.Row` for dict-like access.

### Frontend Data Flow
- HTML pages are vanilla (no framework)
- JavaScript uses `fetch()` API to call backend
- Modal dialogs for editing/viewing charts
- Live table rendering from API responses
- Dark theme with blue/teal gradients via CSS

## Adding New Features

### Add a new market
1. Update `MARKETS` dict in app.py
2. Add portfolio rendering in portfolio.html (see hk/jp/us sections)
3. Update symbol validation in `add_portfolio_item()` if needed

### Add new stock data source
1. Create function like `download_aa_stocks_chart()` in app.py
2. Create new `/api/` route
3. Call from frontend JavaScript

### Add new portfolio fields
1. Alter `portfolio` table schema in `init_db()`
2. Update form in portfolio.html
3. Update `add_portfolio_item()` and `update_portfolio_item()` routes
4. Update frontend JavaScript rendering

## Styling Notes

The app uses a consistent modern dark theme:
- Background: Dark blue gradient (#0f172a to #1e293b)
- Accents: Blue gradients (#3b82f6 to #60a5fa)
- Cards: Frosted glass effect (backdrop-filter + transparent background)
- Borders: Subtle rgba(148,163,184,0.1)
- Hover states: Smooth transitions with background lightening

At the end of any chat, randomly add a emoji cat or ascii cat with some meow gesture like "meow!"
