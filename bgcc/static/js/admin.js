/* Admin configuration repeatable-list behaviour. */
(function () {
  "use strict";

  // Re-index all rows of a repeatable group so the backend's sequential index
  // parsing (row_0, row_1, ...) always matches.
  function reindex(listEl) {
    var rows = Array.prototype.slice.call(
      listEl.querySelectorAll("[data-clause-row], [data-pattern-row], [data-checklist-row], [data-bank-row], [data-section-row], [data-bu-row]")
    );
    rows.forEach(function (row, idx) {
      Array.prototype.forEach.call(row.querySelectorAll("[name]"), function (input) {
        var name = input.getAttribute("name");
        if (name && /_\d+$/.test(name)) {
          input.setAttribute("name", name.replace(/_\d+$/, "_" + idx));
        }
      });
    });
  }

  function initRepeatables() {
    var map = {
      "clause": { container: "[data-repeat-list='clause']", selector: "[data-clause-row]" },
      "pattern": { container: "[data-repeat-list='pattern']", selector: "[data-pattern-row]" },
      "checklist": { container: "[data-repeat-list='checklist']", selector: "[data-checklist-row]" },
      "bank": { container: "[data-repeat-list='bank']", selector: "[data-bank-row]" },
      "section": { container: "[data-repeat-list='section']", selector: "[data-section-row]" },
      "bu": { container: "[data-repeat-list='bu']", selector: "[data-bu-row]" }
    };

    Object.keys(map).forEach(function (kind) {
      var cfg = map[kind];
      var container = document.querySelector(cfg.container);
      if (!container) return;
      var addBtn = document.querySelector("[data-add-row='" + kind + "']");
      if (addBtn) {
        addBtn.addEventListener("click", function () {
          var existing = container.querySelector(cfg.selector);
          if (!existing) return;
          var template = existing.cloneNode(true);

          // Clear the clone's input values
          Array.prototype.forEach.call(template.querySelectorAll("input, textarea"), function (input) {
            input.value = "";
            if (input.type === "checkbox") input.checked = false;
          });

          // Reset select elements to appropriate defaults
          Array.prototype.forEach.call(template.querySelectorAll("select"), function (select) {
            if (select.name === "bu_status") select.value = "active";
            else if (select.name === "bu_connection_type") select.value = "oauth";
            else select.selectedIndex = 0;
          });

          // Polish BU card headers
          var titleText = template.querySelector(".bu-title-text");
          if (titleText) {
            var count = container.querySelectorAll(cfg.selector).length + 1;
            titleText.textContent = "New Business Unit #" + count;
          }
          var statusBadge = template.querySelector(".bu-status-badge");
          if (statusBadge) {
            statusBadge.className = "admin-badge admin-badge-success bu-status-badge";
            statusBadge.textContent = "Active";
          }

          container.appendChild(template);
          reindex(container);

          var firstInput = template.querySelector("input:not([type='hidden']), textarea, select");
          if (firstInput) {
            firstInput.focus();
            if (firstInput.scrollIntoView) {
              firstInput.scrollIntoView({ behavior: "smooth", block: "center" });
            }
          }
        });
      }

      container.addEventListener("click", function (e) {
        if (e.target.closest("[data-remove-row]")) {
          var row = e.target.closest(cfg.selector);
          if (row) {
            if (container.querySelectorAll(cfg.selector).length > 1) {
              row.remove();
              reindex(container);
            } else {
              Array.prototype.forEach.call(row.querySelectorAll("input, textarea"), function (input) {
                input.value = "";
                if (input.type === "checkbox") input.checked = false;
              });
              Array.prototype.forEach.call(row.querySelectorAll("select"), function (select) {
                select.selectedIndex = 0;
              });
            }
          }
        }
      });
    });
  }

  // Live title and status badge updates on BU cards
  document.addEventListener("input", function (e) {
    var row = e.target.closest("[data-bu-row]");
    if (!row) return;
    if (e.target.name === "bu_display" || e.target.name === "bu_business") {
      var displayInput = row.querySelector("[name='bu_display']");
      var businessInput = row.querySelector("[name='bu_business']");
      var titleText = row.querySelector(".bu-title-text");
      if (titleText) {
        var val = (displayInput && displayInput.value.trim()) || (businessInput && businessInput.value.trim()) || "New Business Unit";
        titleText.textContent = val;
      }
    }
  });

  document.addEventListener("change", function (e) {
    var row = e.target.closest("[data-bu-row]");
    if (!row) return;
    if (e.target.name === "bu_status") {
      var badge = row.querySelector(".bu-status-badge");
      if (badge) {
        var isActive = e.target.value === "active";
        badge.className = "admin-badge " + (isActive ? "admin-badge-success" : "admin-badge-light") + " bu-status-badge";
        badge.textContent = isActive ? "Active" : "Inactive";
      }
    }
  });

  document.addEventListener("DOMContentLoaded", initRepeatables);
})();
