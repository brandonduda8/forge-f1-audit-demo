/* Apex Forge Website Checkup — owner dashboard.
   The admin token lives only in sessionStorage (or, if the browser blocks it, in this page's
   memory). It is never put in local storage, a URL, the page, or a log. API data is text only. */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;
  var TOKEN_KEY = "apexAdminToken";
  var TOKEN_RE = /^[\x21-\x7E]{1,512}$/;     // printable, no spaces: safe in an Authorization header

  function isToken(v) { return typeof v === "string" && TOKEN_RE.test(v); }

  /* sessionStorage with an in-memory fallback. Nothing else ever stores the token. */
  function tokenStore(win) {
    var mem = "";
    function ss() { try { return win.sessionStorage || null; } catch (e) { return null; } }
    return {
      get: function () {
        var s = ss();
        try { if (s) { var v = s.getItem(TOKEN_KEY); return isToken(v) ? v : ""; } } catch (e) { /* blocked */ }
        return mem;
      },
      set: function (v) {
        var s = ss();
        try { if (s) { s.setItem(TOKEN_KEY, v); mem = ""; return; } } catch (e) { /* blocked */ }
        mem = v;
      },
      clear: function () {
        var s = ss();
        try { if (s) s.removeItem(TOKEN_KEY); }
        catch (e) { try { s.setItem(TOKEN_KEY, ""); } catch (e2) { /* blocked */ } }
        mem = "";
      }
    };
  }

  function whenText(v) {
    return C.formatWhen(v);
  }

  function tile(doc, label, value, note) {
    return C.el(doc, "div", { className: "tile" }, [
      C.el(doc, "p", { className: "tile-label", text: label }),
      C.el(doc, "p", { className: "tile-value", text: value }),
      note ? C.el(doc, "p", { className: "muted small", text: note }) : null
    ]);
  }

  function count(v) {
    var n = C.num(v);
    return n === null ? "—" : String(Math.round(n));
  }

  function money(v) {
    var s = C.dollars(v, true);
    return s || "—";
  }

  function table(doc, headers, rows, numCols) {
    var el = C.el;
    var t = el(doc, "table");
    t.appendChild(el(doc, "thead", {}, [el(doc, "tr", {}, headers.map(function (h, i) {
      return el(doc, "th", { className: numCols.indexOf(i) >= 0 ? "num" : "", text: h, attrs: { scope: "col" } });
    }))]));
    var tb = el(doc, "tbody");
    rows.forEach(function (r) {
      tb.appendChild(el(doc, "tr", {}, r.map(function (c, i) {
        return el(doc, "td", { className: numCols.indexOf(i) >= 0 ? "num" : "", text: c });
      })));
    });
    t.appendChild(tb);
    return el(doc, "div", { className: "table-wrap" }, [t]);
  }

  function facts(doc, rows) {
    var dl = C.el(doc, "dl", { className: "facts" });
    rows.forEach(function (f) {
      dl.appendChild(C.el(doc, "dt", { text: f[0] }));
      dl.appendChild(C.el(doc, "dd", { text: f[1] }));
    });
    return dl;
  }

  /* The monitoring part: {running, last_run: {started, finished, counts: {...}} | null}. */
  function renderMonitoring(doc, mon) {
    var el = C.el;
    var ms = el(doc, "section", { attrs: { "aria-labelledby": "h-mon" } }, [el(doc, "h2", { text: "Monitoring", attrs: { id: "h-mon" } })]);
    mon = mon && typeof mon === "object" ? mon : {};
    if (mon.running === true) ms.appendChild(el(doc, "p", { className: "status", text: "A monitoring run is going now. Press Refresh in a few minutes to see its result." }));
    var last = mon.last_run && typeof mon.last_run === "object" ? mon.last_run : null;
    if (!last) {
      ms.appendChild(el(doc, "p", { text: "No monitoring run is recorded on the API's disk (it is wiped when the API is redeployed)." }));
      return ms;
    }
    var c = last.counts && typeof last.counts === "object" ? last.counts : {};
    ms.appendChild(el(doc, "h3", { text: "Last run" }));
    ms.appendChild(facts(doc, [
      ["Started", whenText(last.started) || "—"],
      ["Finished", whenText(last.finished) || (mon.running === true ? "still running" : "didn't finish")],
      ["Monitored sites", count(c.sites)],
      ["Checked", count(c.scanned)],
      ["Skipped (checked in the last 25 days)", count(c.skipped_recent)],
      ["Couldn't be checked (tried again next run)", count(c.not_checked)],
      ["Emails sent", count(c.emails_sent)],
      ["Emails not sent", count(c.emails_skipped)]
    ]));
    if (c.error) ms.appendChild(el(doc, "p", { className: "status error", text: c.error === "square_unavailable"
      ? "This run couldn't list the subscriptions from Square, so no site was checked."
      : "This run stopped early: " + C.str(c.error, 200) }));
    return ms;
  }

  /* Pure: build the summary node from a 200 GET /api/admin/summary response:
     {ok, counters: {free_checks, checkouts_started, reports_delivered, since}, monitoring: {...},
      sales: sales_summary | null, sales_error?}. */
  function renderSummary(doc, data) {
    var el = C.el;
    data = data || {};
    var s = data.sales && typeof data.sales === "object" ? data.sales : null;
    var counters = data.counters && typeof data.counters === "object" ? data.counters : {};
    var root = el(doc, "div", { className: "admin" });
    root.appendChild(el(doc, "h2", { text: "Summary", attrs: { id: "h-summary", tabindex: "-1" } }));

    var since = whenText(counters.since);
    var countersNote = "Counters are since the API last started" + (since ? " (" + since + ")" : "") + ".";
    var freeTile = tile(doc, "Free checks", count(counters.free_checks),
      "Checkouts started " + count(counters.checkouts_started) + ", reports delivered " + count(counters.reports_delivered));

    if (s) {
      var days = C.num(s.days);
      var period = days === null ? "this period" : "the last " + Math.round(days) + " day" + (Math.round(days) === 1 ? "" : "s");
      root.appendChild(el(doc, "p", { className: "muted", text: "Sales for " + period + ", read from Square. " + countersNote }));
      if (s.truncated === true) {
        root.appendChild(el(doc, "p", { className: "status error", text:
          "Square had more records than this page reads in one go, so these totals are incomplete. Pick a shorter period, or check Square's own dashboard." }));
      }
      var cancelling = C.num(s.cancelling);
      root.appendChild(el(doc, "div", { className: "tiles" }, [
        tile(doc, "Net sales", money(s.net_cents), "Gross " + money(s.gross_cents) + ", refunded " + money(s.refunded_cents)),
        tile(doc, "Orders", count(s.orders)),
        tile(doc, "Monthly recurring", money(s.mrr_cents), "From active subscriptions that aren't set to end"),
        tile(doc, "Active subscriptions", count(s.active_subscriptions),
          cancelling === null ? null : count(cancelling) + " more cancelled, ending at the end of their paid month"),
        freeTile
      ]));

      var byPlan = s.by_plan && typeof s.by_plan === "object" ? s.by_plan : {};
      var planRows = Object.keys(byPlan).filter(C.isPlanId).sort().map(function (id) {
        var p = byPlan[id] || {};
        return [C.planName(id), count(p.count), money(p.gross_cents)];
      });
      var bp = el(doc, "section", { attrs: { "aria-labelledby": "h-by-plan" } }, [el(doc, "h2", { text: "By plan", attrs: { id: "h-by-plan" } })]);
      bp.appendChild(planRows.length ? table(doc, ["Plan", "Orders", "Gross"], planRows, [1, 2]) : el(doc, "p", { text: "No sales in this period." }));
      root.appendChild(bp);

      var recent = (Array.isArray(s.recent) ? s.recent : []).filter(function (x) { return x && typeof x === "object"; }).slice(0, 20);
      var rs = el(doc, "section", { attrs: { "aria-labelledby": "h-recent" } }, [el(doc, "h2", { text: "Recent sales", attrs: { id: "h-recent" } })]);
      rs.appendChild(recent.length ? table(doc, ["When", "Plan", "Site", "Amount", "Refunded"], recent.map(function (r) {
        return [whenText(r.created) || "—", C.planName(C.isPlanId(r.plan) ? r.plan : "checkup"), C.str(r.site, 100) || "—",
          money(r.amount_cents), r.refunded ? "Yes" : "No"];
      }), [3]) : el(doc, "p", { text: "No sales in this period." }));
      root.appendChild(rs);
    } else {
      root.appendChild(el(doc, "p", { className: "status error", text: C.str(data.sales_error, 300) ||
        "Sales weren't loaded from Square this time. Press Refresh to try again." }));
      root.appendChild(el(doc, "p", { className: "muted", text: countersNote }));
      root.appendChild(el(doc, "div", { className: "tiles" }, [freeTile]));
    }

    root.appendChild(renderMonitoring(doc, data.monitoring));
    return root;
  }

  function init(doc, win) {
    var cfg = C.getConfig(win);
    var store = tokenStore(win);
    var form = doc.getElementById("admin-login");
    var input = doc.getElementById("admin-token");
    var bar = doc.getElementById("admin-bar");
    var daysSel = doc.getElementById("admin-days");
    var refresh = doc.getElementById("admin-refresh");
    var run = doc.getElementById("admin-run");
    var logout = doc.getElementById("admin-logout");
    var status = doc.getElementById("admin-status");
    var out = doc.getElementById("admin-root");
    if (!form || !input || !bar || !status || !out) return;

    function showStatus(text, isError, spinning) {
      C.clear(status);
      if (spinning) status.appendChild(C.el(doc, "span", { className: "spinner", attrs: { "aria-hidden": "true" } }));
      status.appendChild(doc.createTextNode(text));
      status.className = "status" + (isError ? " error" : "");
      status.hidden = false;
    }

    function signedOut(message, isError) {
      store.clear();
      C.clear(out);
      bar.hidden = true;
      form.hidden = false;
      input.value = "";
      if (message) showStatus(message, isError, false); else status.hidden = true;
    }

    function signedIn() {
      form.hidden = true;
      bar.hidden = false;
    }

    function authHeaders() {
      return { Authorization: "Bearer " + store.get() };
    }

    function failed(r, fallback) {
      var d = r.data || {};
      if (r.status === 401 || r.status === 403) { signedOut("That admin token wasn't accepted. Paste it again.", true); return; }
      if (r.status === 429) { showStatus("Too many tries. Wait a minute, then try again.", true, false); return; }
      if (r.status === 404 || r.status === 503) {
        signedOut(C.errorText(d, "The owner dashboard is switched off on the API (ADMIN_TOKEN is not set, or is shorter than 24 characters)."), true);
        return;
      }
      showStatus(C.errorText(d, fallback + " (code " + r.status + ")."), true, false);
    }

    function days() {
      var v = daysSel ? String(daysSel.value || "30") : "30";
      return /^(7|30|90)$/.test(v) ? v : "30";
    }

    var busy = false;
    function load(focusSummary) {
      if (busy || !store.get()) return;
      busy = true;
      showStatus("Loading…", false, true);
      C.fetchJson(cfg.apiBase + "/api/admin/summary?days=" + days(), { method: "GET", headers: authHeaders() },
        90000, function () { showStatus(C.WAKE_MESSAGE, false, true); }, 8000)
        .then(function (r) {
          if (r.status === 200 && r.data && r.data.ok === true) {
            signedIn();
            status.hidden = true;
            C.clear(out);
            out.appendChild(renderSummary(doc, r.data));
            if (focusSummary === true) {         // the sign-in form just went away: put focus on what replaced it
              var h = out.querySelector("#h-summary") || out.querySelector("h2");
              if (h && h.focus) h.focus();
            }
          } else {
            failed(r, "Couldn't load the summary");
          }
        }, function () { showStatus(C.NETWORK_MESSAGE, true, false); })
        .catch(function () { showStatus("Something went wrong showing the summary.", true, false); })
        .then(function () { busy = false; });
    }

    if (!cfg.apiOk) {
      var b = doc.getElementById("admin-login-btn");
      if (b) b.disabled = true;
      showStatus("APEX_API_BASE isn't configured, so there is nothing to connect to.", true, false);
      return;
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var t = String(input.value || "").trim();
      input.value = "";                                     // never left on the page
      if (!isToken(t)) {
        showStatus("Paste the admin token (no spaces).", true, false);
        return;
      }
      store.set(t);
      load(true);
    });
    if (refresh) refresh.addEventListener("click", function () { load(false); });
    if (daysSel) daysSel.addEventListener("change", function () { load(false); });
    if (logout) logout.addEventListener("click", function () { signedOut("Logged out. The token was removed from this tab.", false); });
    if (run) run.addEventListener("click", function () {
      if (run.disabled || !store.get()) return;
      run.disabled = true;
      showStatus("Starting a monitoring run…", false, true);
      C.fetchJson(cfg.apiBase + "/api/admin/run-monitoring", { method: "POST", headers: authHeaders() }, 60000, null)
        .then(function (r) {
          if (r.status === 202 && r.data && r.data.ok === true) {
            showStatus("Monitoring run started. It runs in the background; press Refresh in a few minutes to see the result.", false, false);
          } else if (r.status === 409) {
            showStatus("A monitoring run is already going. Press Refresh later to see the result.", false, false);
          } else {
            failed(r, "Couldn't start a monitoring run");
          }
        }, function () { showStatus(C.NETWORK_MESSAGE, true, false); })
        .then(function () { run.disabled = false; });
    });

    if (store.get()) load(true);
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { renderSummary: renderSummary, renderMonitoring: renderMonitoring, tokenStore: tokenStore, isToken: isToken, init: init };
  } else {
    init(document, window);
  }
})();
