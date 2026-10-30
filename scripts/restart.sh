#!/usr/bin/env bash
# =============================================================================
# 重启个人云主机：服务、配置生效、以及整机重启前的自查
#
# 用法：
#   sudo ./scripts/restart.sh              # 重启 portal / files / caddy（含 ttyd）
#   sudo ./scripts/restart.sh --status     # 只看状态，不重启
#   sudo ./scripts/restart.sh --reload     # 不重启进程，只重载 Caddy 配置
#   sudo ./scripts/restart.sh --reboot     # 重启整块板子
#
# 说明：改 config.yaml 需要重启 cloudhost-portal 才生效；改 Caddyfile 用
# --reload 即可。若只是某个服务异常，门户「服务」面板的「重启」按钮等效于
# systemctl restart <unit>，不需要 sudo。
# =============================================================================
set -Eeuo pipefail

PORTAL_UNIT="cloudhost-portal"
FILES_UNIT="cloudhost-files"
TTYD_UNIT="cloudhost-ttyd"
GATEWAY_UNIT="caddy"
FAIL2BAN_UNIT="fail2ban"

C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_OFF=""
if [[ -t 1 ]]; then
	C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_INFO=$'\033[36m'; C_OFF=$'\033[0m'
fi

log()  { printf '%s[cloudhost]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
ok()   { printf '%s[  ok  ]%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s[warn  ]%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s[error ]%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }

usage() {
	# 打印文件头部的注释块（第 2 行起，遇到第一行非注释即停）
	awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

MODE="restart"
case "${1:-}" in
	"") ;;
	--status) MODE="status" ;;
	--reload) MODE="reload" ;;
	--reboot) MODE="reboot" ;;
	-h|--help) usage; exit 0 ;;
	*) die "未知参数：$1（用 --help 查看用法）" ;;
esac

need_root() {
	[[ ${EUID} -eq 0 ]] || die "请用 root 执行：sudo ./scripts/restart.sh"
}

units() {
	local list=("${PORTAL_UNIT}" "${FILES_UNIT}" "${GATEWAY_UNIT}" "${FAIL2BAN_UNIT}")
	if systemctl list-unit-files "${TTYD_UNIT}.service" >/dev/null 2>&1 &&
		[[ -n "$(systemctl list-unit-files "${TTYD_UNIT}.service" --no-legend 2>/dev/null)" ]]; then
		list+=("${TTYD_UNIT}")
	fi
	printf '%s\n' "${list[@]}"
}

show_status() {
	printf '%-24s %-10s %-10s %s\n' "服务" "状态" "开机自启" "内存"
	while read -r unit; do
		local active enabled mem
		active="$(systemctl is-active "${unit}.service" 2>/dev/null || true)"
		enabled="$(systemctl is-enabled "${unit}.service" 2>/dev/null || true)"
		mem="$(systemctl show "${unit}.service" -p MemoryCurrent --value 2>/dev/null || true)"
		[[ -n "${mem}" && "${mem}" != "[not set]" ]] || mem="-"
		printf '%-24s %-10s %-10s %s\n' "${unit}" "${active:-unknown}" "${enabled:-unknown}" "${mem}"
	done < <(units)
}

wait_portal() {
	local i
	for i in $(seq 1 30); do
		if curl -fsS "http://127.0.0.1:8080/api/healthz" >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	return 1
}

check_http() {
	local path code
	for path in /api/healthz / /files/ /site/; do
		code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1${path}" || true)"
		case "${path}:${code}" in
			/api/healthz:200|/:200|/files/:302|/files/:200|/site/:200)
				ok "http ${path} → ${code}" ;;
			*)
				warn "http ${path} → ${code:-无响应}" ;;
		esac
	done
}

case "${MODE}" in
	status)
		show_status
		check_http
		exit 0
		;;
	reload)
		need_root
		log "校验并重载 Caddy 配置"
		"$(command -v caddy)" validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 ||
			die "Caddy 配置校验失败，未应用（错误码 E2001）：caddy validate --config /etc/caddy/Caddyfile"
		# Caddyfile 里有 `admin off`，caddy reload 依赖 admin API，因此 reload
		# 一定失败（CanReload 仍显示 yes，容易误判）。这里直接判定并重启。
		if systemctl reload "${GATEWAY_UNIT}.service" 2>/dev/null; then
			ok "Caddy 已热重载（连接未中断）"
		else
			warn "Caddyfile 含 admin off，无法热重载；改为重启 caddy（连接会短暂中断）"
			systemctl restart "${GATEWAY_UNIT}.service" || die "Caddy 重启失败（错误码 E2002）"
			ok "Caddy 已重启"
		fi
		check_http
		exit 0
		;;
	reboot)
		need_root
		warn "即将重启整块板子，SSH / 门户会断开约 60 秒"
		sync
		systemctl reboot
		exit 0
		;;
esac

need_root
log "重启服务：$(units | tr '\n' ' ')"
while read -r unit; do
	systemctl restart "${unit}.service" || warn "${unit} 重启失败"
done < <(units)

if wait_portal; then
	ok "cloudhost-portal 已就绪（/api/healthz 200）"
else
	warn "门户 30 秒内未就绪：journalctl -u ${PORTAL_UNIT} -n 50 --no-pager"
fi

# File Browser 退出 1 通常只剩两类原因：配置不可读、数据库目录属主不对
sleep 1
if ! systemctl is-active --quiet "${FILES_UNIT}.service"; then
	warn "${FILES_UNIT} 未保持运行，最近日志："
	journalctl -u "${FILES_UNIT}" -n 20 --no-pager >&2 || true
	RUN_USER="$(systemctl show "${FILES_UNIT}.service" -p User --value)"
	warn "自查：ls -l /etc/cloudhost/filebrowser.json（应为 root:${RUN_USER:-<用户>} 0640）"
fi

show_status
check_http
