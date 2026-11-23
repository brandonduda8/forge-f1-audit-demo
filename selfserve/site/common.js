/* Apex Forge Website Checkup — shared helpers.
   Loaded in the browser as window.ApexCommon; in node (tests) via require().
   Rule: API data only ever reaches the page through textContent / createTextNode. */
(function (root) {
  "use strict";

  var SEVERITIES = ["critical", "high", "medium", "low"];
  var WAKE_MESSAGE = "Waking up the checker — the first check of the hour can take up to a minute.";
  var NETWORK_MESSAGE = "We couldn't reach the checker. Check your connection and try again in a moment.";

  function isHttpsUrl(value) {
    if (typeof value !== "string" || value.length > 2000) return false;
    return /^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/[^\s"'<>\\`]*)?$/.test(value);
  }

  function isPaymentLink(value) {
    return isHttpsUrl(value) && /^https:\/\/buy\.stripe\.com\/[^\s]+$/.test(value);
  }

  /* Read and validate window.APEX_CONFIG. Never throws. */
  function getConfig(win) {
    var raw = (win && win.APEX_CONFIG) || {};
    var apiBase = typeof raw.apiBase === "string" ? raw.apiBase.replace(/\/+$/, "") : "";
    var cfg = {
      apiBase: isHttpsUrl(apiBase) ? apiBase : "",
      paymentLink: isPaymentLink(raw.paymentLink) ? raw.paymentLink : "",
      price: typeof raw.price === "string" && raw.price.length <= 20 ? raw.price : "$29",
      supportEmail: typeof raw.supportEmail === "string" &&
        /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/.test(raw.supportEmail) ? raw.supportEmail : ""
    };
    cfg.apiOk = cfg.apiBase !== "";
    cfg.payOk = cfg.paymentLink !== "";
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
    if (n === null) return "—";
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
    isPaymentLink: isPaymentLink,
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
