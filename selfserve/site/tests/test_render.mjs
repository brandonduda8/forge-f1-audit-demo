// Offline render test: no npm packages. Loads the pure render functions from
// report.js / app.js with a tiny fake DOM and checks hostile API strings stay text.
import { createRequire } from "node:module";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const C = require("../common.js");
const R = require("../report.js");
const A = require("../app.js");

// ---- minimal fake DOM (only what the render code uses) ----
class FakeText {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}
class FakeElement {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = [];
    this.attributes = new Map(); this.style = {}; this.parentNode = null; this.listeners = {};
  }
  get className() { return this.attributes.get("class") || ""; }
  set className(v) { this.attributes.set("class", String(v)); }
  get firstChild() { return this.children[0] || null; }
  appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
  get hidden() { return this.attributes.has("hidden"); }
  set hidden(v) { if (v) this.attributes.set("hidden", ""); else this.attributes.delete("hidden"); }
  get disabled() { return this.attributes.has("disabled"); }
  set disabled(v) { if (v) this.attributes.set("disabled", ""); else this.attributes.delete("disabled"); }
  get id() { return this.attributes.get("id") || ""; }
  removeAttribute(k) { this.attributes.delete(k); }
  focus() { focused = this; }
  matches(sel) {
    let m;
    if ((m = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel))) return this.attributes.has(m[1]) && (m[2] === undefined || this.attributes.get(m[1]) === m[2]);
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) { const out = []; walk(this, (n) => { if (n !== this && n.nodeType === 1 && n.matches(sel)) out.push(n); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  click() { (this.listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); }
  submit() { (this.listeners.submit || []).forEach((fn) => fn({ preventDefault() {} })); }
  removeChild(n) { this.children = this.children.filter((c) => c !== n); n.parentNode = null; return n; }
  setAttribute(k, v) { this.attributes.set(String(k), String(v)); }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (v !== "") this.appendChild(new FakeText(v)); }
}
const doc = {
  createElement: (t) => new FakeElement(t),
  createTextNode: (t) => new FakeText(t),
};

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function serialize(n) {
  if (n.nodeType === 3) return esc(n.data);
  const tag = n.tagName.toLowerCase();
  const attrs = [...n.attributes].map(([k, v]) => ` ${k}="${esc(v)}"`).join("");
  const style = Object.keys(n.style).length ? ` style="${esc(Object.entries(n.style).map(([k, v]) => `${k}:${v}`).join(";"))}"` : "";
  return `<${tag}${attrs}${style}>${n.children.map(serialize).join("")}</${tag}>`;
}
function walk(n, fn) { fn(n); if (n.children) n.children.forEach((c) => walk(c, fn)); }
function assertNoInjectedMarkup(root) {
  walk(root, (n) => {
    if (n.nodeType !== 1) return;
    assert.ok(!["IMG", "SCRIPT", "IFRAME", "SVG", "OBJECT"].includes(n.tagName), `unexpected <${n.tagName}> element`);
    for (const [k, v] of n.attributes) {
      assert.ok(!/^on/i.test(k), `event handler attribute ${k}`);
      if (k === "href") assert.ok(/^(https:\/\/|#)/.test(v), `unsafe href ${v}`);
    }
  });
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

const EVIL = "<img src=x onerror=alert(1)>";
const cfg = C.getConfig({ APEX_CONFIG: {
  apiBase: "https://api.example.test/", paymentLink: "https://buy.stripe.com/test_abc", price: "$29", supportEmail: "",
} });

test("config is validated and trailing slash stripped", () => {
  assert.equal(cfg.apiBase, "https://api.example.test");
  assert.equal(cfg.payOk, true);
  const bad = C.getConfig({ APEX_CONFIG: { apiBase: "javascript:alert(1)", paymentLink: "https://evil.test/x" } });
  assert.equal(bad.apiOk, false);
  assert.equal(bad.payOk, false);
  assert.equal(C.getConfig({}).apiOk, false);
});

test("session id validation", () => {
  assert.equal(R.isValidSessionId("cs_test_a1B2c3D4e5F6g7"), true);
  assert.equal(R.isValidSessionId("cs_live_" + "a".repeat(200)), true);
  assert.equal(R.isValidSessionId("cs_live_" + "a".repeat(201)), false);
  assert.equal(R.isValidSessionId("cs_test_short"), false);
  assert.equal(R.isValidSessionId("cs_test_abc123456789<script>"), false);
  assert.equal(R.isValidSessionId("pi_test_abcdefghijkl"), false);
  assert.equal(R.isValidSessionId(""), false);
  assert.equal(R.isValidSessionId(null), false);
});

test("pdf link only accepts the expected API path", () => {
  const id = "cs_test_abcdefghijkl";
  assert.equal(R.pdfHref(cfg, "/api/report.pdf?session_id=" + id, id), "https://api.example.test/api/report.pdf?session_id=" + id);
  assert.equal(R.pdfHref(cfg, "javascript:alert(1)", id), "https://api.example.test/api/report.pdf?session_id=" + id);
  assert.equal(R.pdfHref(cfg, "//evil.test/x", id), "https://api.example.test/api/report.pdf?session_id=" + id);
});

test("report renders hostile API strings as text", () => {
  const data = {
    ok: true, site: EVIL, score: EVIL, grade: EVIL, measured_at: EVIL, pdf_url: "javascript:alert(1)",
    report: {
      categories: [{ name: EVIL, score: 50, weight: 25 }, { name: "Speed", score: 250, weight: "x" }],
      findings: [
        { id: EVIL, severity: EVIL, title: EVIL, measured: EVIL, why: EVIL, fix: EVIL, effort: EVIL },
        { id: "1.1", severity: "CRITICAL", title: "Slow server", measured: "1900 ms", why: "w", fix: "f", effort: "1 hour" },
      ],
      passed: [{ id: "2.1", title: EVIL, measured: EVIL }],
      not_measured: [{ id: "1.2", label: EVIL, reason: EVIL }],
    },
  };
  const node = R.renderReport(doc, data, cfg, "cs_test_abcdefghijkl");
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"), "hostile string should be escaped text");
  assert.ok(!html.includes("<img"), "no raw <img> in output");
  assert.ok(html.includes('class="sev sev-low"'), "unknown severity falls back to low class");
  assert.ok(html.includes('class="sev sev-critical"'));
  assert.ok(html.includes('class="grade"') && html.includes(">?</span>"), "bad grade shows ?");
  assert.ok(html.includes("Score: not measured<"), "non-numeric score says not measured");
  assert.ok(html.includes("width:100%"), "category bar is clamped to 100%");
  assert.ok(html.includes('href="https://api.example.test/api/report.pdf?session_id=cs_test_abcdefghijkl"'));
  assert.ok(html.includes("Bookmark this page"));
  assert.ok(html.includes("Top 3 fixes") && html.includes("Not measured (1)") && html.includes("What passed (1)"));
});

test("report handles a missing report body", () => {
  const node = R.renderReport(doc, { ok: true, site: "example.com", score: 80, grade: "B" }, cfg, "cs_test_abcdefghijkl");
  const html = serialize(node);
  assert.ok(html.includes("No failing checks were found"));
  assert.ok(html.includes('class="grade grade-b"'));
});

test("mini-check renders hostile API strings as text", () => {
  const data = { ok: true, site: EVIL, score: 62, grade: "C", failing: 9, passed: 12, not_measured: 17,
    measured_at: "2026-11-23T10:00:00Z",
    top: [{ severity: EVIL, title: EVIL }, { severity: "HIGH", title: "No tap-to-call link" }, { severity: "MEDIUM", title: "t" }, { severity: "LOW", title: "4th is dropped" }] };
  const node = A.renderMiniResult(doc, data, cfg);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("4th is dropped"), "only top 3 shown");
  assert.ok(html.includes("+ 6 more issues found so far"));
  assert.ok(html.includes("62/100"));
  assert.ok(html.includes('href="https://buy.stripe.com/test_abc"'));
  assert.ok(html.includes("On the checkout page, type the same website address"));
});

test("mini-check never links to a non-Stripe payment link", () => {
  const badCfg = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", paymentLink: "javascript:alert(1)" } });
  const node = A.renderMiniResult(doc, { ok: true, site: "example.com", score: 90, grade: "A", top: [], failing: 0 }, badCfg);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(!html.includes("javascript:"));
  assert.ok(html.includes('href="#price"'));
});

test("error messages come from text, with fallbacks", () => {
  assert.equal(A.friendlyStatusMessage(429, { ok: false, error: EVIL }), EVIL); // later set via textContent
  assert.match(A.friendlyStatusMessage(503, null), /busy/);
  assert.match(A.friendlyStatusMessage(500, {}), /code 500/);
});

// ---- page flow tests: run init() against a fake page and a fake fetch ----
let focused = null;
function page(ids, extra) {
  const body = new FakeElement("body");
  ids.forEach((id) => { const e = new FakeElement(id === "check-form" ? "form" : id === "site-url" ? "input" : id === "check-btn" ? "button" : "div"); e.setAttribute("id", id); body.appendChild(e); });
  (extra || []).forEach((e) => body.appendChild(e));
  return Object.assign({}, doc, {
    body, title: "",
    getElementById: (id) => body.querySelectorAll("[id]").find((n) => n.id === id) || null,
    querySelectorAll: (sel) => body.querySelectorAll(sel),
  });
}
const calls = [];
function mockFetch(routes) {
  calls.length = 0;
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || "GET", body: opts.body });
    const key = Object.keys(routes).find((k) => url.includes(k));
    const r = key ? routes[key] : { status: 404, body: { ok: false, error: "nope" } };
    if (r === "network") throw new TypeError("Failed to fetch");
    const next = Array.isArray(r) ? r.shift() : r;
    return { status: next.status, text: async () => JSON.stringify(next.body) };
  };
}
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const win = (search) => ({ APEX_CONFIG: { apiBase: "https://api.example.test", paymentLink: "https://buy.stripe.com/test_abc", price: "$29" }, location: { search }, printed: 0, print() { this.printed++; } });
const SID = "cs_test_abcdefghijkl";
const REPORT = { ok: true, site: "example.com", score: 62, grade: "C", measured_at: "2026-11-23T10:00:00Z", pdf_url: "/api/report.pdf?session_id=" + SID,
  report: { categories: [{ name: "Speed", score: 50, weight: 25 }], findings: [{ id: "1.1", severity: "CRITICAL", title: "Slow", measured: "m", why: "w", fix: "f", effort: "e" }], passed: [], not_measured: [] } };

