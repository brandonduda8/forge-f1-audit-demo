# RENDER_DEPLOY.md — Forge F1 Audit Demo → Render (free tier)

Prepared 2026-10-08. **NOT DEPLOYED.** Public exposure is Brandon's tap.
Nothing here connects the Render account, creates a service, or publishes a URL.

## What this is
The Forge F1 one-click audit demo (paste a business URL → real measurements →
scored report → branded PDF), packaged for Render's free tier:
- 1 web service, `free` plan: 512MB RAM / 0.1 CPU, $0/mo
- Pure stdlib Python + 3 tiny pip packages (`requests`, `beautifulsoup4`, `fpdf2`)
- No secrets, no database, no persistent disk (audit jobs are ephemeral)
- Sleeps after 15 min idle; ~30–60s cold start on first visit after sleep

## Files
- `render/Dockerfile` — container build (validated by inspection; image build
  happens on Render's side at first deploy — Docker cannot run on our box)
- `render/render.yaml` — Render Blueprint (one-click service creation)
- `render/requirements.txt` — the 3 pip deps (versions match the local venv:
  requests 2.34.2, beautifulsoup4 4.15.0, fpdf2 2.8.9)
- `render/package.sh` — assembles `render/bundle/` (108K, 11 files) from the workspace
- `render/bundle/` — the exact payload to push to GitHub

Code changes made for deploy (minimal, backwards-compatible, tailnet unaffected):
- `server.py`: `PORT`/`BIND`/`VENV_PY`/`SAMPLES` now read env vars
  (`PORT`, `BIND`, `AUDIT_PYBIN`, `F1_SAMPLES`) with the old values as defaults.
- `audit_pipeline.py`: `VENV_PY`/`F1` now read env vars (`AUDIT_PYBIN`, `F1_DIR`).

## Brandon's tap sequence (in order — each is his hands)
1. **Create the GitHub repo** (his tap — machine never creates accounts/repos):
   new repo, e.g. `apex-forge-audit-demo`, push the contents of `render/bundle/`
   to `main`. (Bundle is at
   `~/workspace/genesis-os/war-room/empire/showcase/audit-demo/render/bundle/`.)
2. **Render Dashboard → New → Blueprint**: connect the repo. Render reads
   `render.yaml` and proposes the `forge-audit-demo` web service on the
   **free** plan. Confirm plan = Free before applying.
3. **Apply.** Render builds the Docker image (~2–4 min first build) and serves
   the demo at `https://forge-audit-demo.onrender.com`.
4. **Verify**: open the URL → landing page loads → paste a business URL →
   real audit runs → PDF downloads. First visit after deploy may take
   ~60s (cold start); that's the free tier, not a bug.

## Monthly $0 footprint
| Item | Free-tier allowance | This deploy |
|---|---|---|
| Web service | 512MB / 0.1 CPU, 750 instance hrs/mo per workspace | 1 service, sleeps when idle → ~50–150 hrs/mo typical demo use |
| Bandwidth | 5 GB/mo (Hobby workspace, verified Aug 2026) | Demo pages + PDFs are KBs; effectively uncapped for this use |
| Build minutes | 500/mo | ~3 min per deploy; ~10–20 deploys/mo headroom |
| Services cap | 25 total | 1 used |

**Keep-awake math (optional):** a 10-min uptime ping keeps it always warm but
burns ~730 of the 750 monthly hours — leaving almost no headroom. Recommended:
let it sleep; the cold start is ~30–60s and fine for a sales demo link.

## What NOT to put on Render free tier
- **Genesis Browser engine** (Playwright/Chromium): needs 1GB+ RAM and real CPU;
  free tier's 512MB/0.1 CPU will OOM or crawl. The mission-control *UI* could
  ship as a static site, but without the engine it's a shell — not recommended
  as a first deploy.
- **Cron jobs / background workers**: NOT free on Render (cron from $1/mo,
  workers paid-only). Scheduled audits stay on our own box.
- **Postgres**: free 1GB instance expires ~30 days after creation — don't build
  anything durable on it.

## Secrets needed at deploy time
**None.** The demo calls no paid APIs, no model endpoints, no SMTP.
If a future version adds email delivery or a model call, those keys get entered
in the Render dashboard (never committed).

## Astra's pending consult should still answer
Her reply (requested 2026-10-07) hasn't arrived. It should still cover:
1. Her ranked monetization plays for a public audit-demo URL (the $75 audit
   funnel: demo → paid audit — does she agree this is play #1?).
2. Whether she wants the demo URL gated (email capture) or open.
3. Any Alliance service she wants co-deployed on the same free tier
   (25-service cap means room for ~24 more $0 services).
