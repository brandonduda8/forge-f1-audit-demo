// Contract test: render REAL responses captured from the local API server
// (written by selfserve/api/tests/test_server_e2e.py) with the page code.
// Usage: node contract.mjs <free-check.json> <report.json>
//          [--plans=plans.json] [--dashboard=dashboard.json] [--admin=admin-summary.json]
// The report may be a Pro report (report.pages). Each optional file is checked against the
// shape the pages are built for, then rendered.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import fs from "node:fs";
import { doc, walk } from "./fakedom.mjs";

const require = createRequire(import.meta.url);
const C = require("../common.js");
const R = require("../report.js");
const A = require("../app.js");
const D = require("../dashboard.js");
const AD = require("../admin.js");
const cfg = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", price: "$29" } });

const positional = process.argv.slice(2).filter((a) => !a.startsWith("--"));
const flags = Object.fromEntries(process.argv.slice(2).filter((a) => a.startsWith("--")).map((a) => {
  const i = a.indexOf("=");
  return [a.slice(2, i < 0 ? undefined : i), i < 0 ? "" : a.slice(i + 1)];
}));
const load = (p) => JSON.parse(fs.readFileSync(p, "utf8"));
const mini = load(positional[0]);
const rep = load(positional[1]);
const done = [];

const m = A.renderMiniResult(doc, mini, cfg);
const mt = m.textContent;
assert.ok(mt.includes(mini.grade), "mini result shows the grade");
assert.ok(mt.includes(String(mini.score)), "mini result shows the score");
for (const t of mini.top) assert.ok(mt.includes(t.title), "mini result shows top issue: " + t.title);
assert.ok(Array.isArray(mini.sections) && mini.sections.length === 2, "free check has the two separate sections");
for (const s of mini.sections) {
  assert.ok(mt.includes(s.name + ": "), "mini result shows section " + s.name);
  if (s.top) assert.ok(mt.includes(s.top.title), "mini result shows the section's top issue: " + s.top.title);
}

const orderId = rep.pdf_url.split("order_id=")[1];
const r = R.renderReport(doc, rep, cfg, orderId);
const rt = r.textContent;
assert.ok(rt.includes(rep.site), "report shows the site");
assert.ok(rt.includes(rep.grade) && rt.includes(String(rep.score)), "report shows grade and score");
for (const c of rep.report.categories) assert.ok(rt.includes(c.name), "category " + c.name);
for (const f of rep.report.findings) {
  for (const k of ["title", "measured", "fix", "effort"]) assert.ok(rt.includes(f[k]), `finding ${f.id} ${k} shown`);
}
for (const s of rep.report.sections) {
  assert.ok(rt.includes(s.name), "section " + s.name);
  for (const f of s.findings) {
    for (const k of ["title", "measured", "fix", "effort"]) assert.ok(rt.includes(f[k]), `section finding ${f.id} ${k} shown`);
  }
}
for (const p of rep.report.passed) assert.ok(rt.includes(p.title), "passed " + p.id);
for (const n of rep.report.not_measured) assert.ok(rt.includes(n.label), "not measured " + n.id);
let pdf = null, dash = null;
walk(r, (n) => {
  if (n.nodeType !== 1) return;
  const h = n.getAttribute("href") || "";
  if (h.includes("/api/report.pdf")) pdf = h;
  if (h.startsWith("dashboard.html")) dash = h;
});
assert.equal(pdf, "https://api.example.test" + rep.pdf_url, "PDF link points at the API");
assert.equal(dash, "dashboard.html?order_id=" + orderId, "report links to its dashboard");
done.push(`report (${rep.report.findings.length} findings, ${rep.report.passed.length} passed, ${rep.report.not_measured.length} not measured)`);

// Pro: report.pages = [{url, path, title, issues: [{id, title, severity, detail}], passed: int, not_measured: [str]}]
if (rep.report.pages !== undefined) {
  const pages = rep.report.pages;
  assert.ok(Array.isArray(pages) && pages.length <= 4, "Pro report has at most 4 extra pages");
  for (const pg of pages) {
    for (const k of ["url", "path"]) assert.equal(typeof pg[k], "string", `page ${k} is a string`);
    assert.ok(pg.title === null || typeof pg.title === "string", "page title is a string or null");
    assert.ok(Number.isInteger(pg.passed) && pg.passed >= 0, "page passed is a count");
    assert.ok(Array.isArray(pg.not_measured) && pg.not_measured.every((x) => typeof x === "string"), "page not_measured is a list of strings");
    assert.ok(Array.isArray(pg.issues), "page issues is a list");
    for (const it of pg.issues) for (const k of ["id", "title", "severity", "detail"]) assert.equal(typeof it[k], "string", `page issue ${k}`);
    assert.ok(rt.includes(pg.path), "page shown: " + pg.path);
    for (const it of pg.issues) assert.ok(rt.includes(it.title) && rt.includes(it.detail), "page issue shown: " + it.title);
    for (const n of pg.not_measured) assert.ok(rt.includes(n), "page not-measured shown: " + n);
  }
  done.push(`Pro pages (${pages.length})`);
}