test("report page: bad session id makes no request", async () => {
  mockFetch({});
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=cs_test_x"));
  await tick();
  assert.equal(calls.length, 0);
  assert.match(d.getElementById("report-status").textContent, /missing its payment reference/);
});

test("report page: 200 renders report, print button works", async () => {
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  const d = page(["report-status", "report-root"]); const w = win("?session_id=" + SID);
  R.init(d, w);
  await tick();
  assert.equal(calls[0].url, "https://api.example.test/api/report?session_id=" + SID);
  assert.equal(d.getElementById("report-status").hidden, true);
  const root = d.getElementById("report-root");
  assert.match(root.textContent, /Website report for example\.com/);
  root.querySelector('[data-action="print"]').click();
  assert.equal(w.printed, 1);
  assert.match(d.title, /example\.com/);
});

test("report page: 402 shows the server's reason, or a payment message", async () => {
  mockFetch({ "/api/report?": { status: 402, body: { ok: false, error: "This payment was refunded, so this report link no longer works." } } });
  let d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=" + SID));
  await tick();
  assert.equal(d.getElementById("report-status").textContent, "This payment was refunded, so this report link no longer works.");
  mockFetch({ "/api/report?": { status: 402, body: { ok: false } } });
  d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=" + SID));
  await tick();
  assert.equal(d.getElementById("report-status").textContent, "We couldn't find a completed payment for this link.");
});

