#!/usr/bin/env bash
# =============================================================================
# Quectel Pi M2 Personal Cloud · One-click installer (docs/35-个人云主机.md v1.0)
#
# Usage:
#   sudo ./install.sh                       # Set the portal password interactively and install
#   sudo ./install.sh --password 'xxx'      # Non-interactive (CI / reproducible install)
#   sudo ./install.sh --uninstall           # Uninstall; keep ~/cloud
#   sudo ./install.sh --help
#
# Acceptance criteria:
#   A5 All services are ready within 60 seconds after reboot (systemd enable + dependencies)
#   A6 A fresh system can be installed by running this script once
# =============================================================================
set -Eeuo pipefail
export LC_ALL=C LANG=C

APP_NAME="cloudhost"
INSTALL_DIR="/opt/cloudhost"
ETC_DIR="/etc/cloudhost"
SECRET_DIR="${ETC_DIR}/secret"
LOG_DIR="/var/log/cloudhost"
STATE_DIR="/var/lib/cloudhost"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORTAL_SRC="${REPO_DIR}/portal"
DEPLOY_SRC="${REPO_DIR}/deploy"
TOOLS_SRC="${REPO_DIR}/tools"

RUN_USER="${SUDO_USER:-${USER}}"
CLOUD_ROOT=""
ADMIN_USER="admin"
ADMIN_PASSWORD=""
ENABLE_TTYD=1
HARDEN_SSH=1
ASSUME_YES=0
DO_UNINSTALL=0
PRIMARY_IFACE=""
FILE_ROOT_MODE=""     # home (default) | cloud (legacy behavior, for rollback)
AUTH_MODE=""          # system | password_hash (empty = detect via PAM)
KEEP_SITE=0
WITH_PEANUT=0
SITE_BACKUP_ROOT="/var/backups/cloudhost-site"
FILE_BROWSER_VERSION="${FILE_BROWSER_VERSION:-2.31.2}"
TTYD_VERSION="${TTYD_VERSION:-1.7.7}"
CADDY_VERSION_DEFAULT="2.9.1"

# --------------------------------------------------------------------------- #
# 输出工具
# --------------------------------------------------------------------------- #
if [[ -t 1 ]]; then
	C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_INFO=$'\033[36m'; C_OFF=$'\033[0m'
else
	C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_OFF=""
fi

log()  { printf '%s[cloudhost]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
ok()   { printf '%s[  ok  ]%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s[warn  ]%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s[error ]%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }

trap 'die "Command failed at line ${LINENO}; installation aborted. Check the log above."' ERR

usage() {
	awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next }
	     NR > 1 && /^$/ { print; next }
	     NR > 1 { exit }' "${BASH_SOURCE[0]}"
	cat <<-EOF

Options:
  --user NAME           Service account (default: sudo caller, ${RUN_USER})
  --cloud-root PATH     Site root directory (default: ~/cloud)
  --file-root {home,cloud}  File entry root policy (default: home; cloud is legacy)
  --auth-mode {system,password_hash}  Force sign-in mode (default: detect via PAM)
  --admin-user NAME     Portal sign-in account (default: admin)
  --password PASS       Set portal password non-interactively (prompt if omitted)
  --primary-iface IFACE Primary network interface for status (default: auto-detect)
  --no-ttyd             Do not install the Web terminal (E1)
  --no-ssh-harden       Do not modify SSH configuration (not recommended)
  --keep-site           Keep the existing site instead of installing the demo homepage
  --with-peanut         Install pinned ARM64 SDK/controller; use private sidecar or securely prompt
  --yes                 Skip confirmation
  --uninstall           Uninstall (keep ~/cloud and backup files)
  -h, --help            Show this help
EOF
}

# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
while [[ $# -gt 0 ]]; do
	case "$1" in
		--user) RUN_USER="${2:?}"; shift 2 ;;
		--cloud-root) CLOUD_ROOT="${2:?}"; shift 2 ;;
		--file-root) FILE_ROOT_MODE="${2:?}"; shift 2 ;;
		--auth-mode) AUTH_MODE="${2:?}"; shift 2 ;;
		--admin-user) ADMIN_USER="${2:?}"; shift 2 ;;
		--password) ADMIN_PASSWORD="${2:?}"; shift 2 ;;
		--primary-iface) PRIMARY_IFACE="${2:?}"; shift 2 ;;
		--no-ttyd) ENABLE_TTYD=0; shift ;;
		--no-ssh-harden) HARDEN_SSH=0; shift ;;
		--keep-site) KEEP_SITE=1; shift ;;
		--with-peanut) WITH_PEANUT=1; shift ;;
		--yes|-y) ASSUME_YES=1; shift ;;
		--uninstall) DO_UNINSTALL=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) die "Unknown option: $1 (run --help for usage)" ;;
	esac
done

case "${FILE_ROOT_MODE:-home}" in
	home|cloud) ;;
	*) die "--file-root must be home or cloud; got: ${FILE_ROOT_MODE}" ;;
esac
case "${AUTH_MODE:-}" in
	""|system|password_hash) ;;
	*) die "--auth-mode must be system or password_hash; got: ${AUTH_MODE}" ;;
esac

need_root() {
	[[ ${EUID} -eq 0 ]] || die "Run this installer as root: sudo ./install.sh"
}

