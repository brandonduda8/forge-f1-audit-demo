/* Apex Forge Website Checkup — buyer dashboard (plan, report link, history, monitoring).
   API data only reaches the page through textContent / text nodes. */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;

  var STATUS_TEXT = {
    ACTIVE: "Active",
    PENDING: "Starting",
    PAUSED: "Paused",
    CANCELED: "Cancelled",
    DEACTIVATED: "Ended"
  };

  function subStatus(sub) {
    var raw = String((sub && sub.status) || "").toUpperCase();
    return STATUS_TEXT[raw] || (raw ? C.str(raw, 30) : "Unknown");
  }

  /* Monitoring is still on and has not been cancelled yet. */
  function canCancel(plan, sub) {
    return plan === "monitor" && !!sub && String(sub.status || "").toUpperCase() === "ACTIVE" && !sub.canceled_date;
  }

  /* When a cancelled subscription stops: the end of the paid month, if Square told us. */
  function endsText(day) {
    var d = C.formatDay(day);
    return d ? "Monitoring stays on until " + d + ". You won't be charged again." : "You won't be charged again.";
  }

  function section(doc, title, id) {
    var s = C.el(doc, "section", { className: "dash-section", attrs: { "aria-labelledby": id } });
    s.appendChild(C.el(doc, "h2", { text: title, attrs: { id: id } }));
    return s;
  }

  function fact(doc, dl, label, value) {
    if (!value) return;
    dl.appendChild(C.el(doc, "dt", { text: label }));
    dl.appendChild(C.el(doc, "dd", { text: value }));
  }

  function isCancelled(sub) {
    return !!sub && (!!sub.canceled_date || String(sub.status || "").toUpperCase() === "CANCELED");
  }

  /* Fill the Monitoring section (it is re-filled in place after a cancel). */
  function fillSubscription(doc, s, plan, sub, refunded) {
    var el = C.el;
    s.appendChild(el(doc, "h2", { text: "Monitoring", attrs: { id: "h-sub" } }));
    if (plan !== "monitor") {
      s.appendChild(el(doc, "p", { text: "This order is a one-time report, so there is nothing to cancel and you won't be charged again." }));
      return s;
    }
    if (refunded) {
      s.appendChild(el(doc, "p", { className: "notice", text: "This payment was refunded, so its report link no longer works. " +
        "The monitoring subscription is shown below so you can check that it has ended, or cancel it." }));
    }
    if (!sub || typeof sub !== "object") {
      s.appendChild(el(doc, "p", { className: "status error", text:
        "We couldn't read your subscription from Square just now. Please reload this page in a minute." }));
      return s;
    }
    var dl = el(doc, "dl", { className: "facts", attrs: { "data-sub": "" } });
    var cancelled = isCancelled(sub);
    fact(doc, dl, "Status", cancelled && String(sub.status || "").toUpperCase() === "ACTIVE" ? "Active until the end of the paid month (cancelled)" : subStatus(sub));
    fact(doc, dl, "Started", C.formatDay(sub.start_date));
    fact(doc, dl, "Paid through", C.formatDay(sub.charged_through_date));
    fact(doc, dl, "Ends on", C.formatDay(sub.canceled_date));
    s.appendChild(dl);
    if (cancelled) {
      s.appendChild(el(doc, "p", { text: endsText(sub.canceled_date || sub.charged_through_date) }));
    } else if (canCancel(plan, sub)) {
      s.appendChild(el(doc, "p", { className: "muted small", text:
        "Renews monthly until you cancel. Cancelling takes effect at the end of the month you've already paid for." }));
    }
    return s;
  }

  /* Pure: the subscription facts, as a <dl>. */
  function renderSubscription(doc, plan, sub, refunded) {
    var s = C.el(doc, "section", { className: "dash-section", attrs: { "aria-labelledby": "h-sub", "data-sub-section": "" } });
    return fillSubscription(doc, s, plan, sub, refunded);
  }

  /* One problem from latest_changes: {id, title, severity} (a bare string is accepted too). */
  function changeItem(doc, x) {
    if (typeof x === "string") return x ? C.el(doc, "li", { text: C.str(x, 300) }) : null;
    if (!x || typeof x !== "object") return null;
    var title = C.str(x.title || x.id, 300);
    if (!title) return null;
    return C.el(doc, "li", {}, [x.severity ? C.severityBadge(doc, x.severity) : null, x.severity ? " " : null, C.el(doc, "span", { text: title })]);
  }

  /* latest_changes: {score_change, new: [...], fixed: [...], since?, grade_before?, grade_after?}. */
  function renderChanges(doc, changes) {
    var el = C.el;
    var cs = section(doc, "What changed since the last check", "h-changes");
    var since = C.formatWhen(changes.since);
    if (since) cs.appendChild(el(doc, "p", { className: "muted small", text: "Compared with the check on " + since + "." }));
    var sc = C.num(changes.score_change);
    cs.appendChild(el(doc, "p", { className: "score", text: sc === null ? "Score change: not measured"
      : sc === 0 ? "Score: no change" : "Score: " + (sc > 0 ? "+" : "") + Math.round(sc) + " point" + (Math.abs(Math.round(sc)) === 1 ? "" : "s") }));
    var gb = C.gradeKey(changes.grade_before) ? C.str(changes.grade_before, 3).trim().toUpperCase() : "";
    var ga = C.gradeKey(changes.grade_after) ? C.str(changes.grade_after, 3).trim().toUpperCase() : "";
    if (gb && ga) cs.appendChild(el(doc, "p", { text: gb === ga ? "Grade: " + ga + " (no change)" : "Grade: " + gb + " before, " + ga + " now" }));
    [["New problems", changes["new"]], ["Fixed", changes.fixed]].forEach(function (pair) {
      var items = (Array.isArray(pair[1]) ? pair[1] : []).slice(0, 50).map(function (x) { return changeItem(doc, x); })
        .filter(function (x) { return x; });
      cs.appendChild(el(doc, "h3", { text: pair[0] + " (" + items.length + ")" }));
      if (items.length) {
        cs.appendChild(el(doc, "ul", { className: "checklist" }, items));
      } else {
        cs.appendChild(el(doc, "p", { className: "muted", text: "None." }));
      }
    });
    cs.appendChild(el(doc, "p", { className: "muted small", text:
      "Only checks that could be measured both times are compared, so a check we couldn't measure this time is never shown as fixed or new." }));
    return cs;
  }

  /* Pure: build the dashboard node from a 200 GET /api/dashboard response. */
  function renderDashboard(doc, data, orderId, prices) {
    var el = C.el;
    data = data || {};
    var site = C.str(data.site, 200) || "your website";
    var plan = C.isPlanId(data.plan) ? data.plan : "checkup";
    var sub = data.subscription && typeof data.subscription === "object" ? data.subscription : null;
    var refunded = data.refunded === true;
    var history = (Array.isArray(data.history) ? data.history : []).filter(function (x) { return x && typeof x === "object"; });
    var changes = data.latest_changes && typeof data.latest_changes === "object" ? data.latest_changes : null;

    var root = el(doc, "div", { className: "dashboard" });

    var head = el(doc, "section", { className: "dash-head", attrs: { "aria-label": "Your order" } });
    var dl = el(doc, "dl", { className: "facts" });
    fact(doc, dl, "Website", site);
    fact(doc, dl, "Plan", C.planName(plan));
    if (refunded) fact(doc, dl, "Payment", "Refunded");
    head.appendChild(dl);
    var href = refunded ? "" : C.reportHref(data.report_path, orderId);
    var row = el(doc, "div", { className: "btn-row no-print" });
    if (href) row.appendChild(C.localLink(doc, href, "Open your report", "btn"));
    if (plan === "monitor") row.appendChild(el(doc, "button", { className: "btn btn-secondary", text: "Print this page", attrs: { type: "button", "data-action": "print" } }));
    head.appendChild(row);
    root.appendChild(head);

    root.appendChild(renderSubscription(doc, plan, sub, refunded));

    if (changes) root.appendChild(renderChanges(doc, changes));

    var hs = section(doc, "Check history", "h-history");
    if (history.length) {
      var table = el(doc, "table");
      table.appendChild(el(doc, "thead", {}, [el(doc, "tr", {}, [
        el(doc, "th", { text: "Checked", attrs: { scope: "col" } }),
        el(doc, "th", { text: "Grade", attrs: { scope: "col" } }),
        el(doc, "th", { className: "num", text: "Score", attrs: { scope: "col" } })
      ])]));
      var tb = el(doc, "tbody");
      history.slice(0, 36).forEach(function (h) {
        var g = C.gradeKey(h.grade) ? C.str(h.grade, 3).trim().toUpperCase() : "?";
        tb.appendChild(el(doc, "tr", {}, [
          el(doc, "td", { text: C.formatWhen(h.at) || "unknown date" }),
          el(doc, "td", { text: g }),
          el(doc, "td", { className: "num", text: C.num(h.score) === null ? "not measured" : C.scoreText(h.score) + "/100" })
        ]));
      });
      table.appendChild(tb);
      hs.appendChild(el(doc, "div", { className: "table-wrap" }, [table]));
    } else {
      hs.appendChild(el(doc, "p", { text: "No checks are recorded yet." }));
    }
    hs.appendChild(el(doc, "p", { className: "muted small", text:
      "History is kept on the checker's server and can be lost when the server is updated. Payments and your subscription are always kept by Square." }));
    root.appendChild(hs);

    if (canCancel(plan, sub)) {
      var xs = section(doc, "Cancel monitoring", "h-cancel");
      xs.setAttribute("data-cancel-section", "");
      var paid = C.formatDay(sub.charged_through_date);
      xs.appendChild(el(doc, "p", { attrs: { "data-cancel-intro": "" }, text: "You can cancel online at any time. Monitoring then stays on until the end of the month you've already paid for" +
        (paid ? " (" + paid + ")" : "") + ", and you won't be charged again." }));
      xs.appendChild(el(doc, "div", { className: "btn-row" }, [
        el(doc, "button", { className: "btn btn-secondary", text: "Cancel monitoring", attrs: { type: "button", "data-action": "cancel-start", "aria-controls": "cancel-confirm", "aria-expanded": "false" } })
      ]));
      var confirm = el(doc, "div", { className: "confirm", attrs: { id: "cancel-confirm", role: "group", "aria-labelledby": "h-confirm", hidden: "" } });
      confirm.appendChild(el(doc, "h3", { text: "Cancel Monthly Monitoring for " + site + "?", attrs: { id: "h-confirm", tabindex: "-1" } }));
      confirm.appendChild(el(doc, "p", { text: endsText(sub.charged_through_date) + " Your past reports stay where they are." }));
      confirm.appendChild(el(doc, "div", { className: "btn-row" }, [
        el(doc, "button", { className: "btn btn-danger", text: "Yes, cancel monitoring", attrs: { type: "button", "data-action": "cancel-confirm" } }),
        el(doc, "button", { className: "btn btn-secondary", text: "Keep monitoring", attrs: { type: "button", "data-action": "cancel-keep" } })
      ]));
      xs.appendChild(confirm);
      xs.appendChild(el(doc, "p", { className: "status", attrs: { "data-cancel-status": "", role: "status", "aria-live": "polite", hidden: "" } }));
      root.appendChild(xs);
    }

    if (plan === "monitor") root.appendChild(C.renewalTerms(doc, orderId, prices || null));
    return root;
  }

  function init(doc, win) {
    var cfg = C.getConfig(win);
    var out = doc.getElementById("dash-root");
    var status = doc.getElementById("dash-status");
    if (!out || !status) return;
    var orderId = C.orderIdFrom(win).id;

    function showStatus(text, isError, spinning) {
      C.clear(status);
      if (spinning) status.appendChild(C.el(doc, "span", { className: "spinner", attrs: { "aria-hidden": "true" } }));
      status.appendChild(doc.createTextNode(text));
      status.className = "status" + (isError ? " error" : "");
      status.hidden = false;
    }

    if (!C.isOrderId(orderId)) {
      showStatus("This link is missing its order reference. Use Find my report with the email you paid with, then open the dashboard from your report.", true, false);
      return;
    }
    try {
      var params = new URLSearchParams(win.location.search);
      if (params.get("order_id") !== orderId && win.history && win.history.replaceState) {
        win.history.replaceState(null, "", C.dashboardHref(orderId));
      }
    } catch (e) { /* ignore */ }
    if (!cfg.apiOk) {
      showStatus("The dashboard isn't switched on yet. Please try again later.", true, false);
      return;
    }

    function retry() {
      var b = C.el(doc, "button", { className: "btn btn-secondary", text: "Try again", attrs: { type: "button" } });
      b.addEventListener("click", load);
      C.clear(out);
      out.appendChild(C.el(doc, "div", { className: "btn-row" }, [b]));
    }

    function wireCancel(data) {
      var start = out.querySelector('[data-action="cancel-start"]');
      if (!start) return;
      var panel = out.querySelector('[role="group"]');
      var yes = out.querySelector('[data-action="cancel-confirm"]');
      var keep = out.querySelector('[data-action="cancel-keep"]');
      var st = out.querySelector("[data-cancel-status]");
      function say(text, isError) {
        st.textContent = text;
        st.className = "status" + (isError ? " error" : "");
        st.hidden = false;
      }
      start.addEventListener("click", function () {
        panel.hidden = false;
        start.setAttribute("aria-expanded", "true");
        var h = panel.querySelector("h3");
        if (h) h.focus();
      });
      keep.addEventListener("click", function () {
        panel.hidden = true;
        start.setAttribute("aria-expanded", "false");
        start.focus();
      });
      var busy = false;
      yes.addEventListener("click", function () {
        if (busy) return;
        busy = true;
        yes.disabled = true;
        keep.disabled = true;
        say("Cancelling…", false);
        C.fetchJson(cfg.apiBase + "/api/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ order_id: orderId })
        }, 60000, null).then(function (r) {
          var d = r.data || {};
          if (r.status === 200 && d.ok === true) {
            var before = data.subscription || {};
            var ends = typeof d.ends === "string" && d.ends ? d.ends : (before.canceled_date || before.charged_through_date || null);
            var after = Object.assign({}, before, { canceled_date: ends, status: typeof d.status === "string" && d.status ? d.status : before.status });
            data.subscription = after;
            var subSec = out.querySelector("[data-sub-section]");
            if (subSec) { C.clear(subSec); fillSubscription(doc, subSec, "monitor", after, data.refunded === true); }
            panel.hidden = true;
            start.hidden = true;
            start.disabled = true;
            var intro = out.querySelector("[data-cancel-intro]");
            if (intro) intro.hidden = true;
            say("Monitoring is cancelled. " + endsText(ends), false);
            if (st.focus) { st.setAttribute("tabindex", "-1"); st.focus(); }
            return;
          }
          say(C.errorText(d, "The cancellation didn't go through (code " + r.status + "). Please try again."), true);
          busy = false; yes.disabled = false; keep.disabled = false;
        }, function () {
          say(C.NETWORK_MESSAGE, true);
          busy = false; yes.disabled = false; keep.disabled = false;
        });
      });
    }

    function load() {
      C.clear(out);
      showStatus("Loading your dashboard…", false, true);
      C.fetchJson(cfg.apiBase + "/api/dashboard?order_id=" + encodeURIComponent(orderId), { method: "GET" },
        90000, function () { showStatus(C.WAKE_MESSAGE, false, true); }, 8000)
        .then(function (r) {
          var d = r.data || {};
          if (r.status === 200 && d.ok === true) {
            status.hidden = true;
            C.clear(out);
            out.appendChild(renderDashboard(doc, d, orderId, null));
            wireCancel(d);
            Array.prototype.forEach.call(out.querySelectorAll('[data-action="print"]'), function (b) {
              b.addEventListener("click", function () { if (win.print) win.print(); });
            });
            if (C.isPlanId(d.plan) && d.plan === "monitor") {
              C.loadPlans(cfg).then(function (plans) { if (plans && plans.monitor) C.fillRenewalPrice(out, plans.monitor); });
            }
            if (d.site) doc.title = "Dashboard for " + C.str(d.site, 100) + " — Apex Forge Website Checkup";
          } else {
            showStatus(C.errorText(d, "We couldn't load your dashboard (code " + r.status + ")."), true, false);
            retry();
          }
        }, function () {
          showStatus(C.NETWORK_MESSAGE, true, false);
          retry();
        })
        .catch(function () {
          showStatus("Something went wrong showing your dashboard. Please try again.", true, false);
          retry();
        });
    }

    load();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { renderDashboard: renderDashboard, renderSubscription: renderSubscription, renderChanges: renderChanges, canCancel: canCancel, init: init };
  } else {
    init(document, window);
  }
})();
