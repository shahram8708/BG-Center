/* BG lifecycle front-end behaviour. */
(function () {
  "use strict";

  function csrf() {
    var m = document.querySelector("meta[name='csrf-token']");
    return m ? m.getAttribute("content") : "";
  }

  // Live eligibility check on the closure page (pure render of computed facts).
  function initEligibility() {
    var select = document.querySelector("[data-eligibility-select]");
    var btn = document.querySelector("[data-eligibility-check]");
    var result = document.querySelector("[data-eligibility-result]");
    if (!select || !btn || !result) return;

    btn.addEventListener("click", function () {
      var bgId = select.value;
      btn.disabled = true;
      btn.textContent = "Checking eligibility…";
      result.style.display = "block";
      result.innerHTML = '<div class="text-muted small"><i class="bi bi-hourglass-split me-1"></i> Computing eligibility from the underlying PO/contract…</div>';
      fetch("/api/closure/eligibility/" + bgId, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "Check eligibility";
          if (data.error) {
            result.innerHTML = '<div class="alert alert-danger small mb-0">' + data.error + '</div>';
            return;
          }
          var badge = data.standard
            ? '<span class="badge text-bg-success">Standard</span>'
            : '<span class="badge text-bg-warning">Exception</span>';
          result.innerHTML = '<div class="alert alert-light border small mb-0"><div class="fw-semibold mb-1">' + badge + ' - live computed result</div>' + data.reasoning + '</div>';
        })
        .catch(function () {
          btn.disabled = false;
          btn.textContent = "Check eligibility";
          result.innerHTML = '<div class="alert alert-danger small mb-0">Could not compute eligibility. Please try again.</div>';
        });
    });
  }

  document.addEventListener("DOMContentLoaded", initEligibility);
})();
