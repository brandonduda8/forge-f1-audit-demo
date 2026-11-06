"""render_pdf.py turns a report.md into a PDF offline."""
import os
import subprocess
import sys

import audit_pipeline as ap
import render_pdf
from conftest import ROOT


def test_md_inline():
    assert render_pdf.md_inline("**bold** and *it* — `code`") == \
        "<b>bold</b> and <i>it</i> -- <font face='courier'>code</font>"


def test_md_to_html_structure():
    html = render_pdf.md_to_html("# T\n## H2\n### H3\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n---\n> q")
    assert "<h1>T</h1>" in html and "<h2>H2</h2>" in html and "<h3>" in html
    assert "<ul>" in html and "<li>a</li>" in html
    assert "<table" in html and "</table>" in html and "<hr/>" in html
    assert "<blockquote><i>q</i></blockquote>" in html


def test_render_full_report_pdf(tmp_path, fake_web):
    import f1_measure
    from conftest import ACME, acme_routes
    fake_web(acme_routes())
    ev = f1_measure.measure("Acme Plumbing", ACME + "/", str(tmp_path))
    md = ap.build_report("Acme Plumbing", ACME + "/", ev, ap.score(ev), "2026-11-05",
                         "acme-plumbing.test")
    src, dst = tmp_path / "report.md", tmp_path / "audit.pdf"
    src.write_text(md)
    script = os.path.join(ROOT, "f1-audit-product", "tools", "render_pdf.py")
    r = subprocess.run([sys.executable, script, str(src), str(dst)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    data = dst.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) >= 5000  # audit_pipeline treats <5000 bytes as a failed render
