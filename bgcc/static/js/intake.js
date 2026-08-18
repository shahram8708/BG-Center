/* BG intake & validation front-end behaviour. */
(function () {
  "use strict";

  function csrf() {
    var m = document.querySelector("meta[name='csrf-token']");
    return m ? m.getAttribute("content") : "";
  }
  function post(url, data) {
    return fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(data)
    });
  }

  /* ---- Step 1: upload form ---- */
  function initUploadForm() {
    initChipInput();
    initDropZone();
    initParentSelector();
  }

  function initChipInput() {
    document.querySelectorAll("[data-chip-input]").forEach(function (wrap) {
      var input = wrap.querySelector("[data-chip-input-field]");
      var list = wrap.querySelector("[data-chip-list]");
      var errorEl = wrap.parentElement.querySelector("[data-chip-error]");
      var vendors = {};
      var order = [];

      function setError(msg) {
        if (!errorEl) return;
        errorEl.textContent = msg || "";
        errorEl.style.display = msg ? "block" : "none";
      }
      function render() {
        list.innerHTML = "";
        order.forEach(function (po) {
          var chip = document.createElement("span");
          chip.className = "chip";
          chip.innerHTML = '<span>' + po + '</span><span class="chip-remove" data-remove="' + po + '">&times;</span>';
          chip.querySelector("[data-remove]").addEventListener("click", function () {
            order = order.filter(function (x) { return x !== po; });
            delete vendors[po];
            render();
            setError("");
          });
          list.appendChild(chip);
        });
        order.forEach(function (po) {
          var hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = "po_number";
          hidden.value = po;
          wrap.appendChild(hidden);
        });
      }
      function add(po) {
        po = po.trim();
        if (!po) return;
        if (order.indexOf(po) !== -1) { input.value = ""; return; }
        fetch("/api/po/context/" + encodeURIComponent(po), { headers: { "Accept": "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.found) {
              setError("PO number '" + po + "' was not found in the financial records.");
              return;
            }
            var vendor = data.vendor_name || "";
            if (order.length && vendors[order[0]] && vendor &&
                vendors[order[0]].toLowerCase() !== vendor.toLowerCase()) {
              setError("All purchase order numbers must belong to the same vendor.");
              return;
            }
            order.push(po);
            vendors[po] = vendor;
            input.value = "";
            setError("");
            render();
          });
      }
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          add(input.value);
        }
      });
      input.addEventListener("blur", function () { if (input.value.trim()) add(input.value); });
    });
  }

  function initDropZone() {
    document.querySelectorAll("[data-drop-zone]").forEach(function (zone) {
      var fileInput = zone.querySelector("[data-file-input]");
      var fileName = zone.querySelector("[data-file-name]");
      function setFile(file) {
        if (file) {
          zone.classList.add("has-file");
          if (fileName) fileName.textContent = file.name;
        } else {
          zone.classList.remove("has-file");
        }
      }
      function validate(file) {
        var err = zone.parentElement.querySelector("[data-file-error]");
        var msg = "";
        if (file) {
          if (file.type !== "application/pdf" && !/\.pdf$/i.test(file.name)) {
            msg = "Only PDF files are accepted. Please upload a PDF.";
          }
        }
        if (err) { err.textContent = msg; err.style.display = msg ? "block" : "none"; }
        return !msg;
      }
      zone.addEventListener("click", function () { fileInput.click(); });
      zone.addEventListener("dragover", function (e) { e.preventDefault(); zone.classList.add("dragover"); });
      zone.addEventListener("dragleave", function () { zone.classList.remove("dragover"); });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        zone.classList.remove("dragover");
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
          var f = e.dataTransfer.files[0];
          if (validate(f)) setFile(f);
        }
      });
      fileInput.addEventListener("change", function () {
        var f = fileInput.files[0];
        if (f) { if (validate(f)) setFile(f); }
      });
    });
  }

  function initParentSelector() {
    var select = document.querySelector("[data-parent-select]");
    if (!select) return;
    var meta = window.__parentMeta || {};
    var search = document.querySelector("[data-parent-search]");
    var summary = document.querySelector("[data-parent-summary]");
    var sapEl = document.querySelector("[data-parent-sap]");
    var typeEl = document.querySelector("[data-parent-type]");
    var fmtEl = document.querySelector("[data-parent-format]");
    var expEl = document.querySelector("[data-parent-expiry]");

    function showParent() {
      var info = meta[select.value];
      if (!info) { summary.style.display = "none"; return; }
      sapEl.textContent = info.sap_system;
      typeEl.textContent = info.bg_type;
      fmtEl.textContent = info.format_variant + " · " + info.expenditure_type;
      expEl.textContent = info.expiry_date;
      summary.style.display = "flex";
    }
    select.addEventListener("change", showParent);
    if (search) {
      search.addEventListener("input", function () {
        var q = (search.value || "").toLowerCase();
        Array.prototype.forEach.call(select.options, function (opt) {
          opt.style.display = (!q || opt.text.toLowerCase().indexOf(q) !== -1) ? "" : "none";
        });
      });
    }
  }

  /* ---- Step 2: progress / polling ---- */
  function initProgress(bgId, reviewUrl, opts) {
    var attempts = 0;
    var maxAttempts = 90;
    var longTimer = window.setTimeout(function () {
      var el = document.getElementById("longRunning");
      if (el) el.style.display = "block";
    }, 20000);

    function setStage(state) {
      Object.keys(state).forEach(function (key) {
        var row = document.querySelector("[data-stage-row='" + key + "']");
        if (!row) return;
        var s = state[key];
        var spinner = document.querySelector("[data-stage-spinner='" + key + "']");
        var detail = document.querySelector("[data-stage-detail='" + key + "']");
        row.classList.remove("processing", "completed", "failed");
        if (s.status === "processing" || s.status === "queued") {
          row.classList.add("processing");
          if (spinner) spinner.style.display = "inline-block";
        } else if (s.status === "completed") {
          row.classList.add("completed");
          if (spinner) spinner.style.display = "none";
          if (detail) detail.textContent = "Complete";
        } else if (s.status === "failed") {
          row.classList.add("failed");
          if (spinner) spinner.style.display = "none";
          if (detail) detail.textContent = "Failed - " + (s.error_message || "unexpected error");
        }
      });
    }

    function renderFailure(data) {
      document.getElementById("failureState").style.display = "block";
      var failed = (data.stages || []).filter(function (s) { return s.status === "failed"; });
      document.getElementById("failureMessage").textContent =
        "One or more validation steps failed. You can retry only the failed step(s) - successfully completed steps are kept.";
      var btns = document.getElementById("retryButtons");
      btns.innerHTML = "";
      failed.forEach(function (s) {
        var b = document.createElement("button");
        b.className = "btn btn-outline-brand";
        b.textContent = "Retry " + s.stage.replace(/_/g, " ");
        b.addEventListener("click", function () {
          b.disabled = true;
          b.textContent = "Retrying…";
          post("/api/pipeline/retry/" + bgId + "/" + s.stage, {}).then(function () {
            document.getElementById("failureState").style.display = "none";
            attempts = 0;
            poll();
          });
        });
        btns.appendChild(b);
      });
    }

    function poll() {
      attempts++;
      fetch("/api/pipeline/status/" + bgId, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          setStage(rowsToObj(data.stages));
          if (data.overall === "ready") {
            clearTimeout(longTimer);
            window.location.href = reviewUrl;
          } else if (data.overall === "blocked") {
            clearTimeout(longTimer);
            document.getElementById("blockedState").style.display = "block";
            document.getElementById("stageList").style.display = "none";
          } else if (data.overall === "failed") {
            clearTimeout(longTimer);
            renderFailure(data);
          } else if (attempts >= maxAttempts) {
            clearTimeout(longTimer);
            document.getElementById("failureState").style.display = "block";
            document.getElementById("failureMessage").textContent =
              "Validation is taking too long. Please retry the individual steps below.";
          } else {
            setTimeout(poll, 2000);
          }
        })
        .catch(function () {
          if (attempts >= maxAttempts) {
            clearTimeout(longTimer);
            document.getElementById("failureState").style.display = "block";
            document.getElementById("failureMessage").textContent =
              "We couldn't reach the validation service. Please try again.";
          } else {
            setTimeout(poll, 2000);
          }
        });
    }
    function rowsToObj(stages) {
      var o = {};
      (stages || []).forEach(function (s) { o[s.stage] = s; });
      return o;
    }
    poll();
  }

  /* ---- Step 3/4: review form ---- */
  function initReviewForm(opts) {
    var form = document.querySelector("[data-review-form]");
    if (!form) return;
    var saveBtn = form.querySelector("[data-action-save]");
    var submitBtn = form.querySelector("[data-action-submit]");

    // Mark fields as human-confirmed when edited.
    form.querySelectorAll("[data-confirm-for]").forEach(function (input) {
      input.addEventListener("change", function () {
        var box = form.querySelector("[data-confirm='" + input.getAttribute("data-confirm-for") + "']");
        if (box) box.checked = true;
      });
    });

    // Dispatch mode toggles the relevant fields.
    var mode = form.querySelector("[name='dispatch_mode']");
    var courier = form.querySelector("[data-dispatch-courier]");
    var cmr = form.querySelector("[data-dispatch-cmr]");
    function toggleDispatch() {
      var v = mode.value;
      if (courier) courier.style.display = v === "courier" ? "" : "none";
      if (cmr) cmr.style.display = v === "cmr" ? "" : "none";
    }
    if (mode) mode.addEventListener("change", toggleDispatch);
    toggleDispatch();

    // Enable submit only when every missing critical clause is acknowledged and
    // dispatch details are complete.
    function dispatchComplete() {
      var v = mode ? mode.value : "";
      if (!v) return false;
      if (v === "courier") {
        var cn = form.querySelector("[name='courier_name']");
        var tn = form.querySelector("[name='tracking_number']");
        return cn && cn.value.trim() && tn && tn.value.trim();
      }
      if (v === "cmr") {
        var dn = form.querySelector("[name='cmr_deliverer_name']");
        var dm = form.querySelector("[name='cmr_deliverer_mobile']");
        return dn && dn.value.trim() && dm && dm.value.trim();
      }
      return false;
    }
    function updateSubmitState() {
      var acks = form.querySelectorAll(".ack-clause");
      var allAcked = acks.length > 0 && Array.prototype.every.call(acks, function (c) { return c.checked; });
      var noAcks = acks.length === 0;
      var ready = (noAcks || allAcked) && dispatchComplete();
      if (submitBtn) submitBtn.disabled = !ready;
    }
    form.querySelectorAll(".ack-clause").forEach(function (c) { c.addEventListener("change", updateSubmitState); });
    form.querySelectorAll("[name='dispatch_mode'],[name='courier_name'],[name='tracking_number'],[name='cmr_deliverer_name'],[name='cmr_deliverer_mobile']").forEach(function (el) {
      el.addEventListener("change", updateSubmitState);
      el.addEventListener("input", updateSubmitState);
    });
    updateSubmitState();

    if (saveBtn) saveBtn.addEventListener("click", function (e) {
      e.preventDefault();
      form.action = opts.saveUrl;
      form.submit();
    });
    if (submitBtn) submitBtn.addEventListener("click", function (e) {
      e.preventDefault();
      form.action = opts.submitUrl;
      form.submit();
    });

    var discard = form.querySelector("[data-discard-draft]");
    var discardForm = document.querySelector("[data-discard-form]");
    if (discard && discardForm) discard.addEventListener("click", function () {
      if (window.confirm("Discard this draft permanently? This cannot be undone.")) discardForm.submit();
    });
  }

  /* ---- Saved drafts page ---- */
  function initDraftsPage() {
    document.querySelectorAll("[data-confirm-discard]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (window.confirm("Discard this draft permanently? This removes the draft and its uploaded document.")) {
          var f = document.createElement("form");
          f.method = "post";
          f.action = btn.getAttribute("data-discard-url");
          var csrfInput = document.createElement("input");
          csrfInput.type = "hidden"; csrfInput.name = "csrf_token"; csrfInput.value = csrf();
          f.appendChild(csrfInput);
          document.body.appendChild(f);
          f.submit();
        }
      });
    });
  }

  window.initUploadForm = initUploadForm;
  window.initProgress = initProgress;
  window.initReviewForm = initReviewForm;
  window.initDraftsPage = initDraftsPage;
})();
