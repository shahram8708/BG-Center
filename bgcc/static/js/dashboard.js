/* Dashboard charts (Chart.js). */
(function () {
  "use strict";

  var metric = "value";

  function toLabelsMap(obj) {
    var labels = [], values = [];
    Object.keys(obj || {}).forEach(function (k) {
      labels.push(k);
      values.push(obj[k][metric] || 0);
    });
    return { labels: labels, values: values };
  }

  function barChart(ctxId, data) {
    var el = document.getElementById(ctxId);
    if (!el) return null;
    var chart = new Chart(el, {
      type: "bar",
      data: { labels: data.labels, datasets: [{ data: data.values, backgroundColor: "#7C3AED" }] },
      options: { plugins: { legend: { display: false } }, responsive: true, maintainAspectRatio: false }
    });
    return chart;
  }

  var charts = { bank: null, vendor: null, bu: null };

  function renderAll() {
    var bank = toLabelsMap(window.__dashboard.by_bank);
    var vendor = toLabelsMap(window.__dashboard.by_vendor);
    var bu = toLabelsMap(window.__dashboard.by_business_unit);
    if (charts.bank) { charts.bank.data.datasets[0].data = bank.values; charts.bank.update(); }
    else charts.bank = barChart("chartBank", bank);
    if (charts.vendor) { charts.vendor.data.datasets[0].data = vendor.values; charts.vendor.update(); }
    else charts.vendor = barChart("chartVendor", vendor);
    if (charts.bu) { charts.bu.data.datasets[0].data = bu.values; charts.bu.update(); }
    else charts.bu = barChart("chartBU", bu);
  }

  function renderMix() {
    var el = document.getElementById("chartMix");
    if (!el) return;
    var labels = [], values = [];
    Object.keys(window.__dashboard.by_bg_type || {}).forEach(function (k) {
      labels.push(k);
      values.push(window.__dashboard.by_bg_type[k].count);
    });
    new Chart(el, {
      type: "doughnut",
      data: { labels: labels, datasets: [{ data: values, backgroundColor: ["#7C3AED", "#C026D3", "#f59e0b", "#10b981", "#3b82f6"] }] },
      options: { responsive: true, maintainAspectRatio: false }
    });
  }

  window.initDashboard = function (data) {
    window.__dashboard = data;
    renderMix();
    renderAll();
    document.querySelectorAll("[data-chart-metric]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        metric = btn.getAttribute("data-chart-metric");
        document.querySelectorAll("[data-chart-metric]").forEach(function (b) {
          b.classList.toggle("active", b === btn);
          b.classList.toggle("btn-brand", b === btn);
          b.classList.toggle("btn-outline-brand", b !== btn);
        });
        renderAll();
      });
    });
  };
})();