arch_triplet() {
	case "$(uname -m)" in
		aarch64|arm64) echo "arm64" ;;
		x86_64|amd64) echo "amd64" ;;
		armv7l) echo "armv7" ;;
		*) die "Unsupported architecture: $(uname -m)" ;;
	esac
}

# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #
uninstall() {
	need_root
	RUN_USER_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6 || true)"
	RUN_USER_HOME="${RUN_USER_HOME:-/home/${RUN_USER}}"
	local cloud="${CLOUD_ROOT:-${RUN_USER_HOME}/cloud}"

	log "Stopping and disabling services"
	for unit in cloudhost-peanut cloudhost-remote-gateway cloudhost-peanut-control; do
		if [[ -f "/etc/systemd/system/${unit}.service" ]]; then
			systemctl disable --now "${unit}.service"
			rm -f "/etc/systemd/system/${unit}.service"
		fi
	done
	if [[ -f /var/lib/cloudhost-peanut-control/control.sqlite3 && -x "${INSTALL_DIR}/venv/bin/python" ]]; then
		PYTHONPATH="${INSTALL_DIR}/portal" "${INSTALL_DIR}/venv/bin/python" -c '
from app.peanut_control import Controller
controller = Controller()
with controller.db:
    controller.put("enabled", False)
    controller.invalidate()
controller.close()
'
	fi
	rm -f /etc/logrotate.d/cloudhost-peanut
	warn "Peanut application credentials, SDK identity, private logs and control history are retained in their independent directories. Revoke mappings in the official console."
	for unit in cloudhost-ttyd cloudhost-files cloudhost-portal; do
		systemctl disable --now "${unit}.service" >/dev/null 2>&1 || true
		rm -f "/etc/systemd/system/${unit}.service"
	done
	systemctl disable --now caddy >/dev/null 2>&1 || true
	rm -f /etc/caddy/Caddyfile
	rm -rf /etc/caddy/conf.d
	rm -f /etc/pam.d/cloudhost

	log "Removing fail2ban configuration"
	rm -f /etc/fail2ban/filter.d/cloudhost-caddy.conf
	rm -f /etc/fail2ban/jail.d/cloudhost.conf
	systemctl reload fail2ban >/dev/null 2>&1 || true

	log "Removing logrotate and SSH hardening configuration"
	rm -f /etc/logrotate.d/cloudhost
	if [[ -f /etc/ssh/sshd_config.d/90-cloudhost.conf ]]; then
		rm -f /etc/ssh/sshd_config.d/90-cloudhost.conf
		systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
	fi

	log "Removing the application and runtime data (see design document, section 7)"
	rm -rf "${INSTALL_DIR}" "${STATE_DIR}"
	rm -rf "${LOG_DIR}"
	rm -rf "${ETC_DIR}"
	systemctl daemon-reload

	warn "User data was preserved at ${cloud} (remove it manually with rm -rf if desired)"
	ok "Uninstallation complete."
}

if [[ ${DO_UNINSTALL} -eq 1 ]]; then
	uninstall
	exit 0
fi

# --------------------------------------------------------------------------- #
# 前置检查
# --------------------------------------------------------------------------- #
need_root

[[ -f /etc/os-release ]] || die "Cannot find /etc/os-release; unable to identify the operating system"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
	debian|ubuntu|raspbian) ;;
	*) warn "Detected ${PRETTY_NAME:-unknown}; this installer was validated on Debian 13, continuing may be risky" ;;
esac

[[ -d "${PORTAL_SRC}/app" ]] || die "Portal source not found: ${PORTAL_SRC}/app"
[[ -f "${DEPLOY_SRC}/Caddyfile.tmpl" ]] || die "Deployment templates not found: ${DEPLOY_SRC}"

if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
	die "User ${RUN_USER} does not exist. Use --user to select an existing account."
fi
RUN_USER_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
RUN_USER_HOME="${RUN_USER_HOME:-/home/${RUN_USER}}"
CLOUD_ROOT="${CLOUD_ROOT:-${RUN_USER_HOME}/cloud}"
SITE_DIR="${CLOUD_ROOT}/www"

