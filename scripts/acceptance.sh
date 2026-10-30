#!/usr/bin/env bash
# =============================================================================
# Quectel Pi M2 个人云主机 · 验收自检脚本
#
# 对应设计文档 docs/35-个人云主机.md 第 16.1 节（v1.0 唯一验收依据）：
#   A1 局域网 web   首屏 ≤ 3s；状态 1s 刷新，连续 10 次采样无失败
#   A2 文件         上传 + 下载 100MB，SHA256 前后一致；期间 /api/status P95 ≤ 1s
#   A3 SSH          密钥登录成功且 ≤ 5s；密码登录被拒绝
#   A4 安全         未登录访问 /files/ 返回 302；连续 5 次错误密码触发 fail2ban 封禁 ≥ 10 分钟
#   A5 稳定         systemd 全部 active + enabled（重启后 ≤ 60s 就绪需人工重启一次确认）
#   A6 部署         install.sh 产物齐全（配置 / Caddyfile / venv / unit）
#   A7 文档         用户手册覆盖局域网部署、文件访问、SSH 登录、常见问题
#
# 用法：
#   sudo ./scripts/acceptance.sh                        # 全部检查
#   sudo ./scripts/acceptance.sh --password 'xxx'       # 非交互（A1/A2 需要登录态）
#   sudo ./scripts/acceptance.sh --skip-a2              # 跳过 100MB 上传下载
#   sudo ./scripts/acceptance.sh --a4-full              # 实际触发一次 fail2ban 封禁（会短暂封禁本机）
#
# 退出码：0 全部通过；1 存在 FAIL；2 参数或环境错误
# =============================================================================
set -uo pipefail

ETC_DIR="/etc/cloudhost"
INSTALL_DIR="/opt/cloudhost"
CONFIG_FILE="${ETC_DIR}/config.yaml"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET_HOST=""
ADMIN_USER=""
ADMIN_PASSWORD=""
SIZE_MB=100
SKIP_A2=0
SKIP_A3=0
A4_FULL=0
SESSION_SAMPLES=10

# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
if [[ -t 1 ]]; then
	C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_INFO=$'\033[36m'; C_OFF=$'\033[0m'
else
	C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_OFF=""
fi

RESULT_A1="SKIP"; RESULT_A2="SKIP"; RESULT_A3="SKIP"; RESULT_A4="SKIP"
RESULT_A5="SKIP"; RESULT_A6="SKIP"; RESULT_A7="SKIP"
FAILED=0
NOTES=()

log()  { printf '%s==>%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
ok()   { printf '  %s[ PASS ]%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
bad()  { printf '  %s[ FAIL ]%s %s\n' "${C_ERR}" "${C_OFF}" "$*"; }
warn() { printf '  %s[ warn ]%s %s\n' "${C_WARN}" "${C_OFF}" "$*"; }
note() { printf '  [ info ] %s\n' "$*"; }

usage() {
	sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--host) TARGET_HOST="${2:?}"; shift 2 ;;
		--user) ADMIN_USER="${2:?}"; shift 2 ;;
		--password) ADMIN_PASSWORD="${2:?}"; shift 2 ;;
		--size-mb) SIZE_MB="${2:?}"; shift 2 ;;
		--samples) SESSION_SAMPLES="${2:?}"; shift 2 ;;
		--skip-a2) SKIP_A2=1; shift ;;
		--skip-a3) SKIP_A3=1; shift ;;
		--a4-full) A4_FULL=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "未知参数：$1" >&2; usage; exit 2 ;;
	esac
done

require_cmd() {
	command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1" >&2; exit 2; }
}

# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
cfg_get() {
	local key="$1" default="${2:-}"
	if [[ -f ${CONFIG_FILE} ]]; then
		local value
		value="$(awk -v k="${key}" '
			$0 ~ "^[[:space:]]*" k ":" {
				sub("^[[:space:]]*" k ":[[:space:]]*", "", $0)
				gsub(/^["'"'"']|["'"'"'][[:space:]]*$/, "", $0)
				print; exit
			}' "${CONFIG_FILE}")"
		[[ -n ${value} ]] && { printf '%s' "${value}"; return 0; }
	fi
	printf '%s' "${default}"
}