// GET /api/plans -> {ok, plans: [{id, label, cents, kind, available}]}
if (flags.plans) {
  const plans = load(flags.plans);
  assert.equal(plans.ok, true);
  assert.ok(Array.isArray(plans.plans), "plans is a list");
  for (const p of plans.plans) {
    assert.ok(C.isPlanId(p.id), "plan id " + p.id);
    assert.equal(typeof p.label, "string");
    assert.ok(Number.isInteger(p.cents) && p.cents > 0, "plan cents is a positive integer");
    assert.ok(["one_time", "subscription"].includes(p.kind), "plan kind " + p.kind);
    assert.equal(typeof p.available, "boolean");
  }
  const ids = plans.plans.map((p) => p.id);
  for (const id of ["checkup", "pro", "monitor"]) assert.ok(ids.includes(id), "plans lists " + id);
  const byId = C.plansById(plans);
  assert.equal(byId.monitor.kind, "subscription");
  assert.equal(byId.checkup.kind, "one_time");
  done.push(`plans (${ids.join(", ")})`);
}

// GET /api/dashboard -> {ok, site, plan, report_path, subscription|null, history: [{at, grade, score}], latest_changes|null}
if (flags.dashboard) {
  const d = load(flags.dashboard);
  assert.equal(d.ok, true);
  for (const k of ["site", "plan", "report_path"]) assert.equal(typeof d[k], "string", "dashboard " + k);
  assert.ok(C.reportHref(d.report_path, "") !== "", "report_path is a same-site report link");
  assert.ok(d.subscription === null || typeof d.subscription === "object", "subscription is an object or null");
  if (d.subscription) for (const k of ["id", "status", "start_date", "charged_through_date", "canceled_date"]) assert.ok(k in d.subscription, "subscription." + k);
  assert.ok(Array.isArray(d.history), "history is a list");
  for (const h of d.history) for (const k of ["at", "grade", "score"]) assert.ok(k in h, "history." + k);
  assert.ok(d.latest_changes === null || (typeof d.latest_changes === "object" && Array.isArray(d.latest_changes.new_problems) &&
    Array.isArray(d.latest_changes.fixed) && "score_change" in d.latest_changes), "latest_changes shape");
  const did = C.reportHref(d.report_path, "").split("order_id=")[1];
  const node = D.renderDashboard(doc, d, did);
  const t = node.textContent;
  assert.ok(t.includes(d.site), "dashboard shows the site");
  assert.ok(t.includes(C.planName(d.plan)), "dashboard shows the plan");
  for (const h of d.history) if (C.num(h.score) !== null) assert.ok(t.includes(C.scoreText(h.score) + "/100"), "history score shown");
  if (d.latest_changes) for (const x of [...d.latest_changes.new_problems, ...d.latest_changes.fixed]) assert.ok(t.includes(x), "change shown: " + x);
  done.push(`dashboard (${d.plan}, ${d.history.length} checks)`);
}

// GET /api/admin/summary -> sales_summary + counters + monitoring
if (flags.admin) {
  const s = load(flags.admin);
  assert.equal(s.ok, true);
  for (const k of ["days", "orders", "gross_cents", "refunded_cents", "net_cents", "active_subscriptions", "mrr_cents"]) {
    assert.ok(Number.isInteger(s[k]), "summary " + k + " is an integer");
  }
  assert.equal(typeof s.by_plan, "object");
  for (const [id, v] of Object.entries(s.by_plan)) { assert.ok(C.isPlanId(id)); assert.ok(Number.isInteger(v.count) && Number.isInteger(v.gross_cents)); }
  assert.ok(Array.isArray(s.recent) && s.recent.length <= 20, "recent is a list of at most 20");
  for (const x of s.recent) {
    for (const k of ["created", "plan", "amount_cents", "site", "refunded"]) assert.ok(k in x, "recent." + k);
    assert.ok(!String(x.site).includes("@"), "recent sales never carry emails");
  }
  for (const k of ["free_checks", "checkouts_started", "reports_delivered"]) assert.ok(Number.isInteger(s.counters[k]), "counters." + k);
  assert.ok(s.monitoring === null || ["last_run_at", "checked", "emailed", "errors"].every((k) => k in s.monitoring), "monitoring shape");
  const t = AD.renderSummary(doc, s).textContent;
  assert.ok(t.includes(C.dollars(s.net_cents, true)), "net shown in dollars");
  assert.ok(t.includes(C.dollars(s.mrr_cents, true)), "MRR shown in dollars");
  for (const x of s.recent) assert.ok(t.includes(x.site), "recent sale shown: " + x.site);
  done.push(`admin summary (${s.orders} orders)`);
}

console.log(`contract ok: free check (${mini.top.length} issues), ${done.join(", ")} render from real API output`);