[[ "${CLOUD_ROOT}" != "/" ]] || die "cloud_root cannot be /"
case "${CLOUD_ROOT}" in
	/*) ;;
	*) die "cloud_root must be an absolute path; got: ${CLOUD_ROOT}" ;;
esac

cat <<-EOF

  Quectel Pi M2 Personal Cloud v1.0 (LAN edition)
  ------------------------------------------------------------
  Service user  : ${RUN_USER} (${RUN_USER_HOME})
  Site root     : ${CLOUD_ROOT}
  Portal account: ${ADMIN_USER}
  Web terminal  : $([[ ${ENABLE_TTYD} -eq 1 ]] && echo enabled || echo disabled)
  SSH hardening : $([[ ${HARDEN_SSH} -eq 1 ]] && echo enabled || echo skipped)
  Listen ports  : 80 (Caddy) / 127.0.0.1:8080,8081$([[ ${ENABLE_TTYD} -eq 1 ]] && echo ",7681")
  ------------------------------------------------------------

EOF

if [[ ${ASSUME_YES} -eq 0 ]]; then
	read -r -p "Continue with installation? [y/N] " reply
	[[ "${reply}" =~ ^[Yy]$ ]] || die "Installation cancelled."
fi

# --------------------------------------------------------------------------- #
# 1. 系统依赖
# --------------------------------------------------------------------------- #
log "1/8 Installing system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# acl 提供 setfacl：Caddy 以 caddy 用户运行，需要给 ~/cloud/www 放行读取权限
PACKAGES=(python3 python3-venv python3-pip curl ca-certificates jq fail2ban logrotate openssh-server acl)
apt-get install -y -qq "${PACKAGES[@]}" >/dev/null

install_ttyd_binary() {
	install -d -m 0755 "${INSTALL_DIR}/bin"
	[[ -x "${INSTALL_DIR}/bin/ttyd" ]] && return 0
	if apt-get install -y -qq ttyd >/dev/null 2>&1; then
		install -m 0755 "$(command -v ttyd)" "${INSTALL_DIR}/bin/ttyd"
	fi
	if [[ ! -x "${INSTALL_DIR}/bin/ttyd" ]]; then
		case "$(uname -m)" in
			aarch64|arm64) suffix="aarch64" ;;
			x86_64|amd64)  suffix="x86_64" ;;
			*)             suffix="" ;;
		esac
		if [[ -n "${suffix}" ]]; then
			tmp="$(mktemp -d)"
			for name in "ttyd.${suffix}" "ttyd_${suffix}" "ttyd-${suffix}"; do
				if curl -fsSL --max-time 60 \
					"https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${name}" \
					-o "${tmp}/ttyd" 2>/dev/null; then
					install -m 0755 "${tmp}/ttyd" "${INSTALL_DIR}/bin/ttyd"
					break
				fi
			done
			rm -rf "${tmp}"
		fi
	fi
	"${INSTALL_DIR}/bin/ttyd" --version >/dev/null 2>&1
}

if [[ ${ENABLE_TTYD} -eq 1 ]]; then
	if ! install_ttyd_binary; then
		warn "Unable to install ttyd from apt or the GitHub static binary; skipping E1"
		ENABLE_TTYD=0
	elif ! "${INSTALL_DIR}/bin/ttyd" --version 2>&1 | grep -Eq '1\.7\.7([^-0-9.]|[-]|$)'; then
		warn "ttyd client theme is verified for 1.7.7; this installed version requires a compatibility check"
	fi
fi

# --------------------------------------------------------------------------- #
# 2. Caddy
# --------------------------------------------------------------------------- #
log "2/8 Installing Caddy 2"
if ! command -v caddy >/dev/null 2>&1; then
	if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
		warn "Unable to install Caddy from apt; falling back to the official arm64/amd64 static binary"
		TRIPLET="$(arch_triplet)"
		CADDY_VER="${CADDY_VERSION:-${CADDY_VERSION_DEFAULT}}"
		URL="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VER}/caddy_${CADDY_VER}_linux_${TRIPLET}.tar.gz"
		tmp="$(mktemp -d)"
		curl -fsSL "${URL}" -o "${tmp}/caddy.tgz" || die "Failed to download Caddy: ${URL}"
		tar -xzf "${tmp}/caddy.tgz" -C "${tmp}" caddy
		install -m 0755 "${tmp}/caddy" /usr/local/bin/caddy
		rm -rf "${tmp}"
		# 静态二进制场景下自建 systemd 单元
		cat >/etc/systemd/system/caddy.service <<-'UNIT'
			[Unit]
			Description=Caddy (CloudHost)
			After=network-online.target
			Wants=network-online.target

			[Service]
			Type=notify
			User=root
			ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
			ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
			TimeoutStopSec=5s
			LimitNOFILE=1048576
			PrivateTmp=true
			ProtectSystem=full
			AmbientCapabilities=CAP_NET_BIND_SERVICE

			[Install]
			WantedBy=multi-user.target
		UNIT
		systemctl daemon-reload
	fi
fi
CADDY_BIN="$(command -v caddy)"
caddy version >/dev/null || die "Caddy installation failed"
ok "Caddy: $("${CADDY_BIN}" version | head -n1)"

# --------------------------------------------------------------------------- #
# 3. 目录与门户虚拟环境
# --------------------------------------------------------------------------- #
log "3/8 Deploying the portal to ${INSTALL_DIR}"
if [[ -f /etc/systemd/system/cloudhost-peanut-control.service ]]; then
	WITH_PEANUT=1
fi
if [[ ${WITH_PEANUT} -eq 1 ]]; then
	[[ "${RUN_USER}" != "root" ]] || die "--with-peanut requires a non-root portal user"
	[[ "$(uname -m)" == "aarch64" || "$(uname -m)" == "arm64" ]] ||
		die "--with-peanut supports only GNU ARM64"
	install -d -m 0750 -o root -g "${RUN_USER}" /run/cloudhost-peanut
	cat /proc/sys/kernel/random/boot_id > /run/cloudhost-peanut/paused
	chmod 0600 /run/cloudhost-peanut/paused
	for unit in cloudhost-peanut cloudhost-remote-gateway cloudhost-peanut-control; do
		if [[ -f "/etc/systemd/system/${unit}.service" ]]; then
			systemctl stop "${unit}.service"
		fi
	done
fi
install -d -m 0755 -o root -g root "${INSTALL_DIR}"
# File Browser 以 ${RUN_USER} 运行，其数据库目录必须属主一致；
# 否则 render_config.py（root 执行）会先建出 root:root 的目录，
# filebrowser 启动即报 "open .../filebrowser.db: permission denied"。
install -d -m 0750 -o "${RUN_USER}" -g "${RUN_USER}" \
	"${LOG_DIR}" "${STATE_DIR}" "${LOG_DIR}/filebrowser" "${STATE_DIR}/filebrowser"
# secret 目录属主 root:tris、权限 0750 —— portal 以 ${RUN_USER} 运行，
# 需要「进入目录 + 以组身份读取」才能加载密码哈希与会话密钥。
# 若用 0700，进程能拉起但登录恒失败（load_password_hash 静默返回 None），
# 排查成本极高，这里必须保留组可读可执行。
install -d -m 0750 -o root -g "${RUN_USER}" "${SECRET_DIR}"
chmod 0750 "${SECRET_DIR}"
install -d -m 0750 -o "${RUN_USER}" -g "${RUN_USER}" "${CLOUD_ROOT}" "${SITE_DIR}" "${CLOUD_ROOT}/upload"

# `/site/*` 由 Caddy 的 file_server 直接读盘，而 Caddy 以 caddy 用户运行。
# 用户家目录通常是 0700，caddy 无法穿越，/site/ 会一律 403。
# 这里用最小 ACL 精确放行：家目录只给 --x（可穿越、不可列目录），
# cloud/www 给 r-X（可读、可进入子目录），不改变原有 0750 权限位。
CADDY_RUN_USER="$(systemctl show caddy.service -p User --value 2>/dev/null || true)"
if [[ -n "${CADDY_RUN_USER}" && "${CADDY_RUN_USER}" != "root" ]]; then
	if command -v setfacl >/dev/null 2>&1; then
		setfacl -m "u:${CADDY_RUN_USER}:--x,g:caddy:--x" "${RUN_USER_HOME}" ||
			warn "Unable to add an ACL to ${RUN_USER_HOME}; /site/ may return 403"
		setfacl -R -m "u:${CADDY_RUN_USER}:rX,g:caddy:rX" "${CLOUD_ROOT}" ||
			warn "Unable to add an ACL to ${CLOUD_ROOT}; /site/ may return 403"
		# 默认 ACL 让用户之后新建的站点文件也自动可读
		find "${SITE_DIR}" -type d -exec setfacl -m "d:u:${CADDY_RUN_USER}:r-x,d:g:caddy:r-x" {} + ||
			die "Unable to configure default Caddy site ACLs"
		ok "Granted Caddy (${CADDY_RUN_USER}) access to ${SITE_DIR}"
	else
		warn "setfacl is unavailable (acl package missing); /site/ may return 403 if the home directory is mode 0700"
	fi
fi

# 默认更新演示首页；备份放在站点和文件服务根目录之外。
if [[ ${KEEP_SITE} -eq 1 ]]; then
	log "Keeping the existing site (--keep-site): ${SITE_DIR}"
else
	[[ -f "${DEPLOY_SRC}/site-index.html" ]] || die "Demo site template not found: ${DEPLOY_SRC}/site-index.html"
	[[ ! -L "${SITE_DIR}" && ! -L "${SITE_DIR}/index.html" ]] ||
		die "Refusing to replace a symlinked site; use --keep-site to preserve it"
	if [[ -e "${SITE_DIR}/index.html" ]]; then
		[[ -f "${SITE_DIR}/index.html" ]] || die "Site index.html is not a regular file"
		if ! cmp -s "${DEPLOY_SRC}/site-index.html" "${SITE_DIR}/index.html"; then
			install -d -m 0700 "${SITE_BACKUP_ROOT}"
			SITE_BACKUP_DIR="$(mktemp -d "${SITE_BACKUP_ROOT}/site.XXXXXXXX")"
			cp -p "${SITE_DIR}/index.html" "${SITE_BACKUP_DIR}/index.html"
			ok "Backed up the previous site homepage: ${SITE_BACKUP_DIR}/index.html"
		fi
	fi
	install -m 0644 -o "${RUN_USER}" -g "${RUN_USER}" \
		"${DEPLOY_SRC}/site-index.html" "${SITE_DIR}/index.html"
	ok "Installed the current demo site homepage: ${SITE_DIR}/index.html"
fi

# 三个源码目录都先删除再复制：cp -a 在目标已存在时会「嵌套复制」成
# /opt/cloudhost/tools/tools，导致脚本反复升级后目录结构错乱。
for src_dir in "${PORTAL_SRC}:portal" "${TOOLS_SRC}:tools" "${DEPLOY_SRC}:deploy"; do
	src="${src_dir%%:*}"
	dst="${INSTALL_DIR}/${src_dir##*:}"
	rm -rf "${dst}"
	cp -a "${src}" "${dst}"
done

if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
	python3 -m venv "${INSTALL_DIR}/venv"
fi
(
	umask 022
	"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
	"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${PORTAL_SRC}/requirements.txt"
)
if [[ ${WITH_PEANUT} -eq 1 || -f /etc/systemd/system/cloudhost-peanut-control.service ]]; then
	# The root controller must never import user-writable deployed Python code.
	chown -R root:root "${INSTALL_DIR}/portal" "${INSTALL_DIR}/tools" \
		"${INSTALL_DIR}/deploy" "${INSTALL_DIR}/venv"
	chmod -R go-w "${INSTALL_DIR}/portal" "${INSTALL_DIR}/tools" \
		"${INSTALL_DIR}/deploy" "${INSTALL_DIR}/venv"
fi
ok "Python dependencies installed ($( "${INSTALL_DIR}/venv/bin/python" -V ))"

# PAM 服务文件：仅用于门户网页登录校验；刻意不含 faillock，
# 避免门户失败次数把系统账号一起锁死（门户自身已有 5 次/300 秒限流）。
install -d -m 0755 /etc/pam.d
install -m 0644 "${DEPLOY_SRC}/pam-cloudhost.conf" /etc/pam.d/cloudhost

# --------------------------------------------------------------------------- #
# 4. File Browser 静态二进制
# --------------------------------------------------------------------------- #
log "4/8 Installing File Browser ${FILE_BROWSER_VERSION}"
if [[ ! -x "${INSTALL_DIR}/bin/filebrowser" ]]; then
	install -d -m 0755 "${INSTALL_DIR}/bin"
	TRIPLET="$(arch_triplet)"
	URL="https://github.com/filebrowser/filebrowser/releases/download/v${FILE_BROWSER_VERSION}/linux-${TRIPLET}-filebrowser.tar.gz"
	tmp="$(mktemp -d)"
	if curl -fsSL "${URL}" -o "${tmp}/fb.tgz" 2>/dev/null; then
		tar -xzf "${tmp}/fb.tgz" -C "${tmp}" filebrowser
		install -m 0755 "${tmp}/filebrowser" "${INSTALL_DIR}/bin/filebrowser"
		rm -rf "${tmp}"
		ok "File Browser installed"
	else
		rm -rf "${tmp}"
		warn "Failed to download File Browser (offline environment?). A2 can fall back to the portal's built-in /api/files endpoint"
		warn "Install it later at ${INSTALL_DIR}/bin/filebrowser and run systemctl start cloudhost-files"
	fi
fi

# --------------------------------------------------------------------------- #
# 5. 配置、密码、会话密钥
# --------------------------------------------------------------------------- #
log "5/8 Generating configuration and secrets"
RENDER=( "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/tools/render_config.py" )

CONFIG_ARGS=(
	config
	--output "${ETC_DIR}/config.yaml"
	--home "${RUN_USER_HOME}"
	--cloud-root "${CLOUD_ROOT}"
	--admin-user "${ADMIN_USER}"
	--etc-dir "${ETC_DIR}"
	--log-dir "${LOG_DIR}"
	--state-dir "${STATE_DIR}"
	--run-user "${RUN_USER}"
	--system-home "${RUN_USER_HOME}"
)
[[ -n "${PRIMARY_IFACE}" ]] && CONFIG_ARGS+=(--primary-iface "${PRIMARY_IFACE}")
[[ ${ENABLE_TTYD} -eq 0 ]] && CONFIG_ARGS+=(--no-ttyd)
[[ "${FILE_ROOT_MODE:-home}" == "cloud" ]] && CONFIG_ARGS+=(--file-root cloud)
# 已存在配置时保留，除非使用 --force（这里不做覆盖，避免升级丢配置）
"${RENDER[@]}" "${CONFIG_ARGS[@]}"

# 配置已存在时 render_config 只补缺失键、不覆盖已有取值；
# 显式 --file-root / --auth-mode 必须直接改这一行，保证升级与回滚幂等。
if [[ -n "${FILE_ROOT_MODE}" ]]; then
	sed -i "s/^file_root_mode:.*/file_root_mode: ${FILE_ROOT_MODE}/" "${ETC_DIR}/config.yaml"
