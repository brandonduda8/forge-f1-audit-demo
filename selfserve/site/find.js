/* Apex Forge Website Checkup — find a lost report link (email + website).
   API data only reaches the page through textContent. */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;
  var PATH_RE = /^report\.html\?order_id=[A-Za-z0-9_-]{10,64}$/;
  var EMAILED_MESSAGE = "We've emailed the links to that address if we found any. It can take a few minutes; check your spam folder too. " +
    "Use the email you typed at checkout.";

  function renderMatches(doc, list, data) {
    C.clear(list);
    var reports = (data && Array.isArray(data.reports)) ? data.reports : [];
    var n = 0;
    reports.slice(0, 5).forEach(function (r) {
      if (!r || typeof r.path !== "string" || !PATH_RE.test(r.path)) return;
      var when = C.formatWhen(r.created);
      var a = C.el(doc, "a", { text: "Open the report for " + C.str(r.site, 100) });
      a.setAttribute("href", r.path);           // checked against PATH_RE above: same-site page only
      var id = r.path.split("order_id=")[1];
      list.appendChild(C.el(doc, "li", {}, [a, when ? " (bought " + when + ")" : null, " · ",
        C.localLink(doc, C.dashboardHref(id), "Dashboard")]));
      n += 1;
    });
    list.hidden = n === 0;
    return n;
  }

  function init(doc, win) {
    var cfg = C.getConfig(win);
    var form = doc.getElementById("find-form");
    var email = doc.getElementById("find-email");
    var site = doc.getElementById("find-site");
    var btn = doc.getElementById("find-btn");
    var status = doc.getElementById("find-status");
    var list = doc.getElementById("find-result");
    if (!form || !email || !site || !btn || !status || !list) return;

    function showStatus(text, isError, spinning) {
      C.clear(status);
      if (spinning) status.appendChild(C.el(doc, "span", { className: "spinner", attrs: { "aria-hidden": "true" } }));
      status.appendChild(doc.createTextNode(text));
      status.className = "status" + (isError ? " error" : "");
      status.hidden = false;
    }

    if (!cfg.apiOk) {
      btn.disabled = true;
      showStatus("Report lookup isn't switched on yet. Please come back soon.", true, false);
      return;
    }

    var busy = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) return;
      var e = (email.value || "").trim(), s = (site.value || "").trim();
      if (!e || !s) {
        showStatus("Enter both the email you paid with and your website.", true, false);
        return;
      }
      busy = true;
      btn.disabled = true;
      list.hidden = true;
      showStatus("Looking for your report…", false, true);
      C.fetchJson(cfg.apiBase + "/api/find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: e.slice(0, 254), website: s.slice(0, 300) })
      }, 90000, function () { showStatus(C.WAKE_MESSAGE, false, true); }, 8000)
        .then(function (r) {
          if (r.status === 200 && r.data && r.data.ok === true && r.data.emailed === true) {
            // Emails are on: the links went to that inbox, never to this page (so a stranger can't open or cancel).
            C.clear(list);
            list.hidden = true;
            showStatus(EMAILED_MESSAGE, false, false);
          } else if (r.status === 200 && r.data && r.data.ok === true && renderMatches(doc, list, r.data) > 0) {
            showStatus("Found it. Bookmark the report page this time.", false, false);
          } else {
            showStatus(C.errorText(r.data, "We couldn't find a report for that email and website."), true, false);
          }
        }, function () { showStatus(C.NETWORK_MESSAGE, true, false); })
        .catch(function () { showStatus("Something went wrong. Please try again.", true, false); })
        .then(function () { busy = false; btn.disabled = false; });
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { renderMatches: renderMatches, init: init, EMAILED_MESSAGE: EMAILED_MESSAGE };
  } else {
    init(document, window);
  }
})();
