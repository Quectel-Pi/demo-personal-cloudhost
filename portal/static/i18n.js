(function (window, document) {
  "use strict";

  var STORAGE_KEY = "cloudhost.language";
  var VALID_LANGUAGES = ["en", "zh"];
  var messages = {
    en: {
      "theme.label": "Theme",
      "theme.current": "Theme: {theme}",
      "theme.classic": "Classic",
      "theme.bento": "Hardware Bento",
      "theme.swiss": "Swiss Light",
      "theme.cyber": "Cyber Neon",
      "theme.switched": "Switched to {theme}. Preference saved.",
      "theme.invalid": "The saved theme is unavailable. Using the default theme.",
      "theme.read": "Cannot read the theme preference. Using the default theme; changes may not be saved.",
      "theme.write": "Switched to {theme}, but could not save. Refreshing or returning may restore the previous theme.",
      "theme.applyFailed": "The theme could not be applied. The previous appearance is retained. Please retry.",
      "theme.unavailable": "Theme switching is unavailable. The basic page remains usable.",
      "workbench.menu": "Menu",
      "workbench.account": "Account",
      "workbench.title": "Pi M2 workspace",
      "workbench.siteHint": "Your pages and apps",
      "workbench.terminalHint": "Local shell in your browser",
      "workbench.diagnostics": "Connection details",
      "workbench.paths": "Workspace locations",
      "workbench.reloadConfirm": "Reload this app? Unsaved edits or transfers may be lost. This does not check the app for unsaved work.",
      "workbench.remoteSummary": "Device: {device} · Account: {binding}",
      "workbench.remoteProcess": "SDK: {process}",
      "workbench.blocked": "{layer}: {state}. {reason}",
      "workbench.bindingNext": "Complete account binding with the official QR when available.",
      "workbench.unknownNext": "Binding is not confirmed. Review connection details and the official console. Capability gate: {reason}.",
      "workbench.reviewSetup": "Configure public addresses",
      "workbench.loadFailed": "Connection state is unavailable. Refresh to retry; do not assume the device is connected.",
      "site.transport": "Transport depends on the entry you use. Check the browser address bar; this static page does not verify public access.",
      "dashboard.footerPrefixPublic": "Public HTTPS · Status refresh every ",
      "dashboard.siteCardNewTab": "My site — opens in a new tab",
      "ssh.publicTitle": "Public SSH",
      "ssh.publicNotConfigured": "Public SSH is not configured.",
      "ssh.title": "SSH",
      "ssh.lanTitle": "Local network",
      "peanut.title": "PHTunnel device",
      "flow.stepEnable": "1 / 3 · Enable",
      "flow.deviceOnline": "Device online",
      "flow.deviceUnknown": "Device status unavailable",
      "flow.deviceState": "Device: {state}",
      "flow.stepBind": "2 / 3 · Scan to sign in",
      "flow.stepMapping": "3 / 3 · Configure mapping",
      "flow.enable": "Enable remote access",
      "flow.confirmEnable": "Start the SDK now? Existing mappings may resume. Boot-start settings will not change.",
      "flow.resumeRemote": "Start remote access",
      "flow.confirmResume": "Start the SDK now? Existing public mappings may become active. The saved boot-start intent will not change. If the result is unknown, refresh status before taking another action.",
      "flow.bindingUnknown": "Binding not confirmed: {reason}.",
      "flow.qrBlocked": "QR unavailable: {reason}.",
      "flow.manageReady": "Binding confirmed.",
      "flow.managementBlocked": "Controlled management is unavailable: {reason}. Use normal official console sign-in instead.",
      "flow.manageFallback": "Binding not confirmed.",
      "flow.registerAddress": "Configure public addresses",
      "flow.addressSource": "User-registered; not synced from the official console.",
      "flow.interrupted": "The start sequence could not be confirmed. Refresh state from LAN before taking another action; no automatic retry was made.",
      "reset.autostart": "Start remote access automatically at boot",
      "reset.autostartHelp": "Applies to the next boot. Existing mappings may resume. Changing this does not start or stop the current connection.",
      "reset.autostartConfirm": "Allow remote access to start on the next boot? Existing public mappings may resume. This will not start the SDK now.",
      "reset.autostartSaved": "Boot-start setting saved. The current connection has not changed.",
      "reset.saving": "Saving boot-start setting...",
      "reset.advanced": "Advanced operations",
      "reset.open": "Clear current configuration",
      "reset.title": "Clear remote access configuration",
      "reset.help": "Remote access will stop, boot start will be disabled, and registered public addresses and verification results will be cleared. Files, application credentials and device identity are retained. Official cloud mappings are not deleted.",
      "reset.lan": "Before continuing, keep your original LAN entry available. If this public connection drops, check the persistent task result from LAN; do not assume success or failure.",
      "reset.local": "Clear local configuration (retain account binding; this does not allow an already bound device to scan again)",
      "reset.account": "Unbind account and clear configuration (sign in again by QR)",
      "reset.gate": "Automatic unbinding has not been verified. Unbind in the official console, then refresh from LAN to confirm.",
      "reset.stateGate": "Start the SDK explicitly and refresh until the account binding state can be confirmed before resetting the account.",
      "reset.loss": "I understand the remote connection may be lost and can access this device from LAN.",
      "reset.unbindConfirm": "I understand this will unbind the current account.",
      "reset.localAction": "Clear local configuration",
      "reset.accountAction": "Unbind account and clear configuration",
      "reset.cancel": "Cancel",
      "reset.reconcile": "Confirm pending request",
      "reset.recover": "Recheck result / continue local cleanup",
      "reset.unknown": "Request outcome unknown. Check from LAN or confirm the pending request using the same request key; do not start a new action.",
      "reset.disconnected": "Connection interrupted; task {id} outcome is unconfirmed. Check the persistent result from LAN.",
      "reset.localDone": "Configuration cleared; remote access is off. Account binding is retained. Start explicitly when ready; no QR was requested.",
      "reset.accountDone": "Account unbinding confirmed and configuration cleared; remote access is off. Start explicitly, then scan a new QR to sign in.",
      "reset.phase.accepted": "Checking conditions",
      "reset.phase.executing": "Executing",
      "reset.phase.checking_binding": "Checking account binding",
      "reset.phase.unbinding": "Unbinding account",
      "reset.phase.confirming_unbound": "Confirming account unbinding",
      "reset.phase.account_unbound": "Account unbinding confirmed",
      "reset.phase.stopping": "Stopping remote access",
      "reset.phase.clearing_local": "Clearing local configuration",
      "reset.phase.completed": "Completed",
      "reset.phase.failed": "Failed",
      "reset.phase.interrupted": "Interrupted; check from LAN",
      "peanut.confirmService": "Restart {name}? Existing sessions or public access may disconnect. An accepted task is not completion; use LAN to check its result if disconnected.",
      "peanut.serviceJob": "Service task {id}: {status}. {reason}",
      "peanut.serviceUnknown": "Service task {id}: connection lost or result unavailable. Check this task from LAN; do not assume failure or success.",
      "peanut.binding": "Account binding",
      "peanut.qrCreate": "Get official QR",
      "peanut.qrRefresh": "Refresh QR",
      "peanut.qrCancel": "Cancel local binding session",
      "peanut.qrAlt": "Official account binding QR",
      "peanut.cancelHelp": "Cancel only hides this QR and stops local polling. It cannot revoke an issued official QR or undo a completed scan.",
      "peanut.management": "Configure mapping",
      "peanut.managementNewTab": "Configure mapping (opens in a new tab)",
      "peanut.popupBlocked": "New tab blocked. Allow pop-ups for this portal, then try again. No management request was sent.",
      "peanut.managementOpened": "Mapping configuration opened in a new tab.",
      "peanut.addressLabel": "Public HTTPS root address",
      "peanut.sshAddress": "Public SSH address (optional)",
      "peanut.sshInvalid": "Invalid SSH mapping. Enter hostname:port with a port from 1 to 65535, without a protocol or path. Port defaults to 22 when omitted; leave empty to clear SSH.",
      "peanut.save": "Save address",
      "peanut.copy": "Copy address",
      "peanut.open": "Open public portal",
      "peanut.error": "{code}: {reason}. Refresh state first; do not repeat an uncertain write.",
      "peanut.credentialMissing": "PHTunnel application credentials were not provided during deployment. Complete the secure deployment setup with valid credentials, then refresh. This page does not accept credentials.",
      "peanut.credentialInvalid": "The deployed PHTunnel credential configuration is invalid. Have an administrator correct it through the secure deployment process, then refresh; existing files are never replaced automatically.",
      "peanut.credentialUnreadable": "The deployed PHTunnel credential configuration cannot be read. Have an administrator correct its ownership and permissions through deployment, then refresh.",
      "peanut.credentialPermissions": "PHTunnel credential ownership or permissions are unsafe. Have an administrator correct them through deployment, then refresh.",
      "peanut.notObserved": "Not observed",
      "peanut.layer.app_config": "Application file",
      "peanut.layer.installation": "SDK installation",
      "peanut.layer.daemon": "SDK process",
      "peanut.layer.device": "Device",
      "peanut.layer.binding": "Account",
      "peanut.layer.https": "TLS / gateway proof",
      "peanut.intent": "Boot-start intent",
      "peanut.intentSummary": "Boot start: {enabled} · paused this boot: {paused}",
      "peanut.intentValue": "{enabled}; paused this boot: {paused}; revision {revision}",
      "peanut.job": "Task {id}: {status}{reason}",
      "peanut.working": "Working…",
      "peanut.accepted": "Accepted {id}, not complete. If disconnected, check from LAN.",
      "peanut.saved": "Address saved, not yet verified.",
      "peanut.copied": "Address copied.",
      "peanut.webOnly": "This public address is Web-only, not native SSH. Use the configured user/port with the board's LAN address. The Web terminal is a local shell.",
      "peanut.state.valid": "Valid",
      "peanut.state.invalid": "Invalid",
      "peanut.state.missing": "Missing",
      "peanut.state.unreadable": "Unreadable",
      "peanut.state.unsafe_permissions": "Unsafe permissions",
      "peanut.state.changed_pending_restart": "Changed; explicit restart required",
      "peanut.state.installed": "Installed (pinned SDK)",
      "peanut.state.not_installed": "Not installed",
      "peanut.state.incompatible": "Incompatible",
      "peanut.state.running": "Running",
      "peanut.state.stopped": "Stopped",
      "peanut.state.starting": "Starting",
      "peanut.state.stopping": "Stopping",
      "peanut.state.failed": "Failed",
      "peanut.state.unknown": "Unknown",
      "peanut.state.online": "Device online (not proof of binding)",
      "peanut.state.offline": "Offline",
      "peanut.state.logging_in": "Device logging in",
      "peanut.state.retrying": "Retrying",
      "peanut.state.bound": "Bound (verified adapter)",
      "peanut.state.unbound": "Unbound",
      "peanut.state.waiting": "Waiting for binding",
      "peanut.state.expired": "Expired",
      "peanut.state.cancelled": "Cancelled locally",
      "peanut.state.unverified": "Unverified",
      "peanut.state.endpoint_ready": "TLS and gateway challenge matched",
      "peanut.state.stale": "Stale",
      "peanut.state.enabled": "Enabled",
      "peanut.state.disabled": "Disabled",
      "peanut.state.yes": "Yes",
      "peanut.state.no": "No",
      "peanut.state.queued": "Queued",
      "peanut.state.succeeded": "Succeeded",
      "peanut.state.recovery_required": "Check state before recovery",
      "experience.headline": "One board. Your personal cloud.",
      "login.systemHint": "Use the board's system account password (PAM). Changing the system password also changes portal sign-in.",
      "login.hashHint": "Use the configured portal administrator password.",
      "login.fallbackHint": "If PAM is unavailable, the configured administrator password fallback remains available.",
      "experience.intro": "Manage files, host a website and run commands in your own real workspace.",
      "experience.diagram": "Concept diagram · not live device status",
      "experience.boundary": "HTTP only. Use a trusted local network; this connection is not encrypted.",
      "experience.boundaryPublic": "Public HTTPS portal. Existing Web permissions are unchanged; HTTPS alone does not verify every function.",
      "login.brandSubPublic": "Quectel Pi M2 · HTTPS portal",
      "login.footNetworkPublic": "Public HTTPS access. Password sign-in is protected by an attempt limit.",
      "experience.terminalOff": "Web terminal is not installed or is disabled. Use SSH with your own key.",
      "dashboard.unknown": "Unknown",
      "dashboard.servicesUnavailable": "Service status could not be refreshed. Previously shown values may be stale. Retry with Refresh; on the board, check systemctl status.",
      "embed.loading": "Checking the application route…",
      "embed.documentReady": "Application page responded. This does not confirm completion of file operations or site publication.",
      "embed.terminalDocument": "Terminal page responded, not a confirmed shell connection. See the terminal's own connection / reconnect feedback. This is a local browser shell, not SSH.",
      "embed.sessionExpired": "Sign-in is required or your session expired. Sign in again, then reopen this workspace.",
      "embed.networkError": "Application check failed or timed out. Check your connection, then use Refresh to retry.",
      "common.languageEnglish": "English",
      "common.languageChinese": "简体中文",
      "common.languageGroup": "Language",
      "common.preferenceNotSaved": "Language changed for this page, but your browser could not save the preference.",
      "login.pageTitle": "Sign in · Pi M2 Personal Cloud",
      "login.brand": "Personal Cloud",
      "login.brandSub": "Quectel Pi M2",
      "login.authHint": "Sign in with the account and password configured for this portal.",
      "login.username": "Username",
      "login.password": "Password",
      "login.submit": "Sign in",
      "login.submitting": "Signing in…",
      "login.error.required": "Enter a username and password.",
      "login.error.rateLimit": "Too many attempts. Try again in {seconds} seconds.",
      "login.error.rateLimitLater": "Too many attempts. Please try again later.",
      "login.error.credentials": "Incorrect username or password.",
      "login.error.failure": "Sign-in failed ({code}).",
      "login.error.network": "Network error. Make sure you are on the same local network as the board.",
      "login.footNetwork": "Local network access only. Password sign-in is protected by an attempt limit.",
      "login.footSsh": "SSH allows key-based sign-in only. Use SSH in the top bar after signing in to view and copy the full command.",
      "dashboard.pageTitle": "Personal Cloud · Pi M2",
      "dashboard.brand": "Personal Cloud",
      "dashboard.navLabel": "Main navigation",
      "dashboard.overview": "Overview",
      "dashboard.files": "Files",
      "dashboard.site": "Site",
      "remote.title": "Remote access",
      "remote.refresh": "Refresh",
      "remote.noLiveProbe": "See individual PHTunnel layers below. A running process or online device does not prove binding or external reachability.",
      "remote.statusLabel": "Remote access status",
      "remote.provider": "Configured provider",
      "remote.intent": "Enabled by configuration",
      "remote.connection": "Connection",
      "remote.https": "HTTPS endpoint",
      "remote.reachability": "External reachability",
      "remote.updated": "Response generated",
      "remote.stale": "Runtime status is unknown or stale. No live client probe has been verified.",
      "remote.loadFailed": "Could not load remote status. Previously shown values may be stale.",
      "remote.value.none": "None",
      "remote.value.tailscale": "Tailscale",
      "remote.value.zerotier": "ZeroTier",
      "remote.value.peanut": "Peanut Shell",
      "remote.value.unknown": "Unknown",
      "remote.value.unverified": "Unverified",
      "remote.value.verified": "Verified",
      "remote.value.stale": "Stale",
      "remote.value.connected": "Connected",
      "remote.value.connecting": "Connecting",
      "remote.value.disconnected": "Disconnected",
      "remote.value.degraded": "Degraded",
      "remote.value.failed": "Failed",
      "remote.value.endpoint_ready": "Endpoint ready",
      "remote.value.configuring": "Configuring",
      "remote.value.needs_authorization": "Needs authorization",
      "remote.value.off": "Off",
      "remote.value.configured": "Configured",
      "remote.value.enabled": "Enabled",
      "remote.value.disabled": "Disabled",
      "remote.value.not_enabled": "Not enabled",
      "dashboard.ssh": "SSH",
      "dashboard.terminal": "Terminal",
      "dashboard.logout": "Sign out",
      "dashboard.systemStatus": "System status",
      "dashboard.connecting": "Connecting…",
      "dashboard.live": "Live",
      "dashboard.disconnected": "Connection lost",
      "dashboard.retrying": "Retrying…",
      "dashboard.cpu": "CPU",
      "dashboard.load": "Load",
      "dashboard.memory": "Memory",
      "dashboard.temperature": "Temperature",
      "dashboard.socTemperature": "SoC temperature",
      "dashboard.disk": "Disk",
      "dashboard.primaryInterface": "Primary interface",
      "dashboard.download": "Download",
      "dashboard.upload": "Upload",
      "dashboard.uptime": "Uptime",
      "dashboard.days": "{count} d",
      "dashboard.hours": "{count} h",
      "dashboard.minutes": "{count} min",
      "dashboard.seconds": "{count} s",
      "dashboard.services": "Services",
      "dashboard.refresh": "Refresh",
      "dashboard.service": "Service",
      "dashboard.state": "State",
      "dashboard.enabledAtBoot": "Enabled at boot",
      "dashboard.action": "Action",
      "dashboard.loading": "Loading…",
      "dashboard.noServices": "No service information",
      "dashboard.enabled": "Enabled",
      "dashboard.disabled": "Disabled",
      "dashboard.running": "Running",
      "dashboard.stopped": "Stopped",
      "dashboard.failed": "Failed",
      "dashboard.starting": "Starting",
      "dashboard.restart": "Restart",
      "dashboard.restarting": "Restarting…",
      "dashboard.restartSubmitted": "Restart submitted for {name}. Status will refresh in about 3 seconds.",
      "dashboard.restartFailed": "Restart failed: {code}",
      "dashboard.restartRequestFailed": "Restart request failed. Please try again.",
      "dashboard.links": "Quick links",
      "dashboard.filesCard": "Files",
      "dashboard.fileActions": "Upload / download / preview / rename / delete",
      "dashboard.siteCard": "My site",
      "dashboard.siteDescription": "Static website: put HTML in ~/cloud/www and open /site/ in a browser.",
      "dashboard.terminalCard": "Web terminal",
      "dashboard.terminalDescription": "Local browser shell, not SSH. Protected by the shared gateway.",
      "dashboard.sshCard": "SSH access",
      "dashboard.sshDescription": "Key-based sign-in only · copy the command to your local terminal",
      "dashboard.openSsh": "Open SSH",
      "dashboard.copyCommand": "Copy command",
      "dashboard.copied": "Copied",
      "dashboard.copyFailed": "Copy failed. Select and copy the text manually.",
      "dashboard.entryNote": "Files, site and terminal open in this page through Caddy. Files and terminal require portal sign-in; site access follows site_public configuration.",
      "embed.filesUnavailable": "File service is unavailable (cloudhost-files). On the board, run: systemctl status cloudhost-files",
      "embed.siteUnavailable": "The site directory cannot be read. Check that ~/cloud/www exists and the Caddy user can read it.",
      "embed.terminalUnavailable": "Terminal service is unavailable (cloudhost-ttyd). On the board, run: systemctl status cloudhost-ttyd",
      "embed.gatewayMissing": "The gateway did not route {path} to its backend; the request fell through to the portal. After deployment authorization, run sudo ./install.sh && sudo systemctl restart caddy on the board to render the configuration again; use --keep-site to preserve a custom homepage.",
      "embed.themeUnavailable": "The embedded page cannot be accessed to synchronize its theme. Its current appearance is retained.",
      "embed.backendUnavailable": "Backend is unavailable",
      "embed.httpError": "{message} (HTTP {status})",
      "embed.refresh": "Refresh",
      "embed.reloadPage": "Reload page",
      "embed.sitePreview": "Site preview",
      "embed.openSiteTab": "Open in new tab",
      "embed.backOverview": "Back to overview",
      "embed.frameTitle": "Embedded application",
      "dashboard.footerPrefix": "LAN edition · HTTP port 80 only · Status refresh every ",
      "dashboard.footerSuffix": " ms",
      "site.pageTitle": "My site · Quectel Pi M2",
      "site.brand": "Quectel Pi M2 · Personal Cloud",
      "site.ready": "Your static site is ready",
      "site.introBefore": "This page is served from ",
      "site.introAfter": " on the board through Caddy at ",
      "site.introEnd": ". It is a placeholder home page — replace it with your own page.",
      "site.publishTitle": "Publish your own page",
      "site.publishFiles": "Select Files in the portal's top bar and open the ",
      "site.orSsh": " directory, or sign in to the board over SSH.",
      "site.replacePageBefore": "Replace ",
      "site.replacePageAfter": " with your page; put CSS / JS / images in the same directory or a subdirectory.",
      "site.openSiteBefore": "Open ",
      "site.subdirectoryAfter": " in a browser; subdirectories are available under ",
      "site.suitableTitle": "What can I host?",
      "site.suitableContentBefore": "Static content such as a personal homepage, project documentation, demo or download page. Pages requiring a backend should use another service or the portal's ",
      "site.suitableContentAfter": " API.",
      "site.backPortal": "Back to the Personal Cloud portal",
      "site.heroTitle": "A small board. Your own corner of the web.",
      "site.heroText": "A personal project showcase hosted on Quectel Pi M2. Explore a website, try a browser-only app, then publish your own page.",
      "site.navigation": "Site sections",
      "site.aboutTitle": "About this host",
      "site.aboutText": "Quectel Pi M2 / RK3576 / Debian 13 is the target platform. Caddy serves this static site at /site/. This is a platform introduction, not live device telemetry.",
      "site.boundary": "The portal handles status, files and terminal access. This site demonstrates personal pages, documentation and static apps, without a new backend or an internet connection.",
      "site.projectsTitle": "Project showcase",
      "site.profileCard": "Personal homepage",
      "site.profileText": "Introduce your board and projects with a page you control.",
      "site.appCard": "A browser-only app",
      "site.appText": "Edit Markdown and see a safe preview immediately.",
      "site.guideCard": "A publishing notebook",
      "site.guideText": "Follow a real file-to-website workflow on your board.",
      "site.markdownTitle": "Markdown playground",
      "site.markdownHelp": "Supports headings, paragraphs, lists, quotes, fenced code and links. Raw HTML is displayed as text; unsafe or malformed links remain text. This is a basic demo, not a full Markdown implementation. Input stays in this page and is not saved or sent to the server.",
      "site.editor": "Markdown source",
      "site.preview": "Live preview",
      "site.reset": "Restore example",
      "site.empty": "Type some Markdown to see a preview.",
      "site.example": "# My Pi M2 project\n\nA personal website, hosted on my own board.\n\n## Things to try\n\n- Edit this heading\n- Publish your own page\n\n1. Save the page\n2. Refresh your browser\n\n> Your local network. Your own site.\n\n```text\n# Code stays as text\necho \"Hello from Pi M2\"\n```\n\n[Publishing guide](#publish)",
      "site.publishLocate": "Open Files from the portal or connect over SSH. Find your configured site_dir; the default is ~/cloud/www (under cloud/www when Files opens your home directory).",
      "site.publishBackup": "Back up index.html outside the public site directory. Edit its heading, or replace it with your own HTML; keep CSS, JS and images in the site directory using relative links.",
      "site.publishRefresh": "Save the file and refresh /site/ to show the change. No service restart is required. If necessary, bypass the browser cache.",
      "site.publishSafety": "Deployment installs the current demo homepage by default. A different existing index.html is backed up under /var/backups/cloudhost-site before replacement; other site files are kept. Use --keep-site when reinstalling to retain your custom homepage. Public sites must not contain secrets.",
      "site.downloadsTitle": "Sample downloads",
      "site.downloadHelp": "Download the project introduction or a preset Markdown example in the current language. These UTF-8 files are generated in your browser, not fetched from the file service. Your editor contents are not included.",
      "site.downloadIntro": "Download introduction (.txt)",
      "site.downloadMarkdown": "Download example (.md)",
      "site.downloadStarted": "Download requested. Check your browser's download list.",
      "site.downloadFailed": "Could not start the download. Check browser download permissions and try again."
    },
    zh: {
      "theme.label": "主题",
      "theme.current": "主题：{theme}",
      "theme.classic": "经典蓝青",
      "theme.bento": "硬件展台",
      "theme.swiss": "瑞士极简",
      "theme.cyber": "赛博霓虹",
      "theme.switched": "已切换为{theme}，偏好已保存。",
      "theme.invalid": "保存的主题不可用，已使用默认主题。",
      "theme.read": "无法读取主题偏好，当前使用默认主题；切换结果可能无法保存。",
      "theme.write": "已切换为{theme}，但无法保存；刷新或重新进入可能恢复原主题。",
      "theme.applyFailed": "主题未能应用，已保留先前外观，请重试。",
      "theme.unavailable": "主题切换不可用，基础页面仍可使用。",
      "workbench.menu": "菜单",
      "workbench.account": "账号",
      "workbench.title": "Pi M2 工作台",
      "workbench.siteHint": "你的网页与应用",
      "workbench.terminalHint": "浏览器内的本地 shell",
      "workbench.diagnostics": "连接详情",
      "workbench.paths": "工作区位置",
      "workbench.reloadConfirm": "重新加载此应用？未保存的编辑或传输可能丢失。此提示不会检测应用内的未保存内容。",
      "workbench.remoteSummary": "设备：{device} · 账号：{binding}",
      "workbench.remoteProcess": "SDK：{process}",
      "workbench.blocked": "{layer}：{state}。{reason}",
      "workbench.bindingNext": "在能力可用后，使用官方二维码完成账号绑定。",
      "workbench.unknownNext": "尚未确认绑定。请核对连接详情与官方控制台。能力门禁：{reason}。",
      "workbench.reviewSetup": "配置公网地址",
      "workbench.loadFailed": "无法获取连接状态。请刷新重试，不能据此判断设备已连接。",
      "site.transport": "传输方式取决于当前入口。请查看浏览器地址栏；此静态页面不验证公网访问是否可用。",
      "dashboard.footerPrefixPublic": "公网 HTTPS · 状态刷新 ",
      "dashboard.siteCardNewTab": "我的站点 — 在新标签打开",
      "ssh.publicTitle": "公网 SSH",
      "ssh.publicNotConfigured": "未配置公网 SSH。",
      "ssh.title": "SSH",
      "ssh.lanTitle": "局域网",
      "peanut.title": "PHTunnel 设备",
      "flow.stepEnable": "1 / 3 · 启用",
      "flow.deviceOnline": "设备在线",
      "flow.deviceUnknown": "设备状态待确认",
      "flow.deviceState": "设备：{state}",
      "flow.stepBind": "2 / 3 · 扫码登录",
      "flow.stepMapping": "3 / 3 · 配置映射",
      "flow.enable": "启用远程访问",
      "flow.confirmEnable": "立即启动 SDK？已有公网映射可能恢复；不会改变开机自启设置。",
      "flow.resumeRemote": "启动远程访问",
      "flow.confirmResume": "立即启动 SDK？已有公网映射可能恢复；已保存的开机自启意图不会改变。若结果未知，请先刷新状态再操作。",
      "flow.bindingUnknown": "绑定未确认：{reason}。",
      "flow.qrBlocked": "二维码不可用：{reason}。",
      "flow.manageReady": "绑定已确认。",
      "flow.managementBlocked": "受控管理入口暂不可用：{reason}。请使用普通官方控制台登录。",
      "flow.manageFallback": "绑定未确认。",
      "flow.registerAddress": "配置公网地址",
      "flow.addressSource": "用户登记，非官方自动同步。",
      "flow.interrupted": "无法确认启动流程完成。请通过局域网刷新状态后再操作，没有自动重试。",
      "reset.autostart": "开机自动启动远程访问",
      "reset.autostartHelp": "开启后，设备下次开机将自动启动远程访问，已有映射可能恢复。修改此选项不会启动或停止当前连接。",
      "reset.autostartConfirm": "允许下次开机自动启动远程访问？已有公网映射可能恢复。此操作不会立即启动 SDK。",
      "reset.autostartSaved": "开机自启设置已保存，当前连接未改变。",
      "reset.saving": "正在保存开机自启设置...",
      "reset.advanced": "高级操作",
      "reset.open": "清除当前配置",
      "reset.title": "清除远程访问配置",
      "reset.help": "清除后远程访问会停止，开机自启关闭，本机保存的公网地址和验证结果会被清空。不会删除文件、应用凭据和设备身份，也不会代你删除花生壳控制台中的映射。",
      "reset.lan": "继续前请保留原局域网入口。若公网连接中断，请从局域网查看持久任务结果，不要据此判断成功或失败。",
      "reset.local": "清除本机配置（保留账号绑定，不能让已绑定设备重新扫码登录）",
      "reset.account": "解绑账号并清除配置（重新扫码登录）",
      "reset.gate": "当前版本尚未验证自动解绑，请先在花生壳官方控制台解除设备绑定，再回到局域网页面刷新确认。",
      "reset.stateGate": "请先明确启动 SDK 并刷新，待账号绑定状态可核实后再重置账号。",
      "reset.loss": "我了解远程连接可能中断，并能通过局域网重新访问设备。",
      "reset.unbindConfirm": "我了解将解除当前花生壳账号绑定。",
      "reset.localAction": "清除本机配置",
      "reset.accountAction": "解绑账号并清除配置",
      "reset.cancel": "取消",
      "reset.reconcile": "核实待确认请求",
      "reset.recover": "重新核实状态 / 继续本机清理",
      "reset.unknown": "请求结果未知。请从局域网检查，或使用原请求键核实待确认请求；不要另建操作。",
      "reset.disconnected": "连接已中断，任务 {id} 结果待确认，请从局域网查看持久结果。",
      "reset.localDone": "配置已清除，远程访问已关闭。花生壳账号绑定仍保留；准备好后请手动启用，没有自动申请二维码。",
      "reset.accountDone": "账号解绑已确认，配置已清除，远程访问已关闭。手动启用后可重新扫码登录。",
      "reset.phase.accepted": "检查条件",
      "reset.phase.executing": "执行中",
      "reset.phase.checking_binding": "核实账号绑定",
      "reset.phase.unbinding": "解绑账号",
      "reset.phase.confirming_unbound": "确认解绑结果",
      "reset.phase.account_unbound": "已确认账号解绑",
      "reset.phase.stopping": "停止远程访问",
      "reset.phase.clearing_local": "清除本机配置",
      "reset.phase.completed": "完成",
      "reset.phase.failed": "失败",
      "reset.phase.interrupted": "执行中断，请从局域网核实",
      "peanut.confirmService": "重启 {name}？会话或公网连接可能断开。已受理不等于完成；断连后请通过局域网查询任务结果。",
      "peanut.serviceJob": "服务任务 {id}：{status}。{reason}",
      "peanut.serviceUnknown": "服务任务 {id}：连接断开或结果暂不可查。请通过局域网查询，不要断言失败或成功。",
      "peanut.binding": "账号绑定",
      "peanut.qrCreate": "获取官方二维码",
      "peanut.qrRefresh": "刷新二维码",
      "peanut.qrCancel": "取消本地绑定会话",
      "peanut.qrAlt": "官方账号绑定二维码",
      "peanut.cancelHelp": "取消仅隐藏二维码并停止本地轮询，不能撤销已签发的官方二维码，也不撤销已完成的扫码。",
      "peanut.management": "配置映射",
      "peanut.managementNewTab": "配置映射（在新标签页打开）",
      "peanut.popupBlocked": "新标签页被拦截。请允许此门户打开弹窗后重试；尚未发送管理请求。",
      "peanut.managementOpened": "已在新标签页打开映射配置。",
      "peanut.addressLabel": "公网 HTTPS 根地址",
      "peanut.sshAddress": "公网 SSH 地址（选填）",
      "peanut.sshInvalid": "SSH 映射信息无效。请填写主机名:端口，端口范围为 1–65535，不带协议或路径；省略端口使用 22，留空可清除 SSH。",
      "peanut.save": "保存地址",
      "peanut.copy": "复制地址",
      "peanut.open": "打开公网门户",
      "peanut.error": "{code}：{reason}。请先刷新状态；结果未知的写操作不要重复。",
      "peanut.credentialMissing": "部署时未提供 PHTunnel 应用凭据。请通过安全部署流程配置有效凭据后刷新；本页面不接收凭据。",
      "peanut.credentialInvalid": "部署的 PHTunnel 凭据配置无效。请管理员通过安全部署流程修正后刷新；不会自动覆盖已有文件。",
      "peanut.credentialUnreadable": "无法读取部署的 PHTunnel 凭据配置。请管理员通过部署流程修正所有者和权限后刷新。",
      "peanut.credentialPermissions": "PHTunnel 凭据所有者或权限不安全。请管理员通过部署流程修正后刷新。",
      "peanut.notObserved": "尚未采样",
      "peanut.layer.app_config": "应用文件",
      "peanut.layer.installation": "SDK 安装",
      "peanut.layer.daemon": "SDK 进程",
      "peanut.layer.device": "设备",
      "peanut.layer.binding": "账号",
      "peanut.layer.https": "TLS / 网关证明",
      "peanut.intent": "自启意图",
      "peanut.intentSummary": "开机自启：{enabled} · 本次暂停：{paused}",
      "peanut.intentValue": "{enabled}；本次开机暂停：{paused}；修订 {revision}",
      "peanut.job": "任务 {id}：{status}{reason}",
      "peanut.working": "正在处理…",
      "peanut.accepted": "已受理 {id}，尚未完成；断连后请从局域网查询。",
      "peanut.saved": "地址已登记，尚未验证。",
      "peanut.copied": "地址已复制。",
      "peanut.webOnly": "此公网地址仅支持 Web，不是原生 SSH。请使用板子的局域网地址及已配置的用户名/端口；Web 终端是本地 shell。",
      "peanut.state.valid": "有效",
      "peanut.state.invalid": "无效",
      "peanut.state.missing": "未配置",
      "peanut.state.unreadable": "不可读",
      "peanut.state.unsafe_permissions": "权限不安全",
      "peanut.state.changed_pending_restart": "文件已变更，需显式重启",
      "peanut.state.installed": "已安装固定 SDK",
      "peanut.state.not_installed": "未安装",
      "peanut.state.incompatible": "不兼容",
      "peanut.state.running": "运行中",
      "peanut.state.stopped": "已停止",
      "peanut.state.starting": "启动中",
      "peanut.state.stopping": "停止中",
      "peanut.state.failed": "失败",
      "peanut.state.unknown": "未知",
      "peanut.state.online": "设备在线（不代表已绑定）",
      "peanut.state.offline": "离线",
      "peanut.state.logging_in": "设备登录中",
      "peanut.state.retrying": "正在重试",
      "peanut.state.bound": "已绑定（核验过的适配规则）",
      "peanut.state.unbound": "未绑定",
      "peanut.state.waiting": "等待绑定",
      "peanut.state.expired": "已过期",
      "peanut.state.cancelled": "本地已取消",
      "peanut.state.unverified": "未验证",
      "peanut.state.endpoint_ready": "TLS 与网关 challenge 一致",
      "peanut.state.stale": "证据已过期",
      "peanut.state.enabled": "已启用",
      "peanut.state.disabled": "已禁用",
      "peanut.state.yes": "是",
      "peanut.state.no": "否",
      "peanut.state.queued": "等待执行",
      "peanut.state.succeeded": "已完成",
      "peanut.state.recovery_required": "需先查询状态再恢复",
      "experience.headline": "一块 Pi M2，你的个人云工作台。",
      "login.systemHint": "使用开发板系统账号密码登录（PAM）。修改系统密码后门户登录同步生效。",
      "experience.boundaryPublic": "公网 HTTPS 门户。既有 Web 权限不变；HTTPS 本身不代表每项功能均已验收。",
      "login.brandSubPublic": "Quectel Pi M2 · HTTPS 门户",
      "login.footNetworkPublic": "公网 HTTPS 访问。密码登录保留尝试次数限制。",
      "login.hashHint": "使用已配置的门户管理员密码登录。",
      "login.fallbackHint": "PAM 不可用时，可使用已配置的管理员后备密码。",
      "experience.intro": "管理文件、托管网站、运行命令，操作你自己的真实工作空间。",
      "experience.diagram": "节点示意图 · 非实时设备状态",
      "experience.boundary": "仅 HTTP。请使用受信任的本地网络；当前连接未经加密。",
      "experience.terminalOff": "Web 终端未安装或已禁用。请使用自己的密钥连接 SSH。",
      "dashboard.unknown": "未知",
      "dashboard.servicesUnavailable": "服务状态刷新失败，先前显示的数值可能已过期。请点刷新重试；在板子上用 systemctl status 检查。",
      "embed.loading": "正在检查应用路由…",
      "embed.documentReady": "应用页面已响应；不代表文件操作完成或站点已经发布。",
      "embed.terminalDocument": "终端页面已响应，并不代表 shell 已连接。请查看终端自身的连接/重连提示。这是浏览器本地 shell，不是 SSH。",
      "embed.sessionExpired": "需要登录或会话已过期。请重新登录后再次打开工作区。",
      "embed.networkError": "应用检查失败或超时。请检查网络连接，然后点刷新重试。",
      "common.languageEnglish": "English",
      "common.languageChinese": "简体中文",
      "common.languageGroup": "语言",
      "common.preferenceNotSaved": "本页语言已切换，但浏览器无法保存此偏好设置。",
      "login.pageTitle": "登录 · Pi M2 个人云主机",
      "login.brand": "个人云主机",
      "login.brandSub": "Quectel Pi M2",
      "login.authHint": "使用为此门户配置的账号和密码登录。",
      "login.username": "账号",
      "login.password": "密码",
      "login.submit": "登录",
      "login.submitting": "登录中…",
      "login.error.required": "请输入账号与密码。",
      "login.error.rateLimit": "尝试过于频繁，请 {seconds} 秒后再试。",
      "login.error.rateLimitLater": "尝试过于频繁，请稍后再试。",
      "login.error.credentials": "账号或密码错误。",
      "login.error.failure": "登录失败（{code}）。",
      "login.error.network": "网络异常，请确认与开发板处于同一局域网。",
      "login.footNetwork": "仅限局域网访问；密码登录受失败次数限制保护。",
      "login.footSsh": "SSH 仅支持密钥登录；登录后可在顶栏「SSH」查看并复制完整命令。",
      "dashboard.pageTitle": "个人云主机 · Pi M2",
      "dashboard.brand": "个人云主机",
      "dashboard.navLabel": "主要入口",
      "dashboard.overview": "概览",
      "dashboard.files": "文件",
      "dashboard.site": "站点",
      "remote.title": "远程访问",
      "remote.refresh": "刷新",
      "remote.noLiveProbe": "请查看下方 PHTunnel 独立分层状态。进程运行或设备在线不代表绑定成功或外网可达。",
      "remote.statusLabel": "远程访问状态",
      "remote.provider": "配置的方案",
      "remote.intent": "配置启用意图",
      "remote.connection": "连接状态",
      "remote.https": "HTTPS 入口",
      "remote.reachability": "外网可达性",
      "remote.updated": "响应生成时间",
      "remote.stale": "运行状态未知或已过期；尚未核实真实客户端探测。",
      "remote.loadFailed": "远程状态加载失败，先前显示的值可能已过期。",
      "remote.value.none": "无",
      "remote.value.tailscale": "Tailscale",
      "remote.value.zerotier": "ZeroTier",
      "remote.value.peanut": "花生壳",
      "remote.value.unknown": "未知",
      "remote.value.unverified": "未验证",
      "remote.value.verified": "已验证",
      "remote.value.stale": "已过期",
      "remote.value.connected": "已连接",
      "remote.value.connecting": "连接中",
      "remote.value.disconnected": "未连接",
      "remote.value.degraded": "降级",
      "remote.value.failed": "失败",
      "remote.value.endpoint_ready": "入口就绪",
      "remote.value.configuring": "配置中",
      "remote.value.needs_authorization": "待授权",
      "remote.value.off": "关闭",
      "remote.value.configured": "已配置",
      "remote.value.enabled": "已启用",
      "remote.value.disabled": "已停用",
      "remote.value.not_enabled": "未启用",
      "dashboard.ssh": "SSH",
      "dashboard.terminal": "终端",
      "dashboard.logout": "退出",
      "dashboard.systemStatus": "系统状态",
      "dashboard.connecting": "连接中…",
      "dashboard.live": "实时",
      "dashboard.disconnected": "连接中断",
      "dashboard.retrying": "重试中…",
      "dashboard.cpu": "CPU",
      "dashboard.load": "负载",
      "dashboard.memory": "内存",
      "dashboard.temperature": "温度",
      "dashboard.socTemperature": "SoC 温度",
      "dashboard.disk": "磁盘",
      "dashboard.primaryInterface": "主网卡",
      "dashboard.download": "下行",
      "dashboard.upload": "上行",
      "dashboard.uptime": "运行时长",
      "dashboard.days": "{count}天",
      "dashboard.hours": "{count}小时",
      "dashboard.minutes": "{count}分",
      "dashboard.seconds": "{count}秒",
      "dashboard.services": "服务",
      "dashboard.refresh": "刷新",
      "dashboard.service": "服务",
      "dashboard.state": "状态",
      "dashboard.enabledAtBoot": "开机自启",
      "dashboard.action": "操作",
      "dashboard.loading": "加载中…",
      "dashboard.noServices": "无服务信息",
      "dashboard.enabled": "已启用",
      "dashboard.disabled": "未启用",
      "dashboard.running": "运行中",
      "dashboard.stopped": "已停止",
      "dashboard.failed": "失败",
      "dashboard.starting": "启动中",
      "dashboard.restart": "重启",
      "dashboard.restarting": "重启中…",
      "dashboard.restartSubmitted": "已提交重启：{name}，约 3 秒后刷新状态。",
      "dashboard.restartFailed": "重启失败：{code}",
      "dashboard.restartRequestFailed": "重启请求失败，请稍后再试。",
      "dashboard.links": "入口",
      "dashboard.filesCard": "文件",
      "dashboard.fileActions": "上传 / 下载 / 预览 / 重命名 / 删除",
      "dashboard.siteCard": "我的站点",
      "dashboard.siteDescription": "静态网站：把 HTML 放进 ~/cloud/www，浏览器打开 /site/ 即可发布",
      "dashboard.terminalCard": "Web 终端",
      "dashboard.terminalDescription": "浏览器中的本地 shell，不是 SSH；复用统一鉴权",
      "dashboard.sshCard": "SSH 入口",
      "dashboard.sshDescription": "仅密钥登录 · 复制命令到本机终端执行",
      "dashboard.openSsh": "打开 SSH",
      "dashboard.copyCommand": "复制命令",
      "dashboard.copied": "已复制",
      "dashboard.copyFailed": "复制失败，请手动选择",
      "dashboard.entryNote": "文件 / 站点 / 终端通过 Caddy 在本页打开。文件和终端需门户登录；站点访问遵循 site_public 配置。",
      "embed.filesUnavailable": "文件服务未响应（cloudhost-files）。请在板子上执行：systemctl status cloudhost-files",
      "embed.siteUnavailable": "站点目录不可读。请确认 ~/cloud/www 存在，且 Caddy 用户有读取权限。",
      "embed.terminalUnavailable": "终端服务未响应（cloudhost-ttyd）。请在板子上执行：systemctl status cloudhost-ttyd",
      "embed.gatewayMissing": "网关没有把 {path} 转发到后端，请求落到了门户兜底。取得部署授权后，在板子上执行 sudo ./install.sh && sudo systemctl restart caddy 重新渲染配置；自定义首页请加 --keep-site。",
      "embed.themeUnavailable": "无法访问嵌入页面以同步主题，已保留其当前外观。",
      "embed.backendUnavailable": "后端服务不可达",
      "embed.httpError": "{message}（HTTP {status}）",
      "embed.refresh": "刷新",
      "embed.reloadPage": "刷新页面",
      "embed.sitePreview": "站点预览",
      "embed.openSiteTab": "在新标签打开",
      "embed.backOverview": "返回概览",
      "embed.frameTitle": "内嵌应用",
      "dashboard.footerPrefix": "局域网版 · 仅监听 HTTP 80 · 状态刷新 ",
      "dashboard.footerSuffix": " ms",
      "site.pageTitle": "我的站点 · Quectel Pi M2",
      "site.brand": "Quectel Pi M2 · 个人云主机",
      "site.ready": "你的静态站点已就绪",
      "site.introBefore": "这个页面来自板子上的 ",
      "site.introAfter": "，由 Caddy 通过 ",
      "site.introEnd": " 对外提供。它只是占位首页 —— 替换成你自己的页面即可。",
      "site.publishTitle": "怎么发布自己的页面",
      "site.publishFiles": "在门户顶栏点「文件」，进入 ",
      "site.orSsh": " 目录；或者用 SSH 登录板子。",
      "site.replacePageBefore": "把 ",
      "site.replacePageAfter": " 替换成你的页面，CSS / JS / 图片放在同一目录或子目录。",
      "site.openSiteBefore": "浏览器打开 ",
      "site.subdirectoryAfter": "；子目录对应 ",
      "site.suitableTitle": "适合放什么",
      "site.suitableContentBefore": "纯静态内容：个人主页、项目文档、演示页、下载页。需要后端接口的页面请放到其它服务，或改用门户的 ",
      "site.suitableContentAfter": " 接口。",
      "site.backPortal": "← 返回个人云主机门户",
      "site.heroTitle": "一块小板子，你自己的 Web 空间。",
      "site.heroText": "运行在 Quectel Pi M2 上的个人项目展示站。浏览网站、体验纯前端应用，再发布你自己的页面。",
      "site.navigation": "站点内容导航",
      "site.aboutTitle": "关于这台主机",
      "site.aboutText": "目标平台为 Quectel Pi M2 / RK3576 / Debian 13。Caddy 通过 /site/ 路径提供此静态站点。这是平台介绍，不是实时设备检测数据。",
      "site.boundary": "门户负责状态、文件与终端；这个站点展示个人主页、项目文档与静态应用，无需新后端服务，也不依赖互联网连接。",
      "site.projectsTitle": "项目展示",
      "site.profileCard": "个人主页",
      "site.profileText": "用自己的页面介绍开发板与个人项目。",
      "site.appCard": "纯前端小应用",
      "site.appText": "编辑 Markdown，立即查看安全的渲染预览。",
      "site.guideCard": "网站发布手记",
      "site.guideText": "在板子上走完从修改文件到网站更新的真实流程。",
      "site.markdownTitle": "Markdown 体验区",
      "site.markdownHelp": "支持标题、段落、列表、引用、围栏代码块和链接。原始 HTML 按文本显示，不安全或格式错误的链接保留为文本。这是基础演示，不是完整 Markdown 实现。输入仅在本页处理，不会保存或发送到服务器。",
      "site.editor": "Markdown 源文",
      "site.preview": "实时预览",
      "site.reset": "恢复示例",
      "site.empty": "输入 Markdown，即可查看预览。",
      "site.example": "# 我的 Pi M2 项目\n\n由自己的开发板托管的个人网站。\n\n## 可以试试\n\n- 修改这个标题\n- 发布自己的页面\n\n1. 保存页面\n2. 刷新浏览器\n\n> 你的局域网，你自己的站点。\n\n```text\n# 代码保持为文本\necho \"Hello from Pi M2\"\n```\n\n[发布指引](#publish)",
      "site.publishLocate": "从门户进入「文件」或通过 SSH 登录。找到配置中的 site_dir，默认是 ~/cloud/www；文件入口为用户主目录时，进入 cloud/www。",
      "site.publishBackup": "先把 index.html 备份到公开站点目录之外。修改标题或换成自己的 HTML；CSS、JS 和图片放在站点目录内，使用相对链接。",
      "site.publishRefresh": "保存后刷新 /site/，现场查看变化，无需重启服务。如有必要，绕过浏览器缓存刷新。",
      "site.publishSafety": "部署默认安装当前版本的演示首页。已有 index.html 与模板不同时，会先备份到 /var/backups/cloudhost-site 再替换；其他站点文件保留。重新部署自定义站点时请加 --keep-site，保留自己的首页。公开站点中不要放置敏感信息。",
      "site.downloadsTitle": "示例资料下载",
      "site.downloadHelp": "下载当前语言的项目介绍或预置 Markdown 示例。UTF-8 文件由浏览器生成，不是从文件服务拉取，也不会包含你在编辑框中的内容。",
      "site.downloadIntro": "下载项目介绍（.txt）",
      "site.downloadMarkdown": "下载示例（.md）",
      "site.downloadStarted": "已请求下载，请查看浏览器下载列表。",
      "site.downloadFailed": "无法发起下载，请检查浏览器下载权限后重试。"
    }
  };

  var language = "en";
  var storageAvailable = true;
  var storageFailureReported = false;
  var authHintOriginal = null;

  function isValidLanguage(value) {
    return VALID_LANGUAGES.indexOf(value) !== -1;
  }

  function getStorage() {
    try {
      var storage = window.localStorage;
      if (!storage) { storageAvailable = false; }
      return storage;
    } catch (error) {
      storageAvailable = false;
      return null;
    }
  }

  function reportStorageFailure() {
    if (!storageFailureReported) {
      storageFailureReported = true;
      if (window.console && typeof window.console.warn === "function") {
        window.console.warn("Cloudhost language preference could not be stored.");
      }
    }
    var status = document.querySelector("[data-i18n-status]");
    if (status) {
      status.textContent = messages[language]["common.preferenceNotSaved"];
      status.hidden = false;
    }
  }

  function readSavedLanguage() {
    var storage = getStorage();
    if (!storage) { return "en"; }
    try {
      var value = storage.getItem(STORAGE_KEY);
      if (value === "zh-CN") {
        try {
          storage.setItem(STORAGE_KEY, "zh");
        } catch (error) {
          storageAvailable = false;
        }
        return "zh";
      }
      return isValidLanguage(value) ? value : "en";
    } catch (error) {
      storageAvailable = false;
      return "en";
    }
  }

  function translate(key, params) {
    var dictionary = messages[language];
    if (!Object.prototype.hasOwnProperty.call(dictionary, key)) {
      if (window.console && typeof window.console.error === "function") {
        window.console.error("Missing Cloudhost translation: " + key);
      }
      return "";
    }
    var value = dictionary[key];
    return value.replace(/\{([a-zA-Z0-9_]+)\}/g, function (match, name) {
      return params && Object.prototype.hasOwnProperty.call(params, name)
        ? String(params[name])
        : match;
    });
  }

  function setTranslatedText(node, key) {
    var value = translate(key);
    if (!value && !Object.prototype.hasOwnProperty.call(messages[language], key)) { return; }

    if (node.childNodes.length === 0) {
      node.textContent = value;
      return;
    }
    for (var i = 0; i < node.childNodes.length; i += 1) {
      if (node.childNodes[i].nodeType === 3) {
        node.childNodes[i].nodeValue = value;
        return;
      }
    }
    node.insertBefore(document.createTextNode(value), node.firstChild);
  }

  function apply(root) {
    var scope = root || document;
    var selectors = [
      ["data-i18n", null],
      ["data-i18n-title", "title"],
      ["data-i18n-aria-label", "aria-label"]
    ];
    for (var i = 0; i < selectors.length; i += 1) {
      var attribute = selectors[i][0];
      var outputAttribute = selectors[i][1];
      var nodes = [];
      if (scope.nodeType === 1 && scope.hasAttribute(attribute)) { nodes.push(scope); }
      var descendants = scope.querySelectorAll("[" + attribute + "]");
      for (var j = 0; j < descendants.length; j += 1) { nodes.push(descendants[j]); }
      for (var k = 0; k < nodes.length; k += 1) {
        var key = nodes[k].getAttribute(attribute);
        var value = translate(key);
        if (!Object.prototype.hasOwnProperty.call(messages[language], key)) { continue; }
        if (outputAttribute) {
          nodes[k].setAttribute(outputAttribute, value);
        } else {
          setTranslatedText(nodes[k], key);
        }
      }
    }
    updateAuthHint();
    updateLanguageControls();
    updateStorageNotice();
  }

  function updateAuthHint() {
    var hint = document.getElementById("auth-hint");
    if (!hint) { return; }
    if (authHintOriginal === null) { authHintOriginal = hint.textContent; }
    var context = hint.closest("[data-auth-mode]");
    if (!context) {
      hint.textContent = language === "zh" ? authHintOriginal : translate("login.authHint");
      return;
    }
    hint.textContent = language === "zh" ? authHintOriginal :
      translate(context.dataset.authMode === "system" ? "login.systemHint" : "login.hashHint") +
      (context.dataset.authFallback === "true" ? " " + translate("login.fallbackHint") : "");
  }

  function updateLanguageControls() {
    var buttons = document.querySelectorAll("[data-language]");
    for (var i = 0; i < buttons.length; i += 1) {
      var selected = buttons[i].getAttribute("data-language") === language;
      buttons[i].setAttribute("aria-pressed", selected ? "true" : "false");
    }
  }

  function updateStorageNotice() {
    var status = document.querySelector("[data-i18n-status]");
    if (!status) { return; }
    if (storageAvailable) {
      status.hidden = true;
      status.textContent = "";
    } else {
      status.textContent = translate("common.preferenceNotSaved");
      status.hidden = false;
    }
  }

  function dispatchLanguageChange() {
    window.dispatchEvent(new CustomEvent("cloudhost:languagechange", {
      detail: { language: language }
    }));
  }

  function setLanguage(value, persist) {
    if (!isValidLanguage(value)) {
      throw new TypeError("Unsupported Cloudhost language: " + value);
    }
    language = value;
    document.documentElement.lang = language;
    apply(document);
    if (persist !== false) {
      var storage = getStorage();
      if (storage) {
        try {
          storage.setItem(STORAGE_KEY, language);
        } catch (error) {
          storageAvailable = false;
          reportStorageFailure();
        }
      }
    }
    updateStorageNotice();
    dispatchLanguageChange();
  }

  language = readSavedLanguage();
  document.documentElement.lang = language;
  document.documentElement.classList.add("i18n-pending");

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-language]");
    if (!button) { return; }
    setLanguage(button.getAttribute("data-language"));
  });

  window.addEventListener("storage", function (event) {
    if (event.key !== STORAGE_KEY && event.key !== null) { return; }
    var nextLanguage = event.newValue === "zh-CN"
      ? "zh"
      : isValidLanguage(event.newValue) ? event.newValue : "en";
    if (nextLanguage !== language) { setLanguage(nextLanguage, false); }
  });

  function initializePage() {
    apply(document);
    document.documentElement.classList.remove("i18n-pending");
    if (!storageAvailable) { reportStorageFailure(); }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializePage, { once: true });
  } else {
    initializePage();
  }

  window.CloudhostI18n = {
    getLanguage: function () { return language; },
    setLanguage: setLanguage,
    t: translate,
    apply: apply
  };
})(window, document);