test("find page: lists only same-site report links, as text", async () => {
  const F = require("../find.js");
  const d = page(["find-result"]);
  const list = d.getElementById("find-result");
  const n = F.renderMatches(d, list, { reports: [
    { site: "<b>roof</b>.com", created: 1700000000, path: "report.html?session_id=" + SID },
    { site: "evil", created: 1, path: "https://evil.example/report.html?session_id=" + SID },
    { site: "evil2", created: 1, path: "javascript:alert(1)" }] });
  assert.equal(n, 1);
  const html = serialize(list);
  assert.ok(html.includes('href="report.html?session_id=' + SID + '"'));
  assert.ok(!html.includes("evil") && !html.includes("javascript:") && !html.includes("<b>"));
});

test("report page: 422 refundable offers retry and automatic refund", async () => {
  mockFetch({ "/api/report?": [{ status: 422, body: { ok: false, error: EVIL, refundable: true } }, { status: 200, body: REPORT }],
    "/api/refund": { status: 200, body: { ok: true, status: "pending" } } });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=" + SID));
  await tick();
  const st = d.getElementById("report-status");
  assert.equal(st.textContent, EVIL); // stored as a text node, never parsed
  assert.ok(st.children.every((c) => c.nodeType === 3));
  const buttons = d.getElementById("report-root").querySelectorAll("button");
  assert.deepEqual(buttons.map((b) => b.textContent), ["Try again", "Refund me automatically"]);
  buttons[1].click();
  await tick();
  const refundCall = calls.find((c) => c.url.endsWith("/api/refund"));
  assert.equal(refundCall.method, "POST");
  assert.deepEqual(JSON.parse(refundCall.body), { session_id: SID });
  assert.equal(st.textContent, "Refund issued \u2014 it shows on your card in 5\u201310 days.");
});

