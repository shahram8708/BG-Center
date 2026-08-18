/* Dedicated AI Assistant page behavior with dynamic page-context integration. */
(function () {
  "use strict";

  function csrf() {
    var m = document.querySelector("meta[name='csrf-token']");
    return m ? m.getAttribute("content") : "";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function appendMsg(thread, role, text, sources, link) {
    var placeholder = document.querySelector("[data-assistant-empty-placeholder]");
    if (placeholder) placeholder.remove();

    var wrap = document.createElement("div");
    wrap.className = "assistant-msg " + (role === "user" ? "assistant-user" : "assistant-bot") + " mb-3";
    var html = '<div class="small fw-semibold text-muted mb-1">' + (role === "user" ? "You" : "Assistant") + '</div>';
    html += '<div class="small">' + escapeHtml(text) + '</div>';
    if (sources && sources.length) {
      html += '<div class="small text-muted mt-1"><i class="bi bi-journal-text me-1"></i>Sources: ' + sources.map(escapeHtml).join(", ") + '</div>';
    }
    if (link) {
      html += '<div class="mt-1"><a class="small" href="' + escapeHtml(link) + '" target="_blank">Related screen →</a></div>';
    }
    wrap.innerHTML = html;
    thread.appendChild(wrap);
    thread.scrollTop = thread.scrollHeight;
  }

  function resolveActiveContextUrl() {
    // 1. Initial hidden field
    var initInput = document.querySelector("[data-initial-context-url]");
    if (initInput && initInput.value && initInput.value !== "/assistant/" && initInput.value !== "/assistant") {
      return initInput.value;
    }

    // 2. Query param
    var params = new URLSearchParams(window.location.search);
    var fromQuery = params.get("context_url") || params.get("source_url") || params.get("source");
    if (fromQuery && fromQuery !== "/assistant/" && fromQuery !== "/assistant") {
      return fromQuery;
    }
    if (params.get("bg_id")) {
      return "/bg-multi-stage-approval/" + encodeURIComponent(params.get("bg_id"));
    }

    // 3. Session storage from last visited application screen
    try {
      var storedUrl = sessionStorage.getItem("bgcc_last_page_url");
      if (storedUrl && !storedUrl.startsWith("/assistant") && !storedUrl.startsWith("/auth/")) {
        return storedUrl;
      }
    } catch (e) {}

    // 4. Referrer
    if (document.referrer) {
      try {
        var refUrl = new URL(document.referrer);
        if (refUrl.origin === window.location.origin && !refUrl.pathname.startsWith("/assistant") && !refUrl.pathname.startsWith("/auth/")) {
          return refUrl.pathname + refUrl.search;
        }
      } catch (e) {}
    }

    return "/dashboard/";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var thread = document.querySelector("[data-assistant-page-thread]");
    var form = document.querySelector("[data-assistant-page-form]");
    var question = document.querySelector("[data-assistant-page-question]");
    var sendBtn = document.querySelector("[data-assistant-send-btn]");
    var contextLabel = document.querySelector("[data-assistant-context-label]");
    var clearBtn = document.querySelector("[data-assistant-clear-btn]");

    if (!form || !thread) return;

    var activeContextUrl = resolveActiveContextUrl();
    var activeContextTitle = "Dashboard";

    // Inspect and display context label
    if (contextLabel) {
      fetch("/assistant/context?url=" + encodeURIComponent(activeContextUrl), {
        headers: { "Accept": "application/json" }
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          activeContextTitle = data.title || "Live Screen";
          contextLabel.textContent = "Context: " + (data.title || data.route || "Dashboard");
          contextLabel.title = "Active page context: " + (data.route || activeContextUrl);
        })
        .catch(function () {
          contextLabel.textContent = "Context: Active Workflow (" + activeContextUrl + ")";
        });
    }

    // Quick prompt chips
    document.querySelectorAll("[data-prompt-chip]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var promptText = chip.getAttribute("data-prompt-chip");
        if (promptText && question) {
          question.value = promptText;
          form.dispatchEvent(new Event("submit", { cancelable: true }));
        }
      });
    });

    // Clear Chat
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        if (!confirm("Are you sure you want to clear this assistant conversation history?")) return;
        var fd = new FormData();
        fd.append("csrf_token", csrf());
        fetch("/assistant/clear", { method: "POST", body: fd, headers: { "X-CSRFToken": csrf() } })
          .then(function () {
            thread.innerHTML = '<div class="text-center text-muted my-4 py-3" data-assistant-empty-placeholder>' +
              '<i class="bi bi-chat-dots fs-1 d-block mb-2 text-primary opacity-50"></i>' +
              '<p class="mb-1 fw-semibold text-dark">Conversation cleared</p>' +
              '<p class="small mb-0">Ask any question to begin a fresh conversation.</p>' +
              '</div>';
          });
      });
    }

    // Form Submit
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var q = question.value.trim();
      if (!q) return;

      appendMsg(thread, "user", q, null, null);
      question.value = "";
      if (sendBtn) sendBtn.disabled = true;

      var typing = document.createElement("div");
      typing.className = "assistant-msg assistant-bot small text-muted mb-3";
      typing.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" style="width:0.85rem; height:0.85rem;"></span>Thinking…';
      thread.appendChild(typing);
      thread.scrollTop = thread.scrollHeight;

      var payload = {
        question: q,
        page_url: activeContextUrl,
        page_title: activeContextTitle,
        client_context: {
          detected_url: activeContextUrl,
          resolved_title: activeContextTitle
        }
      };

      fetch("/assistant/ask", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf()
        },
        body: JSON.stringify(payload)
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          typing.remove();
          if (sendBtn) sendBtn.disabled = false;
          if (data.error) {
            appendMsg(thread, "assistant", data.error, null, null);
          } else {
            appendMsg(thread, "assistant", data.answer, data.sources, data.link);
          }
        })
        .catch(function () {
          typing.remove();
          if (sendBtn) sendBtn.disabled = false;
          appendMsg(thread, "assistant", "I couldn't retrieve an answer just now. Please try again.", null, null);
        });
    });

    thread.scrollTop = thread.scrollHeight;
  });
})();
