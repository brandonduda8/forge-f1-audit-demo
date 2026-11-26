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

// GET /api/plans -> {ok, plans: [{id, label, cents, kind, available, first_cents?}]}
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
    if ("first_cents" in p) assert.ok(Number.isInteger(p.first_cents) && p.first_cents > 0, "first_cents is a positive integer");
  }
  const ids = plans.plans.map((p) => p.id);
  for (const id of ["checkup", "pro", "monitor"]) assert.ok(ids.includes(id), "plans lists " + id);
  const byId = C.plansById(plans);
  assert.equal(byId.monitor.kind, "subscription");
  assert.equal(byId.checkup.kind, "one_time");
  const mon = plans.plans.find((p) => p.id === "monitor");
  if (mon.first_cents && mon.first_cents !== mon.cents) {
    assert.equal(C.priceText(byId.monitor), C.dollars(mon.first_cents) + " for the first month, then " + C.dollars(mon.cents) + " a month");
  }
  done.push(`plans (${ids.join(", ")})`);
}

// GET /api/dashboard -> {ok, site, plan, report_path, subscription|null, history: [{at, grade, score}], latest_changes|null}
// (server.dashboard; latest_changes is monitor.diff: {score_change, new: [{id,title,severity}], fixed: [...], ...})
if (flags.dashboard) {
  const d = load(flags.dashboard);
  assert.equal(d.ok, true);
  for (const k of ["site", "plan", "report_path"]) assert.equal(typeof d[k], "string", "dashboard " + k);
  assert.ok(C.reportHref(d.report_path, "") !== "", "report_path is a same-site report link");
  assert.ok(d.subscription === null || typeof d.subscription === "object", "subscription is an object or null");
  if (d.subscription) for (const k of ["status", "start_date", "charged_through_date", "canceled_date"]) assert.ok(k in d.subscription, "subscription." + k);
  assert.ok(Array.isArray(d.history), "history is a list");
  for (const h of d.history) {
    for (const k of ["at", "grade", "score"]) assert.ok(k in h, "history." + k);
    assert.equal(typeof h.at, "number", "history at is epoch seconds");
  }
  const lc = d.latest_changes;
  assert.ok(lc === null || (typeof lc === "object" && Array.isArray(lc.new) && Array.isArray(lc.fixed) && "score_change" in lc), "latest_changes shape");
  if (lc) for (const x of [...lc.new, ...lc.fixed]) for (const k of ["id", "title", "severity"]) assert.equal(typeof x[k], "string", "change." + k);
  const did = C.reportHref(d.report_path, "").split("order_id=")[1];
  const node = D.renderDashboard(doc, d, did);
  const t = node.textContent;
  assert.ok(t.includes(d.site), "dashboard shows the site");
  assert.ok(t.includes(C.planName(d.plan)), "dashboard shows the plan");
  for (const h of d.history) {
    if (C.num(h.score) !== null) assert.ok(t.includes(C.scoreText(h.score) + "/100"), "history score shown");
    assert.ok(t.includes(C.formatWhen(h.at)) && C.formatWhen(h.at) !== "", "history date shown");
  }
  if (lc) for (const x of [...lc.new, ...lc.fixed]) assert.ok(t.includes(x.title), "change shown: " + x.title);
  assert.equal(t.includes("Your Monthly Monitoring terms"), d.plan === "monitor", "renewal terms only for monitoring");
  if (d.subscription && d.subscription.canceled_date) assert.ok(t.includes("Ends on"), "a cancelled subscription says when it ends");
  done.push(`dashboard (${d.plan}, ${d.history.length} checks)`);
}

// GET /api/admin/summary -> {ok, counters, monitoring: {running, last_run|null}, sales: sales_summary|null, sales_error?}
if (flags.admin) {
  const a = load(flags.admin);
  assert.equal(a.ok, true);
  for (const k of ["free_checks", "checkouts_started", "reports_delivered"]) assert.ok(Number.isInteger(a.counters[k]), "counters." + k);
  assert.equal(typeof a.monitoring, "object");
  assert.equal(typeof a.monitoring.running, "boolean", "monitoring.running");
  const lr = a.monitoring.last_run;
  assert.ok(lr === null || (typeof lr.started === "number" && (lr.finished === null || typeof lr.finished === "number")), "last_run times");
  if (lr && lr.counts) for (const k of ["sites", "scanned", "skipped_recent", "not_checked", "emails_sent", "emails_skipped"]) assert.ok(Number.isInteger(lr.counts[k]), "counts." + k);
  const s = a.sales;
  assert.ok(s === null || typeof s === "object", "sales is an object or null");
  if (s === null) assert.equal(typeof a.sales_error, "string", "sales_error explains a missing sales block");
  const t = AD.renderSummary(doc, a).textContent;
  if (s) {
    for (const k of ["days", "orders", "gross_cents", "refunded_cents", "net_cents", "active_subscriptions", "mrr_cents"]) {
      assert.ok(Number.isInteger(s[k]), "sales " + k + " is an integer");
    }
    assert.equal(typeof s.by_plan, "object");
    for (const [id, v] of Object.entries(s.by_plan)) { assert.ok(C.isPlanId(id)); assert.ok(Number.isInteger(v.count) && Number.isInteger(v.gross_cents)); }
    assert.ok(Array.isArray(s.recent) && s.recent.length <= 20, "recent is a list of at most 20");
    for (const x of s.recent) {
      for (const k of ["created", "plan", "amount_cents", "site", "refunded"]) assert.ok(k in x, "recent." + k);
      assert.ok(!String(x.site).includes("@"), "recent sales never carry emails");
    }
    assert.ok(t.includes(C.dollars(s.net_cents, true)), "net shown in dollars");
    assert.ok(t.includes(C.dollars(s.mrr_cents, true)), "MRR shown in dollars");
    for (const x of s.recent) assert.ok(t.includes(x.site), "recent sale shown: " + x.site);
    if (Number.isInteger(s.cancelling)) assert.ok(t.includes(s.cancelling + " more cancelled"), "cancelling shown");
  } else {
    assert.ok(t.includes(a.sales_error), "sales_error shown");
  }
  if (lr && lr.counts) assert.ok(t.includes("Monitored sites") && t.includes(String(lr.counts.sites)), "last monitoring run shown");
  done.push(`admin summary (${s ? s.orders + " orders" : "no sales"})`);
}

console.log(`contract ok: free check (${mini.top.length} issues), ${done.join(", ")} render from real API output`);
