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
    calls.push({ url, method: opts.method || "GET", body: opts.body });
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
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), map: m };
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
  assert.deepEqual(JSON.parse(co.body), { url: "example.com" });
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

let passed = 0;
for (const [name, fn] of tests) {
  try { await fn(); passed++; console.log(`ok - ${name}`); }
  catch (e) { console.log(`FAIL - ${name}`); console.error(e); process.exitCode = 1; }
}
console.log(`\n${passed} passed, ${tests.length - passed} failed`);
