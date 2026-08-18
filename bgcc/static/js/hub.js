/* BG Status Hub + Bank Tracker behaviour. */
(function () {
  "use strict";

  function csrf() {
    var m = document.querySelector("meta[name='csrf-token']");
    return m ? m.getAttribute("content") : "";
  }

  function initSaveStatusView() {
    document.querySelectorAll("[data-save-status-view]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var name = (document.getElementById("viewName").value || "").trim();
        if (!name) { alert("Please enter a name for the view."); return; }
        var form = document.createElement("form");
        form.method = "POST";
        form.action = "/bg-status/save-view";
        var pairs = [
          ["csrf_token", csrf()], ["name", name],
          ["status", (document.getElementById("status") || {}).value || ""],
          ["bg_type", (document.getElementById("bg_type") || {}).value || ""],
          ["expenditure", (document.getElementById("expenditure") || {}).value || ""],
          ["business_unit", (document.getElementById("business_unit") || {}).value || ""],
          ["vendor", (document.getElementById("vendor") || {}).value || ""],
          ["q", (document.getElementById("q") || {}).value || ""],
          ["date_from", (document.getElementById("date_from") || {}).value || ""],
          ["date_to", (document.getElementById("date_to") || {}).value || ""]
        ];
        pairs.forEach(function (kv) {
          var i = document.createElement("input"); i.type = "hidden";
          i.name = kv[0]; i.value = kv[1]; form.appendChild(i);
        });
        document.body.appendChild(form); form.submit();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", initSaveStatusView);
})();
