/* Apex Forge Website Checkup — landing page + free mini-check.
   All API data is rendered with textContent / text nodes only. */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;

  /* Pure: build the mini-check result node from a 200 response. */
  function renderMiniResult(doc, data, cfg) {
    var el = C.el;
    var site = C.str(data.site, 200) || "your site";
    var top = Array.isArray(data.top) ? data.top.slice(0, 3) : [];
    var failing = C.num(data.failing);
    var passed = C.num(data.passed);
    var notMeasured = C.num(data.not_measured);

    var wrap = el(doc, "div", { className: "result-inner" });

    wrap.appendChild(el(doc, "div", { className: "result-head" }, [
      C.gradeBadge(doc, data.grade),
      el(doc, "div", {}, [
        el(doc, "h3", { text: site }),
        el(doc, "p", { className: "score", text: C.scoreText(data.score) + (C.num(data.score) === null ? "" : "/100") })
      ])
    ]));

    if (top.length) {
      wrap.appendChild(el(doc, "h4", { text: top.length === 1 ? "Top issue found" : "Top " + top.length + " issues found" }));
      var list = el(doc, "ul", { className: "issues" });
      top.forEach(function (item) {
        item = item || {};
        list.appendChild(el(doc, "li", {}, [
          C.severityBadge(doc, item.severity),
          el(doc, "span", { text: C.str(item.title, 300) })
        ]));
      });
      wrap.appendChild(list);
    } else {
      wrap.appendChild(el(doc, "p", { text: "No failing checks showed up in the free check." }));
    }

    if (failing !== null && failing > top.length) {
      var more = Math.round(failing - top.length);
      wrap.appendChild(el(doc, "p", {
        className: "more",
        text: "+ " + more + " more issue" + (more === 1 ? "" : "s") + " found so far. The full report checks more and explains each fix."
      }));
    }

    var counts = [];
    if (passed !== null) counts.push(Math.round(passed) + " checks passed");
    if (notMeasured !== null) counts.push(Math.round(notMeasured) + " not measured (they need a full browser)");
    if (counts.length) wrap.appendChild(el(doc, "p", { className: "muted small", text: counts.join(" · ") }));

    var when = C.formatDate(data.measured_at);
    if (when) wrap.appendChild(el(doc, "p", { className: "muted small", text: "Measured " + when + " from your live site." }));

    var cta = el(doc, "div", { className: "btn-row" });
    if (cfg && cfg.payOk) {
      cta.appendChild(el(doc, "a", {
        className: "btn",
        href: cfg.paymentLink,
        text: "Get the full report — " + cfg.price
      }));
    } else {
      cta.appendChild(el(doc, "a", { className: "btn", href: "#price", text: "See the full report" }));
    }
    wrap.appendChild(cta);
    wrap.appendChild(el(doc, "p", {
      className: "small",
      text: "On the checkout page, type the same website address: " + site
    }));
    return wrap;
  }

  function friendlyStatusMessage(status, data) {
    if (status === 429) return C.errorText(data, "Too many checks right now. Please wait a minute and try again.");
    if (status === 503) return C.errorText(data, "The checker is busy. Please try again in a minute.");
    if (status === 400 || status === 422) return C.errorText(data, "That website address didn't work. Check it and try again.");
    return C.errorText(data, "Something went wrong (code " + status + "). Please try again.");
  }

  function init(doc, win) {
    var cfg = C.getConfig(win);

    // Wake the free-tier server early; ignore the result.
    if (cfg.apiOk) {
      try { fetch(cfg.apiBase + "/health", { mode: "cors", credentials: "omit", cache: "no-store" }).catch(function () {}); }
      catch (e) { /* ignore */ }
    }

    // Buy buttons and price labels.
    Array.prototype.forEach.call(doc.querySelectorAll("[data-buy]"), function (a) {
      if (cfg.payOk) {
        a.setAttribute("href", cfg.paymentLink);
        a.removeAttribute("aria-disabled");
      } else {
        a.setAttribute("href", "#price");
        a.setAttribute("aria-disabled", "true");
      }
    });
    Array.prototype.forEach.call(doc.querySelectorAll("[data-price]"), function (n) { n.textContent = cfg.price; });
    var payNote = doc.getElementById("pay-unavailable");
    if (payNote) payNote.hidden = cfg.payOk;

    var form = doc.getElementById("check-form");
    var input = doc.getElementById("site-url");
    var button = doc.getElementById("check-btn");
    var status = doc.getElementById("check-status");
    var result = doc.getElementById("check-result");
    if (!form || !input || !button || !status || !result) return;

    function showStatus(text, isError, spinning) {
      C.clear(status);
      if (spinning) status.appendChild(C.el(doc, "span", { className: "spinner", attrs: { "aria-hidden": "true" } }));
      status.appendChild(doc.createTextNode(text));
      status.className = "status" + (isError ? " error" : "");
      status.hidden = false;
    }

    if (!cfg.apiOk) {
      button.disabled = true;
      showStatus("The free check isn't switched on yet. Please come back soon.", true, false);
      return;
    }

    var busy = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) return;
      var value = (input.value || "").trim();
      if (!value) {
        showStatus("Type your website address first, for example yourbusiness.com", true, false);
        input.focus();
        return;
      }
      if (value.length > 300) {
        showStatus("That address is too long. Use just your homepage, for example yourbusiness.com", true, false);
        return;
      }
      busy = true;
      button.disabled = true;
      result.hidden = true;
      C.clear(result);
      showStatus("Checking your site… this usually takes under a minute.", false, true);

      C.fetchJson(cfg.apiBase + "/api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: value })
      }, 150000, function () { showStatus(C.WAKE_MESSAGE, false, true); }, 8000)
        .then(function (r) {
          if (r.status === 200 && r.data && r.data.ok === true) {
            status.hidden = true;
            result.appendChild(renderMiniResult(doc, r.data, cfg));
            result.hidden = false;
            if (result.focus) result.focus();
          } else {
            showStatus(friendlyStatusMessage(r.status, r.data), true, false);
          }
        }, function (err) {
          if (err && err.kind === "timeout") {
            showStatus("The check took too long. The checker may still be waking up — please try again.", true, false);
          } else {
            showStatus(C.NETWORK_MESSAGE, true, false);
          }
        })
        .catch(function () { showStatus("Something went wrong showing the result. Please try again.", true, false); })
        .then(function () { busy = false; button.disabled = false; });
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { renderMiniResult: renderMiniResult, friendlyStatusMessage: friendlyStatusMessage, init: init };
  } else {
    init(document, window);
  }
})();
