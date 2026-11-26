// Offline render test: no npm packages. Loads the pure render functions from
// report.js / app.js with a tiny fake DOM and checks hostile API strings stay text.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import fs from "node:fs";

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
// our own pages, built only from a checked order id or a fixed anchor
const SAME_SITE = /^[a-z0-9-]+\.html(\?order_id=[A-Za-z0-9_-]{10,64}|#[a-z0-9-]+)?$/;
function walk(n, fn) { fn(n); if (n.children) n.children.forEach((c) => walk(c, fn)); }
function assertNoInjectedMarkup(root) {
  walk(root, (n) => {
    if (n.nodeType !== 1) return;
    assert.ok(!["IMG", "SCRIPT", "IFRAME", "SVG", "OBJECT"].includes(n.tagName), `unexpected <${n.tagName}> element`);
    for (const [k, v] of n.attributes) {
      assert.ok(!/^on/i.test(k), `event handler attribute ${k}`);
      if (k === "href") assert.ok(/^(https:\/\/|#)/.test(v) || SAME_SITE.test(v), `unsafe href ${v}`);
    }
  });
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

const EVIL = "<img src=x onerror=alert(1)>";
const cfg = C.getConfig({ APEX_CONFIG: {
  apiBase: "https://api.example.test/", price: "$29", supportEmail: "",
} });

test("config is validated and trailing slash stripped", () => {
  assert.equal(cfg.apiBase, "https://api.example.test");
  assert.equal(cfg.payOk, true);
  const bad = C.getConfig({ APEX_CONFIG: { apiBase: "javascript:alert(1)" } });
  assert.equal(bad.apiOk, false);
  assert.equal(bad.payOk, false);
  assert.equal(C.getConfig({}).apiOk, false);
  assert.equal(C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", checkout: false } }).payOk, false);
});

test("only Square's hosted checkout pages are trusted", () => {
  for (const ok of ["https://square.link/u/AbC123", "https://sandbox.square.link/u/x", "https://checkout.square.site/merchant/M/checkout/X",
    "https://connect.squareupsandbox.com/v2/checkout?c=1"]) assert.equal(C.isCheckoutUrl(ok), true, ok);
  for (const bad of ["http://square.link/u/x", "https://square.link.evil.test/u/x", "https://evilsquare.link/x", "https://square.link:8443/x",
    "javascript:alert(1)", "https://pay.example.com/x", "", null]) assert.equal(C.isCheckoutUrl(bad), false, String(bad));
});

test("order id validation", () => {
  assert.equal(R.isValidOrderId("CAISEHkjZ5kOIDGv5eBhaH8BgfwgAQ"), true);
  assert.equal(R.isValidOrderId("a".repeat(64)), true);
  assert.equal(R.isValidOrderId("a".repeat(65)), false);
  assert.equal(R.isValidOrderId("short"), false);
  assert.equal(R.isValidOrderId("ORDabc123456789<script>"), false);
  assert.equal(R.isValidOrderId("../v2/payments"), false);
  assert.equal(R.isValidOrderId(""), false);
  assert.equal(R.isValidOrderId(null), false);
});

test("pdf link only accepts the expected API path", () => {
  const id = "ORDabcdefghijkl";
  assert.equal(R.pdfHref(cfg, "/api/report.pdf?order_id=" + id, id), "https://api.example.test/api/report.pdf?order_id=" + id);
  assert.equal(R.pdfHref(cfg, "javascript:alert(1)", id), "https://api.example.test/api/report.pdf?order_id=" + id);
  assert.equal(R.pdfHref(cfg, "//evil.test/x", id), "https://api.example.test/api/report.pdf?order_id=" + id);
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
      sections: [
        { name: EVIL, score: 40, findings: [{ id: "7.1", severity: "MAJOR", title: EVIL, measured: EVIL, why: EVIL, fix: EVIL, effort: EVIL }] },
        { name: "Security and email trust", score: null, findings: [] },
        "junk", null,
      ],
      passed: [{ id: "2.1", title: EVIL, measured: EVIL }],
      not_measured: [{ id: "1.2", label: EVIL, reason: EVIL }],
    },
  };
  const node = R.renderReport(doc, data, cfg, "ORDabcdefghijkl");
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"), "hostile string should be escaped text");
  assert.ok(!html.includes("<img"), "no raw <img> in output");
  assert.ok(html.includes('class="sev sev-low"'), "unknown severity falls back to low class");
  assert.ok(html.includes('class="sev sev-critical"'));
  assert.ok(html.includes('class="grade"') && html.includes(">?</span>"), "bad grade shows ?");
  assert.ok(html.includes("Score: not measured<"), "non-numeric score says not measured");
  assert.ok(html.includes("width:100%"), "category bar is clamped to 100%");
  assert.ok(html.includes('href="https://api.example.test/api/report.pdf?order_id=ORDabcdefghijkl"'));
  assert.ok(html.includes("Bookmark this page"));
  assert.ok(html.includes("Top 3 fixes") && html.includes("Not measured (1)") && html.includes("What passed (1)"));
  assert.ok(html.includes("not in grade"), "section rows in the score table say they are not in the grade");
  assert.ok(html.includes("Security and email trust<") && html.includes("Nothing in this section could be measured"));
  assert.ok(html.includes("(40/100)"));
});

test("report handles a missing report body", () => {
  const node = R.renderReport(doc, { ok: true, site: "example.com", score: 80, grade: "B" }, cfg, "ORDabcdefghijkl");
  const html = serialize(node);
  assert.ok(html.includes("No failing checks were found"));
  assert.ok(html.includes('class="grade grade-b"'));
});

test("mini-check renders hostile API strings as text", () => {
  const data = { ok: true, site: EVIL, score: 62, grade: "C", failing: 9, passed: 12, not_measured: 17,
    measured_at: "2026-11-23T10:00:00Z",
    top: [{ severity: EVIL, title: EVIL }, { severity: "HIGH", title: "No tap-to-call link" }, { severity: "MEDIUM", title: "t" }, { severity: "LOW", title: "4th is dropped" }],
    sections: [{ name: EVIL, score: 20, failing: 5, top: { severity: "MAJOR", title: EVIL } },
      { name: "Security and email trust", score: 100, failing: 0, top: null }, null, 7] };
  const node = A.renderMiniResult(doc, data, cfg);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("4th is dropped"), "only top 3 shown");
  assert.ok(html.includes("+ 6 more issues found so far"));
  assert.ok(html.includes("62/100"));
  assert.ok(html.includes("(+4 more in the full report)"));
  assert.ok(html.includes("Security and email trust: <") && html.includes("nothing below the bar in the free check"));
  assert.ok(html.includes("data-buy-checked"));
  assert.ok(html.includes("We never see your card details"));
});

test("mini-check without checkout switched on only points at the price section", () => {
  const badCfg = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", checkout: false } });
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
    calls.push({ url, method: opts.method || "GET", body: opts.body, headers: opts.headers || {} });
    const key = Object.keys(routes).find((k) => url.includes(k));
    const r = key ? routes[key] : { status: 404, body: { ok: false, error: "nope" } };
    if (r === "network") throw new TypeError("Failed to fetch");
    const next = Array.isArray(r) ? r.shift() : r;
    return { status: next.status, text: async () => JSON.stringify(next.body) };
  };
}
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
function store(init) {
  const m = new Map(Object.entries(init || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), map: m };
}
const win = (search, saved) => ({ APEX_CONFIG: { apiBase: "https://api.example.test", price: "$29" },
  location: { search, assigned: [], assign(u) { this.assigned.push(u); } },
  history: { replaced: [], replaceState(_s, _t, u) { this.replaced.push(u); } },
  localStorage: store(saved), printed: 0, print() { this.printed++; } });
const SID = "ORDabcdefghijkl";
const REPORT = { ok: true, site: "example.com", score: 62, grade: "C", measured_at: "2026-11-23T10:00:00Z", pdf_url: "/api/report.pdf?order_id=" + SID,
  report: { categories: [{ name: "Speed", score: 50, weight: 25 }], findings: [{ id: "1.1", severity: "CRITICAL", title: "Slow", measured: "m", why: "w", fix: "f", effort: "e" }], passed: [], not_measured: [] } };

test("report page: bad or missing order id makes no request", async () => {
  mockFetch({});
  for (const search of ["?order_id=x", "?order_id=../v2/payments", ""]) {
    const d = page(["report-status", "report-root"]);
    R.init(d, win(search));
    await tick();
    assert.equal(calls.length, 0);
    assert.match(d.getElementById("report-status").textContent, /missing its order reference/);
  }
});

test("report page: takes Square's orderId, or the order saved before checkout", async () => {
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  let w = win("?checkoutId=c1&orderId=" + SID + "&transactionId=t1");
  R.init(page(["report-status", "report-root"]), w);
  await tick();
  assert.equal(calls[0].url, "https://api.example.test/api/report?order_id=" + SID);
  assert.deepEqual(w.history.replaced, ["report.html?order_id=" + SID]);   // a clean link to bookmark
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  w = win("", { apexLastOrder: SID });
  R.init(page(["report-status", "report-root"]), w);
  await tick();
  assert.equal(calls[0].url, "https://api.example.test/api/report?order_id=" + SID);
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  w = win("", { apexLastOrder: "<bad>" });
  R.init(page(["report-status", "report-root"]), w);
  await tick();
  assert.equal(calls.length, 0);
});

test("report page: 200 renders report, print button works", async () => {
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  const d = page(["report-status", "report-root"]); const w = win("?order_id=" + SID);
  R.init(d, w);
  await tick();
  assert.equal(calls[0].url, "https://api.example.test/api/report?order_id=" + SID);
  assert.deepEqual(w.history.replaced, []);
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
  R.init(d, win("?order_id=" + SID));
  await tick();
  assert.equal(d.getElementById("report-status").textContent, "This payment was refunded, so this report link no longer works.");
  mockFetch({ "/api/report?": { status: 402, body: { ok: false } } });
  d = page(["report-status", "report-root"]);
  R.init(d, win("?order_id=" + SID));
  await tick();
  assert.equal(d.getElementById("report-status").textContent, "We couldn't find a completed payment for this link.");
});

test("find page: lists only same-site report links, as text", async () => {
  const F = require("../find.js");
  const d = page(["find-result"]);
  const list = d.getElementById("find-result");
  const n = F.renderMatches(d, list, { reports: [
    { site: "<b>roof</b>.com", created: 1700000000, path: "report.html?order_id=" + SID },
    { site: "evil", created: 1, path: "https://evil.example/report.html?order_id=" + SID },
    { site: "evil3", created: 1, path: "report.html?order_id=" + SID + "&x=<b>" },
    { site: "evil2", created: 1, path: "javascript:alert(1)" }] });
  assert.equal(n, 1);
  const html = serialize(list);
  assert.ok(html.includes('href="report.html?order_id=' + SID + '"'));
  assert.ok(!html.includes("evil") && !html.includes("javascript:") && !html.includes("<b>"));
});

test("report page: 422 refundable offers retry and automatic refund", async () => {
  mockFetch({ "/api/report?": [{ status: 422, body: { ok: false, error: EVIL, refundable: true } }, { status: 200, body: REPORT }],
    "/api/refund": { status: 200, body: { ok: true, status: "pending" } } });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?order_id=" + SID));
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
  assert.deepEqual(JSON.parse(refundCall.body), { order_id: SID });
  assert.match(st.textContent, /^Refund issued\. Square sends it back/);
});

test("report page: refund error shows server text and re-enables button", async () => {
  mockFetch({ "/api/report?": { status: 422, body: { ok: false, error: "Site unreachable", refundable: true } },
    "/api/refund": { status: 409, body: { ok: false, error: "Already refunded" } } });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?order_id=" + SID));
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
  R.init(d, win("?order_id=" + SID));
  await tick();
  assert.match(d.getElementById("report-status").textContent, /couldn't reach the checker/);
  assert.equal(d.getElementById("report-root").querySelector("button").textContent, "Try again");
});

const CHECKOUT = { status: 200, body: { ok: true, checkout_url: "https://sandbox.square.link/u/AbC123", order_id: SID } };

test("index: wakes server, enables Buy, runs the free check, buys the checked site", async () => {
  // "/api/checkout" first: the mock matches by substring, and "/api/check" is inside it
  mockFetch({ "/health": { status: 200, body: { ok: true } }, "/api/checkout": CHECKOUT, "/api/check": { status: 200,
    body: { ok: true, site: "example.com", score: 62, grade: "C", top: [{ severity: "CRITICAL", title: "Slow" }], failing: 9, passed: 12, not_measured: 17 } } });
  const price = new FakeElement("p"); price.setAttribute("data-price", "");
  const d = page(["check-form", "site-url", "check-btn", "check-status", "check-result", "pay-unavailable", "buy-form", "buy-url", "buy-btn", "buy-status"], [price]);
  d.getElementById("buy-btn").disabled = true;
  const w = win("");
  A.init(d, w);
  assert.equal(calls[0].url, "https://api.example.test/health");
  assert.equal(d.getElementById("buy-btn").disabled, false);
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
  assert.equal(d.getElementById("buy-url").value, "example.com");        // the price box is filled in for them
  res.querySelector("[data-buy-checked]").click();
  await tick();
  const co = calls.find((c) => c.url.endsWith("/api/checkout"));
  assert.deepEqual(JSON.parse(co.body), { url: "example.com", plan: "checkup" });
  assert.deepEqual(w.location.assigned, ["https://sandbox.square.link/u/AbC123"]);
  assert.equal(w.localStorage.map.get("apexLastOrder"), SID);
});

test("index: price-box Buy needs a website and refuses a checkout URL that isn't Square's", async () => {
  mockFetch({ "/health": { status: 200, body: {} },
    "/api/checkout": [{ status: 200, body: { ok: true, checkout_url: "https://evil.example/pay", order_id: SID } },
                      { status: 503, body: { ok: false, error: EVIL } }, CHECKOUT] });
  const d = page(["check-form", "site-url", "check-btn", "check-status", "check-result", "buy-form", "buy-url", "buy-btn", "buy-status"]);
  const w = win("");
  A.init(d, w);
  const form = d.getElementById("buy-form"), st = d.getElementById("buy-status"), btn = d.getElementById("buy-btn");
  form.submit();
  await tick();
  assert.match(st.textContent, /Type your website address first/);
  assert.equal(calls.filter((c) => c.url.endsWith("/api/checkout")).length, 0);
  d.getElementById("buy-url").value = "roof.example";
  form.submit();
  await tick();
  assert.match(st.textContent, /looked wrong/);
  assert.deepEqual(w.location.assigned, []);
  assert.equal(w.localStorage.map.size, 0);
  form.submit();
  await tick();
  assert.equal(st.textContent, EVIL);
  assert.equal(btn.disabled, false);
  form.submit();
  await tick();
  assert.deepEqual(w.location.assigned, ["https://sandbox.square.link/u/AbC123"]);
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


// ---------------------------------------------------------------- v2: plans, Pro, dashboard, admin
const D = require("../dashboard.js");
const AD = require("../admin.js");
function mk(tag, attrs, text) {
  const e = new FakeElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
  if (text !== undefined) e.textContent = text;
  return e;
}
const PLANS = { ok: true, plans: [
  { id: "checkup", label: "Website Checkup", cents: 2900, kind: "one_time", available: true },
  { id: "pro", label: "Pro Checkup (homepage + up to 4 more pages)", cents: 7950, kind: "one_time", available: true },
  { id: "monitor", label: "Monthly Monitoring", cents: 1900, kind: "subscription", available: false }] };
const withMonitor = () => ({ ok: true, plans: PLANS.plans.map((p) => (p.id === "monitor" ? { ...p, available: true } : p)) });
function pricingNodes() {
  const n = {};
  for (const id of ["checkup", "pro", "monitor"]) {
    n[id] = {
      card: mk("article", { "data-plan-card": id }),
      price: mk("span", { "data-plan-price": id }, "fallback"),
      btn: mk("button", id === "checkup" ? { id: "buy-btn", "data-buy-plan": id } : { "data-buy-plan": id }),
      note: mk("p", { "data-plan-unavailable": id, hidden: "" }),
    };
    n[id].card.appendChild(n[id].price); n[id].card.appendChild(n[id].btn); n[id].card.appendChild(n[id].note);
  }
  n.monitor.card.hidden = true;
  n.renew = mk("span", { "data-plan-price": "monitor" }, "$19");
  n.monitor.card.appendChild(n.renew);
  n.ifNo = mk("p", { "data-if-no-plan": "monitor", hidden: "" });
  n.ifEmails = mk("li", { "data-if-emails": "", hidden: "" });
  return n;
}
function pricingPage(n) {
  const form = mk("form", { id: "buy-form" });
  form.appendChild(mk("input", { id: "buy-url" }));
  for (const id of ["checkup", "pro", "monitor"]) form.appendChild(n[id].card);
  return page(["check-form", "site-url", "check-btn", "check-status", "check-result", "buy-status", "pay-unavailable"],
    [form, n.ifNo, n.ifEmails, mk("section", { id: "price" })]);
}

test("money is shown as dollars from cents", () => {
  assert.equal(C.dollars(2900), "$29");
  assert.equal(C.dollars(7950), "$79.50");
  assert.equal(C.dollars(1900, true), "$19.00");
  assert.equal(C.dollars(123456789, true), "$1,234,567.89");
  assert.equal(C.dollars(-500, true), "-$5.00");
  assert.equal(C.dollars(5), "$0.05");
  assert.equal(C.dollars("nope"), "");
  assert.equal(C.dollars(null), "");
});

test("plansById keeps only well-formed plans", () => {
  const p = C.plansById({ ok: true, plans: [...PLANS.plans, { id: "<x>", cents: 1, available: true }, null, "junk",
    { id: "free", cents: 0, available: true }, { id: "pro", cents: 1, available: true }] });
  assert.deepEqual(Object.keys(p).sort(), ["checkup", "free", "monitor", "pro"]);
  assert.equal(p.pro.cents, 7950, "first entry for an id wins");
  assert.equal(p.free.available, false, "a zero price is never available");
  assert.equal(p.monitor.kind, "subscription");
  assert.deepEqual(C.plansById(null), {});
});

test("pricing: prices from the API, monitor card hidden when unavailable", () => {
  const n = pricingNodes();
  const d = pricingPage(n);
  const cfg2 = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", price: "$29" } });
  A.applyPlans(d, C.plansById(PLANS), cfg2);
  assert.equal(n.checkup.price.textContent, "$29");
  assert.equal(n.pro.price.textContent, "$79.50");
  assert.equal(n.monitor.card.hidden, true, "monitor card stays hidden while the plan is unavailable");
  assert.equal(n.monitor.btn.disabled, true);
  assert.equal(n.ifNo.hidden, false, "pages that mention monitoring say it isn't open");
  assert.equal(n.pro.btn.disabled, false);
  assert.equal(n.checkup.btn.disabled, false);
  A.applyPlans(d, C.plansById(withMonitor()), cfg2);
  assert.equal(n.monitor.card.hidden, false);
  assert.equal(n.monitor.btn.disabled, false);
  assert.equal(n.renew.textContent, "$19", "the auto-renew line shows the real monthly price");
  assert.equal(n.ifNo.hidden, true);
  // the API is down: only the one-time checkup is offered, and only at the configured fallback price
  const m = pricingNodes();
  const d2 = pricingPage(m);
  A.applyPlans(d2, null, cfg2);
  assert.equal(m.checkup.btn.disabled, false);
  assert.equal(m.pro.btn.disabled, true);
  assert.equal(m.pro.note.hidden, false);
  assert.equal(m.pro.price.textContent, "fallback");
  assert.equal(m.monitor.card.hidden, true);
  // checkout switched off: nothing can be bought, prices still shown
  const off = C.getConfig({ APEX_CONFIG: { apiBase: "https://api.example.test", checkout: false } });
  const o = pricingNodes();
  A.applyPlans(pricingPage(o), C.plansById(withMonitor()), off);
  assert.ok(["checkup", "pro", "monitor"].every((id) => o[id].btn.disabled));
  assert.equal(o.pro.price.textContent, "$79.50");
});

test("index: each plan button sends its plan to checkout", async () => {
  mockFetch({ "/health": { status: 200, body: {} }, "/api/plans": { status: 200, body: withMonitor() },
    "/api/checkout": [CHECKOUT, CHECKOUT, CHECKOUT] });
  const n = pricingNodes();
  const d = pricingPage(n);
  const w = win("");
  A.init(d, w);
  assert.equal(n.pro.btn.disabled, true, "Pro waits for /api/plans");
  await tick();
  assert.ok(calls.some((c) => c.url === "https://api.example.test/api/plans" && c.method === "GET"));
  assert.equal(n.pro.btn.disabled, false);
  assert.equal(n.monitor.card.hidden, false);
  // no website yet: a clear message, no checkout
  n.pro.btn.click();
  await tick();
  assert.match(d.getElementById("buy-status").textContent, /Type your website address above first/);
  assert.equal(calls.filter((c) => c.url.endsWith("/api/checkout")).length, 0);
  d.getElementById("buy-url").value = "roof.example";
  n.pro.btn.click();
  await tick();
  n.monitor.btn.click();
  await tick();
  d.getElementById("buy-form").submit();
  await tick();
  const bodies = calls.filter((c) => c.url.endsWith("/api/checkout")).map((c) => JSON.parse(c.body));
  assert.deepEqual(bodies, [{ url: "roof.example", plan: "pro" }, { url: "roof.example", plan: "monitor" }, { url: "roof.example", plan: "checkup" }]);
  assert.equal(w.location.assigned.length, 3);
});

test("index: plans request failing keeps monitor hidden and Pro off", async () => {
  mockFetch({ "/health": { status: 200, body: {} }, "/api/plans": "network" });
  const n = pricingNodes();
  A.init(pricingPage(n), win(""));
  await tick();
  assert.equal(n.monitor.card.hidden, true);
  assert.equal(n.pro.btn.disabled, true);
  assert.equal(n.checkup.btn.disabled, false);
  assert.equal(n.ifEmails.hidden, true, "emails aren't mentioned unless APEX_EMAILS is on");
  mockFetch({ "/health": { status: 200, body: {} }, "/api/plans": { status: 404, body: { ok: false } } });
  const m = pricingNodes();
  const w = win(""); w.APEX_CONFIG.emails = true;
  A.init(pricingPage(m), w);
  await tick();
  assert.equal(m.monitor.card.hidden, true);
  assert.equal(m.ifEmails.hidden, false);
});

test("startCheckout refuses a malformed plan without a request", async () => {
  mockFetch({});
  const r = await C.startCheckout(win(""), cfg, "example.com", "<pro>");
  assert.equal(r.ok, false);
  assert.equal(calls.length, 0);
});

test("mini-check on a landing page links to the home page plans", () => {
  const node = A.renderMiniResult(doc, { ok: true, site: "example.com", score: 70, grade: "C", top: [] }, cfg, { pricingHref: "index.html#price" });
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(html.includes('href="index.html#price"'));
  const bad = A.renderMiniResult(doc, { ok: true, site: "x", top: [] }, cfg, { pricingHref: "javascript:alert(1)" });
  assert.ok(serialize(bad).includes('href="#price"'));
});

const PRO_PAGES = [
  { url: "https://roof.example/about", path: "/about", title: EVIL, passed: 7, not_measured: [],
    issues: [{ id: "p.meta", title: EVIL, severity: "MAJOR", detail: EVIL }, { id: "p.h1", title: "2 H1 elements", severity: "MINOR", detail: "found 2" }] },
  { url: "https://roof.example/contact", path: "/contact", title: "Contact", passed: 9, issues: [], not_measured: ["broken or redirecting page"] },
  { url: "https://roof.example/x", path: "/x", issues: [], passed: 0, not_measured: ["the time limit cut this page short"] },
  null, "junk",
];

test("report: Pro pages render as text, not part of the grade", () => {
  const data = { ...REPORT, report: { ...REPORT.report, pages: PRO_PAGES } };
  const node = R.renderReport(doc, data, cfg, SID);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("Pages checked (3)"));
  assert.ok(html.includes("not part of the grade"));
  assert.ok(html.includes(">/about<") && html.includes(">/contact<"));
  assert.ok(html.includes("Page title: &lt;img"));
  assert.ok(html.includes("2 H1 elements") && html.includes(" — found 2"));
  assert.ok(html.includes("7 checks passed"));
  assert.ok(html.includes("not measured: the time limit cut this page short"));
  assert.ok(html.includes("No problems found in what could be measured on this page."));
  assert.ok(html.includes('class="sev sev-high"'), "MAJOR maps to high");
  const plain = serialize(R.renderReport(doc, REPORT, cfg, SID));
  assert.ok(!plain.includes("Pages checked"), "no pages section for a non-Pro report");
});

test("report links to the order's dashboard", () => {
  const html = serialize(R.renderReport(doc, REPORT, cfg, SID));
  assert.ok(html.includes('href="dashboard.html?order_id=' + SID + '"'));
  assert.ok(!serialize(R.renderReport(doc, REPORT, cfg, "bad id")).includes("dashboard.html"));
});

test("find page also links each report's dashboard", () => {
  const F = require("../find.js");
  const d = page(["find-result"]);
  F.renderMatches(d, d.getElementById("find-result"), { reports: [{ site: "roof.com", created: 1700000000, path: "report.html?order_id=" + SID }] });
  assert.ok(serialize(d.getElementById("find-result")).includes('href="dashboard.html?order_id=' + SID + '"'));
});

// Real-shaped responses (copied from server.dashboard / server.admin_summary / monitor.diff), shared with contract.mjs.
const fixture = (name) => JSON.parse(fs.readFileSync(new URL("./fixtures/" + name, import.meta.url), "utf8"));
const DASH = fixture("dashboard.json");

test("dashboard renders plan, report link, history, changes and status as text", () => {
  const hostile = { ...DASH, site: EVIL, report_path: "javascript:alert(1)", plan: EVIL,
    subscription: { ...DASH.subscription, status: EVIL }, history: [{ at: EVIL, grade: EVIL, score: EVIL }, null],
    latest_changes: { score_change: EVIL, new: [{ id: EVIL, title: EVIL, severity: EVIL }, 5, null], fixed: "junk", since: EVIL } };
  const node = D.renderDashboard(doc, hostile, SID);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(!html.includes("<img") && !html.includes("javascript:"));
  assert.ok(html.includes('href="report.html?order_id=' + SID + '"'), "falls back to the order's own report link");
  assert.ok(html.includes("Website Checkup"), "unknown plan is shown as a one-time checkup");
  assert.ok(!html.includes("Cancel monitoring"));
  const good = serialize(D.renderDashboard(doc, DASH, SID));
  assert.ok(good.includes("Monthly Monitoring") && good.includes("roof.example"));
  assert.ok(good.includes("Score: +11 points"));
  assert.ok(good.includes("New problems (1)") && good.includes("No DMARC email record") && good.includes("Fixed (2)"));
  assert.ok(good.includes("Slow server response") && good.includes('class="sev sev-critical"'), "changes show title and severity");
  assert.ok(good.includes("Grade: C before, B now"));
  assert.ok(good.includes(">B<") && good.includes("81/100") && good.includes("70/100"));
  assert.ok(good.includes(C.formatWhen(DASH.history[0].at)) && !good.includes("1795731516"), "epoch-second dates are shown as dates");
  assert.ok(!good.includes("unknown date"));
  assert.ok(good.includes(">Active<") && good.includes("Paid through"));
  assert.ok(good.includes("Cancel monitoring") && good.includes('data-action="cancel-start"'));
  assert.ok(good.includes("can be lost when the server is updated"), "history is described as best-effort");
  const one = serialize(D.renderDashboard(doc, { ...DASH, plan: "checkup", subscription: null, latest_changes: null }, SID));
  assert.ok(one.includes("nothing to cancel") && !one.includes("cancel-start"));
  const gone = serialize(D.renderDashboard(doc, { ...DASH, subscription: { ...DASH.subscription, canceled_date: "2026-11-20" } }, SID));
  assert.ok(gone.includes("(cancelled)") && !gone.includes("cancel-start"), "a cancelled subscription can't be cancelled again");
  assert.ok(gone.includes("Ends on") && !gone.includes("Cancelled on"), "the end date is labelled Ends on");
  const unknown = serialize(D.renderDashboard(doc, { ...DASH, subscription: null }, SID));
  assert.ok(unknown.includes("couldn't read your subscription"));
});

test("dashboard page: bad order id makes no request", async () => {
  mockFetch({});
  const d = page(["dash-status", "dash-root"]);
  D.init(d, win("?order_id=../x"));
  await tick();
  assert.equal(calls.length, 0);
  assert.match(d.getElementById("dash-status").textContent, /missing its order reference/);
});

test("dashboard page: cancel asks in the page first, then cancels and says when it ends", async () => {
  mockFetch({ "/api/dashboard?": { status: 200, body: DASH },
    "/api/cancel": [{ status: 502, body: { ok: false, error: EVIL } }, { status: 200, body: { ok: true, status: "ACTIVE", ends: "2026-12-26" } }] });
  const d = page(["dash-status", "dash-root"]);
  const w = win("?order_id=" + SID);
  w.confirm = () => { throw new Error("window.confirm must not be used"); };
  D.init(d, w);
  await tick();
  assert.equal(calls[0].url, "https://api.example.test/api/dashboard?order_id=" + SID);
  assert.match(d.title, /roof\.example/);
  const root = d.getElementById("dash-root");
  const start = root.querySelector('[data-action="cancel-start"]');
  const panel = root.querySelector('[role="group"]');
  assert.equal(panel.hidden, true);
  start.click();
  assert.equal(panel.hidden, false);
  assert.equal(start.getAttribute("aria-expanded"), "true");
  assert.equal(focused, panel.querySelector("h3"), "focus moves to the question");
  assert.match(panel.textContent, /stays on until .*2026/);
  root.querySelector('[data-action="cancel-keep"]').click();
  assert.equal(panel.hidden, true);
  assert.equal(calls.filter((c) => c.url.endsWith("/api/cancel")).length, 0, "nothing is cancelled without confirming");
  start.click();
  const yes = root.querySelector('[data-action="cancel-confirm"]');
  yes.click();
  await tick();
  const st = root.querySelector("[data-cancel-status]");
  assert.equal(st.textContent, EVIL);
  assert.equal(yes.disabled, false, "a failed cancel can be retried");
  yes.click();
  await tick();
  const cancels = calls.filter((c) => c.url.endsWith("/api/cancel"));
  assert.equal(cancels.length, 2);
  assert.equal(cancels[1].method, "POST");
  assert.deepEqual(JSON.parse(cancels[1].body), { order_id: SID });
  assert.match(st.textContent, /^Monitoring is cancelled\. Monitoring stays on until .*2026\. You won't be charged again\.$/);
  assert.equal(start.hidden, true);
  assert.equal(panel.hidden, true);
});

test("dashboard page: API error offers Try again", async () => {
  mockFetch({ "/api/dashboard?": { status: 404, body: { ok: false, error: "We couldn't find that order." } } });
  const d = page(["dash-status", "dash-root"]);
  D.init(d, win("?orderId=" + SID));
  await tick();
  assert.equal(d.getElementById("dash-status").textContent, "We couldn't find that order.");
  assert.equal(d.getElementById("dash-root").querySelector("button").textContent, "Try again");
});

const SUMMARY = { ok: true,
  counters: { free_checks: 140, checkouts_started: 20, reports_delivered: 11, since: 1795600000.5 },
  monitoring: { running: false, last_run: { started: 1795731516.4, finished: 1795731581.9,
    counts: { sites: 2, scanned: 2, skipped_recent: 0, not_checked: 0, emails_sent: 2, emails_skipped: 0 } } },
  sales: { days: 30, orders: 12, gross_cents: 52800, refunded_cents: 2900, net_cents: 49900,
    by_plan: { checkup: { count: 7, gross_cents: 20300 }, pro: { count: 3, gross_cents: 23700 }, monitor: { count: 2, gross_cents: 3800 } },
    active_subscriptions: 2, cancelling: 1, mrr_cents: 3800, truncated: false,
    recent: [{ created: "2026-11-25T10:00:00Z", plan: "pro", amount_cents: 7900, site: "roof.example", refunded: false },
             { created: 1764000000, plan: "checkup", amount_cents: 2900, site: EVIL, refunded: true }] } };
const TOKEN = "t0k3n-THAT-is-long-enough-for-admin-123";

test("admin summary renders money from cents and hostile strings as text", () => {
  const node = AD.renderSummary(doc, SUMMARY);
  const html = serialize(node);
  assertNoInjectedMarkup(node);
  assert.ok(!html.includes("<img"));
  for (const t of ["$499.00", "$528.00", "$29.00", "$38.00", ">12<", ">140<", "Pro Checkup", "$237.00", "roof.example", ">Yes<", "the last 30 days"]) {
    assert.ok(html.includes(t), t);
  }
  assert.ok(html.includes("Checkouts started 20, reports delivered 11"));
  assert.ok(html.includes("Monitored sites") && html.includes("Emails sent") && html.includes(">2<"));
  assert.ok(html.includes("1 more cancelled, ending at the end of their paid month"), "cancelling subscriptions are shown");
  assert.ok(!html.includes("incomplete"), "no truncation warning when not truncated");
  const empty = serialize(AD.renderSummary(doc, { ok: true, monitoring: null, sales: { recent: "junk", by_plan: null } }));
  assert.ok(empty.includes("No sales in this period.") && empty.includes("No monitoring run is recorded"));
  assert.ok(empty.includes("—"), "missing numbers show a dash, not 0");
});

function adminPage() {
  const input = mk("input", { id: "admin-token" });
  const days = mk("select", { id: "admin-days" }); days.value = "30";
  const bar = mk("div", { id: "admin-bar", hidden: "" });
  [days, mk("button", { id: "admin-refresh" }), mk("button", { id: "admin-run" }), mk("button", { id: "admin-logout" })].forEach((b) => bar.appendChild(b));
  return page(["admin-status", "admin-root"], [mk("form", { id: "admin-login" }), input, mk("button", { id: "admin-login-btn" }), bar]);
}
function adminWin() {
  const w = win("");
  w.sessionStorage = store();
  return w;
}

test("admin: token goes to sessionStorage only and is sent as a Bearer header", async () => {
  mockFetch({ "/api/admin/summary": { status: 200, body: SUMMARY }, "/api/admin/run-monitoring": [{ status: 202, body: { ok: true, started: true } }, { status: 409, body: { ok: false } }] });
  const d = adminPage();
  const w = adminWin();
  AD.init(d, w);
  await tick();
  assert.equal(calls.length, 0, "nothing is requested before a token is given");
  d.getElementById("admin-token").value = "  " + TOKEN + " ";
  d.getElementById("admin-login").submit();
  await tick();
  assert.equal(d.getElementById("admin-token").value, "", "the token is cleared from the field");
  assert.equal(w.sessionStorage.map.get("apexAdminToken"), TOKEN);
  assert.equal(w.localStorage.map.size, 0, "never in localStorage");
  const c = calls[0];
  assert.equal(c.url, "https://api.example.test/api/admin/summary?days=30");
  assert.equal(c.headers.Authorization, "Bearer " + TOKEN);
  assert.equal(d.getElementById("admin-login").hidden, true);
  assert.equal(d.getElementById("admin-bar").hidden, false);
  assert.match(d.getElementById("admin-root").textContent, /\$499\.00/);
  d.getElementById("admin-days").value = "90";
  d.getElementById("admin-refresh").click();
  await tick();
  assert.equal(calls[1].url, "https://api.example.test/api/admin/summary?days=90");
  d.getElementById("admin-run").click();
  await tick();
  const run = calls.find((x) => x.url.endsWith("/api/admin/run-monitoring"));
  assert.equal(run.method, "POST");
  assert.equal(run.headers.Authorization, "Bearer " + TOKEN);
  assert.match(d.getElementById("admin-status").textContent, /Monitoring run started/);
  d.getElementById("admin-run").click();
  await tick();
  assert.match(d.getElementById("admin-status").textContent, /already going/);
  // the token never shows up in a URL, the page, history or localStorage
  for (const x of calls) assert.ok(!x.url.includes(TOKEN));
  assert.ok(!d.body.textContent.includes(TOKEN));
  assert.deepEqual(w.history.replaced, []);
  assert.deepEqual(w.location.assigned, []);
  d.getElementById("admin-logout").click();
  assert.equal(w.sessionStorage.map.has("apexAdminToken"), false, "log out removes the token");
  assert.equal(d.getElementById("admin-login").hidden, false);
  assert.equal(d.getElementById("admin-root").children.length, 0);
});

test("admin: a rejected token is removed and the form comes back", async () => {
  mockFetch({ "/api/admin/summary": { status: 401, body: { ok: false, error: EVIL } } });
  const d = adminPage();
  const w = adminWin();
  w.sessionStorage.setItem("apexAdminToken", TOKEN);   // from earlier in this tab
  AD.init(d, w);
  await tick();
  assert.equal(calls[0].headers.Authorization, "Bearer " + TOKEN);
  assert.equal(w.sessionStorage.map.has("apexAdminToken"), false);
  assert.equal(d.getElementById("admin-login").hidden, false);
  assert.match(d.getElementById("admin-status").textContent, /wasn't accepted/);
  // a token with spaces or control characters is refused without a request
  mockFetch({});
  d.getElementById("admin-token").value = "two words";
  d.getElementById("admin-login").submit();
  await tick();
  assert.equal(calls.length, 0);
  assert.match(d.getElementById("admin-status").textContent, /no spaces/);
});

test("admin: switched-off admin and blocked sessionStorage", async () => {
  mockFetch({ "/api/admin/summary": { status: 404, body: { ok: false } } });
  const d = adminPage();
  const w = win("");
  Object.defineProperty(w, "sessionStorage", { get() { throw new Error("blocked"); } });
  AD.init(d, w);
  d.getElementById("admin-token").value = TOKEN;
  d.getElementById("admin-login").submit();
  await tick();
  assert.equal(calls[0].headers.Authorization, "Bearer " + TOKEN, "falls back to page memory, not localStorage");
  assert.equal(w.localStorage.map.size, 0);
  assert.match(d.getElementById("admin-status").textContent, /switched off/);
});

test("admin: if sessionStorage can't remove the token, it is blanked instead", () => {
  const m = new Map([["apexAdminToken", TOKEN]]);
  const ts = AD.tokenStore({ sessionStorage: { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v), removeItem() { throw new Error("no"); } } });
  assert.equal(ts.get(), TOKEN);
  ts.clear();
  assert.equal(ts.get(), "");
  assert.equal(m.get("apexAdminToken"), "");
});

// ---------------------------------------------------------------- review fixes (round 1)
const L = require("../legal.js");
const F2 = require("../find.js");
const MON = { id: "monitor", label: "Monthly Monitoring", cents: 1900, first_cents: 2900, kind: "subscription", available: true };

test("two-phase monitoring price text comes from first_cents and cents", () => {
  const p = C.plansById({ ok: true, plans: [MON, { id: "checkup", cents: 2900, kind: "one_time", available: true }] });
  assert.equal(p.monitor.first_cents, 2900);
  assert.equal(C.priceText(p.monitor), "$29 for the first month, then $19 a month");
  assert.equal(C.priceText({ ...p.monitor, first_cents: null }), "$19 a month", "no first-month price: just the monthly one");
  assert.equal(C.priceText({ ...p.monitor, first_cents: 1900 }), "$19 a month", "same price twice reads as one");
  assert.equal(C.priceText(p.checkup), "$29");
  assert.equal(C.priceText(null), "");
  assert.equal(C.plansById({ plans: [{ ...MON, first_cents: "<b>" }] }).monitor.first_cents, null);
});

test("pricing: monitor card and auto-renew line show both prices", () => {
  const n = pricingNodes();
  const first = mk("span", { "data-plan-first-price": "monitor" }, "x");
  const terms = mk("span", { "data-plan-terms": "monitor" }, "fallback terms");
  n.monitor.card.appendChild(first); n.monitor.card.appendChild(terms);
  const d = pricingPage(n);
  const plans = { ok: true, plans: PLANS.plans.map((p) => (p.id === "monitor" ? { ...p, first_cents: 2900, available: true } : p)) };
  A.applyPlans(d, C.plansById(plans), cfg);
  assert.equal(first.textContent, "$29");
  assert.equal(n.renew.textContent, "$19");
  assert.equal(terms.textContent, "$29 for the first month, then $19 a month");
  // the API is down: the page's own fallback text stays
  const m = pricingNodes();
  const t2 = mk("span", { "data-plan-terms": "monitor" }, "fallback terms");
  m.monitor.card.appendChild(t2);
  A.applyPlans(pricingPage(m), null, cfg);
  assert.equal(t2.textContent, "fallback terms");
});

test("index: plan buttons come back on pageshow from the back/forward cache", async () => {
  mockFetch({ "/health": { status: 200, body: {} },
    "/api/plans": { status: 200, body: { ok: true, plans: PLANS.plans.map((p) => (p.id === "monitor" ? { ...p, first_cents: 2900, available: true } : p)) } },
    "/api/checkout": [CHECKOUT] });
  const n = pricingNodes();
  const d = pricingPage(n);
  const w = win("");
  w.listeners = {};
  w.addEventListener = (type, fn) => { (w.listeners[type] ||= []).push(fn); };
  A.init(d, w);
  await tick();
  d.getElementById("buy-url").value = "roof.example";
  n.monitor.btn.click();
  await tick();
  assert.equal(w.location.assigned.length, 1, "went to Square");
  assert.equal(n.monitor.btn.disabled, true, "the pressed button stays off while leaving");
  assert.match(d.getElementById("buy-status").textContent, /Opening the secure checkout/);
  w.listeners.pageshow.forEach((fn) => fn({ persisted: false }));
  assert.equal(n.monitor.btn.disabled, true, "a normal page load changes nothing");
  w.listeners.pageshow.forEach((fn) => fn({ persisted: true }));
  assert.equal(n.monitor.btn.disabled, false, "back from checkout: the button works again");
  assert.equal(n.pro.btn.disabled, false);
  assert.equal(d.getElementById("buy-status").hidden, true, "the stale 'Opening…' message is gone");
  // a plan that isn't available stays off after pageshow
  mockFetch({ "/health": { status: 200, body: {} }, "/api/plans": { status: 200, body: PLANS } });
  const m = pricingNodes();
  const d2 = pricingPage(m);
  const w2 = win(""); w2.listeners = {}; w2.addEventListener = (t, fn) => { (w2.listeners[t] ||= []).push(fn); };
  A.init(d2, w2);
  await tick();
  w2.listeners.pageshow.forEach((fn) => fn({ persisted: true }));
  assert.equal(m.monitor.btn.disabled, true);
  assert.equal(m.checkup.btn.disabled, false);
});

test("find page: with emails on, the answer says the links were emailed and shows none", async () => {
  mockFetch({ "/api/find": { status: 200, body: { ok: true, emailed: true } } });
  const form = mk("form", { id: "find-form" });
  const email = mk("input", { id: "find-email" }); email.value = "owner@example.com";
  const site = mk("input", { id: "find-site" }); site.value = "roof.example";
  const d = page(["find-status", "find-result"], [form, email, site, mk("button", { id: "find-btn" })]);
  F2.init(d, win(""));
  form.submit();
  await tick();
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(JSON.parse(calls[0].body), { email: "owner@example.com", website: "roof.example" });
  const st = d.getElementById("find-status");
  assert.equal(st.textContent, F2.EMAILED_MESSAGE);
  assert.match(st.textContent, /We've emailed the links to that address if we found any/);
  assert.ok(!/error/.test(st.className), "not shown as an error");
  assert.equal(d.getElementById("find-result").hidden, true);
  assert.equal(d.getElementById("find-btn").disabled, false);
  // emails off: links in the response, as before
  mockFetch({ "/api/find": { status: 200, body: { ok: true, reports: [{ site: "roof.example", created: 1795731516, path: "report.html?order_id=" + SID }] } } });
  form.submit();
  await tick();
  assert.match(st.textContent, /Found it/);
  assert.ok(serialize(d.getElementById("find-result")).includes("dashboard.html?order_id=" + SID));
});

test("renewal terms: printable block on the dashboard and report, only for monitoring", () => {
  const dash = D.renderDashboard(doc, DASH, SID);
  const html = serialize(dash);
  assertNoInjectedMarkup(dash);
  const block = dash.querySelector("[data-renewal-terms]");
  assert.ok(block, "monitor dashboard has the renewal terms");
  const t = block.textContent;
  for (const words of ["first-month price", "renews automatically every month", "until you cancel", "Cancel online anytime from your dashboard",
    "end of the month you've already paid for", "Terms page"]) assert.ok(t.includes(words), words);
  assert.ok(serialize(block).includes('href="terms.html#monitoring"') && serialize(block).includes('href="dashboard.html?order_id=' + SID + '"'));
  assert.ok(!/no-print/.test(block.className), "the terms print with the page");
  C.fillRenewalPrice(dash, C.plansById({ plans: [MON] }).monitor);
  assert.match(block.textContent, /Price: \$29 for the first month, then \$19 a month\. The first month includes the full report\./);
  assert.ok(html.includes('data-action="print"'));
  for (const plan of ["checkup", "pro"]) {
    const one = D.renderDashboard(doc, { ...DASH, plan, subscription: null, latest_changes: null }, SID);
    assert.equal(one.querySelector("[data-renewal-terms]"), null, plan + " has no renewal terms");
  }
  const rep = R.renderReport(doc, { ...REPORT, plan: "monitor" }, cfg, SID);
  assert.ok(rep.querySelector("[data-renewal-terms]"), "monitor report page has the renewal terms");
  assertNoInjectedMarkup(rep);
  for (const plan of [undefined, "checkup", "pro", EVIL]) {
    assert.equal(R.renderReport(doc, { ...REPORT, plan }, cfg, SID).querySelector("[data-renewal-terms]"), null, String(plan));
  }
});

test("report page: monitor order fills the renewal price from /api/plans", async () => {
  mockFetch({ "/api/report?": { status: 200, body: { ...REPORT, plan: "monitor" } }, "/api/plans": { status: 200, body: { ok: true, plans: [MON] } } });
  const d = page(["report-status", "report-root"]);
  R.init(d, win("?order_id=" + SID));
  await tick();
  assert.ok(calls.some((c) => c.url.endsWith("/api/plans")));
  assert.match(d.getElementById("report-root").querySelector("[data-renewal-price]").textContent, /\$29 for the first month, then \$19 a month/);
  mockFetch({ "/api/report?": { status: 200, body: REPORT } });
  const d2 = page(["report-status", "report-root"]);
  R.init(d2, win("?order_id=" + SID));
  await tick();
  assert.ok(!calls.some((c) => c.url.endsWith("/api/plans")), "one-time reports don't ask for prices");
});

test("dashboard page: after cancelling, the Monitoring section updates in place", async () => {
  mockFetch({ "/api/dashboard?": { status: 200, body: JSON.parse(JSON.stringify(DASH)) }, "/api/plans": { status: 200, body: { ok: true, plans: [MON] } },
    "/api/cancel": { status: 200, body: { ok: true, status: "ACTIVE", ends: "2026-12-26" } } });
  const d = page(["dash-status", "dash-root"]);
  D.init(d, win("?order_id=" + SID));
  await tick();
  const root = d.getElementById("dash-root");
  assert.match(root.querySelector("[data-renewal-price]").textContent, /\$29 for the first month/);
  const subBefore = root.querySelector("[data-sub-section]");
  assert.ok(subBefore.textContent.includes("Active") && !subBefore.textContent.includes("Ends on"));
  root.querySelector('[data-action="cancel-start"]').click();
  root.querySelector('[data-action="cancel-confirm"]').click();
  await tick();
  const sub = root.querySelector("[data-sub-section]");
  assert.equal(sub, subBefore, "same section, re-filled");
  const t = sub.textContent;
  assert.ok(t.includes("Active until the end of the paid month (cancelled)"), t);
  assert.ok(t.includes("Ends on") && t.includes(C.formatDay("2026-12-26")));
  assert.ok(t.includes("You won't be charged again"));
  assert.equal(sub.querySelectorAll("h2").length, 1);
  assert.equal(root.querySelector("[data-cancel-intro]").hidden, true);
});

test("dashboard: a refunded monitor order still shows its subscription and can cancel", () => {
  const node = D.renderDashboard(doc, { ...DASH, refunded: true }, SID);
  const html = serialize(node);
  assert.ok(html.includes("was refunded") && html.includes(">Refunded<"));
  assert.ok(!html.includes('href="report.html'), "no dead report link");
  assert.ok(html.includes("Paid through") && html.includes('data-action="cancel-start"'), "status and cancel are still there");
  assert.ok(node.querySelector("[data-renewal-terms]"));
  const ended = serialize(D.renderDashboard(doc, { ...DASH, refunded: true, subscription: { ...DASH.subscription, status: "CANCELED", canceled_date: "2026-11-27" } }, SID));
  assert.ok(ended.includes("Cancelled") && ended.includes("Ends on") && !ended.includes("cancel-start"));
});

test("admin: sales error, truncated totals and a running monitoring run", () => {
  const off = AD.renderSummary(doc, { ok: true, counters: SUMMARY.counters, monitoring: { running: true, last_run: null },
    sales: null, sales_error: "We couldn't reach Square just now." });
  const t = off.textContent;
  assert.ok(t.includes("We couldn't reach Square just now."));
  assert.ok(t.includes("Free checks") && t.includes("140"), "counters still shown without sales");
  assert.ok(!t.includes("Net sales"));
  assert.ok(t.includes("A monitoring run is going now"));
  assert.ok(serialize(AD.renderSummary(doc, { ok: true, counters: {}, monitoring: {}, sales: null })).includes("Sales weren't loaded"));
  const tr = AD.renderSummary(doc, { ...SUMMARY, sales: { ...SUMMARY.sales, truncated: true } }).textContent;
  assert.ok(tr.includes("these totals are incomplete"));
  const bad = AD.renderSummary(doc, { ...SUMMARY, monitoring: { running: false, last_run: { started: 1795731516, finished: null,
    counts: { sites: 0, scanned: 0, skipped_recent: 0, not_checked: 0, emails_sent: 0, emails_skipped: 0, error: "square_unavailable" } } } }).textContent;
  assert.ok(bad.includes("didn't finish") && bad.includes("couldn't list the subscriptions from Square"));
  const html = serialize(AD.renderSummary(doc, { ...SUMMARY, sales_error: EVIL, sales: null }));
  assert.ok(!html.includes("<img") && html.includes("&lt;img"));
});

test("admin: focus moves to the summary after sign-in", async () => {
  mockFetch({ "/api/admin/summary": { status: 200, body: SUMMARY } });
  const d = adminPage();
  AD.init(d, adminWin());
  focused = null;
  d.getElementById("admin-token").value = TOKEN;
  d.getElementById("admin-login").submit();
  await tick();
  assert.ok(focused && focused.tagName === "H2" && focused.getAttribute("id") === "h-summary", "focus is on the summary heading");
  assert.equal(focused.getAttribute("tabindex"), "-1");
  focused = null;
  d.getElementById("admin-refresh").click();
  await tick();
  assert.equal(focused, null, "a refresh doesn't move focus");
});

test("terms page fills the monitoring prices from /api/plans and keeps its text without the API", async () => {
  mockFetch({ "/api/plans": { status: 200, body: { ok: true, plans: [{ ...MON, cents: 2500, first_cents: 3900 }] } } });
  const span = mk("span", { "data-plan-terms": "monitor" }, "$29 for the first month, then $19 a month");
  const d = page([], [span, mk("span", { "data-support": "" }, "email the support address"), mk("li", { "data-if-support": "", hidden: "" })]);
  await L.init(d, win(""));
  assert.equal(span.textContent, "$39 for the first month, then $25 a month");
  mockFetch({ "/api/plans": "network" });
  const s2 = mk("span", { "data-plan-terms": "monitor" }, "$29 for the first month, then $19 a month");
  await L.init(page([], [s2]), win(""));
  assert.equal(s2.textContent, "$29 for the first month, then $19 a month");
  mockFetch({});
  await L.init(page([], []), win(""));
  assert.equal(calls.length, 0, "pages without prices make no request");
});

let passed = 0;
for (const [name, fn] of tests) {
  try { await fn(); passed++; console.log(`ok - ${name}`); }
  catch (e) { console.log(`FAIL - ${name}`); console.error(e); process.exitCode = 1; }
}
console.log(`\n${passed} passed, ${tests.length - passed} failed`);
