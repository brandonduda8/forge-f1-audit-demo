// Contract test: render REAL responses captured from the local API server
// (written by selfserve/api/tests/test_server_e2e.py) with the page code.
// Usage: node contract.mjs <free-check.json> <report.json>
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import fs from "node:fs";
import { doc, walk } from "./fakedom.mjs";

const require = createRequire(import.meta.url);
const C = require("../common.js");
const R = require("../report.js");
const A = require("../app.js");
const cfg = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", paymentLink: "https://buy.stripe.com/test_abc", price: "$29" } });

const mini = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const rep = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const m = A.renderMiniResult(doc, mini, cfg);
const mt = m.textContent;
assert.ok(mt.includes(mini.grade), "mini result shows the grade");
assert.ok(mt.includes(String(mini.score)), "mini result shows the score");
for (const t of mini.top) assert.ok(mt.includes(t.title), "mini result shows top issue: " + t.title);

const r = R.renderReport(doc, rep, cfg, rep.pdf_url.split("session_id=")[1]);
const rt = r.textContent;
assert.ok(rt.includes(rep.site), "report shows the site");
assert.ok(rt.includes(rep.grade) && rt.includes(String(rep.score)), "report shows grade and score");
for (const c of rep.report.categories) assert.ok(rt.includes(c.name), "category " + c.name);
for (const f of rep.report.findings) {
  for (const k of ["title", "measured", "fix", "effort"]) assert.ok(rt.includes(f[k]), `finding ${f.id} ${k} shown`);
}
for (const p of rep.report.passed) assert.ok(rt.includes(p.title), "passed " + p.id);
for (const n of rep.report.not_measured) assert.ok(rt.includes(n.label), "not measured " + n.id);
let pdf = null;
walk(r, (n) => { if (n.nodeType === 1 && (n.getAttribute("href") || "").includes("/api/report.pdf")) pdf = n.getAttribute("href"); });
assert.equal(pdf, "https://api.example.test" + rep.pdf_url, "PDF link points at the API");
console.log(`contract ok: free check (${mini.top.length} issues) and report (${rep.report.findings.length} findings, ${rep.report.passed.length} passed, ${rep.report.not_measured.length} not measured) render from real API output`);
