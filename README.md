# CanopyAI - Predictive Forest Intelligence

A local "Global Forest Watch"-class platform, but **predictive and agentic** instead
of retrospective. Focused on **Canadian forests (Ontario)**, it pulls near-real-time
NASA FIRMS fire data, runs **ML change-detection**, **forecasts where fire is likely
next** from live weather, estimates **carbon impact**, writes an **AI situation
briefing**, and pushes **alerts to your phone** — automatically, every hour.

> **The pitch:** Global Forest Watch tells you a forest *was* lost last month.
> CanopyAI tells you which forest is *about to* burn tomorrow, explains why in plain
> English, estimates the carbon at stake, and texts the ranger before it spreads.

Python (FastAPI) backend + a colorful Leaflet dashboard. No database — everything is
fetched live.

## Features

| | What | Source |
|---|---|---|
| 🟥 **Region-change zones** | Map cells colored by today-vs-yesterday change (flaring up vs cooling down) | NASA FIRMS |
| 🔮 **Predictive risk forecast** | "Where burns next" zones = recent fire density × live Fire Weather Risk | Open-Meteo (free, no key) |
| 🤖 **AI situation briefings** | Natural-language analyst briefing + recommended actions | Claude (`claude-opus-4-8`) |
| 🌍 **Carbon & impact** | Estimated area burned, CO₂ emitted, trees-equivalent | derived from FRP |
| 🧠 **ML change-detection** | DBSCAN clustering + z-score anomaly → new fire clusters | scikit-learn |
| 🚨 **Phone alerts** | Hourly auto-checks, adjustable thresholds, test button, alert log | Telegram (free) |
| 🎬 **Visuals** | Heatmap layer + time-lapse player + per-province breakdown + trend chart | — |

```
forest-watch/
├── backend/
│   ├── main.py        FastAPI app + endpoints
│   ├── config.py      regions, thresholds, runtime settings
│   ├── firms.py       NASA FIRMS fetch + stats
│   ├── analysis.py    ML change-detection (DBSCAN + anomaly)
│   ├── weather.py     Open-Meteo + Fire Weather Risk index
│   ├── risk.py        region-change grid + predictive risk zones
│   ├── impact.py      carbon / area / trees estimator
│   ├── ai.py          Claude situation briefings (graceful fallback)
│   ├── alerts.py      Telegram delivery
│   └── scheduler.py   hourly automated pipeline
├── frontend/          Leaflet + Chart.js + heatmap dashboard
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

### 1. NASA FIRMS key (free, instant, required)
https://firms.modaps.eosdis.nasa.gov/api/map_key/ — enter your email, copy the key.

### 2. Telegram bot (free, required for alerts)
Message **@BotFather** → `/newbot` → copy the token. Then message your new bot once.

### 3. Groq key (optional — for AI briefings)
https://console.groq.com/ — without it, briefings use a plain template.

### 4. Install + configure
```bash
cd forest-watch
cp .env.example .env        # paste FIRMS_MAP_KEY, TELEGRAM_BOT_TOKEN, (optional GROQ_API_KEY)
python -m venv .venv && .venv\Scripts\Activate.ps1   # mac/linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Run
```bash
uvicorn backend.main:app --reload --port 8000
```
Open **http://localhost:8000**. Toggle map layers, scrub the time-lapse, click
**✨ Generate briefing**, and set up alerts in the Alerts panel
(**Find my chat ID → Save → Test alert**). The hourly checker runs automatically.

## Key endpoints
| Endpoint | What |
|---|---|
| `GET /api/grid` | Region-change zones (today vs yesterday) |
| `GET /api/risk` | Predictive risk-forecast zones (weather + density) |
| `GET /api/impact` | Estimated area / CO₂ / trees |
| `GET /api/timelapse` | Fires grouped by day for the animation |
| `POST /api/briefing` | AI situation briefing |
| `POST /api/analysis/run` | Run the ML day-over-day comparison now |
| `POST /api/alert/test` | Send a test Telegram alert |
| `GET /api/stats` / `/api/health` | Dashboard numbers / status |

## Notes & honesty
- NASA FIRMS is **near-real-time** (~3h behind a satellite pass); 5 days is its max window for a region this size.
- The **carbon/impact numbers are first-order estimates** (constants documented in `impact.py`), not a research emissions model — the UI labels them "(est)".
- The risk forecast is a **transparent heuristic** (density × fire-weather index), a strong differentiator vs. a detection-only map — and a clean seam to drop in a trained model later.

## Roadmap
| Next | Source | Notes |
|---|---|---|
| Forest-cover loss overlay | Hansen/UMD (GFW) | Public XYZ tiles, often no key |
| True deforestation ML | Sentinel-2 NDVI (Copernicus/GEE) | Swap into `analysis.py` / `risk.py` |
| Deforestation alerts | GLAD / RADD (GFW API) | Free GFW key |
| WhatsApp / SMS | Twilio | Add alongside Telegram in `alerts.py` |
