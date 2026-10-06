# Memory Tracker

Semiconductor memory research app with a chart gallery, industry and company overviews, and research questions backed by the research API.

## Setup

1. Install Python dependencies: `pip install -r backend/requirements.txt`.
2. Copy `backend/.env.example` to `backend/.env` and configure the API connection and credentials.
3. In `frontend`, run `npm ci` followed by `npm run build`.
4. From the project root, run `python start.py`.

The server uses port 8088 by default. For frontend development, run `npm run dev` in `frontend`.

Runtime data is stored under `%LOCALAPPDATA%/EquitySwarm/memory_chart` on Windows. Set `MEMORY_CHART_RUNTIME_DIR` to override that location. Credentials, downloaded research, and runtime data are excluded from this repository.