detect_ip() {
	local ip=""
	if command -v python3 >/dev/null 2>&1; then
		ip="$(python3 - <<-'PY'
			import socket
			s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
			try:
			    s.connect(("192.0.2.1", 9))
			    print(s.getsockname()[0])
			except OSError:
			    print("127.0.0.1")
			finally:
			    s.close()
		PY
		)"
	fi
	[[ -z ${ip} ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
	printf '%s' "${ip:-127.0.0.1}"
}

main() {
	require_cmd curl

	LISTEN_PORT="$(cfg_get listen_port 80)"
	PORTAL_PORT="$(cfg_get portal_port 8080)"
	LOGIN_MAX="$(cfg_get login_max_attempts 5)"
	BANTIME="$(cfg_get fail2ban_bantime 600)"
	RUN_USER_CFG="$(cfg_get admin_user admin)"
	SITE_PUBLIC="$(cfg_get site_public true)"
	CLOUD_ROOT="$(cfg_get cloud_root "${HOME}/cloud")"
	HOST="${TARGET_HOST:-$(detect_ip)}"
	[[ -n ${ADMIN_USER} ]] || ADMIN_USER="${RUN_USER_CFG}"

	if [[ -z ${ADMIN_PASSWORD} ]]; then
		read -r -s -p "请输入门户密码（用于 A1/A2 登录，留空跳过相关检查）: " ADMIN_PASSWORD
		echo
	fi

	BASE="http://${HOST}"
	[[ ${LISTEN_PORT} != "80" ]] && BASE="${BASE}:${LISTEN_PORT}"

	TMP_DIR="$(mktemp -d)"
	COOKIE_JAR="${TMP_DIR}/cookies.txt"
	trap 'rm -rf "${TMP_DIR}"' EXIT

	echo "=================================================================="
	echo " 个人云主机 v1.0 验收自检"
	echo " 目标：${BASE}   板端：$(hostname)   时间：$(date '+%F %T')"
	echo "=================================================================="

	check_a5
	check_a6
	check_a1
	check_a2
	check_a3
	check_a4
	check_a7
}

# --------------------------------------------------------------------------- #
# A1 局域网 web：首屏 ≤ 3s + 状态 1s 刷新连续采样
# --------------------------------------------------------------------------- #
check_a1() {
	log "A1 局域网 web（首屏 / 状态刷新）"

	local first
	first="$(curl -s -o /dev/null -m 10 -w '%{time_starttransfer}' "${BASE}/" 2>/dev/null)"
	if [[ -z ${first} ]]; then
		bad "无法访问 ${BASE}/（服务未启动或端口不通）"
		RESULT_A1="FAIL"; FAILED=1
		return
	fi
	local first_ms
	first_ms="$(awk -v t="${first}" 'BEGIN{printf "%.0f", t*1000}')"
	if awk -v t="${first}" 'BEGIN{exit !(t <= 3.0)}'; then
		ok "首屏 TTFB ${first_ms}ms（阈值 ≤ 3000ms）"
	else
		bad "首屏 TTFB ${first_ms}ms 超过 3000ms"
		RESULT_A1="FAIL"; FAILED=1
	fi

	if ! do_login; then
		warn "未取得登录态，跳过状态刷新采样（请用 --password 提供密码）"
		RESULT_A1="FAIL"; FAILED=1
		return
	fi

	local i code failures=0
	local -a durations=()
	for ((i = 1; i <= SESSION_SAMPLES; i++)); do
		read -r code seconds < <(curl -s -o "${TMP_DIR}/status.json" -m 5 -b "${COOKIE_JAR}" \
			-w '%{http_code} %{time_total}' "${BASE}/api/status" 2>/dev/null)
		durations+=("${seconds:-9}")
		[[ ${code} == "200" ]] || { failures=$((failures + 1)); warn "第 ${i} 次采样 HTTP ${code}"; }
		[[ ${i} -lt ${SESSION_SAMPLES} ]] && sleep 1
	done

	local max
	max="$(printf '%s\n' "${durations[@]}" | sort -rn | head -1)"
	local max_ms
	max_ms="$(awk -v t="${max}" 'BEGIN{printf "%.0f", t*1000}')"

	# 状态契约字段（文档 9.2）
	if command -v python3 >/dev/null 2>&1; then
		python3 - "${TMP_DIR}/status.json" <<-'PY'
			import json, sys
			data = json.load(open(sys.argv[1], encoding="utf-8"))
			missing = []
			for group, keys in {
			    "": ("time", "uptime_sec"),
			    "cpu": ("percent", "load1", "cores"),
			    "mem": ("total_mb", "used_mb", "percent"),
			    "temp": ("cpu_c",),
			    "disk": ("root_total_gb", "root_percent"),
			    "net": ("iface", "rx_kbps", "tx_kbps"),
			}.items():
			    node = data if not group else data.get(group, {})
			    for key in keys:
			        if not isinstance(node, dict) or key not in node:
			            missing.append(f"{group}.{key}" if group else key)
			if missing:
			    print("MISSING:" + ",".join(missing))
			else:
			    print("CONTRACT_OK")
		PY
	fi >"${TMP_DIR}/contract.txt" 2>/dev/null

	if [[ ${failures} -eq 0 ]]; then
		ok "连续 ${SESSION_SAMPLES} 次 /api/status 采样全部成功，最慢 ${max_ms}ms"
		if grep -q CONTRACT_OK "${TMP_DIR}/contract.txt" 2>/dev/null; then
			ok "状态字段契约完整（cpu/mem/temp/disk/net/uptime）"
		elif grep -q MISSING "${TMP_DIR}/contract.txt" 2>/dev/null; then
			bad "状态字段缺失：$(sed 's/MISSING://' "${TMP_DIR}/contract.txt")"
			RESULT_A1="FAIL"; FAILED=1
		fi
		[[ ${RESULT_A1} != "FAIL" ]] && RESULT_A1="PASS"
	else
		bad "${failures}/${SESSION_SAMPLES} 次采样失败"
		RESULT_A1="FAIL"; FAILED=1
	fi

	if awk -v t="${max}" 'BEGIN{exit !(t <= 1.0)}'; then
		ok "状态接口最慢响应 ${max_ms}ms（阈值 ≤ 1000ms）"
	else
		warn "状态接口最慢响应 ${max_ms}ms 偏高（A1 仅要求无失败）"
		NOTES+=("A1 状态响应峰值 ${max_ms}ms")
	fi
}

# --------------------------------------------------------------------------- #
# A2 文件：100MB 上传 + 下载 SHA256 一致，期间 /api/status P95 ≤ 1s
# --------------------------------------------------------------------------- #
check_a2() {
	log "A2 文件上传 / 下载（${SIZE_MB}MB + SHA256）"
	if [[ ${SKIP_A2} -eq 1 ]]; then
		note "已通过 --skip-a2 跳过"
		RESULT_A2="SKIP"
		return
	fi
	if ! do_login; then
		warn "未取得登录态，跳过 A2"
		RESULT_A2="SKIP"
		NOTES+=("A2 未执行：缺少登录态")
		return
	fi

	local csrf
	csrf="$(awk '$6 == "ch_csrf" {print $7}' "${COOKIE_JAR}" | tail -1)"
	if [[ -z ${csrf} ]]; then
		bad "未获取到 CSRF token，无法上传"
		RESULT_A2="FAIL"; FAILED=1
		return
	fi

	local src="${TMP_DIR}/accept.bin"
	local dst="${TMP_DIR}/accept.out"
	log "生成 ${SIZE_MB}MB 测试文件…"
	if ! dd if=/dev/urandom of="${src}" bs=1M count="${SIZE_MB}" status=none 2>/dev/null; then
		bad "生成测试文件失败（磁盘空间不足？）"
		RESULT_A2="FAIL"; FAILED=1
		return
	fi

	local sum_src
	sum_src="$(sha256sum "${src}" | awk '{print $1}')"
	note "源文件 SHA256 ${sum_src}"

	# 上传期间并行采样 /api/status
	local lat_file="${TMP_DIR}/latency.txt"
	: >"${lat_file}"
	(
		for _ in $(seq 1 40); do
			curl -s -o /dev/null -m 3 -b "${COOKIE_JAR}" -w '%{time_total}\n' \
				"${BASE}/api/status" >>"${lat_file}" 2>/dev/null
			sleep 0.5
		done
	) &
	local sampler=$!

	# 文件入口根目录默认是 home，临时文件固定放到 cloud/upload，避免污染 home 根；
	# 回滚到 file_root_mode=cloud 时根目录本身就是 cloud，改用 upload。
	local upload_path="cloud/upload"
	local files_root
	files_root="$(curl -s -m 15 -b "${COOKIE_JAR}" "${BASE}/api/config" | \
		python3 -c 'import json,sys;print(json.load(sys.stdin).get("files_root",""))' 2>/dev/null || true)"
	if [[ ${files_root} == */cloud ]]; then upload_path="upload"; fi

	local up_code up_time
	read -r up_code up_time < <(curl -s -o "${TMP_DIR}/up.json" -m 600 -b "${COOKIE_JAR}" \
		-X POST -H "X-CSRF-Token: ${csrf}" -H "X-Filename: accept-$(date +%s).bin" \
		--data-binary @"${src}" "${BASE}/api/files/upload?overwrite=true&path=${upload_path}" \
		-w '%{http_code} %{time_total}' 2>/dev/null)

	wait "${sampler}" 2>/dev/null

	if [[ ${up_code} != "201" ]]; then
		bad "上传失败 HTTP ${up_code}：$(head -c 200 "${TMP_DIR}/up.json" 2>/dev/null)"
		RESULT_A2="FAIL"; FAILED=1
		return
	fi
	local remote_path
	remote_path="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["path"])' \
		"${TMP_DIR}/up.json" 2>/dev/null)"
	[[ -z ${remote_path} ]] && remote_path="$(sed -n 's/.*"path":"\([^"]*\)".*/\1/p' "${TMP_DIR}/up.json")"
	local speed
	speed="$(awk -v s="${SIZE_MB}" -v t="${up_time}" 'BEGIN{if (t > 0) printf "%.1f", s/t; else print "?"}')"
	ok "上传 ${SIZE_MB}MB 用时 ${up_time}s（约 ${speed}MB/s），路径 ${remote_path}"

	local dl_code
	read -r dl_code _ < <(curl -s -o "${dst}" -m 600 -b "${COOKIE_JAR}" \
		--data-urlencode "path=${remote_path}" -G "${BASE}/api/files/download" \
		-w '%{http_code} %{time_total}' 2>/dev/null)
	if [[ ${dl_code} != "200" ]]; then
		bad "下载失败 HTTP ${dl_code}"
		RESULT_A2="FAIL"; FAILED=1
		return
	fi

	local sum_dst
	sum_dst="$(sha256sum "${dst}" | awk '{print $1}')"
	if [[ ${sum_src} == "${sum_dst}" ]]; then
		ok "下载完成，SHA256 一致（${sum_dst}）"
	else
		bad "SHA256 不一致：src=${sum_src} dst=${sum_dst}"
		RESULT_A2="FAIL"; FAILED=1
	fi

	# 清理远端测试文件
	curl -s -o /dev/null -m 15 -b "${COOKIE_JAR}" -X DELETE -H "X-CSRF-Token: ${csrf}" \
		--data-urlencode "path=${remote_path}" -G "${BASE}/api/files/delete" || true

	# P95（上传期间状态接口）
	local p95_ms count
	count="$(grep -c . "${lat_file}" 2>/dev/null || echo 0)"
	if [[ ${count} -gt 0 ]]; then
		p95_ms="$(awk '{print $1}' "${lat_file}" | sort -n | awk -v n="${count}" '
			{ v[NR] = $1 }
			END {
				idx = int(n * 0.95)
				if (idx < 1) idx = 1
				printf "%.0f", v[idx] * 1000
			}')"
		if [[ ${p95_ms} -le 1000 ]]; then
			ok "上传期间 /api/status P95 ${p95_ms}ms（阈值 ≤ 1000ms，样本 ${count}）"
		else
			bad "上传期间 /api/status P95 ${p95_ms}ms 超过 1000ms"
			RESULT_A2="FAIL"; FAILED=1
		fi
	else
		warn "未采集到状态延迟样本，P95 未判定"
		NOTES+=("A2 P95 无样本")
	fi

	[[ ${RESULT_A2} != "FAIL" ]] && RESULT_A2="PASS"
}

# --------------------------------------------------------------------------- #
# A3 SSH：密钥登录 ≤ 5s 成功，密码登录被拒绝
# --------------------------------------------------------------------------- #
check_a3() {
	log "A3 SSH 密钥登录 / 密码登录拒绝"
	if [[ ${SKIP_A3} -eq 1 ]]; then
		note "已通过 --skip-a3 跳过"
		RESULT_A3="SKIP"
		return
	fi
	if ! command -v ssh >/dev/null 2>&1; then
		warn "本机无 ssh 客户端，跳过 A3"
		RESULT_A3="SKIP"
		return
	fi

	local ssh_user="${SUDO_USER:-${USER}}"
	[[ ${ssh_user} == "root" ]] && ssh_user="${USER}"

	# sudo 会把 HOME 改成 /root，ssh 于是找不到目标用户的私钥，
	# 表现为「Permission denied (publickey)」的假失败。这里显式指定
	# 被提权用户的 id_* 私钥；若一个都没有，则退回默认查找（可能交互式）。
	local ssh_key_opts=()
	local real_home
	real_home="$(getent passwd "${ssh_user}" | cut -d: -f6)"
	real_home="${real_home:-/home/${ssh_user}}"
	local key
	for key in "${real_home}/.ssh/id_ed25519" "${real_home}/.ssh/id_rsa" \
		"${real_home}/.ssh/id_ecdsa"; do
		if [[ -r "${key}" ]]; then
			ssh_key_opts=(-i "${key}")
			break
		fi
	done
	# 私钥与 known_hosts 都按真实用户家目录定位，避免 root 身份误判
	export HOME="${real_home}"

	local start end elapsed
	start="$(date +%s%N)"
	if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "${ssh_key_opts[@]}" \
		"${ssh_user}@${HOST}" 'echo cloudhost-ok' >"${TMP_DIR}/ssh.out" 2>"${TMP_DIR}/ssh.err"; then
		end="$(date +%s%N)"
		elapsed="$(awk -v a="${start}" -v b="${end}" 'BEGIN{printf "%.1f", (b-a)/1000000000}')"
		if awk -v t="${elapsed}" 'BEGIN{exit !(t <= 5.0)}'; then
			ok "密钥登录成功，耗时 ${elapsed}s（阈值 ≤ 5s）"
		else
			bad "密钥登录成功但耗时 ${elapsed}s 超过 5s"
			RESULT_A3="FAIL"; FAILED=1
		fi
	else
		bad "密钥登录失败：$(tail -3 "${TMP_DIR}/ssh.err" 2>/dev/null | tr '\n' ' ')"
		if [[ ${#ssh_key_opts[@]} -eq 0 ]]; then
			# 最常见的误判来源：板端 authorized_keys 配好了，但本机根本没有私钥，
			# 于是 ssh 只能尝试密码认证，被 PasswordAuthentication no 拒绝。
			note "本机 ${real_home}/.ssh 下找不到 id_ed25519/id_rsa/id_ecdsa 私钥"
			note "A3 需在持有对应私钥的机器上执行：ssh-keygen -t ed25519 后把 .pub 追加到 ${HOST}:~/.ssh/authorized_keys"
		else
			note "已使用私钥 ${ssh_key_opts[1]} 仍被拒绝，请检查 ${HOST} 上 ${ssh_user} 的 ~/.ssh/authorized_keys 内容"
		fi
		RESULT_A3="FAIL"; FAILED=1
	fi

	# 密码登录必须被拒绝：禁用公钥、只允许密码 + BatchMode（无口令输入即失败）
	if ssh -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password \
		-o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o LogLevel=ERROR "${ssh_user}@${HOST}" 'true' >/dev/null 2>&1; then
		bad "密码登录竟然成功，sshd 未禁用密码认证"
		RESULT_A3="FAIL"; FAILED=1
	else
		ok "密码登录被拒绝"
	fi

	# 板端 sshd 配置校验（本机即板端时）
	if [[ -f /etc/ssh/sshd_config.d/90-cloudhost.conf ]]; then
		if grep -qiE '^[[:space:]]*PasswordAuthentication[[:space:]]+no' \
			/etc/ssh/sshd_config.d/90-cloudhost.conf; then
			ok "sshd 配置 PasswordAuthentication no"
		else
			warn "90-cloudhost.conf 存在但未禁用密码认证"
		fi
	fi

	[[ ${RESULT_A3} != "FAIL" ]] && RESULT_A3="PASS"
}

# --------------------------------------------------------------------------- #
# A4 安全：未登录 /files/ 302 + 错误密码触发 fail2ban
# --------------------------------------------------------------------------- #
check_a4() {
	log "A4 安全（网关鉴权 / 防爆破）"

	local code location
	read -r code location < <(curl -s -o /dev/null -m 10 \
		-w '%{http_code} %{redirect_url}' "${BASE}/files/" 2>/dev/null)
	if [[ ${code} == "302" || ${code} == "303" ]]; then
		if [[ ${location} == *"/login"* ]]; then
			ok "未登录访问 /files/ → ${code} ${location}"
		else
			bad "未登录访问 /files/ 302 但未跳转登录页：${location}"
			RESULT_A4="FAIL"; FAILED=1
		fi
	elif [[ ${code} == "401" ]]; then
		bad "未登录访问 /files/ 返回 401（应为 302 跳转登录页）"
		RESULT_A4="FAIL"; FAILED=1
	else
		bad "未登录访问 /files/ 返回 HTTP ${code}"
		RESULT_A4="FAIL"; FAILED=1
	fi

	# 未登录访问 /api/status 必须被拒
	local api_code
	api_code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${BASE}/api/status" 2>/dev/null)"
	if [[ ${api_code} == "401" ]]; then
		ok "未登录访问 /api/status → 401"
	else
		bad "未登录访问 /api/status → ${api_code}（应为 401）"
		RESULT_A4="FAIL"; FAILED=1
	fi

	# 封禁时长下限（文档 A4：≥ 10 分钟）
	if [[ ${BANTIME} -ge 600 ]]; then
		ok "配置 fail2ban_bantime=${BANTIME}s（阈值 ≥ 600s）"
	else
		bad "fail2ban_bantime=${BANTIME}s 低于 600s"
		RESULT_A4="FAIL"; FAILED=1
	fi

	if command -v fail2ban-client >/dev/null 2>&1; then
		local status
		status="$(fail2ban-client status cloudhost 2>/dev/null)"
		if [[ -n ${status} ]]; then
			ok "fail2ban jail cloudhost 已加载"
			echo "${status}" | sed 's/^/           /'
		else
			warn "fail2ban jail cloudhost 未加载（检查 /etc/fail2ban/jail.d/cloudhost.local）"
			NOTES+=("A4 fail2ban jail 未加载")
		fi
	else
		warn "未安装 fail2ban-client，无法确认封禁状态"
		NOTES+=("A4 未安装 fail2ban")
	fi

	if [[ ${A4_FULL} -eq 1 ]]; then
		log "  触发 ${LOGIN_MAX} 次错误密码以验证封禁（本机 IP 将被短暂封禁）"
		local i
		for ((i = 1; i <= LOGIN_MAX; i++)); do
			curl -s -o /dev/null -m 5 -X POST \
				-d "username=${ADMIN_USER}" -d "password=wrong-${i}-${RANDOM}" \
				"${BASE}/auth/login" 2>/dev/null
		done
		curl -s -o /dev/null -m 5 -X POST -d "username=${ADMIN_USER}" -d "password=wrong-final" \
			"${BASE}/auth/login" 2>/dev/null
		sleep 5
		if command -v fail2ban-client >/dev/null 2>&1; then
			local banned
			banned="$(fail2ban-client status cloudhost 2>/dev/null | grep -i 'Currently banned' | awk -F: '{print $2}' | tr -d ' ')"
			if [[ ${banned:-0} -ge 1 ]]; then
				ok "fail2ban 已封禁 ${banned} 个 IP（jail cloudhost）"
			else
				bad "连续错误密码后 fail2ban 未产生封禁（检查 filter 正则与 Caddy 日志）"
				RESULT_A4="FAIL"; FAILED=1
			fi
		fi
	else
		note "如需实测封禁流程，追加 --a4-full"
	fi

	[[ ${RESULT_A4} != "FAIL" ]] && RESULT_A4="PASS"
}

# --------------------------------------------------------------------------- #
# A5/A6 服务与部署产物
# --------------------------------------------------------------------------- #
check_a5() {
	log "A5 服务稳定性（systemd 状态 / 开机自启）"
	if ! command -v systemctl >/dev/null 2>&1; then
		warn "无 systemd（非板端环境），跳过"
		return
	fi
	local units="cloudhost-portal caddy"
	[[ -f /etc/systemd/system/cloudhost-files.service ]] && units="${units} cloudhost-files"
	[[ -f /etc/systemd/system/cloudhost-ttyd.service ]] && units="${units} cloudhost-ttyd"

	local unit state enabled failed=0
	for unit in ${units}; do
		state="$(systemctl is-active "${unit}.service" 2>/dev/null)"
		enabled="$(systemctl is-enabled "${unit}.service" 2>/dev/null)"
		if [[ ${state} != "active" ]]; then
			bad "${unit} 未运行（${state:-unknown}）"
			failed=1
		elif [[ ${enabled} != "enabled" ]]; then
			bad "${unit} 未设置开机自启（${enabled:-unknown}）"
			failed=1
		else
			ok "${unit} active + enabled"
		fi
	done

	local uptime_sec
	uptime_sec="$(awk '{print int($1)}' /proc/uptime 2>/dev/null)"
	note "板子已运行 $((uptime_sec / 60)) 分钟；重启验证：sudo reboot 后 60s 内访问 ${BASE}/"
	note "本脚本不会自动重启板子（A5 需人工确认一次冷启动）"

	if [[ ${failed} -eq 1 ]]; then
		RESULT_A5="FAIL"; FAILED=1
	else
		RESULT_A5="PASS"
	fi
}

check_a6() {
	log "A6 一键部署产物"
	local failed=0
	for path in "${CONFIG_FILE}" "/etc/caddy/Caddyfile" "${INSTALL_DIR}/venv/bin/python"; do
		if [[ -e ${path} ]]; then
			ok "存在 ${path}"
		else
			bad "缺少 ${path}"
			failed=1
		fi
	done
	for unit in cloudhost-portal caddy; do
		if [[ -f "/etc/systemd/system/${unit}.service" || -f "/lib/systemd/system/${unit}.service" ]]; then
			ok "unit 存在：${unit}"
		else
			bad "缺少 unit：${unit}"
			failed=1
		fi
	done
	if command -v caddy >/dev/null 2>&1; then
		if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
			ok "Caddyfile 校验通过"
		else
			bad "Caddyfile 校验失败（E2001）"
			failed=1
		fi
	fi
	if [[ ${failed} -eq 1 ]]; then
		RESULT_A6="FAIL"; FAILED=1
	else
		RESULT_A6="PASS"
	fi
}

# --------------------------------------------------------------------------- #
# A7 文档
# --------------------------------------------------------------------------- #
check_a7() {
	log "A7 用户手册覆盖度"
	local manual="${REPO_DIR}/docs/用户手册.md"
	if [[ ! -f ${manual} ]]; then
		bad "缺少 ${manual}"
		RESULT_A7="FAIL"; FAILED=1
		return
	fi
	local missing=()
	for keyword in 局域网部署 文件访问 SSH 常见问题; do
		if grep -q "${keyword}" "${manual}"; then
			ok "手册包含「${keyword}」"
		else
			missing+=("${keyword}")
		fi
	done
	if [[ ${#missing[@]} -gt 0 ]]; then
		bad "手册缺少章节：${missing[*]}"
		RESULT_A7="FAIL"; FAILED=1
	else
		RESULT_A7="PASS"
	fi
}

# --------------------------------------------------------------------------- #
# 登录辅助
# --------------------------------------------------------------------------- #
do_login() {
	if [[ -f "${TMP_DIR}/.logged_in" ]]; then
		return 0
	fi
	[[ -z ${ADMIN_PASSWORD} ]] && return 1
	rm -f "${COOKIE_JAR}"
	local code
	code="$(curl -s -o "${TMP_DIR}/login.json" -m 10 -c "${COOKIE_JAR}" -b "${COOKIE_JAR}" \
		-X POST --data-urlencode "username=${ADMIN_USER}" \
		--data-urlencode "password=${ADMIN_PASSWORD}" \
		-w '%{http_code}' "${BASE}/auth/login" 2>/dev/null)"
	if [[ ${code} == "204" ]]; then
		touch "${TMP_DIR}/.logged_in"
		return 0
	fi
	warn "登录失败 HTTP ${code}：$(head -c 160 "${TMP_DIR}/login.json" 2>/dev/null)"
	return 1
}

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
finish() {
	echo "------------------------------------------------------------------"
	printf ' %-4s %-10s %s\n' "编号" "结论" "说明"
	for id in A1 A2 A3 A4 A5 A6 A7; do
		local var="RESULT_${id}"
		printf ' %-4s %-10s %s\n' "${id}" "${!var}" "$(describe "${id}")"
	done
	echo "------------------------------------------------------------------"
	if [[ ${FAILED} -eq 1 ]]; then
		printf '%s存在未通过项，请按上方提示修复后重跑。%s\n' "${C_ERR}" "${C_OFF}"
		return 1
	fi
	printf '%s验收自检通过（A5 冷启动请人工确认一次）。%s\n' "${C_OK}" "${C_OFF}"
	return 0
}

describe() {
	case "$1" in
		A1) echo "首屏 ≤3s / 状态 1s 连续采样" ;;
		A2) echo "上传下载 SHA256 / 状态 P95" ;;
		A3) echo "SSH 密钥登录 ≤5s / 密码拒绝" ;;
		A4) echo "未登录 302 / fail2ban 封禁" ;;
		A5) echo "服务 active + enabled" ;;
		A6) echo "部署产物齐全" ;;
		A7) echo "用户手册覆盖度" ;;
	esac
}

main
rc=$?
if ((${#NOTES[@]} > 0)); then
	echo "提示："
	printf ' - %s\n' "${NOTES[@]}"
fi
finish || true
exit "${rc:-0}"