fi
if [[ -n "${AUTH_MODE}" ]]; then
	sed -i "s/^auth_mode:.*/auth_mode: ${AUTH_MODE}/" "${ETC_DIR}/config.yaml"
fi

# ttyd unit 是否渲染取决于 config.yaml 的 ttyd_enabled。升级场景下旧配置可能是
# false（此前 apt 装不上 ttyd），而本次已装好二进制，必须同步打开；--no-ttyd 反之。
if [[ ${ENABLE_TTYD} -eq 1 ]]; then
	sed -i 's/^ttyd_enabled:.*/ttyd_enabled: true/' "${ETC_DIR}/config.yaml"
else
	sed -i 's/^ttyd_enabled:.*/ttyd_enabled: false/' "${ETC_DIR}/config.yaml"
fi

# 密码：已存在哈希且未指定新密码时保留
if [[ -n "${ADMIN_PASSWORD}" ]]; then
	printf '%s\n' "${ADMIN_PASSWORD}" | "${RENDER[@]}" hash --output "${SECRET_DIR}/admin.hash"
elif [[ ! -s "${SECRET_DIR}/admin.hash" ]]; then
	while true; do
		read -r -s -p "Set a password for portal account ${ADMIN_USER} (at least 8 characters): " pass1; echo
		read -r -s -p "Enter it again to confirm: " pass2; echo
		[[ ${#pass1} -ge 8 ]] || { warn "Password must be at least 8 characters"; continue; }
		[[ "${pass1}" == "${pass2}" ]] || { warn "Passwords do not match"; continue; }
		printf '%s\n' "${pass1}" | "${RENDER[@]}" hash --output "${SECRET_DIR}/admin.hash"
		break
	done
else
	log "Reusing the existing administrator password hash (reset with: sudo ${RENDER[*]} hash --output ${SECRET_DIR}/admin.hash)"
fi

"${RENDER[@]}" session --output "${SECRET_DIR}/session.key"
chown -R root:"${RUN_USER}" "${ETC_DIR}"
chmod 0750 "${ETC_DIR}" "${SECRET_DIR}"
chmod 0640 "${ETC_DIR}/config.yaml"
# 0640 而非 0600：portal 以 ${RUN_USER} 运行，靠组权限读取；
# 组写位保持关闭，仍然只有 root 能修改密码哈希与会话密钥。
chmod 0640 "${SECRET_DIR}/admin.hash" "${SECRET_DIR}/session.key"

# --------------------------------------------------------------------------- #
# 6. 渲染模板（Caddy / systemd / fail2ban / logrotate / File Browser）
# --------------------------------------------------------------------------- #
log "6/8 Rendering service configuration"
install -d -m 0755 /etc/caddy
if [[ -f /etc/caddy/Caddyfile && ! -f "${ETC_DIR}/Caddyfile.original" ]]; then
	cp -a /etc/caddy/Caddyfile "${ETC_DIR}/Caddyfile.original"
fi

"${RENDER[@]}" render \
	--config "${ETC_DIR}/config.yaml" \
	--caddyfile /etc/caddy/Caddyfile \
	--systemd-dir /etc/systemd/system \
	--fail2ban-dir /etc/fail2ban \
	--logrotate-dir /etc/logrotate.d \
	--filebrowser-config "${ETC_DIR}/filebrowser.json" \
	--home "${RUN_USER_HOME}" \
	--run-user "${RUN_USER}" >/dev/null

# File Browser runs in noauth mode with a shared user, so set its own locale
# to English instead of coupling it to each portal browser's language choice.
if [[ -f "${ETC_DIR}/filebrowser.json" ]]; then
	"${INSTALL_DIR}/venv/bin/python" \
		"${INSTALL_DIR}/tools/set_filebrowser_locale.py" "${ETC_DIR}/filebrowser.json"
fi

if ! "${CADDY_BIN}" validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>"${ETC_DIR}/caddy-validate.err"; then
	cat "${ETC_DIR}/caddy-validate.err" >&2 || true
	die "Caddy configuration validation failed (error code E2001); see ${ETC_DIR}/caddy-validate.err"
fi
ok "Caddy configuration validation passed"

# File Browser 以 ${RUN_USER} 运行，必须能读到 /etc/cloudhost/filebrowser.json。
# 该文件在第 6 步由 render（root 执行）生成，而第 5 步的 chown 已经结束；
# 若不在生成后修正属组，文件会是 root:root 0640，filebrowser 启动时
# 「permission denied」直接退出 1 → systemd 反复重启 → Caddy 对 /files/ 返回 502。
if [[ -f "${ETC_DIR}/filebrowser.json" ]]; then
	chown root:"${RUN_USER}" "${ETC_DIR}/filebrowser.json"
	chmod 0640 "${ETC_DIR}/filebrowser.json"
	if command -v runuser >/dev/null 2>&1; then
		runuser -u "${RUN_USER}" -- test -r "${ETC_DIR}/filebrowser.json" ||
			die "File Browser configuration is not readable by ${RUN_USER}: ${ETC_DIR}/filebrowser.json (check the group)"
	fi
fi

# 幂等修正：早期版本由 root 建出 ${STATE_DIR}/filebrowser（root:root 0755），
# 升级时不会自动改属主，这里强制拉回 ${RUN_USER}，否则 File Browser 无法建库。
if [[ -d "${STATE_DIR}/filebrowser" ]]; then
	chown -R "${RUN_USER}:${RUN_USER}" "${STATE_DIR}/filebrowser" ||
		warn "Unable to correct the owner of ${STATE_DIR}/filebrowser"
	chmod 0750 "${STATE_DIR}/filebrowser"
fi

if [[ ! -x "${INSTALL_DIR}/bin/filebrowser" ]]; then
	# 没有二进制时禁用 files 服务，避免开机进入 failed
	sed -i 's/^ExecStart=.*/ExecStart=\/bin\/true/' /etc/systemd/system/cloudhost-files.service
	warn "cloudhost-files was disabled because File Browser is not installed; use the portal's built-in /api/files endpoint instead"
fi

systemctl daemon-reload

if [[ ${WITH_PEANUT} -eq 1 ]]; then
	if ! getent passwd cloudhost-peanut >/dev/null; then
		useradd --system --user-group --home-dir /var/lib/cloudhost-peanut \
			--shell /usr/sbin/nologin cloudhost-peanut
	fi
	PEANUT_INSTALL_ARGS=(
		--user "${RUN_USER}" --caddy-bin "${CADDY_BIN}" --configure-app
	)
	PEANUT_CREDENTIALS="${REPO_DIR}/../peanut-app.deploy.json"
	[[ ! -e "${PEANUT_CREDENTIALS}" && ! -L "${PEANUT_CREDENTIALS}" ]] ||
		PEANUT_INSTALL_ARGS+=(--credentials-file "${PEANUT_CREDENTIALS}")
	"${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/tools/install_peanut.py" \
		"${PEANUT_INSTALL_ARGS[@]}"
	"${CADDY_BIN}" validate --config /etc/cloudhost-peanut/Caddyfile --adapter caddyfile
	systemctl daemon-reload
	systemctl enable --now cloudhost-peanut-control.service
	warn "Peanut SDK remains stopped. Use Remote to start it explicitly. Existing identity and autostart intent were preserved."
fi

# PAM 探测：NoNewPrivileges=true 会让 setgid 的 unix_chkpwd 失效，
# 必须实测而非假设。优先「补 shadow 组」，失败再关闭 NNP，最后回退 admin.hash。
pam_probe() {  # $1=NoNewPrivileges(yes|no)  $2=补充组（可空）
	local nnp="$1" groups="$2" unit="cloudhost-pamprobe-$$"
	local -a props=(--property="NoNewPrivileges=${nnp}" --property=Type=oneshot)
	[[ -n "${groups}" ]] && props+=(--property="SupplementaryGroups=${groups}")
	systemd-run --quiet --wait --pipe --collect --unit="${unit}" \
		--uid="${RUN_USER}" "${props[@]}" \
		"${INSTALL_DIR}/venv/bin/python" -c '
import sys
sys.path.insert(0, "/opt/cloudhost/portal")
from app.pam import availability
raise SystemExit(0 if availability(sys.argv[1]) else 3)
' "${RUN_USER}" >/dev/null 2>&1
}

PAM_MODE="system"
if [[ -n "${AUTH_MODE}" ]]; then
	PAM_MODE="${AUTH_MODE}"
	log "Sign-in mode forced by --auth-mode: ${PAM_MODE}"
elif [[ "${RUN_USER}" == "root" ]]; then
	PAM_MODE="password_hash"
	warn "The service runs as root; skipping PAM (unsafe) and using password_hash"
elif command -v systemd-run >/dev/null 2>&1 && getent group shadow >/dev/null 2>&1 && pam_probe yes shadow; then
	# 方案 A：保留 NoNewPrivileges=true，补充 shadow 组
	if ! grep -q '^SupplementaryGroups=shadow' /etc/systemd/system/cloudhost-portal.service; then
		sed -i '/^NoNewPrivileges=/i SupplementaryGroups=shadow' \
			/etc/systemd/system/cloudhost-portal.service
	fi
	ok "PAM is available (NoNewPrivileges=true + SupplementaryGroups=shadow)"
elif command -v systemd-run >/dev/null 2>&1 && pam_probe no ""; then
	# 方案 B：关闭 NNP，允许 setgid helper 提升
	sed -i 's/^NoNewPrivileges=.*/NoNewPrivileges=false/' \
		/etc/systemd/system/cloudhost-portal.service
	ok "PAM is available (NoNewPrivileges=false)"
else
	PAM_MODE="password_hash"
	warn "PAM is unavailable (NoNewPrivileges / unix_chkpwd limitation); falling back to admin.hash sign-in"
	warn "Correct the unit manually and rerun with: sudo ./install.sh --yes"
fi

sed -i "s/^auth_mode:.*/auth_mode: ${PAM_MODE}/" "${ETC_DIR}/config.yaml"

# 首次名单初始化必须在 PAM_MODE 最终确定后、门户启动前执行；已存在名单
#（包括显式空名单）由工具原样保留，解析/权限问题立即失败关闭。
"${RENDER[@]}" remote-admins \
	--config "${ETC_DIR}/config.yaml" \
	--auth-mode "${PAM_MODE}" \
	--system-user "${RUN_USER}" \
	--admin-user "${ADMIN_USER}"

systemctl daemon-reload

# --------------------------------------------------------------------------- #
# 7. 启动服务
# --------------------------------------------------------------------------- #
log "7/8 Starting services and enabling them at boot"
for unit in cloudhost-portal cloudhost-files caddy; do
	systemctl enable "${unit}.service" >/dev/null 2>&1 || warn "Failed to enable ${unit}"
done
systemctl restart cloudhost-portal.service

# 等待门户就绪，最多 30s
READY=0
for _ in $(seq 1 30); do
	if curl -fsS "http://127.0.0.1:8080/api/healthz" >/dev/null 2>&1; then
		READY=1
		break
	fi
	sleep 1
done
[[ ${READY} -eq 1 ]] || die "Portal failed to start: journalctl -u cloudhost-portal -n 50 --no-pager"
ok "cloudhost-portal is ready"

systemctl restart cloudhost-files.service || warn "cloudhost-files failed to start (the portal and SSH are unaffected)"
sleep 2
if [[ -x "${INSTALL_DIR}/bin/filebrowser" && -f "${STATE_DIR}/filebrowser/filebrowser.db" ]]; then
	FB_LOCALE_UPDATE_ALLOWED=1
	if systemctl is-active --quiet cloudhost-files.service; then
		if systemctl stop cloudhost-files.service; then
			FB_LOCALE_UPDATE_ALLOWED=1
		else
			FB_LOCALE_UPDATE_ALLOWED=0
			warn "Unable to stop cloudhost-files before setting its default language"
		fi
	fi
	if [[ ${FB_LOCALE_UPDATE_ALLOWED} -eq 1 ]] && command -v runuser >/dev/null 2>&1; then
		# Official v2.31.2 branding extension; never patch the binary/client or user files.
		runuser -u "${RUN_USER}" -- "${INSTALL_DIR}/venv/bin/python" \
			"${INSTALL_DIR}/tools/filebrowser_branding.py" \
			--binary "${INSTALL_DIR}/bin/filebrowser" \
			--config "${ETC_DIR}/filebrowser.json" \
			--database "${STATE_DIR}/filebrowser/filebrowser.db" \
			--assets "${INSTALL_DIR}/deploy/filebrowser" ||
			warn "File Browser branding was not applied; verify version 2.31.2 and branding assets (see redesign verification report)"
		runuser -u "${RUN_USER}" -- "${INSTALL_DIR}/bin/filebrowser" \
			--config "${ETC_DIR}/filebrowser.json" \
			--database "${STATE_DIR}/filebrowser/filebrowser.db" \
			users update 1 --locale en ||
			warn "Unable to set the shared File Browser user locale to English"
	elif [[ ${FB_LOCALE_UPDATE_ALLOWED} -eq 1 ]]; then
		warn "runuser is unavailable; unable to set the File Browser user locale to English"
	fi
	if [[ ${FB_LOCALE_UPDATE_ALLOWED} -eq 1 ]]; then
		systemctl restart cloudhost-files.service ||
			warn "cloudhost-files failed to restart after setting its language"
		sleep 2
	fi
fi
if ! systemctl is-active --quiet cloudhost-files.service; then
	warn "cloudhost-files did not stay running; /files/ will return 502. Recent logs:"
	journalctl -u cloudhost-files -n 20 --no-pager >&2 || true
	warn "Common causes: /etc/cloudhost/filebrowser.json is unreadable by ${RUN_USER}, or ${STATE_DIR}/filebrowser is not owned by ${RUN_USER}"
fi
systemctl restart caddy.service || die "Caddy failed to start (error code E2002): journalctl -u caddy -n 50 --no-pager"

if [[ ${ENABLE_TTYD} -eq 1 ]]; then
	systemctl enable cloudhost-ttyd.service >/dev/null 2>&1 || true
	systemctl restart cloudhost-ttyd.service || warn "cloudhost-ttyd failed to start (E1 is P1 and does not block v1.0)"
fi

systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || warn "fail2ban failed to start; investigate manually to ensure A4 brute-force protection"

# --------------------------------------------------------------------------- #
# 8. SSH 加固
# --------------------------------------------------------------------------- #
log "8/8 Hardening SSH"
AUTHORIZED_KEYS="${RUN_USER_HOME}/.ssh/authorized_keys"
if [[ ${HARDEN_SSH} -eq 1 ]]; then
	# 关键保护：没有公钥就关闭密码登录会把用户锁在门外（尤其板子没接显示器/
	# 串口时只能重刷系统）。检测不到可用公钥时直接跳过加固，并给出补公钥的步骤。
	if [[ -s "${AUTHORIZED_KEYS}" ]]; then
		install -d -m 0755 /etc/ssh/sshd_config.d
		install -m 0644 "${DEPLOY_SRC}/sshd-hardening.conf" /etc/ssh/sshd_config.d/90-cloudhost.conf
		if sshd -t 2>/dev/null; then
			systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
			ok "Disabled SSH password sign-in (PasswordAuthentication no)"
			ok "Available sign-in method: ssh -i <private-key> ${RUN_USER}@<board-IP>"
		else
			rm -f /etc/ssh/sshd_config.d/90-cloudhost.conf
			warn "sshd configuration validation failed; SSH hardening was rolled back"
		fi
	else
		warn "No public key found at ${AUTHORIZED_KEYS} (missing or empty)"
		warn "Skipping PasswordAuthentication no to avoid locking you out; SSH password sign-in remains enabled"
		warn "Append your computer's public key to ${AUTHORIZED_KEYS}, then rerun sudo ./install.sh"
		warn "Alternatively, upload the key with ssh-copy-id ${RUN_USER}@<board-IP>"
	fi
else
	log "SSH hardening skipped (see deploy/sshd-hardening.conf for manual setup)"
fi

# --------------------------------------------------------------------------- #
# 完成
# --------------------------------------------------------------------------- #
IP="$("${INSTALL_DIR}/venv/bin/python" - <<-'PY'
	import socket
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
	    s.connect(("192.0.2.1", 9))  # 不会真正发包，仅用于选路
	    print(s.getsockname()[0])
	except OSError:
	    print("127.0.0.1")
	finally:
	    s.close()
PY
)"

if [[ "${PAM_MODE}" == "system" ]]; then
	PAM_MODE_LABEL="${RUN_USER} (board system password)"
else
	PAM_MODE_LABEL="${ADMIN_USER} (admin.hash)"
fi
FILES_ROOT_LABEL="${RUN_USER_HOME}"
[[ "${FILE_ROOT_MODE:-home}" == "cloud" ]] && FILES_ROOT_LABEL="${CLOUD_ROOT}"

cat <<-EOF

${C_OK}Installation complete${C_OFF}
------------------------------------------------------------------
  Portal        http://${IP}/
  Files         http://${IP}/files/
  Site          http://${IP}/site/
  Terminal (E1) $([[ ${ENABLE_TTYD} -eq 1 ]] && echo "http://${IP}/term/" || echo "disabled")
  SSH           ssh -i ~/.ssh/id_ed25519 ${RUN_USER}@${IP}
                (key-based sign-in only; public key: ${RUN_USER_HOME}/.ssh/authorized_keys)

  Portal account ${PAM_MODE_LABEL}
  Files root     ${FILES_ROOT_LABEL}
  Config         ${ETC_DIR}/config.yaml
  Audit log      ${LOG_DIR}/audit.log
  Service status systemctl status cloudhost-portal cloudhost-files caddy

  Acceptance     sudo ./scripts/acceptance.sh
  User manual    ${REPO_DIR}/docs/用户手册.md
------------------------------------------------------------------
EOF
