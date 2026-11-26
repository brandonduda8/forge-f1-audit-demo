/* Apex Forge Website Checkup — shared helpers.
   Loaded in the browser as window.ApexCommon; in node (tests) via require().
   Rule: API data only ever reaches the page through textContent / createTextNode. */
(function (root) {
  "use strict";

  var SEVERITIES = ["critical", "high", "medium", "low"];
  /* the checker's own levels, mapped onto the page's colours */
  var SEVERITY_ALIASES = { major: "high", minor: "medium" };
  var WAKE_MESSAGE = "Waking up the checker — the first check of the hour can take up to a minute.";
  var NETWORK_MESSAGE = "We couldn't reach the checker. Check your connection and try again in a moment.";

  function isHttpsUrl(value) {
    if (typeof value !== "string" || value.length > 2000) return false;
    return /^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/[^\s"'<>\\`]*)?$/.test(value);
  }

  /* Square's hosted checkout. Card details are typed there, never on our pages or server. */
  var CHECKOUT_HOSTS = ["square.link", "checkout.square.site", "squareup.com", "squareupsandbox.com"];
  var ORDER_RE = /^[A-Za-z0-9_-]{10,64}$/;
  var LAST_ORDER_KEY = "apexLastOrder";

  function isCheckoutUrl(value) {
    if (!isHttpsUrl(value)) return false;
    var m = /^https:\/\/([A-Za-z0-9.-]+)(:\d+)?(\/|$)/.exec(value);
    if (!m || m[2]) return false;
    var host = m[1].toLowerCase();
    return CHECKOUT_HOSTS.some(function (h) { return host === h || host.slice(-(h.length + 1)) === "." + h; });
  }

  function isOrderId(value) {
    return typeof value === "string" && ORDER_RE.test(value);
  }

  function rememberOrder(win, id) {
    try { if (isOrderId(id)) win.localStorage.setItem(LAST_ORDER_KEY, id); } catch (e) { /* private mode */ }
  }

  function lastOrder(win) {
    try { var v = win.localStorage.getItem(LAST_ORDER_KEY); return isOrderId(v) ? v : ""; } catch (e) { return ""; }
  }

  /* The plans the API sells. Names and descriptions are ours; prices and availability come
     from GET /api/plans at runtime. */
  var PLAN_NAMES = { checkup: "Website Checkup", pro: "Pro Checkup", monitor: "Monthly Monitoring" };
  var PLAN_RE = /^[a-z][a-z0-9_]{0,19}$/;

  function isPlanId(value) {
    return typeof value === "string" && PLAN_RE.test(value);
  }

  function planName(id) {
    return Object.prototype.hasOwnProperty.call(PLAN_NAMES, id) ? PLAN_NAMES[id] : (isPlanId(id) ? id : "Website Checkup");
  }

  /* Money arrives in cents (integers). "$29", "$79.50", "$1,234.00" with always2. "" if not a number. */
  function dollars(cents, always2) {
    var n = num(cents);
    if (n === null) return "";
    n = Math.round(n);
    var neg = n < 0;
    n = Math.abs(n);
    var whole = String(Math.floor(n / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var part = n % 100;
    var out = "$" + whole + (part || always2 ? "." + (part < 10 ? "0" : "") + part : "");
    return neg ? "-" + out : out;
  }

  /* Pull the usable plans out of a GET /api/plans body: {id: {cents, kind, available, label}}. */
  function plansById(data) {
    var out = {};
    var list = data && Array.isArray(data.plans) ? data.plans : [];
    list.forEach(function (p) {
      if (!p || typeof p !== "object" || !isPlanId(p.id) || out[p.id]) return;
      var cents = num(p.cents);
      out[p.id] = {
        id: p.id,
        cents: cents !== null && cents > 0 ? Math.round(cents) : null,
        kind: p.kind === "subscription" ? "subscription" : "one_time",
        available: p.available === true && cents !== null && cents > 0,
        label: str(p.label, 120)
      };
    });
    return out;
  }

  /* Ask the API for a Square checkout for this website and plan, then go there.
     Resolves {ok: true} after navigating, or {ok: false, message}. */
  function startCheckout(win, cfg, website, plan) {
    var value = String(website || "").trim();
    plan = plan === undefined || plan === null || plan === "" ? "checkup" : plan;
    if (!value) return Promise.resolve({ ok: false, message: "Type your website address first, for example yourbusiness.com" });
    if (value.length > 300) return Promise.resolve({ ok: false, message: "That address is too long. Use just your homepage." });
    if (!isPlanId(plan)) return Promise.resolve({ ok: false, message: "That plan isn't available. Please pick another." });
    if (!cfg.payOk) return Promise.resolve({ ok: false, message: "Checkout isn't switched on yet. Please come back soon." });
    return fetchJson(cfg.apiBase + "/api/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: value, plan: plan })
    }, 90000, null).then(function (r) {
      var d = r.data || {};
      if (r.status === 200 && d.ok === true && isCheckoutUrl(d.checkout_url) && isOrderId(d.order_id)) {
        rememberOrder(win, d.order_id);
        win.location.assign(d.checkout_url);
        return { ok: true };
      }
      if (r.status === 200) return { ok: false, message: "The checkout link looked wrong, so we stopped. Please try again." };
      return { ok: false, message: errorText(d, "We couldn't open the checkout (code " + r.status + "). Please try again.") };
    }, function () {
      return { ok: false, message: NETWORK_MESSAGE };
    });
  }

  /* The order a page is for: our own link's order_id, Square's orderId, or the one this
     browser saved before going to checkout (Square's test mode may not add it). */
  function orderIdFrom(win) {
    var params = new URLSearchParams((win.location && win.location.search) || "");
    var fromUrl = params.get("order_id") || params.get("orderId") || "";
    if (fromUrl) return { id: fromUrl, fromUrl: true };
    return { id: lastOrder(win), fromUrl: false };
  }

  /* Same-site links to an order's pages. Only ever built from a checked order id. */
  var REPORT_PATH_RE = /^\/?(report\.html\?order_id=[A-Za-z0-9_-]{10,64})$/;

  function reportHref(path, orderId) {
    var m = typeof path === "string" ? REPORT_PATH_RE.exec(path) : null;
    if (m) return m[1];
    return isOrderId(orderId) ? "report.html?order_id=" + encodeURIComponent(orderId) : "";
  }

  function dashboardHref(orderId) {
    return isOrderId(orderId) ? "dashboard.html?order_id=" + encodeURIComponent(orderId) : "";
  }

  /* A same-site link to one of our own pages. Only fixed, checked paths are accepted. */
  function localLink(doc, path, text, className) {
    var a = el(doc, "a", { className: className || "", text: text });
    if (/^([a-z0-9-]+\.html(\?order_id=[A-Za-z0-9_-]{10,64}|#[a-z0-9-]+)?|#[a-z0-9-]+)$/.test(String(path || ""))) a.setAttribute("href", path);
    return a;
  }

  /* "2026-11-26" -> a local date string, without any time-zone shift. Other dates via formatDate. */
  function formatDay(value) {
    if (typeof value !== "string" || !value) return "";
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!m) return formatDate(value);
    var d = new Date(+m[1], +m[2] - 1, +m[3]);
    if (isNaN(d.getTime())) return str(value, 20);
    try { return d.toLocaleDateString(undefined, { dateStyle: "medium" }); }
    catch (e) { return value; }
  }

  /* Read and validate window.APEX_CONFIG. Never throws. */
  function getConfig(win) {
    var raw = (win && win.APEX_CONFIG) || {};
    var apiBase = typeof raw.apiBase === "string" ? raw.apiBase.replace(/\/+$/, "") : "";
    var cfg = {
      apiBase: isHttpsUrl(apiBase) ? apiBase : "",
      checkout: raw.checkout !== false,
      emails: raw.emails === true,
      price: typeof raw.price === "string" && raw.price.length <= 20 ? raw.price : "$29",
      supportEmail: typeof raw.supportEmail === "string" &&
        /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/.test(raw.supportEmail) ? raw.supportEmail : ""
    };
    cfg.apiOk = cfg.apiBase !== "";
    cfg.payOk = cfg.apiOk && cfg.checkout;
    return cfg;
  }

  /* Build an element. opts: {className, text, attrs, href}. children: nodes or strings.
     Strings always become text nodes. href is only set when it is https or a same-page anchor. */
  function el(doc, tag, opts, children) {
    var node = doc.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) {
        if (/^on/i.test(k) || k === "style" || k === "href" || k === "src") return;
        node.setAttribute(k, String(opts.attrs[k]));
      });
    }
    if (opts.href !== undefined) {
      var h = String(opts.href);
      if (isHttpsUrl(h) || /^#[A-Za-z0-9_-]*$/.test(h)) node.setAttribute("href", h);
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "object" ? c : doc.createTextNode(String(c)));
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function str(value, max) {
    if (value === null || value === undefined) return "";
    var s = String(value);
    max = max || 2000;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }

  function severityKey(value) {
    var s = String(value || "").toLowerCase();
    if (SEVERITY_ALIASES[s]) return SEVERITY_ALIASES[s];
    return SEVERITIES.indexOf(s) >= 0 ? s : "low";
  }

  function gradeKey(value) {
    var m = /^([A-Fa-f])[+-]?$/.exec(String(value || "").trim());
    return m ? m[1].toLowerCase() : "";
  }

  function num(value) {
    var n = typeof value === "number" ? value : parseFloat(value);
    return isFinite(n) ? n : null;
  }

  function scoreText(value) {
    var n = num(value);
    if (n === null) return "not measured";
    return String(Math.max(0, Math.min(100, Math.round(n))));
  }

  function formatDate(iso) {
    if (typeof iso !== "string" || !iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return str(iso, 40);
    try {
      return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch (e) {
      return d.toISOString();
    }
  }

  function gradeBadge(doc, grade) {
    var key = gradeKey(grade);
    return el(doc, "span", {
      className: "grade" + (key ? " grade-" + key : ""),
      text: key ? str(grade, 3).trim().toUpperCase() : "?",
      attrs: { "aria-label": "Grade " + (key ? str(grade, 3).trim().toUpperCase() : "unknown") }
    });
  }

  function severityBadge(doc, severity) {
    var key = severityKey(severity);
    return el(doc, "span", { className: "sev sev-" + key, text: str(severity || key, 20) });
  }

  /* fetch JSON with a timeout. onSlow fires once after slowMs if still waiting.
     Resolves {status, data}; rejects with {kind: "timeout"|"network"}. */
  function fetchJson(url, init, timeoutMs, onSlow, slowMs) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; if (ctrl) ctrl.abort(); }, timeoutMs);
    var slow = onSlow ? setTimeout(onSlow, slowMs || 8000) : null;
    var opts = Object.assign({ credentials: "omit", cache: "no-store" }, init || {});
    if (ctrl) opts.signal = ctrl.signal;
    function done() { clearTimeout(timer); if (slow) clearTimeout(slow); }
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (body) {
        done();
        var data = null;
        try { data = body ? JSON.parse(body) : null; } catch (e) { data = null; }
        return { status: res.status, data: data };
      });
    }, function () {
      done();
      throw { kind: timedOut ? "timeout" : "network" };
    });
  }

  function errorText(data, fallback) {
    if (data && typeof data.error === "string" && data.error.trim()) return str(data.error, 500);
    return fallback;
  }

  var api = {
    WAKE_MESSAGE: WAKE_MESSAGE,
    NETWORK_MESSAGE: NETWORK_MESSAGE,
    isHttpsUrl: isHttpsUrl,
    isCheckoutUrl: isCheckoutUrl,
    isOrderId: isOrderId,
    rememberOrder: rememberOrder,
    lastOrder: lastOrder,
    startCheckout: startCheckout,
    PLAN_NAMES: PLAN_NAMES,
    isPlanId: isPlanId,
    planName: planName,
    dollars: dollars,
    plansById: plansById,
    orderIdFrom: orderIdFrom,
    reportHref: reportHref,
    dashboardHref: dashboardHref,
    localLink: localLink,
    formatDay: formatDay,
    getConfig: getConfig,
    el: el,
    clear: clear,
    str: str,
    severityKey: severityKey,
    gradeKey: gradeKey,
    num: num,
    scoreText: scoreText,
    formatDate: formatDate,
    gradeBadge: gradeBadge,
    severityBadge: severityBadge,
    fetchJson: fetchJson,
    errorText: errorText
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ApexCommon = api;
})(typeof window !== "undefined" ? window : this);
