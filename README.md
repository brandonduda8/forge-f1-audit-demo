# Forge F1 Audit Demo

A small web app that audits a local business's homepage. Paste a business name
and URL, and it fetches the live site, measures it against a fixed checklist,
scores it out of 100 with a letter grade, and hands back a branded PDF report.

Pure Python: the web server is the standard library's `ThreadingHTTPServer`, and
the pipeline uses `requests`, `beautifulsoup4` and `fpdf2`. No database, no API
keys, no paid services.

## How it works

```
browser ──POST /audit──▶ audit-demo/server.py ──spawns──▶ audit-demo/audit_pipeline.py
                                                            │
                     1. f1-audit-product/tools/f1_measure.py   live fetch → <slug>-evidence.json
                     2. score()                                pinned thresholds → scoring.json
                     3. build_report()                         measured values only → report.md
                     4. f1-audit-product/tools/render_pdf.py   report.md → audit.pdf
```

- **Measured, not guessed.** Every value in a report traces to a fetch made
  during that run. The 26 checks cover speed and weight (server response
  time, page and image weight, render-blocking assets), mobile basics
  (viewport, tap-to-call), SEO (title, meta description, headings,
  LocalBusiness schema, Google Business Profile link, TLS certificate,
  sitemap/robots), lead capture (visible phone, contact form, calls to action,
  quote path, hours and service area) and trust signals (reviews, about/owner,
  license and insurance, social links, guarantees, broken links).
- **Honest about gaps.** Checks that need a real browser render (LCP, CLS, TBT,
  tap-target size, the mobile menu) are recorded as *Not measured* and left out
  of the score's denominator rather than estimated.
- **Each finding comes with a fix:** a severity (CRITICAL / MAJOR / MINOR), a
  plain-English problem, a rough effort estimate, and what to do about it.
- **Web flow:** a landing page with three sample reports, a progress page that
  polls the background job, and a PDF download. `GET /health` returns 200 for
  health checks.

Sample reports produced by the pipeline are in
[`f1-audit-product/samples/`](f1-audit-product/samples/).

## Quickstart (local)

Requires Python 3.9+ (the pipeline uses `zoneinfo`).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt lxml   # see note on lxml below

cd audit-demo
export F1_DIR="$PWD/../f1-audit-product"
export F1_SAMPLES="$F1_DIR/samples"
export AUDIT_PYBIN="$(command -v python)"
PORT=8000 python server.py
# open http://127.0.0.1:8000
```

Or run one audit from the command line, without the web server:

```bash
cd audit-demo
python audit_pipeline.py "Example Domain" https://example.com /tmp/f1-job
# prints {"ok": true, "score": ..., "grade": ..., "pdf": "/tmp/f1-job/audit.pdf"}
```

> **Note on lxml:** `f1_measure.py` parses HTML with BeautifulSoup's `lxml`
> parser, but `lxml` is not yet listed in `requirements.txt`. Install it
> alongside the requirements as shown above, or the measurement step fails with
> `FeatureNotFound: lxml`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `18951` | HTTP port |
| `BIND` | `127.0.0.1` | Bind address (`0.0.0.0` in the container) |
| `F1_DIR` | a path in the author's workspace | Folder holding `tools/f1_measure.py` and `tools/render_pdf.py` |
| `F1_SAMPLES` | a path in the author's workspace | Folder of sample PDFs shown on the landing page |
| `AUDIT_PYBIN` | a venv in the author's workspace | Python interpreter used to run the pipeline steps |

The defaults point at the author's own machine, so set all three paths when
running from a clone.

## Deploy

The repo includes a `Dockerfile` (Python 3.12 slim) and a Render Blueprint
(`render.yaml`) for a single free-tier web service. Notes on the free-tier
footprint are in [`RENDER_DEPLOY.md`](RENDER_DEPLOY.md). That document was
written before this repo existed and refers to `render/...` paths; in this repo
the same files sit at the root. The Docker image installs only
`requirements.txt`, so it needs `lxml` added there before a deployed audit can
succeed.

## Repo layout

```
audit-demo/server.py           web flow: form, background jobs, progress page, PDF download
audit-demo/audit_pipeline.py   measure → score → report → PDF glue, pinned rubric and fix copy
f1-audit-product/tools/        f1_measure.py (live measurements), render_pdf.py (Markdown → PDF)
f1-audit-product/samples/      three sample PDF reports
Dockerfile, render.yaml        container and Render deploy config
```

## Tests

There is no automated test suite or CI yet. The pipeline was last checked by
hand by running `audit_pipeline.py` against `https://example.com`.

## License

[MIT](LICENSE)
