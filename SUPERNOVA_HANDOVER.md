# Supernova — Full Handover for Another LLM

## The Three Commands

| Command | What It Does |
|---------|---------------|
| **Launch** | Starts the Python server on port 8080 — makes the dashboard accessible at `http://192.168.1.72:8080` |
| **Update** | Fetches fresh RSS data from all configured feeds and saves to `archive.json` (runs every 5 mins automatically too) |
| **Push** | Bakes `archive.json` → `news.json`, copies HTML files to the Website repo, commits & pushes to GitHub (triggers Cloudflare deploy) |

---

## Files Involved

| File | Location | Purpose |
|------|----------|---------|
| `server.py` | `/root/.openclaw/workspace/hello-world/` | Python backend — handles RSS fetching, API endpoints (`/api/news`, `/api/update`, `/api/feeds`), serves HTML |
| `archive.json` | `/root/.openclaw/workspace/hello-world/` | **Local RSS cache** — the live database of fetched articles (500 per feed max) |
| `feeds.json` | `/root/.openclaw/workspace/hello-world/` | **Feed config** — defines which RSS feeds to fetch, their categories, and visibility (Chris/Hazel/both) |
| `dashboard.html` | `/root/.openclaw/workspace/hello-world/` | Main UI — profile picker, weather, news feed, filters |
| `index.html` | `/root/.openclaw/workspace/hello-world/` | Hub page with links to dashboard |
| `admin.html` | `/root/.openclaw/workspace/hello-world/` | Feed management UI (save → updates `feeds.json`) |
| `push_to_git.py` | `/root/.openclaw/workspace/hello-world/` | The push script — bakes + commits + pushes to GitHub |
| `news.json` | `/root/.openclaw/workspace/Website/` | **Static bake** — generated from `archive.json`, served by Cloudflare when Pi is offline |

---

## Data Flow

```
RSS Sources (BBC, Guardian, etc.)
        ↓ fetch
server.py (runs on Pi)
        ↓ saves
archive.json (local cache)
        ↓ [Push]
news.json (GitHub)
        ↓
Cloudflare Pages → https://your-site.pages.dev
```

---

## How to Run Each Command

**Launch:**
```bash
cd /root/.openclaw/workspace/hello-world && nohup python3 server.py > server.log 2>&1 &
```

**Update (manual trigger):**
```bash
curl http://localhost:8080/api/update
```

**Push to GitHub:**
```bash
python3 /root/.openclaw/workspace/hello-world/push_to_git.py
```

---

## Key Details

- **Pi IP:** 192.168.1.72 (port 8080)
- **Cloudflare URL:** Serves `news.json` when Pi is offline
- **Auto-update:** Runs every 5 minutes in background
- **Archive limit:** 500 items per feed (oldest dropped when exceeded)
- **Date fix (2026-03-07):** Push script now parses RFC/ISO dates properly instead of sorting alphabetically
