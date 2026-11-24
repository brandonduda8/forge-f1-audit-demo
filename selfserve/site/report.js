/* Apex Forge Website Checkup — paid report page.
   All API data is rendered with textContent / text nodes only. */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;

  var PDF_PATH_RE = /^\/api\/report\.pdf\?order_id=[A-Za-z0-9_-]{10,64}$/;

  function isValidOrderId(id) {
    return C.isOrderId(id);
  }

  function pdfHref(cfg, pdfUrl, orderId) {
    if (typeof pdfUrl === "string" && PDF_PATH_RE.test(pdfUrl)) return cfg.apiBase + pdfUrl;
    return cfg.apiBase + "/api/report.pdf?order_id=" + encodeURIComponent(orderId);
  }

  /* The order this page is for: Square's own redirect parameter, our link's, or the one
     this browser saved before going to checkout (Square's test mode may not add it). */
  function orderIdFrom(win) {
    var params = new URLSearchParams(win.location.search);
    var fromUrl = params.get("order_id") || params.get("orderId") || "";
    if (fromUrl) return { id: fromUrl, fromUrl: true };
    return { id: C.lastOrder(win), fromUrl: false };
  }

  function section(doc, title, id) {
    var s = C.el(doc, "section", { attrs: id ? { "aria-labelledby": id } : {} });
    s.appendChild(C.el(doc, "h2", { text: title, attrs: id ? { id: id } : {} }));
    return s;
  }

  function findingCard(doc, f) {
    var el = C.el;
    f = f || {};
    var key = C.severityKey(f.severity);
    var card = el(doc, "article", { className: "finding sev-border-" + key });
    var head = el(doc, "div", {}, [C.severityBadge(doc, f.severity)]);
    if (f.id !== undefined && f.id !== null && f.id !== "") {
      head.appendChild(el(doc, "span", { className: "muted small", text: " Check " + C.str(f.id, 12) }));
    }
    card.appendChild(head);
    card.appendChild(el(doc, "h3", { text: C.str(f.title, 300) }));
    var dl = el(doc, "dl");
    [["What we measured", f.measured], ["Why it matters", f.why], ["How to fix it", f.fix], ["Effort", f.effort]]
      .forEach(function (pair) {
        if (pair[1] === undefined || pair[1] === null || pair[1] === "") return;
        dl.appendChild(el(doc, "dt", { text: pair[0] }));
        dl.appendChild(el(doc, "dd", { text: C.str(pair[1], 2000) }));
      });
    card.appendChild(dl);
    return card;
  }

  /* Pure: build the full report node from a 200 response. */
  function renderReport(doc, data, cfg, orderId) {
    var el = C.el;
    var rep = (data && data.report) || {};
    var site = C.str(data.site, 200) || "your website";
    var categories = Array.isArray(rep.categories) ? rep.categories : [];
    var findings = Array.isArray(rep.findings) ? rep.findings : [];
    var passed = Array.isArray(rep.passed) ? rep.passed : [];
    var notMeasured = Array.isArray(rep.not_measured) ? rep.not_measured : [];
    var when = C.formatDate(data.measured_at);

    var root = el(doc, "div", { className: "report" });

    // Header
    var head = el(doc, "section", { className: "report-head" });
    head.appendChild(el(doc, "h1", { text: "Website report for " + site }));
    head.appendChild(el(doc, "div", { className: "report-meta" }, [
      C.gradeBadge(doc, data.grade),
      el(doc, "div", {}, [
        el(doc, "p", { className: "score", text: "Score: " + C.scoreText(data.score) + (C.num(data.score) === null ? "" : "/100") }),
        typeof data.final_url === "string" && data.final_url
          ? el(doc, "p", { className: "muted small", text: "Your address sent us on to " + C.str(data.final_url, 200) + ", so that is the page we checked." })
          : null,
        when ? el(doc, "p", { className: "muted small", text: "Measured " + when + " from your live site." }) : null
      ])
    ]));
    head.appendChild(el(doc, "p", { className: "notice no-print", text: "Bookmark this page — your report link works for 30 days. Lost it? Use Find my report at the bottom of any page." }));
    head.appendChild(el(doc, "div", { className: "btn-row no-print" }, [
      el(doc, "a", { className: "btn", href: pdfHref(cfg, data.pdf_url, orderId), text: "Download PDF", attrs: { rel: "noopener" } }),
      el(doc, "button", { className: "btn btn-secondary", text: "Print", attrs: { type: "button", "data-action": "print" } })
    ]));
    root.appendChild(head);

    // Categories
    if (categories.length) {
      var cs = section(doc, "Scores by category", "h-cats");
      var table = el(doc, "table");
      table.appendChild(el(doc, "thead", {}, [el(doc, "tr", {}, [
        el(doc, "th", { text: "Category", attrs: { scope: "col" } }),
        el(doc, "th", { className: "num", text: "Score", attrs: { scope: "col" } }),
        el(doc, "th", { text: "", attrs: { scope: "col", "aria-hidden": "true" } }),
        el(doc, "th", { className: "num", text: "Weight", attrs: { scope: "col" } })
      ])]));
      var tbody = el(doc, "tbody");
      categories.forEach(function (c) {
        c = c || {};
        var s = C.num(c.score);
        var bar = el(doc, "div", { className: "bar", attrs: { "aria-hidden": "true" } });
        var fill = el(doc, "span");
        if (fill.style) fill.style.width = (s === null ? 0 : Math.max(0, Math.min(100, s))) + "%";
        bar.appendChild(fill);
        var w = C.num(c.weight);
        tbody.appendChild(el(doc, "tr", {}, [
          el(doc, "th", { text: C.str(c.name, 60), attrs: { scope: "row" } }),
          el(doc, "td", { className: "num", text: s === null ? "not measured" : C.scoreText(c.score) + "/100" }),
          el(doc, "td", {}, [bar]),
          el(doc, "td", { className: "num", text: w === null ? "" : Math.round(w) + "%" })
        ]));
      });
      table.appendChild(tbody);
      cs.appendChild(el(doc, "div", { className: "table-wrap" }, [table]));
      root.appendChild(cs);
    }

    // Top 3 fixes
    var ts = section(doc, "Top 3 fixes", "h-top");
    if (findings.length) {
      var ol = el(doc, "ol");
      findings.slice(0, 3).forEach(function (f) {
        f = f || {};
        var li = el(doc, "li", {}, [
          C.severityBadge(doc, f.severity), " ",
          el(doc, "strong", { text: C.str(f.title, 300) })
        ]);
        if (f.fix) li.appendChild(el(doc, "p", { text: C.str(f.fix, 1000) }));
        if (f.effort) li.appendChild(el(doc, "p", { className: "muted small", text: "Effort: " + C.str(f.effort, 200) }));
        ol.appendChild(li);
      });
      ts.appendChild(ol);
    } else {
      ts.appendChild(el(doc, "p", { text: "No failing checks were found. Nice work." }));
    }
    root.appendChild(ts);

    // All findings
    if (findings.length) {
      var fs = section(doc, "All findings (" + findings.length + ")", "h-all");
      findings.forEach(function (f) { fs.appendChild(findingCard(doc, f)); });
      root.appendChild(fs);
    }

    // Passed
    var ps = section(doc, "What passed (" + passed.length + ")", "h-pass");
    if (passed.length) {
      var pl = el(doc, "ul", { className: "passed-list" });
      passed.forEach(function (p) {
        p = p || {};
        var li = el(doc, "li", {}, [el(doc, "strong", { text: C.str(p.title, 300) })]);
        if (p.measured) li.appendChild(doc.createTextNode(" — " + C.str(p.measured, 500)));
        pl.appendChild(li);
      });
      ps.appendChild(pl);
    } else {
      ps.appendChild(el(doc, "p", { text: "No checks passed this time." }));
    }
    root.appendChild(ps);

    // Not measured
    var ns = section(doc, "Not measured (" + notMeasured.length + ")", "h-nm");
    ns.appendChild(el(doc, "p", {
      className: "muted",
      text: "These checks need a full browser render or access we don't have, so we list them instead of guessing."
    }));
    if (notMeasured.length) {
      var nl = el(doc, "ul", { className: "nm-list" });
      notMeasured.forEach(function (n) {
        n = n || {};
        var li = el(doc, "li", {}, [el(doc, "strong", { text: C.str(n.label, 300) })]);
        if (n.reason) li.appendChild(doc.createTextNode(" — " + C.str(n.reason, 500)));
        nl.appendChild(li);
      });
      ns.appendChild(nl);
    }
    root.appendChild(ns);

    root.appendChild(el(doc, "p", {
      className: "muted small",
      text: "Every finding above is an automated measurement of your live site at the time shown. " +
        "This is automated information, not professional advice, and it does not guarantee rankings or sales."
    }));
    return root;
  }

  function init(doc, win) {
    var cfg = C.getConfig(win);
    var out = doc.getElementById("report-root");
    var status = doc.getElementById("report-status");
    if (!out || !status) return;

    var found = orderIdFrom(win);
    var orderId = found.id;

    function showStatus(text, isError, spinning) {
      C.clear(status);
      if (spinning) status.appendChild(C.el(doc, "span", { className: "spinner", attrs: { "aria-hidden": "true" } }));
      status.appendChild(doc.createTextNode(text));
      status.className = "status" + (isError ? " error" : "");
      status.hidden = false;
    }

    function actions(buttons) {
      var row = C.el(doc, "div", { className: "btn-row" });
      buttons.forEach(function (b) { row.appendChild(b); });
      C.clear(out);
      out.appendChild(row);
    }

    function button(label, onClick, secondary) {
      var b = C.el(doc, "button", { className: "btn" + (secondary ? " btn-secondary" : ""), text: label, attrs: { type: "button" } });
      b.addEventListener("click", onClick);
      return b;
    }

    if (!isValidOrderId(orderId)) {
      showStatus("This link is missing its order reference. If you've paid, use Find my report with the email you paid with.", true, false);
      return;
    }
    // Keep a link that can be bookmarked: report.html?order_id=...
    try {
      var params = new URLSearchParams(win.location.search);
      if (params.get("order_id") !== orderId && win.history && win.history.replaceState) {
        win.history.replaceState(null, "", "report.html?order_id=" + encodeURIComponent(orderId));
      }
    } catch (e) { /* ignore */ }
    if (!cfg.apiOk) {
      showStatus("The report service isn't configured yet. Please try again later.", true, false);
      return;
    }

    function refund(btn) {
      btn.disabled = true;
      showStatus("Requesting your refund…", false, true);
      C.fetchJson(cfg.apiBase + "/api/refund", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order_id: orderId })
      }, 60000, null)
        .then(function (r) {
          if (r.status === 200 && r.data && r.data.ok === true) {
            showStatus("Refund issued. Square sends it back to the card or Cash App account you paid with; it can take a few business days to show.", false, false);
            C.clear(out);
          } else {
            showStatus(C.errorText(r.data, "The refund didn't go through (code " + r.status + ")."), true, false);
            btn.disabled = false;
          }
        }, function () {
          showStatus(C.NETWORK_MESSAGE, true, false);
          btn.disabled = false;
        });
    }

    function load() {
      C.clear(out);
      showStatus("Loading your report… if it's still being built this can take a minute or two.", false, true);
      C.fetchJson(cfg.apiBase + "/api/report?order_id=" + encodeURIComponent(orderId), { method: "GET" },
        200000, function () { showStatus(C.WAKE_MESSAGE, false, true); }, 8000)
        .then(function (r) {
          var d = r.data || {};
          if (r.status === 200 && d.ok === true) {
            status.hidden = true;
            C.clear(out);
            out.appendChild(renderReport(doc, d, cfg, orderId));
            Array.prototype.forEach.call(out.querySelectorAll('[data-action="print"]'), function (b) {
              b.addEventListener("click", function () { win.print(); });
            });
            if (d.site) doc.title = "Report for " + C.str(d.site, 100) + " — Apex Forge Website Checkup";
            var h1 = out.querySelector("h1");
            if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus(); }
          } else if (r.status === 402) {
            showStatus(C.errorText(d, "We couldn't find a completed payment for this link."), true, false);
            actions([button("Try again", load, true)]);
          } else if (r.status === 422 && d.refundable === true) {
            showStatus(C.errorText(d, "We couldn't check this website."), true, false);
            var refundBtn = button("Refund me automatically", function () { refund(refundBtn); }, false);
            actions([button("Try again", load, true), refundBtn]);
          } else {
            showStatus(C.errorText(d, "Something went wrong loading your report (code " + r.status + ")."), true, false);
            actions([button("Try again", load, true)]);
          }
        }, function (err) {
          showStatus(err && err.kind === "timeout"
            ? "Your report is taking longer than expected. Please try again — your link still works."
            : C.NETWORK_MESSAGE, true, false);
          actions([button("Try again", load, true)]);
        })
        .catch(function () {
          showStatus("Something went wrong showing your report. Please try again.", true, false);
          actions([button("Try again", load, true)]);
        });
    }

    load();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { isValidOrderId: isValidOrderId, orderIdFrom: orderIdFrom, renderReport: renderReport, pdfHref: pdfHref, findingCard: findingCard, init: init };
  } else {
    init(document, window);
  }
})();
