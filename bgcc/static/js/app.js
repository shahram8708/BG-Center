/* BG Command Centre - general front-end behaviour. */
(function () {
  "use strict";

  var READ_ONLY_PREFIXES = [
    "/dashboard", "/bg-status", "/bg-bank-tracker", "/bg/", "/notifications", "/admin/audit-log"
  ];

  document.addEventListener("DOMContentLoaded", function () {
    trackPageContext();
    initSidebar();
    initRoleSwitcher();
    initPasswordMeters();
    initSubmitGates();
    registerServiceWorker();
    initInstallPrompt();
    initOfflineBehavior();
    initPushSubscription();
  });

  /* Sidebar drawer (below tablet breakpoint). */
  function initSidebar() {
    var toggle = document.querySelector("[data-sidebar-toggle]");
    var sidebar = document.getElementById("appSidebar");
    var backdrop = document.querySelector("[data-sidebar-backdrop]");
    if (!toggle || !sidebar) return;

    function close() {
      sidebar.classList.remove("open");
      if (backdrop) backdrop.classList.remove("show");
    }
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      if (backdrop) backdrop.classList.toggle("show");
    });
    if (backdrop) backdrop.addEventListener("click", close);
  }

  /* Active-role switcher: POST the chosen role to the server-side endpoint. */
  function initRoleSwitcher() {
    document.querySelectorAll("[data-role-switch]").forEach(function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        var role = link.getAttribute("data-role-switch");
        var csrf = document.querySelector("meta[name='csrf-token']");
        var form = document.createElement("form");
        form.method = "POST";
        form.action = "/auth/role-switch";
        form.style.display = "none";
        var csrfInput = document.createElement("input");
        csrfInput.type = "hidden";
        csrfInput.name = "csrf_token";
        csrfInput.value = csrf ? csrf.getAttribute("content") : "";
        var roleInput = document.createElement("input");
        roleInput.type = "hidden";
        roleInput.name = "role";
        roleInput.value = role;
        form.appendChild(csrfInput);
        form.appendChild(roleInput);
        document.body.appendChild(form);
        form.submit();
      });
    });
  }

  /* Live password complexity meter with real-time feedback. */
  function initPasswordMeters() {
    document.querySelectorAll("input[data-password-meter]").forEach(function (input) {
      var meterSel = input.getAttribute("data-password-meter");
      if (!meterSel) return;
      var meter = meterSel.startsWith("#") ? document.querySelector(meterSel) : (document.getElementById(meterSel) || document.querySelector(meterSel));
      if (!meter) return;
      var spans = meter.querySelectorAll(".password-meter-bars span, span");
      var strengthText = meter.querySelector(".strength-text");
      var rules = {
        length: meter.querySelector("[data-rule='length']"),
        uppercase: meter.querySelector("[data-rule='uppercase']"),
        lowercase: meter.querySelector("[data-rule='lowercase']"),
        number: meter.querySelector("[data-rule='number']"),
        special: meter.querySelector("[data-rule='special']")
      };

      function update() {
        var v = input.value || "";
        var checks = {
          length: v.length >= 8,
          uppercase: /[A-Z]/.test(v),
          lowercase: /[a-z]/.test(v),
          number: /\d/.test(v),
          special: /[^A-Za-z0-9]/.test(v)
        };
        var score = [checks.length, checks.uppercase, checks.lowercase, checks.number, checks.special].filter(Boolean).length;

        var strength = "Weak";
        var strengthClass = "text-danger";
        var barClass = "bar-weak";

        if (score === 5) {
          strength = "Strong";
          strengthClass = "text-success";
          barClass = "bar-strong";
        } else if (score >= 3) {
          strength = "Medium";
          strengthClass = "text-warning";
          barClass = "bar-medium";
        }

        if (strengthText) {
          strengthText.textContent = v ? strength : "Weak";
          strengthText.className = "strength-text fw-bold " + (v ? strengthClass : "text-muted");
        }

        spans.forEach(function (span, i) {
          span.classList.toggle("on", i < score && v.length > 0);
          span.classList.remove("bar-weak", "bar-medium", "bar-strong");
          if (i < score && v.length > 0) {
            span.classList.add(barClass);
          }
        });

        Object.keys(rules).forEach(function (key) {
          var el = rules[key];
          if (!el) return;
          var met = checks[key];
          if (met && v.length > 0) {
            el.classList.remove("text-muted");
            el.classList.add("text-success", "fw-medium");
            var icon = el.querySelector("i");
            if (icon) icon.className = "bi bi-check-circle-fill text-success me-1";
          } else {
            el.classList.remove("text-success", "fw-medium");
            el.classList.add("text-muted");
            var icon = el.querySelector("i");
            if (icon) icon.className = "bi bi-circle text-muted me-1";
          }
        });
      }

      input.addEventListener("input", update);
      update();
    });
  }

  /* Disable a form's submit button until all marked required fields have a value. */
  function initSubmitGates() {
    document.querySelectorAll("form[data-submit-gate]").forEach(function (form) {
      var submit = form.querySelector("[type='submit']");
      if (!submit) return;
      var required = Array.prototype.slice.call(form.querySelectorAll("[data-required]"));
      function update() {
        var complete = required.every(function (el) {
          var v = el.value || "";
          return v.trim().length > 0;
        });
        submit.disabled = !complete;
        if (!complete) {
          submit.title = "Fill in all required fields to continue";
          submit.setAttribute("aria-describedby", submit.id + "-hint");
        } else {
          submit.removeAttribute("title");
        }
      }
      required.forEach(function (el) {
        el.addEventListener("input", update);
        el.addEventListener("change", update);
      });
      update();
    });
  }

  /* PWA: register the service worker and handle updates. */
  function registerServiceWorker() {
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", function () {
        navigator.serviceWorker
          .register("/sw.js")
          .then(function (registration) {
            registration.update().catch(function () {});

            if (registration.waiting) {
              registration.waiting.postMessage({ type: "SKIP_WAITING" });
            }

            registration.addEventListener("updatefound", function () {
              var newWorker = registration.installing;
              if (!newWorker) return;
              newWorker.addEventListener("statechange", function () {
                if (newWorker.state === "installed" && navigator.serviceWorker.controller) {
                  newWorker.postMessage({ type: "SKIP_WAITING" });
                }
              });
            });
          })
          .catch(function (err) {
            console.warn("ServiceWorker registration error:", err);
          });
      });
    }
  }

  /* ---- Offline behavior: honest banner on read-only pages, disable workflow submits. */
  function isReadOnlyPath() {
    return READ_ONLY_PREFIXES.some(function (p) { return location.pathname === p || location.pathname.indexOf(p) === 0; });
  }

  function showOfflineBanner() {
    var banner = document.createElement("div");
    banner.className = "offline-banner";
    banner.setAttribute("role", "status");
    banner.innerHTML = '<i class="bi bi-wifi-off me-1"></i> You are offline - showing cached data. This may be out of date.';
    document.body.appendChild(banner);
  }

  function initOfflineBehavior() {
    function apply() {
      var offline = !navigator.onLine;
      // Every POST form is a workflow/state-changing action: it must never
      // appear to succeed offline, and its submit is disabled with a message.
      document.querySelectorAll("form[method='post']").forEach(function (form) {
        var submit = form.querySelector("[type='submit']");
        if (submit) submit.disabled = offline;
      });
      document.querySelectorAll("[data-offline-msg]").forEach(function (el) {
        el.style.display = offline ? "block" : "none";
      });
      var existing = document.querySelector(".offline-banner");
      if (offline && isReadOnlyPath() && !existing) {
        showOfflineBanner();
      } else if (!offline && existing) {
        existing.remove();
      }
    }
    window.addEventListener("online", apply);
    window.addEventListener("offline", apply);
    apply();
  }

  /* ---- Push subscription: robust permission request and registration. */
  function initPushSubscription() {
    var enableBtn = document.querySelector("[data-enable-push]");
    if (!enableBtn) return;

    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      enableBtn.disabled = true;
      enableBtn.innerHTML = '<i class="bi bi-bell-slash me-1"></i> Push not supported on this browser';
      return;
    }

    if (Notification.permission === "denied") {
      enableBtn.disabled = true;
      enableBtn.innerHTML = '<i class="bi bi-bell-slash me-1"></i> Notifications blocked in browser settings';
      return;
    }

    if (Notification.permission === "granted") {
      navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.getSubscription();
      }).then(function (existingSub) {
        if (existingSub) {
          enableBtn.classList.remove("btn-outline-brand");
          enableBtn.classList.add("btn-success");
          enableBtn.innerHTML = '<i class="bi bi-check-circle me-1"></i> Browser push is enabled';
          var pushCheckbox = document.querySelector("[name='notify_push']");
          if (pushCheckbox) pushCheckbox.checked = true;
        }
      }).catch(function () {});
    }

    enableBtn.addEventListener("click", function () {
      if (!navigator.onLine) {
        alert("You are currently offline. Please reconnect to the internet to enable push notifications.");
        return;
      }

      enableBtn.disabled = true;
      enableBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Requesting permission…';

      Notification.requestPermission().then(function (permission) {
        if (permission === "denied") {
          enableBtn.disabled = true;
          enableBtn.classList.remove("btn-outline-brand", "btn-success");
          enableBtn.classList.add("btn-danger");
          enableBtn.innerHTML = '<i class="bi bi-x-circle me-1"></i> Notifications blocked in browser settings';
          return;
        }
        if (permission !== "granted") {
          enableBtn.disabled = false;
          enableBtn.innerHTML = '<i class="bi bi-bell me-1"></i> Enable browser push on this device';
          return;
        }

        enableBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Subscribing…';

        function getVapidKey() {
          var vapidMeta = document.querySelector("meta[name='vapid-public-key']");
          var key = vapidMeta ? vapidMeta.getAttribute("content") : "";
          if (key && key.trim()) {
            return Promise.resolve(key.trim());
          }
          return fetch("/api/push/vapid-public-key")
            .then(function (r) { return r.json(); })
            .then(function (d) { return d.vapid_public_key || ""; });
        }

        getVapidKey().then(function (appKey) {
          if (!appKey) {
            throw new Error("VAPID public key is missing or not configured on server.");
          }
          var applicationServerKey = urlBase64ToUint8Array(appKey);
          return navigator.serviceWorker.ready.then(function (reg) {
            return reg.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: applicationServerKey
            });
          });
        }).then(function (subscription) {
          return fetch("/api/push/subscribe", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrfToken()
            },
            body: JSON.stringify({ subscription: subscription.toJSON() })
          });
        }).then(function (res) {
          if (!res.ok) {
            return res.json().then(function (data) {
              throw new Error(data.error || "Server rejected push subscription.");
            });
          }
          return res.json();
        }).then(function () {
          enableBtn.disabled = false;
          enableBtn.classList.remove("btn-outline-brand", "btn-danger", "btn-outline-danger");
          enableBtn.classList.add("btn-success");
          enableBtn.innerHTML = '<i class="bi bi-check-circle me-1"></i> Push notifications enabled';
          var pushCheckbox = document.querySelector("[name='notify_push']");
          if (pushCheckbox) pushCheckbox.checked = true;
        }).catch(function (err) {
          console.error("Push subscription failed:", err);
          enableBtn.disabled = false;
          enableBtn.classList.remove("btn-outline-brand", "btn-success");
          enableBtn.classList.add("btn-outline-danger");
          enableBtn.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i> Could not enable push. Try again';
        });
      });
    });
  }

  function urlBase64ToUint8Array(base64String) {
    if (!base64String) return new Uint8Array(0);
    var str = base64String.trim();
    if (/^[0-9a-fA-F]{130}$/.test(str)) {
      var bytes = new Uint8Array(65);
      for (var i = 0; i < 65; i++) {
        bytes[i] = parseInt(str.substr(i * 2, 2), 16);
      }
      return bytes;
    }
    var padding = "=".repeat((4 - (str.length % 4)) % 4);
    var base64 = (str + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(base64);
    var output = new Uint8Array(raw.length);
    for (var j = 0; j < raw.length; ++j) {
      output[j] = raw.charCodeAt(j);
    }
    return output;
  }

  function trackPageContext() {
    var p = window.location.pathname;
    if (!p.startsWith("/assistant") && !p.startsWith("/auth/") && p !== "/sw.js") {
      try {
        sessionStorage.setItem("bgcc_last_page_url", window.location.pathname + window.location.search);
        sessionStorage.setItem("bgcc_last_page_title", document.title || "");
      } catch (e) {}
    }
  }

  function csrfToken() {
    var m = document.querySelector("meta[name='csrf-token']");
    return m ? m.getAttribute("content") : "";
  }

  /* ---- Deliberate install-flow polish (custom UI, well-timed). */
  function initInstallPrompt() {
    var deferred = null;
    var engaged = false;
    var installBtn = document.querySelector("[data-install-prompt]");

    function show() {
      if (installBtn) installBtn.classList.remove("d-none");
    }
    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      deferred = e;
      // Only surface after genuine engagement (some navigation + time on page).
      setTimeout(function () {
        if (engaged || sessionStorage.getItem("bgcc_install_shown")) show();
      }, 8000);
    });
    window.addEventListener("scroll", function () { engaged = true; }, { passive: true });
    if (installBtn) {
      installBtn.addEventListener("click", function () {
        if (deferred) {
          deferred.prompt();
          deferred.userChoice.then(function () {
            sessionStorage.setItem("bgcc_install_shown", "1");
          });
        }
      });
    }
    window.addEventListener("appinstalled", function () {
      if (installBtn) installBtn.classList.add("d-none");
      sessionStorage.setItem("bgcc_install_shown", "1");
    });
  }
})();