test("report page: refund error shows server text and re-enables button", async () => {
  mockFetch({ "/api/report?": { status: 422, body: { ok: false, error: "Site unreachable", refundable: true } },
    "/api/refund": { status: 409, body: { ok: false, error: "Already refunded" } } });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=" + SID));
  await tick();
  const btn = d.getElementById("report-root").querySelectorAll("button")[1];
  btn.click();
  await tick();
  assert.equal(d.getElementById("report-status").textContent, "Already refunded");
  assert.equal(btn.disabled, false);
});

test("report page: network failure offers Try again", async () => {
  mockFetch({ "/api/report?": "network" });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?session_id=" + SID));
  await tick();
  assert.match(d.getElementById("report-status").textContent, /couldn't reach the checker/);
  assert.equal(d.getElementById("report-root").querySelector("button").textContent, "Try again");
});

test("index: wakes server, wires Buy buttons, runs the free check", async () => {
  mockFetch({ "/health": { status: 200, body: { ok: true } }, "/api/check": { status: 200,
    body: { ok: true, site: "example.com", score: 62, grade: "C", top: [{ severity: "CRITICAL", title: "Slow" }], failing: 9, passed: 12, not_measured: 17 } } });
  const buy = new FakeElement("a"); buy.setAttribute("data-buy", ""); buy.setAttribute("href", "#price");
  const price = new FakeElement("p"); price.setAttribute("data-price", "");
  const d = page(["check-form", "site-url", "check-btn", "check-status", "check-result", "pay-unavailable"], [buy, price]);
  A.init(d, win(""));
  assert.equal(calls[0].url, "https://api.example.test/health");
  assert.equal(buy.getAttribute("href"), "https://buy.stripe.com/test_abc");
  assert.equal(price.textContent, "$29");
  d.getElementById("site-url").value = "  example.com ";
  d.getElementById("check-form").submit();
  await tick();
  const call = calls.find((c) => c.url.endsWith("/api/check"));
  assert.equal(call.method, "POST");
  assert.deepEqual(JSON.parse(call.body), { url: "example.com" });
  const res = d.getElementById("check-result");
  assert.equal(res.hidden, false);
  assert.match(res.textContent, /\+ 8 more issues found so far/);
  assert.equal(d.getElementById("check-btn").disabled, false);
});

test("index: 429 error shown as text; network failure friendly", async () => {
  mockFetch({ "/health": { status: 200, body: {} }, "/api/check": [{ status: 429, body: { ok: false, error: EVIL } }, "network"] });
  const d = page(["check-form", "site-url", "check-btn", "check-status", "check-result"]);
  A.init(d, win(""));
  d.getElementById("site-url").value = "example.com";
  d.getElementById("check-form").submit();
  await tick();
  const st = d.getElementById("check-status");
  assert.equal(st.textContent, EVIL);
  assert.ok(st.children.every((c) => c.nodeType === 3));
  mockFetch({ "/api/check": "network" });
  d.getElementById("check-form").submit();
  await tick();
  assert.match(st.textContent, /couldn't reach the checker/);
});

test("index: missing config disables the form", async () => {
  mockFetch({});
  const d = page(["check-form", "site-url", "check-btn", "check-status", "check-result"]);
  A.init(d, { location: { search: "" } });
  assert.equal(calls.length, 0);
  assert.equal(d.getElementById("check-btn").disabled, true);
});

let passed = 0;
for (const [name, fn] of tests) {
  try { await fn(); passed++; console.log(`ok - ${name}`); }
  catch (e) { console.log(`FAIL - ${name}`); console.error(e); process.exitCode = 1; }
}
console.log(`\n${passed} passed, ${tests.length - passed} failed`);
