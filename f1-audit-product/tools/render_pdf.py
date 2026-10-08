#!/usr/bin/env python3
"""render_pdf.py — Forge F1 branded PDF render path (template -> PDF).

Usage: render_pdf.py <report.md> <output.pdf>
Converts a SAMPLE_REPORT-style markdown audit into a branded Apex Forge PDF.
Brand: dark slate header, forge-orange accent (#E8632B), clean sans body.
Requires: fpdf2 (pip install fpdf2).
"""
import re, sys, html
from fpdf import FPDF

ACCENT = (232, 99, 43)      # forge orange
DARK = (26, 32, 44)         # slate
GRAY = (100, 116, 139)
LIGHT_BG = (248, 250, 252)

def sanitize(s):
    return (s.replace("—", "--").replace("–", "-").replace("★", "*")
             .replace("©", "(c)").replace("→", "->").replace("≤", "<=")
             .replace("≥", ">=").replace("\u2019", "'").replace("\u201c", '"')
             .replace("\u201d", '"').replace("\u2026", "..."))

def md_inline(s):
    s = sanitize(s)
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<font face='courier'>\1</font>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    return s

def md_to_html(md):
    out, in_table, in_list = [], False, False
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("|") and ln.endswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if not in_table:
                out.append('<table border="1" width="100%" cellpadding="4">')
                in_table = True
            if all(set(c) <= set("-: ") for c in cells):
                i += 1; continue
            tag = "th" if len(out) and out[-1].endswith("<table border=\"1\" width=\"100%\" cellpadding=\"4\">") or (in_table and not any("<td" in x for x in out[-3:])) else "td"
            # simpler: first data row after header separator is header; treat first row as header
            out.append("<tr>" + "".join(f"<{tag}>{md_inline(c)}</{tag}>" for c in cells) + "</tr>")
            i += 1; continue
        if in_table:
            out.append("</table>"); in_table = False
        if ln.startswith("### "):
            out.append(f"<h3><font color='#{ACCENT[0]:02x}{ACCENT[1]:02x}{ACCENT[2]:02x}'>{md_inline(ln[4:])}</font></h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{md_inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{md_inline(ln[2:])}</h1>")
        elif ln.strip() == "---":
            out.append("<hr/>")
        elif ln.startswith("> "):
            out.append(f"<blockquote><i>{md_inline(ln[2:])}</i></blockquote>")
        elif re.match(r"^(\d+\.|-|\*) ", ln):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{md_inline(re.sub(r'^(\d+\.|-|\*) ', '', ln))}</li>")
        elif ln.strip() == "":
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("<p></p>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{md_inline(ln)}</p>")
        i += 1
    if in_table: out.append("</table>")
    if in_list: out.append("</ul>")
    return "\n".join(out)

class Report(FPDF):
    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*GRAY)
        self.cell(0, 8, "APEX FORGE  |  WEBSITE AUDIT", align="L")
        self.ln(10)
    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GRAY)
        self.cell(0, 10, f"Apex Forge  |  Page {self.page_no()}/{{nb}}", align="C")

def cover(pdf, title_lines):
    pdf.add_page()
    pdf.set_fill_color(*DARK)
    pdf.rect(0, 0, 210, 297, "F")
    pdf.set_y(80)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 12, "APEX FORGE", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 16, "WEBSITE AUDIT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(203, 213, 225)
    for t in title_lines:
        pdf.cell(0, 9, sanitize(t), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(12)
    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(*GRAY)
    pdf.cell(0, 8, "Measured, not asserted.", align="C")

def main():
    src, dst = sys.argv[1], sys.argv[2]
    md = open(src).read()
    # title lines from first heading block
    m = re.search(r"^# (.+)$", md, re.M)
    title = m.group(1) if m else "Website Audit"
    m2 = re.search(r"^\*\*Prepared for: (.+?)\*\*", md, re.M)
    prepared = m2.group(1) if m2 else ""
    pdf = Report()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, 20)
    pdf.set_margins(18, 15, 18)
    cover(pdf, [title, prepared[:80]])
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 41, 59)
    pdf.write_html(md_to_html(md))
    pdf.output(dst)
    print("wrote", dst)

if __name__ == "__main__":
    main()
