/* Apex Forge Website Checkup — the legal pages: fills the support contact, and the current
   Monthly Monitoring prices from GET /api/plans (the page's own text is the fallback). */
(function () {
  "use strict";

  var C = (typeof module !== "undefined" && module.exports) ? require("./common.js") : window.ApexCommon;

  function each(doc, sel, fn) {
    Array.prototype.forEach.call(doc.querySelectorAll(sel), fn);
  }

  /* Resolves when done (tests wait on it). */
  function init(doc, win) {
    var cfg = C.getConfig(win);
    each(doc, "[data-support]", function (slot) {
      if (!cfg.supportEmail) return; // keep the fallback text already in the page
      C.clear(slot);
      var a = doc.createElement("a");
      a.setAttribute("href", "mailto:" + cfg.supportEmail);
      a.textContent = cfg.supportEmail;
      slot.appendChild(doc.createTextNode("email "));
      slot.appendChild(a);
    });
    each(doc, "[data-if-support]", function (n) { n.hidden = !cfg.supportEmail; });
    each(doc, "[data-if-no-support]", function (n) { n.hidden = !!cfg.supportEmail; });
    if (!doc.querySelectorAll("[data-plan-terms]").length) return Promise.resolve(null);
    return C.loadPlans(cfg).then(function (plans) {
      C.fillPlanTerms(doc, plans);
      return plans;
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { init: init };
  } else {
    init(document, window);
  }
})();
