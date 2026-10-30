/* 门户看板逻辑：
 * - 1s 轮询 /api/status（对应 A1），服务状态与重启见文档 9.2
 * - 原生 hash 导航；文件与站点保留各自的 iframe，终端离开时卸载
 */
(function () {
  "use strict";

  var CFG = window.CLOUDHOST || {};
  var INTERVAL = Math.min(5000, Math.max(500, parseInt(CFG.interval, 10) || 1000));
  var csrf = CFG.csrf || "";
  var pollTimer = null;
  var failStreak = 0;
  var servicesPending = false;
  var pendingServiceName = "";
  var latestServices = null;
  var serviceHint = null;
  var embedHint = null;
  var latestRemoteStatus = null;
  var remoteStatusRequest = null;
  var remoteStatusHintKey = "remote.noLiveProbe";
  var statusHintKey = "dashboard.connecting";
  var lastUptimeSeconds = null;
  var currentView = "overview";
  var scrollPositions = {};
  var frames = {};
  var terminalThemeObserver = null;
  var fileThemeObserver = null;
  var probeController = null;
  var ttydOn = CFG.ttyd === "true" || CFG.ttyd === true;
  var I18N = window.CloudhostI18n;

  var VIEWS = {
    overview: { titleKey: "dashboard.overview", path: "" },
    remote: { titleKey: "remote.title", path: "" },
    ssh: { titleKey: "dashboard.ssh", path: "" },
    files: {
      titleKey: "dashboard.files",
      path: "/files/",
      hintKey: "embed.filesUnavailable"
    },
    site: {
      titleKey: "dashboard.site",
      path: "/site/",
      hintKey: "embed.siteUnavailable"
    },
    term: {
      titleKey: "dashboard.terminal",
      path: "/term/",
      hintKey: "embed.terminalUnavailable"
    }
  };

  var $ = function (id) { return document.getElementById(id); };
  var t = function (key, params) { return I18N.t(key, params); };

  function setText(id, value) {
    var node = $(id);
    if (node) { node.textContent = value; }
  }

  function setBar(id, percent) {
    var node = $(id);
    if (!node) { return; }
    var value = Math.max(0, Math.min(100, Number(percent) || 0));
    node.style.width = value + "%";
    node.className = value >= 90 ? "crit" : value >= 75 ? "warn" : "";
  }

  function fmt1(value) {
    if (value === null || value === undefined || value === "") { return "—"; }
    var num = Number(value);
    return isNaN(num) ? "—" : num.toFixed(1);
  }

  function fmtUptime(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) { return "—"; }
    var s = Math.max(0, Number(seconds) || 0);
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) { return t("dashboard.days", { count: d }) + " " + t("dashboard.hours", { count: h }); }
    if (h > 0) { return t("dashboard.hours", { count: h }) + " " + t("dashboard.minutes", { count: m }); }
    return t("dashboard.minutes", { count: m }) + " " +
      t("dashboard.seconds", { count: Math.floor(s % 60) });
  }

  function fmtRate(kbps) {
    if (kbps === null || kbps === undefined || !Number.isFinite(Number(kbps))) { return "—"; }
    var v = Number(kbps);
    if (v >= 1000) { return (v / 1000).toFixed(2) + " Mbps"; }
    return v.toFixed(1) + " kbps";
  }

  function setDot(state) {
    var dot = $("status-dot");
    if (!dot) { return; }
    dot.className = "dot " + state;
  }

  function handleUnauthorized() {
    releaseTerminal();
    window.location.replace("/login?redirect=" + encodeURIComponent(window.location.pathname + window.location.hash));
  }

  function apiFetch(url, options) {
    var opts = options || {};
    opts.credentials = "same-origin";
    opts.headers = opts.headers || {};
    if (opts.method && opts.method !== "GET" && csrf) {
      opts.headers["X-CSRF-Token"] = csrf;
    }
    return fetch(url, opts).then(function (response) {
      if (response.status === 401) {
        handleUnauthorized();
        throw new Error("unauthorized");
      }
      return response;
    });
  }

  function renderStatus(payload) {
    var cpu = payload.cpu || {};
    var mem = payload.mem || {};
    var temp = payload.temp || {};
    var disk = payload.disk || {};
    var net = payload.net || {};

    setText("cpu-percent", fmt1(cpu.percent));
    setText("cpu-load", [fmt1(cpu.load1), fmt1(cpu.load5), fmt1(cpu.load15)].join(" / "));
    setBar("cpu-bar", cpu.percent);

    setText("mem-percent", fmt1(mem.percent));
    setText("mem-used", fmt1(mem.used_mb));
    setText("mem-total", fmt1(mem.total_mb));
    setBar("mem-bar", mem.percent);

    setText("temp-value", temp.cpu_c === null || temp.cpu_c === undefined ? "—" : fmt1(temp.cpu_c));
    setBar("temp-bar", temp.cpu_c === null || temp.cpu_c === undefined ? 0 : (Number(temp.cpu_c) / 100) * 100);

    setText("disk-percent", fmt1(disk.root_percent));
    setText("disk-used", fmt1(disk.root_used_gb));
    setText("disk-total", fmt1(disk.root_total_gb));
    setBar("disk-bar", disk.root_percent);

    setText("net-iface", net.iface || "—");
    setText("net-rx", fmtRate(net.rx_kbps));
    setText("net-tx", fmtRate(net.tx_kbps));
    lastUptimeSeconds = payload.uptime_sec;
    setText("uptime", fmtUptime(lastUptimeSeconds));

    var stamp = payload.time ? payload.time.replace("T", " ").slice(0, 19) : "—";
    setText("status-time", stamp);
  }

  function remoteLabel(value) {
    var allowed = [
      "none", "tailscale", "zerotier", "peanut", "unknown", "unverified", "verified", "stale",
      "connected", "connecting", "disconnected", "degraded", "failed",
      "endpoint_ready", "off", "configuring", "needs_authorization",
      "enabled", "disabled", "not_enabled"
    ];
    return allowed.indexOf(value) !== -1 ? t("remote.value." + value) : t("remote.value.unknown");
  }

  function renderRemoteStatus(payload) {
    var summary = payload && payload.summary;
    if (!summary || (payload.observed_at !== null && typeof payload.observed_at !== "string") ||
        typeof payload.stale !== "boolean" || typeof summary.enabled !== "boolean") {
      throw new Error("invalid remote status response");
    }
    latestRemoteStatus = payload;
    setText("remote-provider", remoteLabel(summary.active_provider));
    setText("remote-enabled", remoteLabel(summary.enabled ? "enabled" : "not_enabled"));
    setText("remote-connection", remoteLabel(summary.connection));
    setText("remote-https", remoteLabel(summary.https));
    setText("remote-reachability", remoteLabel(summary.reachability));
    setText("remote-observed", payload.generated_at || payload.observed_at || "—");
    var stale = $("remote-stale");
    if (stale) { stale.hidden = !payload.stale; }
    remoteStatusHintKey = "remote.noLiveProbe";
    renderRemoteHint();
  }

  function renderRemoteHint() {
    var note = $("remote-status-note");
    if (note) { note.textContent = t(remoteStatusHintKey); }
  }

  function loadRemoteStatus() {
    if (remoteStatusRequest) { return remoteStatusRequest; }
    var refreshButton = $("refresh-remote");
    if (refreshButton) { refreshButton.disabled = true; }

    remoteStatusRequest = apiFetch("/api/remote/status", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("remote status " + response.status); }
        return response.json();
      })
      .then(renderRemoteStatus)
      .catch(function (error) {
        if (error && error.message === "unauthorized") { return; }
        remoteStatusHintKey = "remote.loadFailed";
        renderRemoteHint();
        var stale = $("remote-stale");
        if (stale) { stale.hidden = false; }
      })
      .finally(function () {
        remoteStatusRequest = null;
        if (refreshButton) { refreshButton.disabled = false; }
      });
    return remoteStatusRequest;
  }

  function renderServices(services) {
    var tbody = $("service-rows");
    if (!tbody) { return; }
    latestServices = services || [];
    if (!services || !services.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">' +
        escapeHtml(t("dashboard.noServices")) + '</td></tr>';
      return;
    }
    var html = services.map(function (svc) {
      var state = svc.state || "unknown";
      var stateKeys = {
        running: "dashboard.running",
        stopped: "dashboard.stopped",
        failed: "dashboard.failed",
        starting: "dashboard.starting",
        unknown: "dashboard.unknown"
      };
      var stateLabel = stateKeys[state] ? t(stateKeys[state]) : state;
      var enabled = svc.enabled ? t("dashboard.enabled") : t("dashboard.disabled");
      var detail = svc.detail || "";
      var busy = state === "starting" || (servicesPending && pendingServiceName === svc.name);
      var buttonLabel = busy && servicesPending && pendingServiceName === svc.name
        ? t("dashboard.restarting")
        : t("dashboard.restart");
      return '<tr>' +
        '<td><code>' + escapeHtml(svc.name) + '</code><br><span class="hint">' +
          escapeHtml(detail) + '</span></td>' +
        '<td><span class="badge ' + escapeHtml(state) + '">' + escapeHtml(stateLabel) + '</span></td>' +
        '<td>' + enabled + '</td>' +
        '<td><button class="btn btn-outline btn-sm" data-service="' + escapeHtml(svc.name) +
          '"' + (busy ? " disabled" : "") + '>' + escapeHtml(buttonLabel) + '</button></td>' +
      '</tr>';
    }).join("");
    tbody.innerHTML = html;
  }

  /* 用 unicode 转义书写实体，避免编辑器/流水线把 & 之类二次反转义后写坏脚本 */
  var HTML_ESCAPES = {
    "&": "\u0026amp;",
    "<": "\u0026lt;",
    ">": "\u0026gt;",
    '"': "\u0026quot;",
    "'": "\u0026#39;"
  };

  function escapeHtml(value) {
    var text = String(value === null || value === undefined ? "" : value);
    return text.replace(/[&<>"']/g, function (ch) { return HTML_ESCAPES[ch]; });
  }

  function loadStatus() {
    return apiFetch("/api/status")
      .then(function (response) {
        if (!response.ok) { throw new Error("status " + response.status); }
        return response.json();
      })
      .then(function (payload) {
        renderStatus(payload);
        failStreak = 0;
        setDot("live");
        statusHintKey = "dashboard.live";
        setText("status-hint", t(statusHintKey));
      })
      .catch(function (err) {
        if (err && err.message === "unauthorized") { return; }
        failStreak += 1;
        setDot(failStreak >= 3 ? "dead" : "stale");
        statusHintKey = failStreak >= 3 ? "dashboard.disconnected" : "dashboard.retrying";
        setText("status-hint", t(statusHintKey));
      });
  }

  function loadServices() {
    return apiFetch("/api/services")
      .then(function (response) {
        if (!response.ok) { throw new Error("services " + response.status); }
        return response.json();
      })
      .then(function (payload) {
        renderServices(payload.services);
        if (serviceHint && serviceHint.key === "dashboard.servicesUnavailable") {
          serviceHint = null;
          $("service-hint").hidden = true;
        }
      })
      .catch(function () { setServiceHint("dashboard.servicesUnavailable"); });
  }

  function schedulePoll() {
    if (pollTimer) { clearInterval(pollTimer); }
    pollTimer = setInterval(loadStatus, INTERVAL);
  }

  function restartService(name, button) {
    if (servicesPending) { return; }
    if (!window.confirm(t("peanut.confirmService", {name: name}))) { return; }
    servicesPending = true;
    pendingServiceName = name;
    button.disabled = true;
    button.textContent = t("dashboard.restarting");

    apiFetch("/api/services/" + encodeURIComponent(name) + "/restart", { method: "POST" })
      .then(function (response) {
        if (response.status === 202) {
          setServiceHint("dashboard.restartSubmitted", { name: name });
          return response.json().then(function (value) {
            if (value.job_id) { pollServiceJob(value.job_id); }
          });
        } else {
          return response.json().then(function (payload) {
            var code = payload && payload.error ? payload.error.code : response.status;
            setServiceHint("dashboard.restartFailed", { code: code });
          });
        }
      })
      .catch(function () {
        setServiceHint("dashboard.restartRequestFailed");
      })
      .finally(function () {
        servicesPending = false;
        pendingServiceName = "";
        if (latestServices) { renderServices(latestServices); }
      });
  }

  function pollServiceJob(jobId) {
    apiFetch("/api/service-jobs/" + encodeURIComponent(jobId), {cache: "no-store"})
      .then(function (response) {
        if (!response.ok) { throw new Error("job unavailable"); }
        return response.json();
      }).then(function (value) {
        setServiceHint("peanut.serviceJob", {
          id: jobId, status: t("peanut.state." + value.status), reason: value.reason || ""
        });
        if (value.status === "queued" || value.status === "running") {
          setTimeout(function () { pollServiceJob(jobId); }, 2000);
        } else { loadServices(); }
      }).catch(function () { setServiceHint("peanut.serviceUnknown", {id: jobId}); });
  }

  function logout() {
    var btn = $("logout-btn");
    if (btn) { btn.disabled = true; }
    releaseTerminal();
    apiFetch("/auth/logout", { method: "POST" })
      .catch(function () { /* 即使失败也回登录页 */ })
      .finally(function () { window.location.replace("/login"); });
  }

  /* ------------------------------------------------------------------ */
  /* 单页视图切换：overview 与内嵌 iframe 互斥，不产生页面跳转          */
  /* ------------------------------------------------------------------ */
  function hideEmbedHint() {
    var hint = $("embed-hint");
    embedHint = null;
    if (hint) {
      hint.hidden = true;
      hint.textContent = "";
    }
  }

  function showEmbedHint(key, params) {
    var hint = $("embed-hint");
    embedHint = { key: key, params: params || {} };
    if (hint) {
      hint.hidden = false;
      hint.dataset.severity = key === "embed.loading" ? "info" : "warning";
      hint.textContent = t(key, params);
    }
  }

  function setServiceHint(key, params) {
    var hint = $("service-hint");
    serviceHint = { key: key, params: params || {} };
    if (hint) {
      hint.hidden = false;
      hint.textContent = t(key, params);
    }
  }

  function renderLanguageSensitiveState() {
    setText("status-hint", t(statusHintKey));
    setText("uptime", fmtUptime(lastUptimeSeconds));
    if (latestServices) { renderServices(latestServices); }
    if (serviceHint) {
      var serviceHintNode = $("service-hint");
      if (serviceHintNode) { serviceHintNode.textContent = t(serviceHint.key, serviceHint.params); }
    }
    if (embedHint) {
      var embedHintNode = $("embed-hint");
      if (embedHintNode) { embedHintNode.textContent = t(embedHint.key, embedHint.params); }
    }
    renderRemoteHint();
    if (latestRemoteStatus) { renderRemoteStatus(latestRemoteStatus); }
    var view = VIEWS[currentView];
    if (view) {
      setText("embed-title", t(view.titleKey));
      document.title = t(view.titleKey) + " · Pi M2";
    }
    Object.keys(frames).forEach(function (name) { frames[name].title = t(VIEWS[name].titleKey); });
    var copyButtons = document.querySelectorAll("[data-copy-state]");
    for (var i = 0; i < copyButtons.length; i += 1) {
      copyButtons[i].textContent = t(copyButtons[i].getAttribute("data-copy-state"));
    }
  }

  function probeEmbed(name) {
    var view = VIEWS[name];
    if (!view || !view.path) { return; }
    if (probeController) { probeController.abort(); }
    var controller = new AbortController();
    probeController = controller;
    var timeout = setTimeout(function () {
      controller.abort();
      if (currentView === name && probeController === controller) { showEmbedHint("embed.networkError"); }
    }, 12000);
    showEmbedHint("embed.loading");
    var ready = false;
    var frame = frames[name];
    var frameLoaded = frame.dataset.loaded === "true";
    function finish() {
      if (ready && frameLoaded && frames[name] === frame && currentView === name && probeController === controller) {
        clearTimeout(timeout);
        hideEmbedHint();
      }
    }
    frame.onload = function () {
      frame.dataset.loaded = "true";
      frameLoaded = true;
      finish();
    };
    fetch(view.path, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "text/html" }, signal: controller.signal })
      .then(function (response) {
        if (currentView !== name || probeController !== controller) { return; }
        if (response.status === 401 || /\/login(?:\?|$)/.test(response.url)) {
          showEmbedHint("embed.sessionExpired");
        } else if (response.status === 404 && response.headers.get("X-Cloudhost-Gateway-Fallback") === "true") {
          showEmbedHint("embed.gatewayMissing", { path: view.path });
        } else if (!response.ok) {
          showEmbedHint("embed.httpError", {
            message: t(view.hintKey || "embed.backendUnavailable"),
            status: response.status
          });
        } else {
          ready = true;
          finish();
        }
      })
      .catch(function () {
        if (currentView === name && probeController === controller) { showEmbedHint("embed.networkError"); }
      })
      .finally(function () { if (!ready) { clearTimeout(timeout); } });
  }

  function stopTerminalThemeWatch() {
    if (terminalThemeObserver) {
      terminalThemeObserver.disconnect();
      terminalThemeObserver = null;
    }
  }

  function syncFrameTheme(name) {
    var frame = frames[name];
    if (!frame || frame.getAttribute("src") === "about:blank") { return; }
    var doc = frame.contentDocument;
    if (!doc) {
      if (name !== "site") {
        console.warn("Cloudhost embedded theme: inaccessible " + name + " document");
        showEmbedHint("embed.themeUnavailable");
      }
      return;
    }
    // Custom websites own their styling; only the bundled demo opts in.
    if (name === "site" && !doc.documentElement.hasAttribute("data-cloudhost-site")) { return; }
    var source = getComputedStyle(document.documentElement);
    var theme = document.documentElement.getAttribute("data-theme") || "classic";
    var light = theme === "swiss";
    var target = doc.documentElement;
    function token(key) { return source.getPropertyValue("--" + key).trim(); }
    target.setAttribute("data-theme", theme);
    target.style.colorScheme = light ? "light" : "dark";
    var properties = {
      bg: "bg", panel: "panel", border: "border", text: "text", muted: "muted", accent: "accent"
    };
    if (name === "files") {
      properties = {
        background: "bg", surfacePrimary: "panel", surfaceSecondary: "panel-2",
        textPrimary: "muted", textSecondary: "text", blue: "accent", "dark-blue": "accent",
        red: "danger", "dark-red": "danger", "moon-grey": "panel-2", action: "text",
        iconPrimary: "accent", iconTertiary: "muted", hover: "panel-2",
        divider: "border", borderPrimary: "border", borderSecondary: "muted",
        dividerPrimary: "border", dividerSecondary: "border", "icon-blue": "accent",
        "icon-red": "danger", "icon-green": "ok", "icon-yellow": "warn",
        "icon-orange": "warn", "icon-violet": "accent", "input-red": "panel-2",
        "input-green": "panel-2"
      };
      target.style.setProperty("--iconSecondary", token("action-text") || token("bg"));
      target.style.setProperty("--item-selected", token("action-text") || token("bg"));
      if (fileThemeObserver) { fileThemeObserver.disconnect(); }
      function syncFileLogo() {
        var logo = doc.querySelector('header > img[src^="/files/static/img/logo.svg"]');
        var path = "/files/static/img/logo.svg#" + theme;
        if (logo && logo.getAttribute("src") !== path) { logo.setAttribute("src", path); }
      }
      // Vue creates the header after document load and may recreate it on navigation.
      fileThemeObserver = new MutationObserver(syncFileLogo);
      fileThemeObserver.observe(doc.documentElement, {
        childList: true, subtree: true, attributes: true, attributeFilter: ["src"]
      });
      syncFileLogo();
    }
    Object.keys(properties).forEach(function (key) {
      target.style.setProperty("--" + key, token(properties[key]));
    });
    if (name !== "term") { return; }
    stopTerminalThemeWatch();
    var palette = {
      background: token("bg"), foreground: token("text"), cursor: token("accent"),
      cursorAccent: token("bg"), selectionBackground: token("border"),
      black: token("muted"), red: token("danger"), green: token("ok"), yellow: token("warn"),
      blue: light ? "#2457d6" : "#91b5ff", magenta: light ? "#7941a5" : "#ddb3ff",
      cyan: token("accent"), white: token("text"),
      brightBlack: token("muted"), brightRed: token("danger"), brightGreen: token("ok"),
      brightYellow: token("warn"), brightBlue: light ? "#2457d6" : "#bdd2ff",
      brightMagenta: light ? "#7941a5" : "#ebd3ff", brightCyan: token("accent"),
      brightWhite: token("text")
    };
    if (theme === "classic") {
      Object.assign(palette, {
        brightBlack: "#b9c9d9", brightRed: "#ffb4b1", brightGreen: "#7ee5bb",
        brightYellow: "#f9d681", brightMagenta: "#ebd3ff", brightCyan: "#a2e9f6",
        brightWhite: "#ffffff"
      });
    }
    var viewport = null;
    function applyTerminalTheme() {
      var terminal = frame.contentWindow.term;
      if (!terminal || !terminal.options) { return; }
      var current = terminal.options.theme || {};
      if (Object.keys(palette).some(function (key) { return current[key] !== palette[key]; })) {
        terminal.options.theme = Object.assign({}, current, palette);
      }
      var nextViewport = doc.querySelector(".xterm-viewport");
      if (nextViewport && nextViewport !== viewport) {
        viewport = nextViewport;
        terminalThemeObserver.disconnect();
        // ttyd applies server preferences after connecting; keep our palette
        // without reconnecting, including when the user reconnects with Enter.
        terminalThemeObserver.observe(viewport, { attributes: true, attributeFilter: ["style"] });
      }
    }
    terminalThemeObserver = new MutationObserver(applyTerminalTheme);
    terminalThemeObserver.observe(doc.documentElement, { childList: true, subtree: true });
    applyTerminalTheme();
  }

  function releaseTerminal() {
    stopTerminalThemeWatch();
    if (frames.term && frames.term.getAttribute("src") !== "about:blank") {
      var replacement = frames.term.cloneNode(false);
      replacement.src = "about:blank";
      replacement.hidden = true;
      delete replacement.dataset.loaded;
      frames.term.replaceWith(replacement);
      frames.term = replacement;
    }
  }

  function loadFrame(name) {
    if (name === "term") { stopTerminalThemeWatch(); }
    if (name === "files" && fileThemeObserver) {
      fileThemeObserver.disconnect();
      fileThemeObserver = null;
    }
    var frame = frames[name].cloneNode(false);
    frame.addEventListener("load", function () { syncFrameTheme(name); });
    frame.src = VIEWS[name].path;
    frame.hidden = false;
    delete frame.dataset.loaded;
    // Set the initial URL before insertion so iframe navigation adds no extra Back step.
    frames[name].replaceWith(frame);
    frames[name] = frame;
    return frame;
  }

  function copyText(text, button) {
    function done(ok) {
      if (!button) { return; }
      var stateKey = ok ? "dashboard.copied" : "dashboard.copyFailed";
      button.setAttribute("data-copy-state", stateKey);
      button.textContent = t(stateKey);
      setTimeout(function () {
        if (button.getAttribute("data-copy-state") !== stateKey) { return; }
        button.removeAttribute("data-copy-state");
        button.textContent = t(button.getAttribute("data-i18n") || "dashboard.copyCommand");
      }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, legacyCopy);
      return;
    }
    legacyCopy();
    /* 局域网 HTTP 不是安全上下文，Clipboard API 不可用，退回 textarea 方案 */
    function legacyCopy() {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(area);
      if (button) { button.focus(); }
      done(ok);
    }
  }

  function syncTabs(name) {
    var tabs = document.querySelectorAll(".nav-tab[data-view]");
    for (var i = 0; i < tabs.length; i += 1) {
      var tab = tabs[i];
      if (tab.getAttribute("data-view") === name) {
        tab.classList.add("is-active");
        tab.setAttribute("aria-current", "page");
      } else {
        tab.classList.remove("is-active");
        tab.removeAttribute("aria-current");
      }
    }
  }

  function updateWorkspaceHeaderHeight() {
    var header = document.querySelector(".topbar");
    if (header) {
      var height = header.getBoundingClientRect().height;
      document.querySelectorAll("body > [data-i18n-status], body > [data-theme-status]").forEach(function (notice) {
        if (!notice.hidden && !notice.classList.contains("theme-status-success")) {
          var style = getComputedStyle(notice);
          height += notice.getBoundingClientRect().height +
            parseFloat(style.marginTop) + parseFloat(style.marginBottom);
        }
      });
      document.documentElement.style.setProperty(
        "--portal-header-height",
        Math.ceil(height) + "px"
      );
    }
  }

  function setView(name, focus) {
    var sshShortcut = name === "ssh";
    if (sshShortcut) { name = "remote"; }
    if (name === "term" && !ttydOn) { name = "overview"; }
    var view = VIEWS[name] || VIEWS.overview;
    name = VIEWS[name] ? name : "overview";
    if (focus && document.activeElement) { document.activeElement.blur(); }
    var previous = currentView;
    scrollPositions[previous] = window.scrollY;
    if (previous === "term" && name !== "term") { releaseTerminal(); }
    currentView = name;

    var overview = $("view-overview");
    var workspace = $("view-embed");
    var frame = frames[name];

    var embedded = Boolean(view.path);
    document.body.classList.toggle("workspace-open", embedded);
    workspace.dataset.view = name;
    $("site-preview-label").hidden = name !== "site";
    $("site-open-tab").hidden = name !== "site";
    if (overview) { overview.hidden = name !== "overview"; }
    $("overview-services").hidden = name !== "overview";
    $("view-remote").hidden = name !== "remote";
    if (!embedded) {
      if (workspace) { workspace.hidden = true; }
      hideEmbedHint();
      if (probeController) { probeController.abort(); probeController = null; }
    } else {
      if (workspace) { workspace.hidden = false; }
      updateWorkspaceHeaderHeight();
      setText("embed-title", t(view.titleKey));
      hideEmbedHint();
      if (frame) {
        if (frame.getAttribute("src") !== view.path) {
          frame = loadFrame(name);
        }
      }
      Object.keys(frames).forEach(function (key) { frames[key].hidden = key !== name; });
      probeEmbed(name);
    }
    window.dispatchEvent(new CustomEvent("cloudhost:viewchange", { detail: { view: name } }));
    syncTabs(sshShortcut ? "ssh" : name);
    document.title = t(view.titleKey) + " · Pi M2";
    if (focus) {
      var title = $(sshShortcut ? "remote-ssh-title" :
        embedded ? "embed-title" : name === "overview" ? "product-title" : "remote-title");
      requestAnimationFrame(function () {
        if (currentView !== name) { return; }
        if (sshShortcut && title) {
          title.focus({ preventScroll: true });
          title.scrollIntoView({ block: "start" });
        } else {
          if (title) { title.focus({ preventScroll: true }); }
          window.scrollTo(0, scrollPositions[name] || 0);
        }
      });
    }
  }

  function initMenus() {
    var mobile = window.matchMedia("(max-width: 860px)");
    var navButton = $("nav-toggle"), nav = $("main-nav");
    var toolsButton = $("tools-toggle"), tools = $("account-menu");
    function close(button, menu, restore) {
      button.setAttribute("aria-expanded", "false");
      menu.hidden = menu === nav ? mobile.matches : true;
      if (restore) { button.focus(); }
    }
    [[navButton, nav], [toolsButton, tools]].forEach(function (pair) {
      var button = pair[0], menu = pair[1];
      button.addEventListener("click", function () {
        var open = button.getAttribute("aria-expanded") !== "true";
        close(navButton, nav, false);
        close(toolsButton, tools, false);
        button.setAttribute("aria-expanded", String(open));
        menu.hidden = !open;
        if (open) { menu.querySelector("a:not([hidden]), button:not([disabled])").focus(); }
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
          close(button, menu, true);
        }
      });
      document.addEventListener("click", function (event) {
        if (!button.contains(event.target) && !menu.contains(event.target)) { close(button, menu, false); }
      });
      document.addEventListener("focusin", function (event) {
        if (!button.contains(event.target) && !menu.contains(event.target)) { close(button, menu, false); }
      });
    });
    nav.addEventListener("click", function (event) {
      if (event.target.closest("a") && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        close(navButton, nav, mobile.matches);
      }
    });
    function resize() { close(navButton, nav, false); close(toolsButton, tools, false); }
    mobile.addEventListener("change", resize);
    window.addEventListener("resize", resize);
    resize();
  }

  function initViews() {
    if ("scrollRestoration" in window.history) { window.history.scrollRestoration = "manual"; }
    frames = { files: $("embed-frame"), site: $("site-frame"), term: $("term-frame") };
    window.addEventListener("cloudhost:themechange", function () {
      Object.keys(frames).forEach(syncFrameTheme);
    });
    initMenus();
    updateWorkspaceHeaderHeight();
    window.addEventListener("resize", updateWorkspaceHeaderHeight);
    document.addEventListener("click", function (event) {
      var copyTrigger = event.target.closest("[data-copy]");
      if (copyTrigger) {
        event.preventDefault();
        copyText(copyTrigger.getAttribute("data-copy"), copyTrigger);
        return;
      }

      var scrollTrigger = event.target.closest("[data-scroll]");
      if (scrollTrigger) {
        event.preventDefault();
        setView("overview");
        var target = $(scrollTrigger.getAttribute("data-scroll"));
        if (target && target.scrollIntoView) {
          target.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        }
        return;
      }

      var trigger = event.target.closest("a[data-view]");
      if (trigger && !event.ctrlKey && !event.metaKey && !event.shiftKey &&
          trigger.hash === window.location.hash) {
        setView(trigger.getAttribute("data-view"), true);
      }
    });

    var refresh = $("embed-refresh");
    if (refresh) {
      refresh.addEventListener("click", function () {
        var frame = frames[currentView];
        if (frame && window.confirm(t("workbench.reloadConfirm"))) {
          loadFrame(currentView);
          hideEmbedHint();
          probeEmbed(currentView);
        }
      });
    }

    window.addEventListener("hashchange", function () {
      var name = (window.location.hash || "").replace("#", "");
      setView(VIEWS[name] ? name : "overview", true);
    });

    var initial = (window.location.hash || "").replace("#", "");
    setView(VIEWS[initial] ? initial : "overview", initial === "ssh");
  }

  function init() {
    var header = document.querySelector(".topbar");
    if (header) {
      var resize = new ResizeObserver(updateWorkspaceHeaderHeight);
      resize.observe(header);
      document.querySelectorAll("body > [data-i18n-status], body > [data-theme-status]").forEach(function (notice) {
        resize.observe(notice);
      });
      updateWorkspaceHeaderHeight();
    }
    var intervalNode = $("interval");
    if (intervalNode) { intervalNode.textContent = String(INTERVAL); }

    if (ttydOn) {
      var termTab = $("term-tab");
      var termCard = $("term-card");
      if (termTab) { termTab.hidden = false; }
      if (termCard) { termCard.hidden = false; }
    } else {
      var fallback = $("shell-fallback");
      if (fallback) { fallback.hidden = false; }
    }

    var logoutBtn = $("logout-btn");
    if (logoutBtn) { logoutBtn.addEventListener("click", logout); }

    var refreshBtn = $("refresh-services");
    if (refreshBtn) { refreshBtn.addEventListener("click", loadServices); }
    var refreshRemote = $("refresh-remote");
    if (refreshRemote) { refreshRemote.addEventListener("click", loadRemoteStatus); }

    var tbody = $("service-rows");
    if (tbody) {
      tbody.addEventListener("click", function (event) {
        var target = event.target.closest("button[data-service]");
        if (target) { restartService(target.getAttribute("data-service"), target); }
      });
    }

    window.addEventListener("cloudhost:languagechange", renderLanguageSensitiveState);

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      } else {
        loadStatus();
        loadRemoteStatus();
        schedulePoll();
      }
    });

    initViews();
    window.addEventListener("pagehide", releaseTerminal);
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && currentView === "term") { setView("term"); }
    });
    loadStatus();
    loadRemoteStatus();
    loadServices();
    schedulePoll();
    setInterval(loadServices, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
