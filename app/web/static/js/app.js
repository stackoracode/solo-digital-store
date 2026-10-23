// Progressive enhancement only; every flow works without JavaScript. No inline handlers (CSP).
(function () {
  "use strict";

  document.addEventListener("click", function (ev) {
    var copy = ev.target.closest("[data-copy]");
    if (copy) {
      var text = copy.getAttribute("data-copy");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () {
          var old = copy.textContent;
          copy.textContent = "✓";
          setTimeout(function () { copy.textContent = old; }, 1200);
        });
      }
      ev.preventDefault();
      return;
    }
    var confirmEl = ev.target.closest("[data-confirm]");
    if (confirmEl && !window.confirm(confirmEl.getAttribute("data-confirm"))) {
      ev.preventDefault();
    }
  });

  // Prevent double submission of payment / checkout forms.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (form.dataset.submitted === "1") { ev.preventDefault(); return; }
    form.dataset.submitted = "1";
    setTimeout(function () { form.dataset.submitted = ""; }, 8000);
  });

  // Order page: poll status (server is the only source of truth) and reload when it changes.
  var order = document.getElementById("order");
  if (order && order.dataset.statusUrl) {
    var done = order.dataset.fulfillment === "delivered" || ["refunded"].indexOf(order.dataset.payment) >= 0;
    if (!done) {
      var tries = 0;
      var poll = function () {
        tries += 1;
        if (tries > 120 || document.hidden) { setTimeout(poll, 15000); return; }
        fetch(order.dataset.statusUrl + window.location.search, { credentials: "same-origin", cache: "no-store" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (s) {
            if (s && (s.payment_status !== order.dataset.payment || s.fulfillment_status !== order.dataset.fulfillment)) {
              window.location.reload();
            } else { setTimeout(poll, 10000); }
          })
          .catch(function () { setTimeout(poll, 20000); });
      };
      setTimeout(poll, 8000);
    }
  }

  // Show times in the viewer's local timezone.
  document.querySelectorAll("[data-utc]").forEach(function (el) {
    var d = new Date(el.getAttribute("data-utc"));
    if (!isNaN(d)) { el.textContent = d.toLocaleString(); }
  });
})();
