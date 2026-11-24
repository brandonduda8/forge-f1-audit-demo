/* Apex Forge Website Checkup — fills the support contact on the legal pages. */
(function () {
  "use strict";
  var C = window.ApexCommon;
  var cfg = C.getConfig(window);
  Array.prototype.forEach.call(document.querySelectorAll("[data-support]"), function (slot) {
    if (!cfg.supportEmail) return; // keep the fallback text already in the page
    C.clear(slot);
    var a = document.createElement("a");
    a.setAttribute("href", "mailto:" + cfg.supportEmail);
    a.textContent = cfg.supportEmail;
    slot.appendChild(document.createTextNode("email "));
    slot.appendChild(a);
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-if-support]"), function (n) {
    n.hidden = !cfg.supportEmail;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-if-no-support]"), function (n) {
    n.hidden = !!cfg.supportEmail;
  });
})();
