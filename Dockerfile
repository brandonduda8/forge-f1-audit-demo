# Forge F1 Audit Demo — Render Dockerfile
#
# Build context expectation: the repo (or bundle) root contains
#   audit-demo/server.py, audit-demo/audit_pipeline.py
#   f1-audit-product/tools/f1_measure.py, f1-audit-product/tools/render_pdf.py
#   f1-audit-product/samples/*.pdf
# Use render/package.sh to assemble this layout from the workspace.
#
# NOTE: image build is UNVERIFIED on Render until first deploy (Docker cannot
# run on the build box — sandbox setns wall). Render builds it on first push.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- deps (stdlib + 3 tiny pure-python packages; ~15MB) ---
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- app code ---
COPY audit-demo/ ./audit-demo/
COPY f1-audit-product/ ./f1-audit-product/

# --- 12-factor wiring (server.py/audit_pipeline.py read these; see RENDER_DEPLOY.md) ---
ENV BIND=0.0.0.0 \
    F1_DIR=/app/f1-audit-product \
    F1_SAMPLES=/app/f1-audit-product/samples \
    AUDIT_PYBIN=/usr/local/bin/python
# PORT is injected by Render at runtime; default 10000 for local docker runs.
ENV PORT=10000
EXPOSE 10000

# Health endpoint: the demo serves / on GET; Render health check hits /.
# ThreadingHTTPServer serves the marketing/demo page at / — 200 OK.
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/health').read(1)"

WORKDIR /app/audit-demo
CMD ["python", "server.py"]
