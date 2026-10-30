(function (window, document) {
  "use strict";
  var names = ["classic", "bento", "swiss", "cyber"];
  var root = document.documentElement;

  function initialize() {
    var boot = window.CloudhostThemeBoot;
    var i18n = window.CloudhostI18n;
    var host = document.querySelector("[data-theme-control]");
    var notice = document.querySelector("[data-theme-status]");
    if (!boot || !i18n || !host || !notice) { return; }
    notice.removeAttribute("data-i18n");
    var toggle = host.querySelector("button");
    var current = boot.theme;
    var saved = boot.saved;
    var issue = boot.issue;
    var applicationFailed = false;
    var outcome = null;
    var open = false;
    var warned = {};
    var layoutEpoch = 0;
    var scrollAnchoring = root.style.overflowAnchor;
    var panel = document.createElement("div");
    panel.id = "theme-panel";
    panel.className = "theme-panel";
    panel.setAttribute("role", "radiogroup");
    panel.hidden = true;
    var options = names.map(function (name) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "theme-option";
      button.setAttribute("role", "radio");
      button.dataset.themeChoice = name;
      var preview = document.createElement("span");
      preview.className = "theme-preview preview-" + name;
      preview.setAttribute("aria-hidden", "true");
      for (var n = 0; n < 4; n += 1) { preview.appendChild(document.createElement("i")); }
      var label = document.createElement("span");
      label.className = "theme-name";
      var check = document.createElement("span");
      check.className = "theme-check";
      check.setAttribute("aria-hidden", "true");
      button.append(preview, label, check);
      panel.appendChild(button);
      button.addEventListener("click", function () { choose(name); });
      return button;
    });
    host.appendChild(panel);

    function t(key, params) { return i18n.t("theme." + key, params); }
    function available(name) {
      return getComputedStyle(root).getPropertyValue("--theme-ready").trim() === name;
    }
    function report(kind) {
      if (warned[kind]) { return; }
      warned[kind] = true;
      if (window.console) { console.warn("Cloudhost theme preference: " + kind); }
    }
    function render() {
      toggle.textContent = t("label");
      toggle.setAttribute("aria-label", t("current", { theme: t(current) }));
      toggle.setAttribute("aria-expanded", String(open));
      panel.setAttribute("aria-label", t("label"));
      options.forEach(function (button, index) {
        var selected = names[index] === current;
        button.setAttribute("aria-checked", String(selected));
        button.tabIndex = selected ? 0 : -1;
        button.querySelector(".theme-name").textContent = t(names[index]);
        button.querySelector(".theme-check").textContent = selected ? "✓" : "";
      });
      var key = issue || (applicationFailed ? "applyFailed" : outcome);
      notice.dataset.themeIssue = issue || "";
      notice.hidden = !key;
      notice.classList.toggle("theme-status-success", !issue && !applicationFailed);
      if (key) {
        var text = t(key, { theme: t(current) });
        if (issue && applicationFailed) { text += " " + t("applyFailed"); }
        if (notice.textContent !== text) { notice.textContent = text; }
      }
    }
    function close(restore) {
      open = false;
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (restore) { toggle.focus({ preventScroll: true }); }
    }
    function positionPanel() {
      if (!open) { return; }
      var buttonBox = toggle.getBoundingClientRect();
      var panelBox = panel.getBoundingClientRect();
      panel.style.right = "auto";
      panel.style.left = Math.max(16, Math.min(buttonBox.left, window.innerWidth - panelBox.width - 16)) + "px";
      panel.style.top = Math.max(8, Math.min(buttonBox.bottom + 8, window.innerHeight - panelBox.height - 8)) + "px";
    }
    function anchor() {
      if (document.body.classList.contains("workspace-open")) { return null; }
      var overview = document.getElementById("view-overview");
      var remote = document.getElementById("view-remote");
      var view = overview && !overview.hidden ? overview : remote && !remote.hidden ? remote : null;
      if (!view) { return null; }
      var edge = document.querySelector(".topbar").getBoundingClientRect().bottom;
      var candidates = view.querySelectorAll(view === overview ?
        ".product-hero,.link-card,.panel-head,.metric,.submetrics" :
        ".panel-head,#peanut-controls > p,#peanut-primary-actions,#peanut-address-entry,.remote-diagnostics,.remote-ssh-row");
      var services = document.getElementById("overview-services");
      var all = Array.from(candidates);
      if (services && view === overview) { all.push(services); }
      var node = all.find(function (item) { return item.getBoundingClientRect().bottom > edge; });
      return node ? { node: node, top: node.getBoundingClientRect().top, scroll: window.scrollY } : null;
    }
    function restoreAnchor(point) {
      if (!point) { return; }
      var target = window.scrollY + point.node.getBoundingClientRect().top - point.top;
      var maximum = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      window.scrollTo({ top: Math.min(maximum, Math.max(0, target)), behavior: "instant" });
    }
    function choose(name) {
      if (name === current && saved) { close(true); return; }
      var point = anchor();
      var previous = current;
      var epoch = ++layoutEpoch;
      root.style.overflowAnchor = "none";
      root.setAttribute("data-theme-changing", "");
      function finishLayout() {
        restoreAnchor(point);
        requestAnimationFrame(function () {
          if (epoch !== layoutEpoch) { return; }
          restoreAnchor(point);
          requestAnimationFrame(function () {
            if (epoch !== layoutEpoch) { return; }
            root.style.overflowAnchor = scrollAnchoring;
            root.removeAttribute("data-theme-changing");
          });
        });
      }
      try {
        root.setAttribute("data-theme", name);
        root.style.colorScheme = name === "swiss" ? "light" : "dark";
        if (!available(name)) { throw new Error("theme_styles_unavailable"); }
      } catch (error) {
        root.setAttribute("data-theme", previous);
        root.style.colorScheme = previous === "swiss" ? "light" : "dark";
        applicationFailed = true;
        report("apply_failed");
        render();
        close(true);
        finishLayout();
        return;
      }
      current = name;
      window.dispatchEvent(new CustomEvent("cloudhost:themechange"));
      applicationFailed = false;
      saved = false;
      issue = null;
      outcome = "switched";
      try {
        var storage = window.localStorage;
        if (!storage) { throw new Error("storage_unavailable"); }
        storage.setItem("cloudhost.theme", name);
        saved = true;
      } catch (error) {
        issue = "write";
        report(storage ? "setItem" : "storage_object");
      }
      render();
      close(true);
      finishLayout();
    }
    toggle.addEventListener("click", function () {
      if (open) { close(true); return; }
      open = true;
      panel.hidden = false;
      render();
      positionPanel();
      options[names.indexOf(current)].focus({ preventScroll: true });
    });
    panel.addEventListener("keydown", function (event) {
      var index = options.indexOf(document.activeElement);
      var next = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { next = (index + 1) % 4; }
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { next = (index + 3) % 4; }
      else if (event.key === "Home") { next = 0; }
      else if (event.key === "End") { next = 3; }
      else { return; }
      event.preventDefault();
      options.forEach(function (button, i) { button.tabIndex = i === next ? 0 : -1; });
      options[next].focus({ preventScroll: true });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && open) { event.preventDefault(); close(true); }
    });
    document.addEventListener("click", function (event) {
      if (open && !host.contains(event.target)) { close(true); }
    });
    window.addEventListener("cloudhost:languagechange", render);
    window.addEventListener("resize", positionPanel);
    if (!available(current)) {
      current = "classic";
      saved = false;
      root.setAttribute("data-theme", current);
      root.style.colorScheme = "dark";
      issue = "applyFailed";
    }
    toggle.hidden = false;
    render();
    if (issue) { report(boot.failure || issue); }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else { initialize(); }
})(window, document);
