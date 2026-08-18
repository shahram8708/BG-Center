/* BG approval workflow front-end behaviour. */
(function () {
  "use strict";

  function initWorkspace() {
    var form = document.querySelector("[data-workspace-form]");
    if (!form) return;
    var forwardBtn = form.querySelector("[data-forward]");
    var hint = form.querySelector("[data-enable-hint]");
    var hasProhibited = form.querySelector("[data-decision-select][data-prohibited]") !== null;

    var decisions = form.querySelectorAll("[data-decision-select]");
    var acks = form.querySelectorAll(".ack-clause");

    function decideValid() {
      return Array.prototype.every.call(decisions, function (sel) {
        return sel.value === "accepted" || sel.value === "rejected";
      });
    }
    function acksValid() {
      if (acks.length === 0) return true;
      return Array.prototype.every.call(acks, function (c) { return c.checked; });
    }
    function update() {
      var ready = decideValid() && acksValid() && !hasProhibited;
      forwardBtn.disabled = !ready;
      if (ready) {
        hint.textContent = "Ready to submit.";
      } else if (hasProhibited) {
        hint.textContent = "Approval is disabled: a prohibited-tier deviation requires administrator action.";
      } else {
        hint.textContent = "Decide every deviation and acknowledge any missing critical clause to enable submission.";
      }
    }
    decisions.forEach(function (sel) { sel.addEventListener("change", update); });
    acks.forEach(function (c) { c.addEventListener("change", update); });
    update();
  }

  function initSaveView() {
    document.querySelectorAll("[data-save-view]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var name = (document.getElementById("viewName").value || "").trim();
        if (!name) { alert("Please enter a name for the view."); return; }
        var form = document.createElement("form");
        form.method = "POST";
        form.action = "/bg-multi-stage-approval/save-view";
        var csrf = document.querySelector("meta[name='csrf-token']");
        var fields = [
          ["csrf_token", csrf ? csrf.getAttribute("content") : ""],
          ["name", name],
          ["sap", (document.getElementById("sap") || {}).value || ""],
          ["min_amount", (document.getElementById("min_amount") || {}).value || ""],
          ["max_amount", (document.getElementById("max_amount") || {}).value || ""]
        ];
        fields.forEach(function (kv) {
          var i = document.createElement("input"); i.type = "hidden";
          i.name = kv[0]; i.value = kv[1]; form.appendChild(i);
        });
        document.body.appendChild(form); form.submit();
      });
    });
  }

  function initCeoForms() {
    document.querySelectorAll("[data-ceo-form]").forEach(function (formEl) {
      var bgId = formEl.querySelector("[data-ceo-confirm]").getAttribute("data-ceo-confirm");
      var confirm = formEl.querySelector("[data-ceo-confirm]");
      var submit = formEl.querySelector("[data-ceo-submit='" + bgId + "']");
      function update() { submit.disabled = !confirm.checked; }
      confirm.addEventListener("change", update);
      update();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initWorkspace();
    initSaveView();
    initCeoForms();
  });
  window.initWorkspace = initWorkspace;
})();
