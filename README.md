# XenoBiz — Operations Console

Business-grade feedback system with issue memory, policy enforcement, flow control, and handoff discipline.

## What It Does
- Tracks issues by tag with counts and 24h windows
- Applies resolution policies and enforcement flags deterministically
- Controls conversation flow (progress, stall, repeat, resolve)
- Escalates to human review when required
- Enforces non-speculation and scope limits post-LLM

## Quick Start (Windows)
1. `python -m venv venv`
2. `venv\Scripts\activate`
3. `pip install -r requirements.txt`
4. Set `GROQ_API_KEY` in your environment
5. `python app.py`
6. Open `http://127.0.0.1:5000`

## Environment Variables
- `GROQ_API_KEY` (required)
- `SECRET_KEY` (optional; defaults to `quantumshade_secret`)
- `XENOBIZ_DEBUG` (optional; set to `1` to enable debug + auto-open browser)

## Runtime Notes
- Stateless on boot (no preloaded memory)
- Deterministic per input (behavior driven by DB + rules)
- No session dependency beyond SQLite

## Operational Controls
- Handoff lock triggers on critical issues or repeated safety/boundary tags
- 12+ reports of same tag within 24h forces handoff
- Resolution confirmation required before closing if issue is not resolved

## Reset Database
`python init_db.py`

## UI Notes
- Voice input uses browser speech recognition
- Voice disclaimer is shown below the input
- Signals panel stays visible at all times

## Production Checklist
- Remove hardcoded secrets (done)
- Use environment variables for keys
- Disable debug mode for production
- Rotate keys if ever committed

## Render Deployment
1. Push this repo to GitHub.
2. In Render, create a new Web Service from this repo.
3. Render can auto-detect `render.yaml`, or set manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
4. Add environment variables in Render dashboard:
   - `GROQ_API_KEY` (required)
   - `SECRET_KEY` (required)
   - `XENOBIZ_DEBUG=0`

## Security Notes
- Do not log raw user content in production
- Handoff requires contact info before proceeding
- Enforced failure messages are generic by design
