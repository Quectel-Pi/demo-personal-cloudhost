(function () {
  "use strict";
  var panel = document.getElementById("peanut-controls");
  if (!panel) { return; }
  var t = function (key, params) { return window.CloudhostI18n.t(key, params); };
  var $ = function (id) { return document.getElementById(id); };
  var state = null, binding = null, epoch = 0, inFlight = false, busy = false;
  var job = null, timer = null, qrTimer = null, active = false;
  var feedback = null;
  var addressDirty = false, nextTarget = null, unavailable = false;
  var nextMode = null, enablePhase = null;
  var managementWindow = null;
  var pendingAction = null, jobOperation = null;
  function closeManagementWindow() {
    if (managementWindow && !managementWindow.closed) { managementWindow.close(); }
    managementWindow = null;
  }
  panel.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
  function feedbackText() {
    var params = feedback.params;
    if (feedback.key === "peanut.job") {
      params = Object.assign({}, params, {status: label(params.status)});
    }
    return t(feedback.key, params);
  }
  function message(key, params) {
    feedback = { key: key, params: params || {} };
    $("peanut-feedback").textContent = feedbackText();
  }
  function uuid() {
    if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
    var bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }
  function request(path, method, body, key) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 20000);
    var headers = {};
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = (window.CLOUDHOST || {}).csrf || "";
      headers["Idempotency-Key"] = key || uuid();
    }
    return fetch("/api/remote/" + path, {
      method: method, headers: headers, credentials: "same-origin",
      cache: "no-store", signal: controller.signal,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      if (response.status === 401) {
        clearMaterials();
        window.location.replace("/login");
      }
      if (response.status === 204) { return {}; }
      return response.json().then(function (value) {
        if (!response.ok) {
          var error = new Error("request_failed");
          error.httpStatus = response.status;
          error.code = (value.error || {}).code || String(response.status);
          error.reason = (value.error || {}).detail || "check_state";
          throw error;
        }
        return value;
      });
    }).finally(function () { clearTimeout(timeout); });
  }
  function failed(error) {
    if (error.code === "E5001" && error.reason === "invalid_ssh_mapping") {
      message("peanut.sshInvalid");
      return;
    }
    var credentialErrors = {
      missing: "peanut.credentialMissing",
      invalid: "peanut.credentialInvalid",
      unreadable: "peanut.credentialUnreadable",
      unsafe_permissions: "peanut.credentialPermissions"
    };
    if (error.code === "E5001" && credentialErrors[error.reason]) {
      message(credentialErrors[error.reason]);
      return;
    }
    message("peanut.error", {
      code: error.code || "network",
      reason: error.reason || "outcome_unknown"
    });
  }
  function label(value) {
    var key = "peanut.state." + value;
    var text = t(key);
    return text === key ? String(value || t("remote.value.unknown")) : text;
  }
  function observedLabel(layer) {
    return label(layer.state) + (layer.stale ? " · " + label("stale") : "");
  }
  function render() {
    if (unavailable) {
      closeManagementWindow();
      epoch += 1;
      binding = null;
      clearTimeout(qrTimer);
      $("peanut-qr-image").hidden = true;
      $("peanut-qr-image").removeAttribute("src");
      $("peanut-summary").textContent = t("workbench.loadFailed");
      $("peanut-next").textContent = "";
      panel.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
      $("peanut-autostart").disabled = true;
      $("peanut-reconcile").disabled = !pendingAction;
      if (feedback) { $("peanut-feedback").textContent = feedbackText(); }
      return;
    }
    if (!state) { return; }
    var list = $("peanut-layers");
    list.replaceChildren();
    ["app_config", "installation", "daemon", "device", "binding", "https"].forEach(function (name) {
      var layer = state[name] || {};
      var dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = t("peanut.layer." + name);
      dd.textContent = label(layer.state) + " | " + layer.source + " | " +
        (layer.observed_at || t("peanut.notObserved")) +
        (layer.stale && layer.observed_at ? " | " + t("remote.value.stale") : "") +
        (layer.reason ? " | " + layer.reason : "");
      list.append(dt, dd);
    });
    var intent = document.createElement("dt"), description = document.createElement("dd");
    intent.textContent = t("peanut.intent");
    description.textContent = t("peanut.intentValue", {
      enabled: label(state.intent.enabled ? "enabled" : "disabled"),
      paused: label(state.intent.paused_this_boot ? "yes" : "no"),
      revision: state.revision
    });
    list.append(intent, description);
    $("peanut-intent").textContent = t("peanut.intentSummary", {
      enabled: label(state.intent.enabled ? "enabled" : "disabled"),
      paused: label(state.intent.paused_this_boot ? "yes" : "no")
    });
    var actionBlocked = busy || Boolean(pendingAction) || state.reset_pending;
    panel.querySelectorAll("button").forEach(function (button) { button.disabled = actionBlocked; });
    $("peanut-autostart").checked = state.intent.enabled;
    $("peanut-autostart").disabled = actionBlocked;
    $("peanut-reconcile").hidden = !pendingAction || busy;
    $("peanut-reconcile").disabled = busy;
    $("peanut-recover-reset").hidden = !state.reset_pending;
    $("peanut-recover-reset").disabled = busy || Boolean(pendingAction);
    $("peanut-reset-account").disabled = !accountResetAllowed();
    var capabilities = state.capabilities || {};
    var summary;
    if (state.daemon.stale || (state.daemon.state === "running" && state.device.stale)) {
      summary = t("flow.deviceUnknown");
    } else if (state.daemon.state !== "running") {
      summary = t("workbench.remoteProcess", {process: observedLabel(state.daemon)});
    } else {
      summary = state.device.state === "online" ? t("flow.deviceOnline")
        : t("flow.deviceState", {state: label(state.device.state)});
    }
    $("peanut-summary").textContent = summary;
    nextTarget = null;
    nextMode = null;
    var running = state.daemon.state === "running" && !state.daemon.stale;
    var bound = state.binding.state === "bound" && !state.binding.stale;
    if (!running || !bound || !capabilities.management) { closeManagementWindow(); }
    var bindingHadFocus = $("peanut-binding-step").contains(document.activeElement);
    var configurationBlocked =
      ["valid", "changed_pending_restart"].indexOf(state.app_config.state) === -1 ||
      state.installation.state !== "installed";
    $("peanut-step").textContent = t(!running ? "flow.stepEnable" : !bound ? "flow.stepBind" : "flow.stepMapping");
    $("peanut-binding-step").hidden = !running || bound || !binding;
    $("peanut-management-note").hidden = !running;
    $("peanut-management-note").textContent = t(bound ? "flow.manageReady" : "flow.manageFallback");
    if (!running || bound || !capabilities.qr_binding) {
      clearTimeout(qrTimer);
      binding = null;
      $("peanut-qr-image").hidden = true;
      $("peanut-qr-image").removeAttribute("src");
      $("peanut-qr-state").textContent = "";
    }
    if (!configurationBlocked &&
        ["stopped", "failed"].indexOf(state.daemon.state) !== -1) {
      nextMode = "enable";
    } else if (!configurationBlocked && running && !bound) {
      nextTarget = binding ? $("peanut-qr-refresh") : $("peanut-qr-create");
    } else if (!configurationBlocked && running && bound) {
      nextTarget = $("peanut-management");
    }
    var unknown = (running && (state.binding.state === "unknown" || state.binding.stale)) ||
      state.daemon.state === "unknown";
    $("peanut-next").textContent = unknown
      ? t("flow.bindingUnknown", {reason: state.binding.reason || capabilities.unavailable_reason || label("unknown")})
      : "";
    var blockedLayer = ["valid", "changed_pending_restart"].indexOf(state.app_config.state) === -1
      ? "app_config" : state.installation.state !== "installed" ? "installation"
        : state.daemon.state === "failed" ? "daemon" : null;
    if (blockedLayer) {
      var blocked = state[blockedLayer];
      $("peanut-next").textContent += " " + t("workbench.blocked", {
        layer: t("peanut.layer." + blockedLayer), state: observedLabel(blocked),
        reason: blocked.reason || ""
      });
    }
    $("peanut-qr-create").disabled = busy || !capabilities.qr_binding;
    $("peanut-qr-refresh").disabled = busy || !binding || !capabilities.qr_binding;
    $("peanut-qr-cancel").disabled = busy || !binding;
    $("peanut-management").disabled = busy || !running || !bound || !capabilities.management;
    $("peanut-qr-refresh").hidden = !binding || binding.state !== "waiting";
    $("peanut-qr-cancel").hidden = !binding;
    if (running && !bound && !capabilities.qr_binding) {
      $("peanut-next").textContent += " " + t("flow.qrBlocked", {
        reason: capabilities.unavailable_reason || label("unknown")
      });
    }
    if (running && bound && !capabilities.management) {
      $("peanut-next").textContent += " " + t("flow.managementBlocked", {
        reason: capabilities.unavailable_reason || label("unknown")
      });
    }
    $("peanut-next").hidden = !$("peanut-next").textContent;
    var address = state.address.https_url;
    $("peanut-address-source").hidden = !address;
    $("peanut-address-actions").hidden = !address;
    $("peanut-public-address").textContent = address;
    $("peanut-copy").disabled = busy || !address;
    $("peanut-copy").setAttribute("data-copy", address || "");
    $("peanut-open").hidden = !address;
    if (address) { $("peanut-open").href = address; }
    else { $("peanut-open").removeAttribute("href"); }
    var form = $("peanut-address-form");
    var ssh = Object.prototype.hasOwnProperty.call(state.address, "ssh")
      ? state.address.ssh || {host: "", port: 22}
      : {host: form.dataset.sshHost, port: Number(form.dataset.sshPort)};
    if (!addressDirty) {
      $("peanut-address").value = address;
      $("peanut-ssh-address").value = ssh.host ? ssh.host + ":" + ssh.port : "";
    }
    var command = ssh.host ? "ssh " + (ssh.port === 22 ? "" : "-p " + ssh.port + " ") +
      form.dataset.sshUser + "@" + ssh.host : "";
    $("ssh-public-command").textContent = command;
    $("ssh-public-command").hidden = !command;
    $("ssh-public-copy").hidden = !command;
    $("ssh-public-copy").setAttribute("data-copy", command);
    $("ssh-public-not-configured").hidden = Boolean(command);
    $("peanut-next-action").textContent = nextMode === "enable"
      ? t(state.intent.enabled ? "flow.resumeRemote" : "flow.enable")
      : nextTarget ? nextTarget.textContent : t("workbench.reviewSetup");
    if (nextTarget === $("peanut-management")) {
      $("peanut-next-action").setAttribute("aria-label", t("peanut.managementNewTab"));
    } else {
      $("peanut-next-action").removeAttribute("aria-label");
    }
    $("peanut-next-action").hidden = configurationBlocked || Boolean(binding && binding.state === "waiting");
    $("peanut-next-action").disabled = busy || Boolean(nextTarget && nextTarget.disabled) ||
      state.daemon.stale || ["starting", "stopping", "unknown"].indexOf(state.daemon.state) !== -1;
    if (bindingHadFocus && bound && !$("peanut-next-action").hidden) {
      ($("peanut-next-action").disabled ? $("remote-title") : $("peanut-next-action")).focus();
    }
    if (feedback) { $("peanut-feedback").textContent = feedbackText(); }
    if (actionBlocked) {
      panel.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
      $("peanut-reconcile").disabled = busy || !pendingAction;
      $("peanut-recover-reset").disabled = busy || Boolean(pendingAction);
    }
    if ($("peanut-reset-dialog").open) { resetChoice(); }
  }
  function load() {
    if (inFlight || document.hidden) { return Promise.resolve(false); }
    inFlight = true;
    return request("peanut", "GET").then(function (value) {
      unavailable = false;
      state = value;
      if (!job && value.last_job && ["queued", "running"].indexOf(value.last_job.status) !== -1) {
        job = value.last_job.job_id;
        jobOperation = value.last_job.operation;
        busy = true;
      } else if (!feedback && value.last_job) {
        var previous = value.last_job;
        message("peanut.job", {
          id: previous.job_id, status: previous.status,
          reason: previous.reason ? " · " + previous.reason : ""
        });
        showResetResult(previous);
      }
      render();
      return true;
    }).catch(function (error) {
      unavailable = true;
      failed(error);
      render();
      return false;
    }).finally(function () { inFlight = false; });
  }
  function pollJob() {
    if (!job) { return Promise.resolve(); }
    return request("jobs/" + encodeURIComponent(job), "GET").then(function (value) {
      message("peanut.job", {
        id: value.job_id, status: value.status,
        reason: (value.phase ? " · " + t("reset.phase." + value.phase) : "") +
          (value.reason ? " · " + value.reason : "")
      });
      if (["queued", "running"].indexOf(value.status) === -1) {
        job = null;
        busy = false;
        if (value.error) { failed({code: value.error.code, reason: value.error.reason}); }
        var succeeded = value.status === "succeeded";
        if (succeeded && /reset/.test(value.operation)) { addressDirty = false; clearMaterials(); }
        showResetResult(value);
        return load().then(function (loaded) {
          if (!enablePhase) { return; }
          if (!succeeded || !loaded) {
            enablePhase = null;
            if (succeeded) { message("flow.interrupted"); }
            return;
          }
          continueEnable();
        });
      }
    }).catch(function (error) {
      if (/reset/.test(jobOperation || "")) { message("reset.disconnected", {id: job}); }
      else { failed(error); }
    });
  }
  function showResetResult(value) {
    if (value.status === "succeeded" && value.reason === "local_config_cleared_binding_retained") {
      message("reset.localDone");
    } else if (value.status === "succeeded" && value.reason === "account_unbound_config_cleared") {
      message("reset.accountDone");
    } else if (value.status === "succeeded" && value.operation === "set_autostart") {
      message("reset.autostartSaved");
    }
  }
  function clearMaterials() {
    closeManagementWindow();
    epoch += 1;
    binding = null;
    clearTimeout(qrTimer);
    $("peanut-qr-image").hidden = true;
    $("peanut-qr-image").removeAttribute("src");
    $("peanut-qr-state").textContent = "";
    render();
  }
  function cancelLocal() {
    var old = binding;
    clearMaterials();
    if (!old || !state) { return Promise.resolve(); }
    return request("peanut/bindings/" + old.binding_id + "/cancel", "POST", {
      generation: old.generation, expected_revision: state.revision
    }).catch(failed);
  }
  function showBinding(value, expectedEpoch, focusQr) {
    if (epoch !== expectedEpoch || !active || document.hidden || unavailable || !state ||
        state.daemon.state !== "running" || !state.capabilities.qr_binding ||
        (state.binding.state === "bound" && !state.binding.stale)) { return; }
    var first = !binding;
    binding = value;
    $("peanut-qr-state").textContent = label(value.state) + " | " + value.expires_at;
    $("peanut-qr-image").hidden = value.state !== "waiting";
    if (value.state === "waiting") {
      $("peanut-qr-image").src = value.image_path;
      clearTimeout(qrTimer);
      qrTimer = setTimeout(function () {
        if (epoch !== expectedEpoch || !binding || document.hidden || !active) { return; }
        if (Date.now() >= Date.parse(value.expires_at)) {
          $("peanut-qr-image").hidden = true;
          $("peanut-qr-image").removeAttribute("src");
        }
        request("peanut/bindings/" + value.binding_id, "GET")
          .then(function (updated) { showBinding(updated, expectedEpoch); }).catch(function (error) {
            if (epoch !== expectedEpoch) { return; }
            clearMaterials();
            failed(error);
          });
      }, Math.min(3000, Math.max(0, Date.parse(value.expires_at) - Date.now())));
    } else {
      clearTimeout(qrTimer);
      $("peanut-qr-image").removeAttribute("src");
      if (value.state === "bound") { load(); }
    }
    render();
    if (first && value.state === "waiting" && focusQr &&
        (document.activeElement === document.body || document.activeElement === $("peanut-next-action") ||
         document.activeElement === $("peanut-qr-refresh"))) {
      $("peanut-qr-state").focus();
    }
  }
  function write(path, body, callback) {
    if (!state || busy || pendingAction) { return; }
    if (path === "peanut/actions") {
      pendingAction = {body: Object.assign({expected_revision: state.revision}, body), key: uuid()};
      sendAction();
      return;
    }
    busy = true;
    message("peanut.working");
    render();
    request(path, "POST", Object.assign({ expected_revision: state.revision }, body))
      .then(function (value) {
        if (value.job_id) { job = value.job_id; message("peanut.accepted", {id: job}); }
        if (callback) { callback(value); }
      }).catch(function (error) {
        enablePhase = null;
        failed(error);
      }).finally(function () { busy = !!job; render(); load(); });
  }
  function sendAction() {
    busy = true;
    message(pendingAction.body.operation === "set_autostart" ? "reset.saving" : "peanut.working");
    render();
    request("peanut/actions", "POST", pendingAction.body, pendingAction.key).then(function (value) {
      job = value.job_id;
      jobOperation = value.operation;
      pendingAction = null;
      message("peanut.accepted", {id: job});
    }).catch(function (error) {
      enablePhase = null;
      if (error.code && error.httpStatus < 500) { pendingAction = null; failed(error); }
      else { message("reset.unknown"); }
    }).finally(function () { busy = !!job; render(); load(); });
  }
  function continueEnable() {
    enablePhase = null;
    if (state.daemon.state !== "running") {
      message("flow.interrupted");
      return;
    }
    if (active && !document.hidden && (state.binding.state !== "bound" || state.binding.stale) &&
        state.capabilities.qr_binding) {
      $("peanut-qr-create").click();
    }
  }
  $("peanut-qr-create").addEventListener("click", function () {
    var focusQr = document.activeElement === $("peanut-next-action");
    clearMaterials();
    var expectedEpoch = epoch;
    write("peanut/bindings", {}, function (value) { showBinding(value, expectedEpoch, focusQr); });
  });
  $("peanut-qr-refresh").addEventListener("click", function () {
    if (!binding) { return; }
    var old = binding;
    var focusQr = document.activeElement === $("peanut-next-action") ||
      document.activeElement === $("peanut-qr-refresh");
    clearMaterials();
    var expectedEpoch = epoch;
    write("peanut/bindings/" + old.binding_id + "/refresh", {generation: old.generation},
      function (value) { showBinding(value, expectedEpoch, focusQr); });
  });
  $("peanut-qr-cancel").addEventListener("click", cancelLocal);
  $("peanut-qr-image").addEventListener("error", function () {
    clearMaterials();
    failed({code: "image", reason: "qr_image_unavailable"});
  });
  $("peanut-management").addEventListener("click", function () {
    if (!state || busy || $("peanut-management").disabled) { return; }
    var opened = window.open("about:blank", "_blank");
    if (!opened) { message("peanut.popupBlocked"); return; }
    opened.opener = null;
    managementWindow = opened;
    var expectedEpoch = epoch;
    busy = true;
    message("peanut.working");
    render();
    request("peanut/management-intents", "POST", {expected_revision: state.revision}).then(function (value) {
      if (managementWindow !== opened || opened.closed || epoch !== expectedEpoch || !active || unavailable ||
          state.daemon.state !== "running" || state.daemon.stale ||
          state.binding.state !== "bound" || state.binding.stale || !state.capabilities.management) {
        throw {code: "management", reason: "management_context_changed"};
      }
      if (typeof value.redirect_path !== "string" ||
          !/^\/api\/remote\/peanut\/management-redirect\/[A-Za-z0-9_-]+$/.test(value.redirect_path)) {
        throw {code: "management", reason: "invalid_management_redirect"};
      }
      opened.location.replace(value.redirect_path);
      managementWindow = null;
      message("peanut.managementOpened");
    }).catch(function (error) {
      closeManagementWindow();
      failed(error);
    }).finally(function () { busy = false; render(); load(); });
  });
  $("peanut-address-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (!state || busy) { return; }
    var endpoint = $("peanut-ssh-address").value.trim();
    var parts = /^([^:\s/\\?#@]+)(?::([0-9]{1,5}))?$/.exec(endpoint);
    var ssh = {host: "", port: 22};
    if (endpoint) {
      if (!parts || parts[1].length > 253 ||
          (parts[2] && (Number(parts[2]) < 1 || Number(parts[2]) > 65535))) {
        failed({code: "E5001", reason: "invalid_ssh_mapping"});
        $("peanut-ssh-address").focus();
        return;
      }
      ssh = {host: parts[1], port: parts[2] ? Number(parts[2]) : 22};
    }
    busy = true; render();
    request("peanut/address", "PUT", {
      https_url: $("peanut-address").value, expected_revision: state.revision,
      ssh: ssh
    }).then(function () { addressDirty = false; message("peanut.saved"); })
      .catch(failed).finally(function () { busy = false; load(); });
  });
  $("peanut-address-form").addEventListener("input", function () { addressDirty = true; });
  $("peanut-next-action").addEventListener("click", function () {
    if (nextMode === "enable") {
      if (!window.confirm(t("flow.confirmResume"))) { return; }
      clearMaterials();
      enablePhase = "start";
      write("peanut/actions", {operation: "start"});
      return;
    }
    if (nextTarget) {
      var section = nextTarget.parentElement;
      while (section && section !== panel) {
        if (section.tagName === "DETAILS") { section.open = true; }
        section = section.parentElement;
      }
      if (!nextTarget.hidden) { nextTarget.focus(); }
      nextTarget.click();
    } else {
      $("peanut-address-entry").open = true;
      $("peanut-address").focus();
    }
  });
  $("peanut-autostart").addEventListener("change", function () {
    var enabled = this.checked;
    if (enabled && !window.confirm(t("reset.autostartConfirm"))) { render(); return; }
    write("peanut/actions", {operation: "set_autostart", enabled: enabled, confirmed_autostart: enabled});
  });
  $("peanut-reconcile").addEventListener("click", sendAction);
  $("peanut-recover-reset").addEventListener("click", function () {
    if (window.confirm(t("reset.lan"))) {
      write("peanut/actions", {operation: "recover_reset", confirmed_connection_loss: true});
    }
  });
  function accountResetAllowed() {
    return Boolean(state && state.capabilities.unbind && state.daemon.state === "running" &&
      !state.daemon.stale && !state.binding.stale &&
      ["bound", "unbound"].indexOf(state.binding.state) !== -1);
  }
  function resetChoice() {
    var account = $("peanut-reset-account").checked;
    $("peanut-reset-account-confirm").hidden = !account;
    $("peanut-reset-unbind").required = account;
    $("peanut-reset-gate").hidden = accountResetAllowed();
    $("peanut-reset-gate").textContent = t(state && state.capabilities.unbind ? "reset.stateGate" : "reset.gate");
    $("peanut-reset-submit").textContent = t(account ? "reset.accountAction" : "reset.localAction");
  }
  $("peanut-reset-open").addEventListener("click", function () {
    $("peanut-reset-form").reset();
    resetChoice();
    $("peanut-reset-dialog").showModal();
  });
  $("peanut-reset-form").addEventListener("change", resetChoice);
  $("peanut-reset-cancel").addEventListener("click", function () { $("peanut-reset-dialog").close(); });
  $("peanut-reset-dialog").addEventListener("close", function () { $("peanut-reset-open").focus(); });
  $("peanut-reset-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (!state || busy || pendingAction || state.reset_pending) { return; }
    var account = $("peanut-reset-account").checked;
    if (account && !accountResetAllowed()) { message("reset.stateGate"); return; }
    $("peanut-reset-dialog").close();
    clearMaterials();
    write("peanut/actions", {
      operation: account ? "reset_account_config" : "reset_local_config",
      confirmed_connection_loss: $("peanut-reset-loss").checked,
      confirmed_account_unbind: account && $("peanut-reset-unbind").checked
    });
  });
  $("refresh-remote").addEventListener("click", load);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (!managementWindow) { cancelLocal(); }
      clearTimeout(timer);
    }
    else { tick(); }
  });
  window.addEventListener("pagehide", clearMaterials);
  window.addEventListener("hashchange", cancelLocal);
  $("logout-btn").addEventListener("click", clearMaterials);
  window.addEventListener("cloudhost:languagechange", function () { render(); resetChoice(); });
  new IntersectionObserver(function (entries) {
    active = entries[0].isIntersecting;
    if (!active) { cancelLocal(); }
    else { load(); }
  }).observe(panel);
  function tick() {
    clearTimeout(timer);
    if (document.hidden) { return; }
    var needsInitialState = !state && !$("view-remote").hidden;
    Promise.resolve(job ? pollJob() : active || needsInitialState ? load() : null).finally(function () {
      timer = setTimeout(tick, job ? 1000 : 5000);
    });
  }
  var ssh = $("ssh-card");
  if (ssh && ssh.dataset.webOnly === "true") {
    ssh.querySelector("span").textContent = t("peanut.webOnly");
    ssh.querySelector("span").setAttribute("data-i18n", "peanut.webOnly");
    $("ssh-open").hidden = true;
    $("ssh-copy").hidden = true;
  }
  tick();
})();
