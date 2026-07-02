#!/bin/bash
[ -z "${__LF_FIXED:-}" ] && grep -q $'\r' "$0" 2>/dev/null && sed -i 's/\r$//' "$0" && export __LF_FIXED=1 && exec bash "$0" "$@" # self-heal CRLF
# ==============================================================
#  综合代理部署脚本 v3.0 — 多模式 / 多协议 / 多格式 / WARP+ZeroTrust
#  机器类型：NAT小鸡 / 低配VPS / 标准VPS
#  出站模式：直连 / WARP / 双节点
#  核心：sing-box
# ==============================================================
# 注意: 不使用 set -e，因为菜单返回功能需要用 return 1

# ==================== 颜色 / 输出 ====================
if [ -t 1 ]; then
  R='\033[0;31m'; G='\033[0;32m'; Y='\033[0;33m'; B='\033[0;36m'; P='\033[0;35m'; W='\033[0m'
else
  R=''; G=''; Y=''; B=''; P=''; W=''
fi

ui_box() {
    local title="${1:-}" color="${2:-$B}" line
    if [ -n "$title" ]; then
        printf '\n%b%s%b\n' "$color" "$title" "$W"
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
    done
}

ui_log() {
    local color="$1"
    shift
    printf '%b%s%b\n' "$color" "$*" "$W"
}

ui_box_if_text() {
    local title="$1" color="$2" text="$3"
    [ -n "$text" ] && printf '%s\n' "$text" | ui_box "$title" "$color"
}

ui_input_marker() {
    printf '%b> %b' "$Y" "$W"
}

ui_read() {
    local prompt="$1" __var="$2"
    printf '%b%s%b' "$Y" "$prompt" "$W"
    read -r "$__var"
    [ -t 0 ] || printf '\n'
}

ui_read_secret() {
    local prompt="$1" __var="$2"
    printf '%b%s%b' "$Y" "$prompt" "$W"
    read -r -s "$__var"
    printf '\n'
}

green()  { ui_log "$G" "$*"; }
yellow() { ui_log "$Y" "$*"; }
red()    { ui_log "$R" "$*"; }
info()   { ui_log "$B" "$*"; }

# ==================== 全局变量 ====================
WORK_DIR="/etc/sing-box"
CONFIG_FILE="$WORK_DIR/config.json"
INFO_DIR="/root/proxy_info"
LOG_DIR="/tmp/proxy_setup_logs"
SCRIPT_LOG=""
TRACE_LOG=""
MACHINE_MODE=""    # nat / low / standard
OUTBOUND_MODE=""   # direct / warp / dual
PROTOCOL=""        # reality / vless-ws / vmess-ws / hysteria2 / tuic / ss2022
SERVER_IP=""
SERVER_PORT=""
INTERNAL_PORT=""
NODE_NAME="MyProxy"
DOMAIN=""
UUID=""
PASSWORD=""
OBFS_PASSWORD=""
SNI="www.bing.com"
PRIVATE_KEY=""
PUBLIC_KEY=""
SHORT_ID=""
TLS_CERT=""
TLS_KEY=""
WARP_SOCKS_PORT=40000
DIRECT_PORT=""
WARP_PORT=""
WARP_WG_MODE=""    # wireguard / socks5

# ==================== 工具函数 ====================
is_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
is_root() { [[ $EUID -eq 0 ]] || { red "请用 root 运行"; exit 1; }; }

trace_pause() {
    [ "${TRACE_ENABLED:-1}" = "1" ] || return 0
    __TRACE_PAUSE_DEPTH=$(( ${__TRACE_PAUSE_DEPTH:-0} + 1 ))
    if [ "${__TRACE_ACTIVE:-0}" = "1" ] && [ "${__TRACE_PAUSE_DEPTH:-0}" -eq 1 ]; then
        set +x
        __TRACE_ACTIVE=0
    fi
}

trace_resume() {
    [ "${TRACE_ENABLED:-1}" = "1" ] || return 0
    if [ "${__TRACE_PAUSE_DEPTH:-0}" -gt 0 ]; then
        __TRACE_PAUSE_DEPTH=$(( ${__TRACE_PAUSE_DEPTH:-0} - 1 ))
    fi
    if [ "${__TRACE_ACTIVE:-0}" = "0" ] && [ "${__TRACE_PAUSE_DEPTH:-0}" -eq 0 ]; then
        __TRACE_ACTIVE=1
        set -x
    fi
}

init_runtime_logging() {
    [ -n "${__LOGGING_READY:-}" ] && return 0
    mkdir -p "$LOG_DIR"
    chmod 700 "$LOG_DIR" 2>/dev/null || true
    SCRIPT_LOG="$LOG_DIR/proxy_setup_$(date +%Y%m%d_%H%M%S).log"
    TRACE_LOG="$LOG_DIR/proxy_setup_trace_$(date +%Y%m%d_%H%M%S).log"
    exec > >(tee -a "$SCRIPT_LOG") 2>&1
    exec 9>>"$TRACE_LOG"
    export BASH_XTRACEFD=9
    chmod 600 "$SCRIPT_LOG" 2>/dev/null || true
    chmod 600 "$TRACE_LOG" 2>/dev/null || true
    export PS4='+ [${BASH_SOURCE##*/}:${LINENO}:${FUNCNAME[0]}] '
    __LOGGING_READY=1
    __TRACE_ACTIVE=0
    __TRACE_PAUSE_DEPTH=0
    green ">>> 实时日志已开启: $SCRIPT_LOG"
    green ">>> 命令跟踪日志: $TRACE_LOG"
    green ">>> 屏幕输出使用简洁文本；详细命令跟踪写入 trace 日志"
    trace_resume
}

init_runtime_logging

detect_os() {
    if [ -f /etc/alpine-release ]; then
        OS="alpine"; PKG="apk add --no-cache"; SVC="openrc"
    elif [ -f /etc/debian_version ]; then
        OS="debian"; PKG="apt install -y"; SVC="systemd"
    elif [ -f /etc/redhat-release ]; then
        OS="centos"; PKG="yum install -y"; SVC="systemd"
    else
        OS="unknown"; PKG=""; SVC="systemd"
    fi
}

get_ip() {
    SERVER_IP=$(curl -s4m5 ip.sb 2>/dev/null || curl -s4m5 ifconfig.me 2>/dev/null || curl -s4m5 ipv4.ip.sb 2>/dev/null || echo "")
}

gen_uuid()     { UUID=$(cat /proc/sys/kernel/random/uuid); }
gen_password() { PASSWORD=$(openssl rand -base64 16); }
gen_short_id() { SHORT_ID=$(openssl rand -hex 4); }

gen_reality_keys() {
    local sb="$WORK_DIR/sing-box"
    [ -x "$sb" ] || sb="/usr/local/bin/sing-box"
    local keys=$($sb generate reality-keypair 2>/dev/null)
    PRIVATE_KEY=$(echo "$keys" | awk '/PrivateKey/{print $2}')
    PUBLIC_KEY=$(echo "$keys" | awk '/PublicKey/{print $2}')
}

gen_self_signed_cert() {
    mkdir -p "$WORK_DIR/tls"
    openssl ecparam -genkey -name prime256v1 -out "$WORK_DIR/tls/key.pem" 2>/dev/null
    openssl req -new -x509 -days 3650 -key "$WORK_DIR/tls/key.pem" -out "$WORK_DIR/tls/cert.pem" -subj "/CN=bing.com" 2>/dev/null
    chmod 600 "$WORK_DIR/tls/key.pem" 2>/dev/null || true
    TLS_CERT="$WORK_DIR/tls/cert.pem"
    TLS_KEY="$WORK_DIR/tls/key.pem"
}

# ==================== 安装 sing-box ====================
install_singbox() {
    green ">>> 安装依赖..."
    if [ "$OS" = "alpine" ]; then
        apk add --no-cache ca-certificates wget tar curl openssl jq
        apk add --no-cache gcompat libc6-compat || true
    elif [ "$OS" = "debian" ]; then
        apt update -y
        apt install -y ca-certificates wget tar curl openssl jq qrencode
    elif [ "$OS" = "centos" ]; then
        yum install -y ca-certificates wget tar curl openssl jq qrencode
    fi

    green ">>> 获取 sing-box 最新版..."
    local ARCH=$(uname -m)
    case "$ARCH" in x86_64) ARCH="amd64";; aarch64) ARCH="arm64";; armv7l) ARCH="armv7";; esac

    local URL=$(curl -s https://api.github.com/repos/SagerNet/sing-box/releases/latest \
        | grep "browser_download_url" | grep "linux-${ARCH}.tar.gz" \
        | grep -v sha256 | grep -v sbom | head -1 | cut -d'"' -f4)

    # GitHub API 失败时尝试使用镜像
    if [ -z "$URL" ]; then
        yellow ">>> GitHub API 访问失败，尝试使用镜像..."
        URL=$(curl -s https://ghfast.top/https://api.github.com/repos/SagerNet/sing-box/releases/latest \
            | grep "browser_download_url" | grep "linux-${ARCH}.tar.gz" \
            | grep -v sha256 | grep -v sbom | head -1 | cut -d'"' -f4)
    fi

    # 最后兜底：使用已知稳定版本
    if [ -z "$URL" ]; then
        yellow ">>> 镜像也失败了，使用稳定版 v1.11.1..."
        URL="https://github.com/SagerNet/sing-box/releases/download/v1.11.1/sing-box-1.11.1-linux-${ARCH}.tar.gz"
    fi

    # 如果是 GitHub 地址且直连不通，套镜像
    if echo "$URL" | grep -q 'github.com'; then
        if ! curl -sI --connect-timeout 3 "$URL" | head -1 | grep -q '200\|302\|301'; then
            yellow ">>> GitHub 直连不通，使用镜像加速..."
            URL="https://ghfast.top/${URL}"
        fi
    fi

    local TMP="/tmp/singbox_install_$$"
    mkdir -p "$TMP" "$WORK_DIR"
    green ">>> 下载: $URL"
    wget -O "$TMP/sb.tar.gz" "$URL"
    tar -xzf "$TMP/sb.tar.gz" -C "$TMP"
    local BIN=$(find "$TMP" -type f -name sing-box | head -1)
    [ -z "$BIN" ] && { red "未找到 sing-box"; exit 1; }
    install -m 755 "$BIN" /usr/local/bin/sing-box
    cp /usr/local/bin/sing-box "$WORK_DIR/sing-box" 2>/dev/null || true
    rm -rf "$TMP"
    green ">>> sing-box $(sing-box version 2>/dev/null | head -1) 安装完成"
}

# ==================== WARP WireGuard 原生出站 ====================
warp_wg_config_file() { echo "$WORK_DIR/warp_wg.json"; }
warp_wg_config_exists() { [ -f "$(warp_wg_config_file)" ]; }

warp_wg_generate_keypair() {
    local sb="/usr/local/bin/sing-box"
    [ -x "$WORK_DIR/sing-box" ] && sb="$WORK_DIR/sing-box"
    local kp=""
    # sing-box generate
    kp=$("$sb" generate wg-keypair 2>/dev/null) || true
    if [ -n "$kp" ] && echo "$kp" | grep -q "PrivateKey"; then
        echo "$kp"; return 0
    fi
    # wireguard-tools fallback
    if command -v wg >/dev/null 2>&1; then
        local priv pub
        priv=$(wg genkey 2>/dev/null)
        pub=$(echo "$priv" | wg pubkey 2>/dev/null)
        if [ -n "$priv" ] && [ -n "$pub" ]; then
            printf 'PrivateKey: %s\nPublicKey: %s\n' "$priv" "$pub"
            return 0
        fi
    fi
    red "无法生成 WireGuard 密钥"
    red "请确认 sing-box >= 1.3.0 或安装 wireguard-tools"
    return 1
}

warp_register_wireguard() {
    green ">>> 正在注册 WARP WireGuard（无需 warp-cli）..."

    local keypair wg_priv wg_pub
    keypair=$(warp_wg_generate_keypair) || return 1
    wg_priv=$(echo "$keypair" | awk '/PrivateKey/{print $2}')
    wg_pub=$(echo "$keypair" | awk '/PublicKey/{print $2}')
    [ -z "$wg_priv" ] || [ -z "$wg_pub" ] && { red "WireGuard 密钥生成失败"; return 1; }

    local reg_data reg_resp
    reg_data="{\"key\":\"${wg_pub}\",\"install_id\":\"\",\"fcm_token\":\"\",\"tos\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"model\":\"Linux\",\"serial_number\":\"\",\"locale\":\"en_US\"}"

    green ">>> 正在调用 Cloudflare WARP API..."
    reg_resp=$(curl -sS -X POST "https://api.cloudflareclient.com/v0a2158/reg" \
        -H "Content-Type: application/json" \
        -H "User-Agent: okhttp/3.12.1" \
        -d "$reg_data" 2>/dev/null)

    [ -z "$reg_resp" ] && { red "WARP API 无响应，请检查网络"; return 1; }

    local peer_pub client_id v4_addr v6_addr endpoint_host endpoint_addr endpoint_port endpoint_v4_port
    if command -v jq >/dev/null 2>&1; then
        peer_pub=$(printf '%s' "$reg_resp" | jq -r '.config.peers[0].public_key // empty')
        client_id=$(printf '%s' "$reg_resp" | jq -r '.config.client_id // empty')
        v4_addr=$(printf '%s' "$reg_resp" | jq -r '.config.interface.addresses.v4 // empty')
        v6_addr=$(printf '%s' "$reg_resp" | jq -r '.config.interface.addresses.v6 // empty')
        endpoint_host=$(printf '%s' "$reg_resp" | jq -r '.config.peers[0].endpoint.host // empty')
        endpoint_addr=$(printf '%s' "$reg_resp" | jq -r '.config.peers[0].endpoint.v4 // empty')
        endpoint_port=$(printf '%s' "$reg_resp" | jq -r '.config.peers[0].endpoint.ports[0] // empty')
    else
        peer_pub=$(printf '%s' "$reg_resp" | sed -n 's/.*"public_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        client_id=$(printf '%s' "$reg_resp" | sed -n 's/.*"client_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        v4_addr=$(printf '%s' "$reg_resp" | sed -n 's/.*"v4"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        v6_addr=$(printf '%s' "$reg_resp" | sed -n 's/.*"v6"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        endpoint_host=$(printf '%s' "$reg_resp" | sed -n 's/.*"host"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        endpoint_addr=$(printf '%s' "$reg_resp" | sed -n 's/.*"endpoint"[^{]*{[^}]*"v4"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
    fi

    if [ -z "$peer_pub" ] || [ -z "$v4_addr" ]; then
        red "WARP API 返回异常:"
        printf '%s\n' "$reg_resp" | head -20 | ui_box "WARP API 原始返回" "$R"
        return 1
    fi

    # 解码 client_id → reserved 字节
    local r1=0 r2=0 r3=0
    if [ -n "$client_id" ]; then
        local hex
        hex=$(printf '%s' "$client_id" | base64 -d 2>/dev/null | od -A n -t x1 | tr -d ' \n')
        if [ ${#hex} -ge 6 ]; then
            r1=$((16#${hex:0:2}))
            r2=$((16#${hex:2:2}))
            r3=$((16#${hex:4:2}))
        fi
    fi

    if [ -n "$endpoint_host" ] && [ -z "$endpoint_port" ]; then
        endpoint_port=${endpoint_host##*:}
    fi
    if [ -n "$endpoint_addr" ]; then
        endpoint_v4_port=${endpoint_addr##*:}
        endpoint_addr=${endpoint_addr%:*}
        if [ -z "$endpoint_port" ] && [ "$endpoint_v4_port" != "0" ]; then
            endpoint_port=$endpoint_v4_port
        fi
    elif [ -n "$endpoint_host" ]; then
        endpoint_addr=${endpoint_host%:*}
    fi
    : "${endpoint_addr:=engage.cloudflareclient.com}"
    case "$endpoint_port" in
        ''|*[!0-9]*) endpoint_port=2408 ;;
    esac

    mkdir -p "$WORK_DIR"
    cat > "$(warp_wg_config_file)" <<EOWG
{
    "private_key": "${wg_priv}",
    "peer_public_key": "${peer_pub}",
    "reserved": [${r1}, ${r2}, ${r3}],
    "v4_address": "${v4_addr}/32",
    "v6_address": "${v6_addr}/128",
    "endpoint": "${endpoint_addr}",
    "endpoint_port": ${endpoint_port}
}
EOWG
    chmod 600 "$(warp_wg_config_file)"

    green ">>> WARP WireGuard 注册成功!"
    echo "  IPv4: ${v4_addr}"
    echo "  IPv6: ${v6_addr}"
    echo "  Endpoint: ${endpoint_addr}:${endpoint_port}"
    green ">>> 配置已保存: $(warp_wg_config_file)"
}

warp_wg_read_field() {
    local field="$1"
    if command -v jq >/dev/null 2>&1; then
        jq -r ".${field} // empty" "$(warp_wg_config_file)" 2>/dev/null
    else
        sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$(warp_wg_config_file)" | head -1
    fi
}

warp_wg_read_reserved() {
    if command -v jq >/dev/null 2>&1; then
        jq -c '.reserved' "$(warp_wg_config_file)" 2>/dev/null
    else
        sed -n 's/.*"reserved"[[:space:]]*:[[:space:]]*\(\[[^]]*\]\).*/\1/p' "$(warp_wg_config_file)" | head -1
    fi
}

# ==================== WireGuard MTU 自动探测 ====================
detect_optimal_mtu() {
    local target="1.1.1.1"
    local mtu
    for mtu in 1500 1492 1420 1400 1380 1280; do
        if ping -c 1 -W 2 -M do -s $((mtu - 28)) "$target" >/dev/null 2>&1; then
            echo $((mtu - 80))  # WireGuard 开销 (IPv6 worst case)
            return 0
        fi
    done
    echo 1420  # 兜底: WireGuard 推荐默认值
}

# 生成 WireGuard endpoints 配置块 (sing-box 1.11+ 格式)
gen_wg_endpoints_block() {
    local optimal_mtu
    optimal_mtu=$(detect_optimal_mtu)
    local priv peer_pub reserved v4 v6 ep ep_port
    priv=$(warp_wg_read_field "private_key")
    peer_pub=$(warp_wg_read_field "peer_public_key")
    reserved=$(warp_wg_read_reserved)
    v4=$(warp_wg_read_field "v4_address")
    v6=$(warp_wg_read_field "v6_address")
    ep=$(warp_wg_read_field "endpoint")
    ep_port=$(warp_wg_read_field "endpoint_port")
    : "${ep:=engage.cloudflareclient.com}"
    : "${ep_port:=2408}"
    : "${reserved:=[0,0,0]}"
    cat <<EOWGEP
  "endpoints":[
    {
      "type":"wireguard",
      "tag":"out-warp",
      "system":false,
      "mtu":${optimal_mtu},
      "address":["${v4}","${v6}"],
      "private_key":"${priv}",
      "peers":[
        {
          "address":"${ep}",
          "port":${ep_port},
          "public_key":"${peer_pub}",
          "allowed_ips":["0.0.0.0/0","::/0"],
          "persistent_keepalive_interval":25,
          "reserved":${reserved}
        }
      ]
    }
  ],
EOWGEP
}

check_warp_wg_connectivity() {
    warp_wg_config_exists || { yellow "❌ 未注册 WireGuard，可用 WARP 向导注册"; return 1; }
    command -v sing-box >/dev/null 2>&1 || { red "sing-box 未安装"; return 1; }

    local test_dir test_cfg test_log test_port curl_out curl_code
    test_dir="/tmp/sing-box-warp-test-$$"
    test_cfg="$test_dir/config.json"
    test_log="$test_dir/sing-box.log"
    test_port=$((39000 + $$ % 1000))
    mkdir -p "$test_dir"

    cat > "$test_cfg" <<EOCFG
{
  "log":{"level":"debug","timestamp":true},
$(gen_wg_endpoints_block)
  "inbounds":[
    {"type":"mixed","tag":"test-in","listen":"127.0.0.1","listen_port":${test_port}}
  ],
  "outbounds":[
    {"type":"direct","tag":"out-direct"}
  ],
  "route":{"final":"out-warp"}
}
EOCFG

    if ! sing-box check -c "$test_cfg" >/dev/null 2>&1; then
        red "❌ WireGuard 测试配置校验失败"
        sing-box check -c "$test_cfg" || true
        rm -rf "$test_dir"
        return 1
    fi

    sing-box run -c "$test_cfg" >"$test_log" 2>&1 &
    local test_pid=$!
    sleep 2
    curl_out=$(curl -x "socks5h://127.0.0.1:${test_port}" -sS --connect-timeout 6 --max-time 12 https://www.cloudflare.com/cdn-cgi/trace 2>&1)
    curl_code=$?
    kill "$test_pid" >/dev/null 2>&1 || true
    wait "$test_pid" >/dev/null 2>&1 || true

    if [ "$curl_code" -eq 0 ] && printf '%s' "$curl_out" | grep -q '^warp='; then
        green "✅ WARP WireGuard 连通"
        printf '%s\n' "$curl_out" | sed -n '/^ip=/p;/^colo=/p;/^warp=/p' |
            ui_box "Cloudflare trace" "$G"
        rm -rf "$test_dir"
        return 0
    fi

    red "❌ WARP WireGuard 未连通"
    printf '%s\n' "curl: $curl_out" | ui_box "WARP 连通性错误" "$R"
    sed -n '/endpoint\/wireguard/p;/router:/p;/ERROR/p;/FATAL/p;/lookup succeed/p;/exchanged A/p' "$test_log" | tail -40 |
        ui_box "最近 sing-box 日志" "$Y"
    rm -rf "$test_dir"
    return 1
}

check_warp_wg_status() {
    if warp_wg_config_exists; then
        {
            echo "✅ WireGuard 配置已就绪: $(warp_wg_config_file)"
            echo "IPv4: $(warp_wg_read_field v4_address)"
            echo "IPv6: $(warp_wg_read_field v6_address)"
            echo "Endpoint: $(warp_wg_read_field endpoint):$(warp_wg_read_field endpoint_port)"
            echo "Peer: $(warp_wg_read_field peer_public_key | cut -c1-20)..."
        } | ui_box "WARP WireGuard 状态" "$G"
        check_warp_wg_connectivity || true
    else
        yellow "❌ 未注册 WireGuard，可用 WARP 向导注册"
    fi
}

# ==================== WARP 安装向导 ====================
warp_cli() {
    command -v warp-cli >/dev/null 2>&1 || return 127
    if [ -z "${__WARP_TOS_CHECKED:-}" ]; then
        if warp-cli --help 2>&1 | grep -q -- '--accept-tos'; then
            __WARP_TOS_FLAG="--accept-tos"
        else
            __WARP_TOS_FLAG=""
        fi
        __WARP_TOS_CHECKED=1
    fi
    if [ -n "${__WARP_TOS_FLAG:-}" ]; then
        warp-cli "$__WARP_TOS_FLAG" "$@"
    else
        warp-cli "$@"
    fi
}

warp_cli_timeout() {
    local duration="$1"
    shift
    command -v warp-cli >/dev/null 2>&1 || return 127
    if [ -z "${__WARP_TOS_CHECKED:-}" ]; then
        if warp-cli --help 2>&1 | grep -q -- '--accept-tos'; then
            __WARP_TOS_FLAG="--accept-tos"
        else
            __WARP_TOS_FLAG=""
        fi
        __WARP_TOS_CHECKED=1
    fi
    if command -v timeout >/dev/null 2>&1; then
        if [ -n "${__WARP_TOS_FLAG:-}" ]; then
            timeout "$duration" warp-cli "$__WARP_TOS_FLAG" "$@"
        else
            timeout "$duration" warp-cli "$@"
        fi
    else
        warp_cli "$@"
    fi
}

warp_backup_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    cp -f "$f" "${f}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
}

warp_api_get() {
    local api response
    for api in "https://warp.cloudflare.nyc.mn/" "https://warp.cloudflare.now.cc/"; do
        response=$(curl -fsS -G --max-time 25 "$api" "$@" 2>/dev/null) || continue
        [ -n "$response" ] && { printf '%s' "$response"; return 0; }
    done
    return 1
}

warp_extract_json_value() {
    local key="$1" text="$2"
    printf '%s' "$text" | sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -1
}

warp_extract_query_value() {
    local key="$1" text="$2"
    printf '%s' "$text" | tr '\n' ' ' | sed 's/&amp;/\&/g' | grep -o "${key}=[^&\"'<>[:space:]]*" | head -1 | cut -d= -f2-
}

warp_extract_team_token() {
    local org_name="$1" text="$2" token_url="" raw_token=""
    token_url=$(printf '%s' "$text" | tr '\n' ' ' | sed 's/&amp;/\&/g' | grep -o 'com\.cloudflare\.warp://[^"'"'"'<>[:space:]]*' | head -1)
    [ -z "$token_url" ] && token_url=$(warp_extract_json_value "team_token" "$text")
    [ -z "$token_url" ] && token_url=$(warp_extract_json_value "url" "$text")
    if [ -z "$token_url" ]; then
        raw_token=$(warp_extract_json_value "token" "$text")
        if [ -n "$raw_token" ]; then
            if printf '%s' "$raw_token" | grep -q '^com\.cloudflare\.warp://'; then
                token_url="$raw_token"
            else
                token_url="com.cloudflare.warp://${org_name}.cloudflareaccess.com/auth?token=${raw_token}"
            fi
        fi
    fi
    printf '%s' "$token_url"
}

warp_detect_proxy_port() {
    local port=""
    if command -v ss >/dev/null 2>&1; then
        port=$(ss -nltp 2>/dev/null | awk '/warp-svc/{print $4; exit}')
        port=${port##*:}
    fi
    [ -n "$port" ] && printf '%s' "$port" || printf '%s' "$WARP_SOCKS_PORT"
}

warp_settings_is_proxy_mode() {
    local settings="$1"
    [ -z "$settings" ] && settings=$(warp_cli settings 2>/dev/null || true)
    printf '%s\n' "$settings" | grep -Eiq 'WarpProxy|Local proxy mode|Service mode.*proxy'
}

warp_wait_for_proxy_policy() {
    local i=0 settings=""
    while [ "$i" -lt 8 ]; do
        settings=$(warp_cli settings 2>/dev/null || true)
        if warp_settings_is_proxy_mode "$settings"; then
            printf '%s' "$settings"
            return 0
        fi
        sleep 2
        i=$((i + 1))
    done
    return 1
}

warp_wait_for_service() {
    local i=0
    while [ "$i" -lt 20 ]; do
        [ -e /run/cloudflare-warp/warp_service ] && return 0
        if command -v ss >/dev/null 2>&1 && ss -nltp 2>/dev/null | grep -q 'warp-svc'; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

warp_start_service() {
    if command -v systemctl >/dev/null 2>&1; then
        local warp_enabled_state=""
        warp_enabled_state=$(systemctl is-enabled warp-svc 2>/dev/null || true)
        if [ "$warp_enabled_state" = "masked" ]; then
            yellow ">>> 检测到 warp-svc.service 被 masked，正在自动解除..."
            systemctl unmask warp-svc >/dev/null 2>&1 || systemctl unmask warp-svc.service >/dev/null 2>&1 || true
            systemctl daemon-reload >/dev/null 2>&1 || true
            systemctl reset-failed warp-svc >/dev/null 2>&1 || true
        fi
        systemctl enable --now warp-svc || systemctl restart warp-svc || systemctl start warp-svc || true
    elif command -v service >/dev/null 2>&1; then
        service warp-svc start || true
    fi
    warp_wait_for_service || yellow ">>> warp-svc 启动较慢，后续会继续尝试连接"
}

ensure_warp_client_installed() {
    detect_os; is_root
    if ! command -v warp-cli >/dev/null 2>&1; then
        green ">>> 安装 Cloudflare WARP 客户端..."
        if [ "$OS" = "debian" ]; then
            local CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
            [ -z "$CODENAME" ] && CODENAME=$(. /etc/os-release && echo "$UBUNTU_CODENAME")
            [ -z "$CODENAME" ] && { red "无法获取系统代号"; return 1; }
            green ">>> 检测到系统: ${CODENAME}"
            apt update -y
            apt install -y curl gpg ca-certificates
            curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null
            echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${CODENAME} main" > /etc/apt/sources.list.d/cloudflare-client.list
            apt update -y
            apt install -y cloudflare-warp
        elif [ "$OS" = "centos" ]; then
            curl -fsSl https://pkg.cloudflareclient.com/cloudflare-warp-ascii.repo > /etc/yum.repos.d/cloudflare-warp.repo 2>/dev/null
            yum install -y cloudflare-warp
        elif [ "$OS" = "alpine" ]; then
            red "Alpine 不支持官方 WARP 客户端"
            return 1
        else
            red "当前系统暂不支持官方 WARP 客户端"
            return 1
        fi
    else
        green ">>> 已检测到 warp-cli，跳过安装"
    fi

    command -v warp-cli >/dev/null 2>&1 || { red "WARP 安装失败"; return 1; }
    warp_start_service
    green ">>> warp-cli 已就绪"
}

warp_prepare_relogin() {
    local registration=""
    registration=$(warp_cli registration show 2>/dev/null || true)
    if printf '%s' "$registration" | grep -Eq '(^|[[:space:]])ID:|type'; then
        echo ""
        yellow "检测到当前已有 WARP 注册。继续会覆盖现有客户端登录。"
        ui_read "继续覆盖当前注册? (y/N): " confirm
        case "$confirm" in
            y|Y)
                warp_cli disconnect || true
                warp_cli registration delete || true
                sleep 1
                ;;
            *) return 1 ;;
        esac
    fi
    return 0
}

warp_disable_mdm_config() {
    if [ -f /var/lib/cloudflare-warp/mdm.xml ]; then
        yellow ">>> 检测到旧的 MDM 配置，已先备份并停用，避免覆盖新的登录方式"
        warp_backup_file /var/lib/cloudflare-warp/mdm.xml
        rm -f /var/lib/cloudflare-warp/mdm.xml
        if command -v systemctl >/dev/null 2>&1; then
            systemctl restart warp-svc || true
        fi
        warp_wait_for_service || true
    fi
}

warp_configure_proxy_mode() {
    local mode_output="" port_output="" settings_output=""

    settings_output=$(warp_cli settings 2>/dev/null || true)
    if warp_settings_is_proxy_mode "$settings_output"; then
        green ">>> 当前设备已由 Cloudflare 下发为 proxy 模式，跳过本地切换"
        return 0
    fi

    mode_output=$(warp_cli mode proxy 2>&1)
    if [ $? -ne 0 ]; then
        if printf '%s' "$mode_output" | grep -qi 'Operation not authorized in this context'; then
            settings_output=$(warp_wait_for_proxy_policy 2>/dev/null || true)
            if warp_settings_is_proxy_mode "$settings_output"; then
                green ">>> 当前设备已由 Cloudflare 下发为 proxy 模式，跳过本地切换"
                return 0
            fi
        fi
        red "无法切换到 WARP proxy 模式，已中止连接以避免影响当前 SSH。"
        ui_box_if_text "warp-cli mode 输出" "$R" "$mode_output"
        yellow "如果是 Zero Trust 组织策略限制，请改用 5) Service Token / MDM 登录，或先在后台把设备模式调整为 proxy。"
        return 1
    fi

    port_output=$(warp_cli proxy port "${WARP_SOCKS_PORT}" 2>&1)
    if [ $? -ne 0 ]; then
        settings_output=$(warp_cli settings 2>/dev/null || true)
        if warp_settings_is_proxy_mode "$settings_output"; then
            yellow ">>> 代理端口可能由 Cloudflare 后台控制，保留当前端口设置"
            return 0
        fi
        red "无法设置 WARP SOCKS5 端口，已中止连接。"
        ui_box_if_text "warp-cli proxy 输出" "$R" "$port_output"
        return 1
    fi

    ui_box_if_text "warp-cli mode 输出" "$B" "$mode_output"
    ui_box_if_text "warp-cli proxy 输出" "$B" "$port_output"
    return 0
}

warp_connect_and_report() {
    warp_start_service
    warp_configure_proxy_mode || return 1
    warp_cli connect || true
    sleep 3
    check_warp_status
}

warp_login_free() {
    ensure_warp_client_installed || return
    warp_prepare_relogin || return
    warp_disable_mdm_config
    green ">>> 注册免费 WARP..."
    warp_cli registration new || warp_cli register || {
        red "免费 WARP 注册失败"
        return 1
    }
    warp_connect_and_report
}

warp_login_browser() {
    local org_name=""
    ensure_warp_client_installed || return
    printf '%s\n' \
        "需要在浏览器里完成 Cloudflare Zero Trust 登录。" |
        ui_box "浏览器授权登录 Zero Trust" "$Y"
    ui_read "组织名: " org_name
    [ -z "$org_name" ] && { red "组织名不能为空"; return 1; }
    warp_prepare_relogin || return
    warp_disable_mdm_config
    green ">>> 正在拉起浏览器授权流程..."
    warp_cli registration new "$org_name" || warp_cli teams-enroll "$org_name" || {
        red "浏览器授权发起失败"
        return 1
    }
    yellow "如果浏览器成功后终端仍提示 Registration missing，可回到本菜单选择“手动粘贴 Team Token URL”。"
    warp_connect_and_report
}

warp_apply_team_token() {
    local team_token="$1"
    local token_output="" token_rc=0
    [ -z "$team_token" ] && { red "Team Token URL 不能为空"; return 1; }
    ensure_warp_client_installed || return
    warp_prepare_relogin || return
    warp_disable_mdm_config
    green ">>> 写入 Zero Trust Team Token..."
    yellow ">>> 这一步通常几十秒内就会返回；如果超过 60 秒，多半不是正常现象"
    token_output=$(warp_cli_timeout 60s registration token "$team_token" 2>&1)
    token_rc=$?
    if [ "$token_rc" -ne 0 ]; then
        yellow ">>> registration token 返回:"
        ui_box_if_text "registration token 输出" "$Y" "$token_output"
        yellow ">>> 尝试兼容旧版命令 teams-enroll-token ..."
        token_output=$(warp_cli_timeout 60s teams-enroll-token "$team_token" 2>&1)
        token_rc=$?
    fi
    if [ "$token_rc" -eq 124 ]; then
        red "Team Token 提交超时。通常不是正常现象，常见原因是 token 已过期，或当前 warp-cli 版本/环境卡在注册阶段。"
        yellow "建议先改用 4) 手动粘贴最新 Team Token URL，或者 5) Service Token / MDM 登录。"
        ui_box_if_text "Team Token 输出" "$R" "$token_output"
        return 1
    fi
    if [ "$token_rc" -ne 0 ]; then
        red "Team Token 登录失败"
        ui_box_if_text "Team Token 输出" "$R" "$token_output"
        yellow "如果输出里有 401，一般就是 token 过期了，需要重新获取并尽快提交。"
        return 1
    fi
    warp_connect_and_report
}

warp_login_token_url() {
    local team_token=""
    printf '%s\n' \
        "格式类似: com.cloudflare.warp://<team>.cloudflareaccess.com/auth?token=..." |
        ui_box "手动粘贴 Team Token URL" "$Y"
    trace_pause
    ui_read "Team Token URL: " team_token
    warp_apply_team_token "$team_token"
    local rc=$?
    trace_resume
    return "$rc"
}

warp_login_zero_trust_api() {
    local org_name="" login_email="" otp_code="" step1="" step2=""
    local cf_appsession="" cf_session="" nonce="" team_token=""
    ensure_warp_client_installed || return
    printf '%s\n' \
        "流程参考 fscarmen/warp 文档: 组织名 + 邮箱 + 验证码 => Team Token" \
        "如果接口暂时不可用，可改用浏览器授权或手动粘贴 Team Token URL。" |
        ui_box "自动 Zero Trust 登录" "$Y"
    ui_read "组织名: " org_name
    ui_read "登录邮箱: " login_email
    [ -z "$org_name" ] && { red "组织名不能为空"; return 1; }
    [ -z "$login_email" ] && { red "邮箱不能为空"; return 1; }

    green ">>> 正在请求发送验证码..."
    trace_pause
    step1=$(warp_api_get --data-urlencode "run=token" --data-urlencode "organization=${org_name}" --data-urlencode "email=${login_email}") || {
        trace_resume
        red "Zero Trust token 接口访问失败"
        return 1
    }
    trace_resume
    cf_appsession=$(warp_extract_json_value "cf_appsession" "$step1")
    [ -z "$cf_appsession" ] && cf_appsession=$(warp_extract_query_value "cf_appsession" "$step1")
    cf_session=$(warp_extract_json_value "cf_session" "$step1")
    [ -z "$cf_session" ] && cf_session=$(warp_extract_query_value "cf_session" "$step1")
    nonce=$(warp_extract_json_value "nonce" "$step1")
    [ -z "$nonce" ] && nonce=$(warp_extract_query_value "nonce" "$step1")

    if [ -z "$cf_appsession" ] || [ -z "$cf_session" ] || [ -z "$nonce" ]; then
        yellow "接口返回无法自动解析，原始返回如下:"
        ui_box_if_text "Zero Trust 接口返回" "$Y" "$step1"
        yellow "请改用浏览器授权，或从返回内容中取值后手动处理。"
        return 1
    fi

    ui_read "邮箱收到的验证码: " otp_code
    [ -z "$otp_code" ] && { red "验证码不能为空"; return 1; }
    green ">>> 正在换取 Team Token..."
    trace_pause
    step2=$(warp_api_get \
        --data-urlencode "run=token" \
        --data-urlencode "organization=${org_name}" \
        --data-urlencode "cf_appsession=${cf_appsession}" \
        --data-urlencode "cf_session=${cf_session}" \
        --data-urlencode "nonce=${nonce}" \
        --data-urlencode "code=${otp_code}") || {
        trace_resume
        red "验证码校验失败或接口暂时不可用"
        return 1
    }
    trace_resume
    team_token=$(warp_extract_team_token "$org_name" "$step2")
    if [ -z "$team_token" ]; then
        yellow "未能自动提取 Team Token，原始返回如下:"
        ui_box_if_text "Zero Trust 接口返回" "$Y" "$step2"
        yellow "你可以从返回内容中复制 com.cloudflare.warp://... 链接，再走“手动粘贴 Team Token URL”。"
        return 1
    fi

    trace_pause
    warp_apply_team_token "$team_token"
    local rc=$?
    trace_resume
    return "$rc"
}

warp_login_service_token() {
    local org_name="" client_id="" client_secret=""
    ensure_warp_client_installed || return
    printf '%s\n' \
        "适合无浏览器的 VPS。需要在 Cloudflare 后台先创建 Service Token。" \
        "路径: Access controls -> Service credentials -> Service Tokens" \
        "并在 Team & Resources -> Devices -> Device enrollment permissions 里把策略 Action 设为 Service Auth。" |
        ui_box "Service Token / MDM 登录" "$Y"
    ui_read "组织名: " org_name
    ui_read "Client ID: " client_id
    trace_pause
    ui_read_secret "Client Secret: " client_secret
    trace_resume
    [ -z "$org_name" ] && { red "组织名不能为空"; return 1; }
    [ -z "$client_id" ] && { red "Client ID 不能为空"; return 1; }
    [ -z "$client_secret" ] && { red "Client Secret 不能为空"; return 1; }

    warp_prepare_relogin || return
    mkdir -p /var/lib/cloudflare-warp
    warp_backup_file /var/lib/cloudflare-warp/mdm.xml
    cat > /var/lib/cloudflare-warp/mdm.xml <<EOF
<dict>
    <key>auth_client_id</key>
    <string>${client_id}</string>
    <key>auth_client_secret</key>
    <string>${client_secret}</string>
    <key>auto_connect</key>
    <integer>1</integer>
    <key>onboarding</key>
    <false/>
    <key>organization</key>
    <string>${org_name}</string>
    <key>service_mode</key>
    <string>proxy</string>
    <key>proxy_port</key>
    <integer>${WARP_SOCKS_PORT}</integer>
</dict>
EOF
    chmod 600 /var/lib/cloudflare-warp/mdm.xml 2>/dev/null || true
    green ">>> 已写入 /var/lib/cloudflare-warp/mdm.xml"

    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart warp-svc || true
    fi
    warp_start_service
    warp_configure_proxy_mode || return 1
    warp_cli connect || true
    sleep 3
    check_warp_status
}

install_warp_client() {
    trace_pause
    cat <<'EOF' | ui_box "Cloudflare WARP 安装 / 登录向导" "$B"
1) 免费 WARP 注册
   - 普通 WARP 账号，适合只需要基础 Cloudflare 出口
2) Zero Trust 自动登录 (组织名 + 邮箱 + 验证码)
   - 自动向接口换取 Team Token，适合能收邮箱验证码的场景
3) Zero Trust 浏览器授权 (warp-cli registration new)
   - 在浏览器里完成授权，最接近官方交互流程
4) 手动粘贴 Team Token URL
   - 已拿到 com.cloudflare.warp://... 链接时使用
5) Service Token / MDM 登录
   - 无浏览器 VPS 推荐，适合 Cloudflare Access 服务令牌
6) 检查 WARP 状态
   - 查看注册、连接、代理端口和当前出口 IP
7) WireGuard 模式注册 (推荐)
   - 不需要 warp-cli，sing-box 直连 Cloudflare
   - 更轻量更稳定，Alpine / NAT 小鸡也能用
0) 返回主菜单
   - 不更改 WARP 配置，回到综合管理菜单
EOF
    ui_read "请选择 [0-7]: " warp_choice
    trace_resume
    case "$warp_choice" in
        1) warp_login_free ;;
        2) warp_login_zero_trust_api ;;
        3) warp_login_browser ;;
        4) warp_login_token_url ;;
        5) warp_login_service_token ;;
        6) check_warp_status ;;
        7) warp_register_wireguard ;;
        0) show_manage_menu; return ;;
        *) red "无效" ;;
    esac
}

check_warp_status() {
    check_warp_wg_status
    local cli_status="" cli_settings="" cli_registration="" port=""
    local warp_ip4="" warp_ip6="" real_ip4="" real_ip6="" mdm_org=""
    local svc_enabled="" svc_active=""
    command -v warp-cli >/dev/null 2>&1 || { yellow "warp-cli 未安装 (WireGuard 模式无需安装)"; return; }

    cli_status=$(warp_cli status 2>/dev/null || true)
    cli_settings=$(warp_cli settings 2>/dev/null || true)
    cli_registration=$(warp_cli registration show 2>/dev/null || true)
    [ -n "$cli_status" ] && printf '%s\n' "$cli_status" | ui_box "WARP CLI 状态" "$G"
    [ -n "$cli_settings" ] && printf '%s\n' "$cli_settings" | ui_box "WARP 客户端设置" "$G"
    [ -n "$cli_registration" ] && printf '%s\n' "$cli_registration" | ui_box "WARP 注册信息" "$G"

    if command -v systemctl >/dev/null 2>&1; then
        svc_enabled=$(systemctl is-enabled warp-svc 2>/dev/null || true)
        svc_active=$(systemctl is-active warp-svc 2>/dev/null || true)
        {
            [ -n "$svc_enabled" ] && echo "systemd enable: $svc_enabled"
            [ -n "$svc_active" ] && echo "systemd active: $svc_active"
        } | ui_box "warp-svc 服务状态" "$B"
        [ "$svc_enabled" = "masked" ] && yellow ">>> warp-svc.service 当前被 masked，需先 unmask 后才能正常启动"
    fi

    if [ -f /var/lib/cloudflare-warp/mdm.xml ]; then
        mdm_org=$(sed -n '/<key>organization<\/key>/{n;s#.*<string>\(.*\)</string>.*#\1#;p;}' /var/lib/cloudflare-warp/mdm.xml | head -1)
        yellow ">>> 检测到 MDM 配置: /var/lib/cloudflare-warp/mdm.xml${mdm_org:+ (organization: ${mdm_org})}"
    fi

    port=$(warp_detect_proxy_port)
    if (echo > /dev/tcp/127.0.0.1/$port) >/dev/null 2>&1; then
        warp_ip4=$(curl -x socks5h://127.0.0.1:${port} -s4m6 ip.sb 2>/dev/null)
        warp_ip6=$(curl -x socks5h://127.0.0.1:${port} -s6m6 ipv6.ip.sb 2>/dev/null)
        real_ip4=$(curl -s4m5 ip.sb 2>/dev/null)
        real_ip6=$(curl -s6m5 ipv6.ip.sb 2>/dev/null)
        {
            echo "✅ WARP SOCKS5 运行中 :${port}"
            [ -n "$real_ip4" ] && echo "真实 IPv4: ${real_ip4}"
            [ -n "$real_ip6" ] && echo "真实 IPv6: ${real_ip6}"
            [ -n "$warp_ip4" ] && echo "WARP IPv4: ${warp_ip4}"
            [ -n "$warp_ip6" ] && echo "WARP IPv6: ${warp_ip6}"
        } | ui_box "WARP 出口 IP" "$G"
    else
        red "❌ WARP 端口 ${port} 未开启"
    fi
}

# ==================== 服务管理 ====================
setup_service() {
    if [ "$SVC" = "openrc" ]; then
        cat > /etc/init.d/sing-box <<'EOSVC'
#!/sbin/openrc-run
name="sing-box"
description="sing-box proxy service"
command="/usr/local/bin/sing-box"
command_args="run -c /etc/sing-box/config.json"
command_background=true
pidfile="/run/${RC_SVCNAME}.pid"
depend() { need net; }
EOSVC
        chmod +x /etc/init.d/sing-box
        rc-update add sing-box default >/dev/null 2>&1 || true
        rc-service sing-box stop || true
        rc-service sing-box start
    else
        cat > /etc/systemd/system/sing-box.service <<EOSVC
[Unit]
Description=sing-box proxy service
After=network.target
[Service]
User=root
ExecStart=/usr/local/bin/sing-box run -c $CONFIG_FILE
Restart=no
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
EOSVC
        systemctl daemon-reload
        systemctl enable sing-box
        systemctl restart sing-box
    fi
    
    green ">>> 服务已尝试启动，正在进行 8 秒稳定性与证书申请安全验证，请稍候..."
    sleep 8
    
    # 严格状态检查
    if [ "$SVC" = "openrc" ]; then
        if ! rc-service sing-box status | grep -q "started"; then
            red "❌ [一票否决] sing-box 服务启动失败！正在自动清理所有节点和配置..."
            cleanup_proxy_artifacts
            red ">>> 清理完成。安装已终止。"
            exit 1
        fi
    else
        if ! systemctl is-active --quiet sing-box; then
            red "❌ [一票否决] sing-box 服务启动失败！"
            red "================== 失败原因分析 =================="
            # 提取具体的错误信息或 FATAL 日志
            journalctl -u sing-box --no-pager -n 20 | grep -E -i "fatal|error|failed" | sed 's/^/  /' || true
            red "=================================================="
            red ">>> 正在自动执行深度清理，不保留任何残余节点..."
            
            # 关闭并彻底清理
            close_proxy_firewall
            cleanup_proxy_artifacts
            cleanup_warp_client
            cleanup_common_dependencies
            
            red ">>> 深度清理完成。所有配置和节点已完全删除，安装已安全终止。"
            exit 1
        fi
    fi
    green ">>> 服务已完美启动并成功通过安全验证！"
}

open_firewall() {
    local port=$1
    command -v iptables >/dev/null 2>&1 && {
        iptables -I INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
        iptables -I INPUT -p udp --dport "$port" -j ACCEPT 2>/dev/null || true
    }
    command -v ufw >/dev/null 2>&1 && ufw allow "$port" >/dev/null 2>&1 || true
    command -v firewall-cmd >/dev/null 2>&1 && {
        firewall-cmd --zone=public --add-port="$port/tcp" --permanent >/dev/null 2>&1
        firewall-cmd --zone=public --add-port="$port/udp" --permanent >/dev/null 2>&1
        firewall-cmd --reload >/dev/null 2>&1
    }
}

collect_proxy_ports() {
    local ports=""
    if [ -f "$CONFIG_FILE" ]; then
        if command -v jq >/dev/null 2>&1; then
            ports=$(jq -r '.inbounds[]?.listen_port // empty' "$CONFIG_FILE" 2>/dev/null || true)
        else
            ports=$(sed -n 's/.*"listen_port"[[:space:]]*:[[:space:]]*\([0-9]\+\).*/\1/p' "$CONFIG_FILE" 2>/dev/null || true)
        fi
    fi
    printf '%s\n' "$ports" | awk '/^[0-9]+$/ && $1 >= 1 && $1 <= 65535 {print $1}' | sort -n -u
}

close_firewall_port() {
    local port="$1" proto=""
    is_port "$port" || return 0

    for proto in tcp udp; do
        if command -v iptables >/dev/null 2>&1; then
            while iptables -C INPUT -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1; do
                iptables -D INPUT -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1 || break
            done
        fi
        if command -v ip6tables >/dev/null 2>&1; then
            while ip6tables -C INPUT -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1; do
                ip6tables -D INPUT -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1 || break
            done
        fi
        if command -v firewall-cmd >/dev/null 2>&1; then
            firewall-cmd --zone=public --remove-port="$port/$proto" >/dev/null 2>&1 || true
            firewall-cmd --zone=public --remove-port="$port/$proto" --permanent >/dev/null 2>&1 || true
        fi
    done

    if command -v ufw >/dev/null 2>&1; then
        ufw --force delete allow "$port" >/dev/null 2>&1 || true
        ufw --force delete allow "$port/tcp" >/dev/null 2>&1 || true
        ufw --force delete allow "$port/udp" >/dev/null 2>&1 || true
    fi
}

close_proxy_firewall() {
    local ports port
    ports=$(collect_proxy_ports)
    [ -z "$ports" ] && return 0
    green ">>> 清理脚本放行过的本机防火墙端口..."
    for port in $ports; do
        close_firewall_port "$port"
    done
    command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 || true
}

cleanup_warp_client() {
    green ">>> 清理 Cloudflare WARP 客户端和配置..."

    if command -v warp-cli >/dev/null 2>&1; then
        warp_cli disconnect >/dev/null 2>&1 || true
        warp_cli registration delete >/dev/null 2>&1 || true
    fi

    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop warp-svc >/dev/null 2>&1 || true
        systemctl disable warp-svc >/dev/null 2>&1 || true
        systemctl reset-failed warp-svc >/dev/null 2>&1 || true
    fi
    command -v service >/dev/null 2>&1 && service warp-svc stop >/dev/null 2>&1 || true
    pkill -x warp-svc >/dev/null 2>&1 || true

    case "$OS" in
        debian)
            apt purge -y cloudflare-warp >/dev/null 2>&1 || true
            rm -f /etc/apt/sources.list.d/cloudflare-client.list*
            rm -f /etc/apt/trusted.gpg.d/cloudflare*
            rm -f /usr/share/keyrings/cloudflare*
            rm -f /var/cache/apt/archives/cloudflare-warp*
            apt autoremove --purge -y >/dev/null 2>&1 || true
            apt clean >/dev/null 2>&1 || true
            ;;
        centos)
            yum remove -y cloudflare-warp >/dev/null 2>&1 || true
            rm -f /etc/yum.repos.d/cloudflare-warp.repo*
            yum clean all >/dev/null 2>&1 || true
            command -v dnf >/dev/null 2>&1 && dnf clean all >/dev/null 2>&1 || true
            ;;
        alpine)
            apk del cloudflare-warp >/dev/null 2>&1 || true
            ;;
    esac

    rm -f /etc/systemd/system/warp-svc.service
    rm -rf /etc/systemd/system/warp-svc.service.d
    rm -f /etc/systemd/system/multi-user.target.wants/warp-svc.service
    command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload >/dev/null 2>&1 || true
    rm -rf /var/lib/cloudflare-warp /var/log/cloudflare-warp /run/cloudflare-warp /etc/cloudflare-warp
    rm -f /usr/bin/warp-cli /usr/bin/warp-svc /usr/bin/warp-diag
    rm -f /usr/local/bin/warp-cli /usr/local/bin/warp-svc /usr/local/bin/warp-diag
}

cleanup_common_dependencies() {
    local c
    cat <<'EOF' | ui_box "通用依赖清理确认" "$R"
强烈不建议删除通用依赖。
这些包可能被系统、SSH 运维脚本、证书更新、其他代理或自动化任务使用:
ca-certificates wget tar curl openssl jq qrencode
建议直接回车保留；只有你确认这台机器专门用于本脚本且不再使用时才输入 y。
EOF
    ui_read "仍要强制清理这些通用依赖? (y/n) [n]: " c
    [ "${c:-n}" = "y" ] || return 0

    case "$OS" in
        debian)
            apt purge -y ca-certificates wget tar curl openssl jq qrencode >/dev/null 2>&1 || true
            apt autoremove --purge -y >/dev/null 2>&1 || true
            apt clean >/dev/null 2>&1 || true
            ;;
        centos)
            yum remove -y ca-certificates wget tar curl openssl jq qrencode >/dev/null 2>&1 || true
            yum clean all >/dev/null 2>&1 || true
            ;;
        alpine)
            apk del ca-certificates wget tar curl openssl jq qrencode gcompat libc6-compat >/dev/null 2>&1 || true
            ;;
    esac
}

cleanup_proxy_artifacts() {
    green ">>> 清理 sing-box 服务、二进制、配置、节点信息和临时文件..."
    if [ "$SVC" = "openrc" ]; then
        rc-service sing-box stop >/dev/null 2>&1 || true
        rc-update del sing-box >/dev/null 2>&1 || true
        rm -f /etc/init.d/sing-box
        rm -f /etc/runlevels/default/sing-box
    else
        systemctl stop sing-box >/dev/null 2>&1 || true
        systemctl disable sing-box >/dev/null 2>&1 || true
        rm -f /etc/systemd/system/sing-box.service
        rm -rf /etc/systemd/system/sing-box.service.d
        rm -f /etc/systemd/system/multi-user.target.wants/sing-box.service
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl reset-failed sing-box >/dev/null 2>&1 || true
    fi
    pkill -x sing-box >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR" "$INFO_DIR" "$LOG_DIR" /var/log/sing-box
    rm -rf /tmp/sing-box-warp-test-* /tmp/singbox_install_*
    rm -f /usr/local/bin/sing-box
}

# 重新覆盖三步菜单：保留选项说明，同时避免逐行 echo 刷屏
select_machine_mode() {
    while true; do
        trace_pause
        cat <<'EOF' | ui_box "第一步：选择机器类型" "$B"
1) NAT 小鸡
   - 无独立公网 IP，适合面板端口映射 / Alpine / OpenRC
2) 低配 VPS
   - 独立公网 IP，内存较小，走保守配置
3) 标准 VPS
   - 独立公网 IP，资源更充足，功能最完整

0) 返回主菜单
   - 不继续安装，回到综合管理菜单
EOF
        ui_read "请选择 [0-3]: " mode_num
        trace_resume
        case "$mode_num" in
            1) MACHINE_MODE="nat"; return 0 ;;
            2) MACHINE_MODE="low"; return 0 ;;
            3) MACHINE_MODE="standard"; return 0 ;;
            0) return 1 ;;
            *) red "无效选择，请重新选择" ;;
        esac
    done
}

select_protocol() {
    while true; do
        trace_pause
        if [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ]; then
            cat <<'EOF' | ui_box "第二步：选择协议" "$B"
1) VLESS + Reality
   - 抗封锁最强，不需要域名，优先推荐
2) Shadowsocks 2022
   - 配置最简单，兼容性好
3) Hysteria2
   - UDP / QUIC，高速下载，依赖 UDP 环境
4) TUIC v5
   - UDP / QUIC，更稳一些，适合实时场景
5) VLESS + WS + TLS
   - 走 CDN，中转稳定，需要域名
6) VMess + WS + TLS
   - 兼容老客户端，需要域名
7) 自由全协议模式
   - 6种直连协议 + 1个WARP节点 = 7个节点
   - 每个协议单独询问是否安装，需要域名的协议可跳过

0) 返回上一步
   - 回到机器类型选择
EOF
        else
            cat <<'EOF' | ui_box "第二步：选择协议" "$B"
1) VLESS + Reality
   - 抗封锁最强，不需要域名，优先推荐
2) Shadowsocks 2022
   - 配置最简单，兼容性好

提示:
   - NAT 小鸡当前只支持 1) 和 2)

0) 返回上一步
   - 回到机器类型选择
EOF
        fi
        ui_read "请选择协议 [编号]: " proto_num
        trace_resume
        case "$proto_num" in
            1) PROTOCOL="reality"; return 0 ;;
            2) PROTOCOL="ss2022"; return 0 ;;
            3) [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ] && { PROTOCOL="hysteria2"; return 0; } || red "当前模式不可用" ;;
            4) [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ] && { PROTOCOL="tuic"; return 0; } || red "当前模式不可用" ;;
            5) [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ] && { PROTOCOL="vless-ws"; return 0; } || red "当前模式不可用" ;;
            6) [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ] && { PROTOCOL="vmess-ws"; return 0; } || red "当前模式不可用" ;;
            7) [ "$MACHINE_MODE" = "standard" ] || [ "$MACHINE_MODE" = "low" ] && { PROTOCOL="free-multi"; return 0; } || red "当前模式不可用" ;;
            0) return 1 ;;
            *) red "无效选择" ;;
        esac
    done
}

select_outbound_mode() {
    while true; do
        trace_pause
        if [ "$MACHINE_MODE" != "nat" ]; then
            cat <<EOF | ui_box "第三步：选择出站模式" "$B"
已选协议: ${PROTOCOL}

1) 直连出站
   - 显示 VPS 自己的 IP
   - 速度最快，适合日常使用
2) WARP 出站
   - 显示 Cloudflare 的 IP
   - 适合需要 WARP 出口的场景
3) 双节点模式
   - 同时生成直连节点和 WARP 节点
   - 平时走直连，需要时切到 WARP

0) 返回上一步
   - 回到协议选择
EOF
        else
            cat <<EOF | ui_box "第三步：选择出站模式" "$B"
已选协议: ${PROTOCOL}

1) 直连出站
   - 显示 VPS 自己的 IP
   - 速度最快，适合日常使用

提示:
   - NAT 小鸡不支持 WARP / 双节点

0) 返回上一步
   - 回到协议选择
EOF
        fi
        ui_read "请选择: " out_num
        trace_resume
        case "$out_num" in
            1) OUTBOUND_MODE="direct"; return 0 ;;
            2)
                [ "$MACHINE_MODE" = "nat" ] && { red "NAT 小鸡不支持 WARP"; continue; }
                OUTBOUND_MODE="warp"; return 0 ;;
            3)
                [ "$MACHINE_MODE" = "nat" ] && { red "NAT 小鸡不支持 WARP"; continue; }
                OUTBOUND_MODE="dual"; return 0 ;;
            0) return 1 ;;
            *) red "无效选择" ;;
        esac
    done
}

# ==================== 第四步：收集信息 ====================
collect_info() {
    printf '%s\n' "接下来填写节点名称、监听端口、域名和 WARP 出站方式。" |
        ui_box "第四步：配置信息" "$B"

    ui_read "节点备注名称 [MyProxy]: " tmp; NODE_NAME="${tmp:-MyProxy}"
    # 清理中文标点，防止乱码（全角破折号→普通横杠，全角逗号/句号等去掉）
    NODE_NAME=$(echo "$NODE_NAME" | sed 's/——/-/g; s/—/-/g; s/，/-/g; s/。//g; s/：/-/g; s/（/(/g; s/）/)/g')
    get_ip

    if [ "$MACHINE_MODE" = "nat" ]; then
        yellow "NAT小鸡没有独立公网IP，填面板映射页面显示的公网IP"
        ui_read "面板映射公网IP [$SERVER_IP]: " tmp; SERVER_IP="${tmp:-$SERVER_IP}"
        ui_read "内部监听端口 [443]: " tmp; INTERNAL_PORT="${tmp:-443}"
        ui_read "外部映射端口 [443]: " tmp; SERVER_PORT="${tmp:-443}"
    else
        ui_read "服务器公网IP [$SERVER_IP]: " tmp; SERVER_IP="${tmp:-$SERVER_IP}"
        case "$PROTOCOL" in
            ss2022) ui_read "监听端口 [8388]: " tmp; SERVER_PORT="${tmp:-8388}" ;;
            free-multi) ui_read "起始端口 [443]: " tmp; SERVER_PORT="${tmp:-443}" ;;
            *)      ui_read "监听端口 [443]: " tmp;  SERVER_PORT="${tmp:-443}" ;;
        esac
        INTERNAL_PORT="$SERVER_PORT"
    fi
    [ -z "$SERVER_IP" ] && { red "IP 不能为空"; exit 1; }

    # 自由全协议模式
    if [ "$PROTOCOL" = "free-multi" ]; then
        collect_free_multi_protocols
        return $?
    fi

    # CDN 域名
    if [ "$PROTOCOL" = "vless-ws" ] || [ "$PROTOCOL" = "vmess-ws" ]; then
        yellow "CDN中转需要一个已托管到 Cloudflare 的域名"
        ui_read "请输入域名: " DOMAIN
        [ -z "$DOMAIN" ] && { red "域名不能为空"; exit 1; }
    fi

    # Reality SNI
    [ "$PROTOCOL" = "reality" ] && { ui_read "Reality 伪装 SNI [www.bing.com]: " tmp; SNI="${tmp:-www.bing.com}"; }

    # 双节点端口
    if [ "$OUTBOUND_MODE" = "dual" ]; then
        yellow "双节点模式需要两个端口"
        DIRECT_PORT="$SERVER_PORT"
        local dp2=$((SERVER_PORT + 1))
        ui_read "直连节点端口 [${DIRECT_PORT}]: " tmp; DIRECT_PORT="${tmp:-$DIRECT_PORT}"
        ui_read "WARP节点端口 [${dp2}]: " tmp; WARP_PORT="${tmp:-$dp2}"
    fi

    # WARP 出站方式选择
    if [ "$OUTBOUND_MODE" = "warp" ] || [ "$OUTBOUND_MODE" = "dual" ]; then
        cat <<'EOWARPMODE' | ui_box "WARP 出站方式" "$B"
1) WireGuard 直连 (推荐)
   - 不需要安装 warp-cli，sing-box 原生支持
   - 更轻量、更稳定，Alpine 也能用
2) 传统 SOCKS5 代理
   - 需要先用主菜单第5项安装 WARP 客户端
   - 通过 warp-cli 本地 SOCKS5 中转
EOWARPMODE
        ui_read "请选择 [1/2] [1]: " warp_mode_choice
        case "${warp_mode_choice:-1}" in
            2)
                WARP_WG_MODE="socks5"
                ui_read "WARP SOCKS5 本地端口 [40000]: " tmp; WARP_SOCKS_PORT="${tmp:-40000}"
                if ! (echo > /dev/tcp/127.0.0.1/$WARP_SOCKS_PORT) >/dev/null 2>&1; then
                    yellow "⚠️  WARP 端口 ${WARP_SOCKS_PORT} 未检测到"
                    yellow "请先用主菜单第5项安装 WARP"
                    ui_read "继续安装? (y/n) [y]: " tmp
                    [ "${tmp:-y}" != "y" ] && return 1
                fi
                ;;
            *)
                WARP_WG_MODE="wireguard"
                green ">>> 已选择 WireGuard 模式，将在安装 sing-box 后自动注册"
                ;;
        esac
    fi

    return 0
}

# 自由全协议模式：逐个询问协议
collect_free_multi_protocols() {
    # 协议数组
    MULTI_PROTOCOLS=()
    MULTI_PORTS=()
    MULTI_DOMAINS=()

    local current_port=$SERVER_PORT

    cat <<'EOF' | ui_box "自由全协议模式" "$Y"
将逐个询问6种直连协议 + 1个WARP节点
每个协议可选择安装(y)或跳过(n)，默认y
需要域名的协议可以输入域名或输入skip跳过
EOF

    # 1. VLESS + Reality
    ui_read "安装 VLESS+Reality? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        MULTI_PROTOCOLS+=("reality")
        MULTI_PORTS+=("$current_port")
        MULTI_DOMAINS+=("")
        current_port=$((current_port + 1))
    fi

    # 2. Shadowsocks 2022
    ui_read "安装 Shadowsocks 2022? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        MULTI_PROTOCOLS+=("ss2022")
        MULTI_PORTS+=("$current_port")
        MULTI_DOMAINS+=("")
        current_port=$((current_port + 1))
    fi

    # 3. Hysteria2
    ui_read "安装 Hysteria2? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        MULTI_PROTOCOLS+=("hysteria2")
        MULTI_PORTS+=("$current_port")
        MULTI_DOMAINS+=("")
        current_port=$((current_port + 1))
    fi

    # 4. TUIC v5
    ui_read "安装 TUIC v5? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        MULTI_PROTOCOLS+=("tuic")
        MULTI_PORTS+=("$current_port")
        MULTI_DOMAINS+=("")
        current_port=$((current_port + 1))
    fi

    # 5. VLESS + WS + TLS
    ui_read "安装 VLESS+WS+TLS? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        yellow "需要一个已托管到 Cloudflare 的域名"
        ui_read "请输入域名 (或输入skip跳过): " domain_input
        if [ "$domain_input" != "skip" ] && [ -n "$domain_input" ]; then
            MULTI_PROTOCOLS+=("vless-ws")
            MULTI_PORTS+=("$current_port")
            MULTI_DOMAINS+=("$domain_input")
            current_port=$((current_port + 1))
        else
            yellow "已跳过 VLESS+WS+TLS"
        fi
    fi

    # 6. VMess + WS + TLS
    ui_read "安装 VMess+WS+TLS? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        yellow "需要一个已托管到 Cloudflare 的域名"
        ui_read "请输入域名 (或输入skip跳过): " domain_input
        if [ "$domain_input" != "skip" ] && [ -n "$domain_input" ]; then
            MULTI_PROTOCOLS+=("vmess-ws")
            MULTI_PORTS+=("$current_port")
            MULTI_DOMAINS+=("$domain_input")
            current_port=$((current_port + 1))
        else
            yellow "已跳过 VMess+WS+TLS"
        fi
    fi

    # 7. WARP (使用VLESS+Reality协议)
    ui_read "安装 WARP节点 (VLESS+Reality)? (y/n) [y]: " tmp
    if [ "${tmp:-y}" = "y" ]; then
        MULTI_PROTOCOLS+=("reality-warp")
        MULTI_PORTS+=("$current_port")
        MULTI_DOMAINS+=("")

        # WARP 出站方式选择
        cat <<'EOWARPMODE' | ui_box "WARP 出站方式" "$B"
1) WireGuard 直连 (推荐)
   - 不需要安装 warp-cli，sing-box 原生支持
2) 传统 SOCKS5 代理
   - 需要先用主菜单第5项安装 WARP 客户端
EOWARPMODE
        ui_read "请选择 [1/2] [1]: " warp_mode_choice
        case "${warp_mode_choice:-1}" in
            2)
                WARP_WG_MODE="socks5"
                ui_read "WARP SOCKS5 本地端口 [40000]: " tmp; WARP_SOCKS_PORT="${tmp:-40000}"
                ;;
            *)
                WARP_WG_MODE="wireguard"
                green ">>> 已选择 WireGuard 模式"
                ;;
        esac
    fi

    if [ ${#MULTI_PROTOCOLS[@]} -eq 0 ]; then
        red "未选择任何协议"
        return 1
    fi

    green ">>> 已选择 ${#MULTI_PROTOCOLS[@]} 个节点"
    for i in "${!MULTI_PROTOCOLS[@]}"; do
        yellow "  节点$((i+1)): ${MULTI_PROTOCOLS[$i]} - 端口 ${MULTI_PORTS[$i]}"
    done
    return 0
}

# ==================== 配置生成 ====================
gen_inbound_reality() {
    local p=$1 t=$2
    cat <<EOREAL
    {
      "type": "vless",
      "tag": "$t",
      "listen": "0.0.0.0",
      "listen_port": $p,
      "users": [{"uuid": "$UUID", "flow": "xtls-rprx-vision"}],
      "tls": {
        "enabled": true,
        "server_name": "$SNI",
        "reality": {
          "enabled": true,
          "handshake": {"server": "$SNI", "server_port": 443},
          "private_key": "$PRIVATE_KEY",
          "short_id": ["$SHORT_ID"]
        }
      }
    }
EOREAL
}
gen_inbound_hysteria2() {
    local p=$1 t=$2
    cat <<EOHYST
    {
      "type": "hysteria2",
      "tag": "$t",
      "listen": "0.0.0.0",
      "listen_port": $p,
      "ignore_client_bandwidth": true,
      "obfs": {
        "type": "salamander",
        "password": "$OBFS_PASSWORD"
      },
      "users": [{"password": "$PASSWORD"}],
      "tls": {
        "enabled": true,
        "alpn": ["h3"],
        "certificate_path": "$TLS_CERT",
        "key_path": "$TLS_KEY"
      }
    }
EOHYST
}
gen_inbound_tuic() {
    local p=$1 t=$2
    cat <<EOTUIC
    {
      "type": "tuic",
      "tag": "$t",
      "listen": "0.0.0.0",
      "listen_port": $p,
      "users": [{"uuid": "$UUID", "password": "$PASSWORD"}],
      "congestion_control": "bbr",
      "auth_timeout": "3s",
      "zero_rtt_handshake": true,
      "heartbeat": "10s",
      "tls": {
        "enabled": true,
        "alpn": ["h3"],
        "certificate_path": "$TLS_CERT",
        "key_path": "$TLS_KEY"
      }
    }
EOTUIC
}
gen_inbound_ss2022()    { local p=$1 t=$2; local k=$(echo "$PASSWORD"|sed 's/^2022-blake3-aes-256-gcm://'); echo '    { "type":"shadowsocks","tag":"'$t'","listen":"0.0.0.0","listen_port":'$p',"method":"2022-blake3-aes-256-gcm","password":"'$k'"}'; }
gen_inbound_vless_ws()  { local p=$1 t=$2; local wp=$(cat "$WORK_DIR/ws_path.txt" 2>/dev/null); echo '    { "type":"vless","tag":"'$t'","listen":"0.0.0.0","listen_port":'$p',"users":[{"uuid":"'$UUID'"}],"transport":{"type":"ws","path":"'$wp'"},"tls":{"enabled":true,"server_name":"'$DOMAIN'","acme":{"domain":["'$DOMAIN'"],"email":"admin@'$DOMAIN'"}}}'; }
gen_inbound_vmess_ws()  { local p=$1 t=$2; local wp=$(cat "$WORK_DIR/ws_path.txt" 2>/dev/null); echo '    { "type":"vmess","tag":"'$t'","listen":"0.0.0.0","listen_port":'$p',"users":[{"uuid":"'$UUID'","alterId":0}],"transport":{"type":"ws","path":"'$wp'"},"tls":{"enabled":true,"server_name":"'$DOMAIN'","acme":{"domain":["'$DOMAIN'"],"email":"admin@'$DOMAIN'"}}}'; }

gen_inbound() {
    local p=$1 t=$2
    case "$PROTOCOL" in
        reality)   gen_inbound_reality "$p" "$t" ;;
        hysteria2) gen_inbound_hysteria2 "$p" "$t" ;;
        tuic)      gen_inbound_tuic "$p" "$t" ;;
        ss2022)    gen_inbound_ss2022 "$p" "$t" ;;
        vless-ws)  gen_inbound_vless_ws "$p" "$t" ;;
        vmess-ws)  gen_inbound_vmess_ws "$p" "$t" ;;
    esac
}

# ==================== DNS 配置 ====================
gen_dns_config() {
    cat <<EODNS
  "dns": {
    "servers": [
      {
        "type": "https",
        "tag": "dns-doh",
        "server": "1.1.1.1"
      }
    ],
    "final": "dns-doh",
    "strategy": "prefer_ipv4"
  },
EODNS
}

# ==================== 系统性能优化 ====================
optimize_tcp_stack() {
    green ">>> 优化 TCP 内核参数..."
    if grep -q "# TCP 优化 (by proxy script)" /etc/sysctl.conf 2>/dev/null; then
        yellow ">>> TCP 内核参数已优化过，跳过"
        return 0
    fi
    cat >> /etc/sysctl.conf <<'EOSYS'

# TCP 优化 (by proxy script)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_window_scaling = 1
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_notsent_lowat = 16384
EOSYS
    sysctl -p >/dev/null 2>&1 || true
    green "✅ TCP BBR 已启用，窗口已放大"
}

optimize_system_limits() {
    green ">>> 优化系统资源限制..."
    if grep -q "# 文件描述符优化 (by proxy script)" /etc/security/limits.conf 2>/dev/null; then
        yellow ">>> 系统资源限制已优化过，跳过"
        return 0
    fi
    cat >> /etc/security/limits.conf <<'EOLIM'

# 文件描述符优化 (by proxy script)
* soft nofile 1048576
* hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
EOLIM
    ulimit -n 1048576 2>/dev/null || true
    green "✅ 文件描述符已提升到 1048576"
}

generate_config() {
    mkdir -p "$WORK_DIR"
    trace_pause

    # 自由全协议模式
    if [ "$PROTOCOL" = "free-multi" ]; then
        generate_free_multi_config
        trace_resume
        return
    fi

    [ "$PROTOCOL" = "reality" ] && gen_reality_keys
    [[ "$PROTOCOL" =~ ^(hysteria2|tuic)$ ]] && gen_self_signed_cert
    [ "$PROTOCOL" = "hysteria2" ] && OBFS_PASSWORD=$(openssl rand -hex 8)
    if [ "$PROTOCOL" = "ss2022" ]; then
        PASSWORD="2022-blake3-aes-256-gcm:$(openssl rand -base64 32)"
    fi
    trace_resume
    [ "$PROTOCOL" = "vless-ws" ] && echo "/ws-$(openssl rand -hex 4)" > "$WORK_DIR/ws_path.txt"
    [ "$PROTOCOL" = "vmess-ws" ] && echo "/vmws-$(openssl rand -hex 4)" > "$WORK_DIR/ws_path.txt"

    local lp=${INTERNAL_PORT:-$SERVER_PORT}

    # 预计算 WARP 出站
    local use_wg="n"
    [ "$WARP_WG_MODE" = "wireguard" ] && warp_wg_config_exists && use_wg="y"

    if [ "$OUTBOUND_MODE" = "dual" ]; then
        green ">>> 生成双节点配置 (直连:${DIRECT_PORT} + WARP:${WARP_PORT})"
        if [ "$use_wg" = "y" ]; then
            cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
$(gen_wg_endpoints_block)
  "inbounds":[
$(gen_inbound "$DIRECT_PORT" "in-direct"),
$(gen_inbound "$WARP_PORT" "in-warp")
  ],
  "outbounds":[
    {"type":"direct","tag":"out-direct"}
  ],
  "route":{
    "rules":[
      {"inbound":["in-direct"],"action":"route","outbound":"out-direct"},
      {"inbound":["in-warp"],"action":"route","outbound":"out-warp"}
    ],
    "final":"out-direct"
  }
}
EOCFG
        else
            cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
  "inbounds":[
$(gen_inbound "$DIRECT_PORT" "in-direct"),
$(gen_inbound "$WARP_PORT" "in-warp")
  ],
  "outbounds":[
    {"type":"direct","tag":"out-direct"},
    {"type":"socks","tag":"out-warp","server":"127.0.0.1","server_port":$WARP_SOCKS_PORT}
  ],
  "route":{
    "rules":[
      {"inbound":["in-direct"],"action":"route","outbound":"out-direct"},
      {"inbound":["in-warp"],"action":"route","outbound":"out-warp"}
    ],
    "final":"out-direct"
  }
}
EOCFG
        fi
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        if [ "$use_wg" = "y" ]; then
            cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
$(gen_wg_endpoints_block)
  "inbounds":[
$(gen_inbound "$lp" "in-main")
  ],
  "outbounds":[
    {"type":"direct","tag":"direct"}
  ],
  "route":{"final":"out-warp"}
}
EOCFG
        else
            cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
  "inbounds":[
$(gen_inbound "$lp" "in-main")
  ],
  "outbounds":[
    {"type":"socks","tag":"out-warp","server":"127.0.0.1","server_port":$WARP_SOCKS_PORT},
    {"type":"direct","tag":"direct"}
  ],
  "route":{"final":"out-warp"}
}
EOCFG
        fi
    else
        cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
  "inbounds":[
$(gen_inbound "$lp" "in-main")
  ],
  "outbounds":[
    {"type":"direct","tag":"direct"}
  ],
  "route":{"final":"direct"}
}
EOCFG
    fi
    # Security: restrict config file permissions (contains secrets)
    chmod 600 "$CONFIG_FILE" 2>/dev/null || true
}

# 自由全协议模式：生成多协议配置
generate_free_multi_config() {
    local ORIGINAL_PROTOCOL="$PROTOCOL"
    local inbounds_json=""
    local rules_json=""
    local use_wg="n"
    [ "$WARP_WG_MODE" = "wireguard" ] && warp_wg_config_exists && use_wg="y"

    green ">>> 生成自由全协议配置 (${#MULTI_PROTOCOLS[@]}个节点)..."

    # 为每个协议生成密钥和inbound
    for i in "${!MULTI_PROTOCOLS[@]}"; do
        local proto="${MULTI_PROTOCOLS[$i]}"
        local port="${MULTI_PORTS[$i]}"
        local domain="${MULTI_DOMAINS[$i]}"
        local tag="in-${proto}-${port}"

        # 生成UUID（每个协议独立）
        gen_uuid
        local current_uuid="$UUID"

        # 准备协议特定参数
        local current_password=""
        local current_private_key=""
        local current_public_key=""
        local current_short_id=""
        local current_obfs_password=""
        local current_tls_cert=""
        local current_tls_key=""
        local current_domain=""

        case "$proto" in
            reality|reality-warp)
                gen_reality_keys
                gen_short_id
                current_private_key="$PRIVATE_KEY"
                current_public_key="$PUBLIC_KEY"
                current_short_id="$SHORT_ID"
                SNI="www.bing.com"
                ;;
            hysteria2)
                gen_self_signed_cert
                gen_password
                current_password="$PASSWORD"
                current_obfs_password=$(openssl rand -hex 8)
                current_tls_cert="$TLS_CERT"
                current_tls_key="$TLS_KEY"
                ;;
            tuic)
                gen_self_signed_cert
                gen_password
                current_password="$PASSWORD"
                current_tls_cert="$TLS_CERT"
                current_tls_key="$TLS_KEY"
                ;;
            ss2022)
                current_password="2022-blake3-aes-256-gcm:$(openssl rand -base64 32)"
                ;;
            vless-ws)
                current_domain="$domain"
                echo "/ws-$(openssl rand -hex 4)" > "$WORK_DIR/ws_path_${port}.txt"
                DOMAIN="$current_domain"
                ;;
            vmess-ws)
                current_domain="$domain"
                echo "/vmws-$(openssl rand -hex 4)" > "$WORK_DIR/ws_path_${port}.txt"
                DOMAIN="$current_domain"
                ;;
        esac

        # 保存每个协议的密钥信息（用于后续输出）
        eval "MULTI_UUID_${i}='$current_uuid'"
        eval "MULTI_PASSWORD_${i}='$current_password'"
        eval "MULTI_PRIVATE_KEY_${i}='$current_private_key'"
        eval "MULTI_PUBLIC_KEY_${i}='$current_public_key'"
        eval "MULTI_SHORT_ID_${i}='$current_short_id'"
        eval "MULTI_OBFS_PASSWORD_${i}='$current_obfs_password'"
        eval "MULTI_TLS_CERT_${i}='$current_tls_cert'"
        eval "MULTI_TLS_KEY_${i}='$current_tls_key'"
        eval "MULTI_DOMAIN_${i}='$current_domain'"

        # 临时设置全局变量用于gen_inbound
        UUID="$current_uuid"
        PASSWORD="$current_password"
        PRIVATE_KEY="$current_private_key"
        PUBLIC_KEY="$current_public_key"
        SHORT_ID="$current_short_id"
        OBFS_PASSWORD="$current_obfs_password"
        TLS_CERT="$current_tls_cert"
        TLS_KEY="$current_tls_key"

        # 生成inbound（去除reality-warp的-warp后缀）
        local proto_for_gen="${proto%-warp}"
        PROTOCOL="$proto_for_gen"

        local inbound_json=$(gen_inbound "$port" "$tag")

        # 追加逗号（除了第一个）
        [ $i -gt 0 ] && inbounds_json+=","
        inbounds_json+=$'\n'"$inbound_json"

        # 生成路由规则
        if [[ "$proto" == *"-warp"* ]]; then
            rules_json+=$'\n'"      {\"inbound\":[\"$tag\"],\"action\":\"route\",\"outbound\":\"out-warp\"},"
        else
            rules_json+=$'\n'"      {\"inbound\":[\"$tag\"],\"action\":\"route\",\"outbound\":\"out-direct\"},"
        fi
    done

    # 去除最后一个逗号
    rules_json="${rules_json%,}"

    # 生成完整配置
    if [ "$use_wg" = "y" ]; then
        cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
$(gen_wg_endpoints_block)
  "inbounds":[${inbounds_json}
  ],
  "outbounds":[
    {"type":"direct","tag":"out-direct"}
  ],
  "route":{
    "rules":[${rules_json}
    ],
    "final":"out-direct"
  }
}
EOCFG
    else
        cat > "$CONFIG_FILE" <<EOCFG
{
  "log":{"level":"info","timestamp":true},
$(gen_dns_config)
  "inbounds":[${inbounds_json}
  ],
  "outbounds":[
    {"type":"direct","tag":"out-direct"},
    {"type":"socks","tag":"out-warp","server":"127.0.0.1","server_port":$WARP_SOCKS_PORT}
  ],
  "route":{
    "rules":[${rules_json}
    ],
    "final":"out-direct"
  }
}
EOCFG
    fi

    chmod 600 "$CONFIG_FILE" 2>/dev/null || true

    # 恢复原始PROTOCOL值
    PROTOCOL="$ORIGINAL_PROTOCOL"
}

# 生成分享链接（不打印UI，只返回链接字符串）
generate_share_link() {
    local addr="$1" port="$2" name="$3" proto="$4"
    [ "$proto" = "reality-warp" ] && proto="reality"

    local ws_path=""
    if [ -f "$WORK_DIR/ws_path_${port}.txt" ]; then
        ws_path=$(cat "$WORK_DIR/ws_path_${port}.txt")
    elif [ -f "$WORK_DIR/ws_path.txt" ]; then
        ws_path=$(cat "$WORK_DIR/ws_path.txt")
    fi

    local srv="$addr"
    [[ "$proto" =~ ws$ ]] && srv="$DOMAIN"

    local LINK=""
    case "$proto" in
        reality)  LINK="vless://${UUID}@${addr}:${port}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp&headerType=none#${name}" ;;
        hysteria2) LINK="hysteria2://${PASSWORD}@${addr}:${port}?insecure=1&sni=bing.com&obfs=salamander&obfs-password=${OBFS_PASSWORD}#${name}" ;;
        tuic)     LINK="tuic://${UUID}:${PASSWORD}@${addr}:${port}?congestion_control=bbr&alpn=h3&allow_insecure=1#${name}" ;;
        ss2022)   local E=$(echo -n "$PASSWORD"|base64 -w0 2>/dev/null||echo -n "$PASSWORD"|base64); LINK="ss://${E}@${addr}:${port}#${name}" ;;
        vless-ws) LINK="vless://${UUID}@${srv}:${port}?encryption=none&security=tls&sni=${DOMAIN}&type=ws&host=${DOMAIN}&path=${ws_path}#${name}" ;;
        vmess-ws) local J="{\"v\":\"2\",\"ps\":\"${name}\",\"add\":\"${srv}\",\"port\":\"${port}\",\"id\":\"${UUID}\",\"aid\":\"0\",\"scy\":\"auto\",\"net\":\"ws\",\"host\":\"${DOMAIN}\",\"path\":\"${ws_path}\",\"tls\":\"tls\",\"sni\":\"${DOMAIN}\"}"; LINK="vmess://$(echo -n "$J"|base64 -w0 2>/dev/null||echo -n "$J"|base64)" ;;
    esac

    echo "$LINK"
}

# ==================== 多格式输出 ====================
output_node() {
    local addr="$1" port="$2" name="$3" suffix="$4"
    local proto="${suffix:-$PROTOCOL}"
    [ "$proto" = "reality-warp" ] && proto="reality"

    local ws_path=""
    if [ -f "$WORK_DIR/ws_path_${port}.txt" ]; then
        ws_path=$(cat "$WORK_DIR/ws_path_${port}.txt")
    elif [ -f "$WORK_DIR/ws_path.txt" ]; then
        ws_path=$(cat "$WORK_DIR/ws_path.txt")
    fi

    local srv="$addr"
    [[ "$proto" =~ ws$ ]] && srv="$DOMAIN"

    # 分享链接
    local LINK=""
    case "$proto" in
        reality)  LINK="vless://${UUID}@${addr}:${port}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp&headerType=none#${name}" ;;
        hysteria2) LINK="hysteria2://${PASSWORD}@${addr}:${port}?insecure=1&sni=bing.com&obfs=salamander&obfs-password=${OBFS_PASSWORD}#${name}" ;;
        tuic)     LINK="tuic://${UUID}:${PASSWORD}@${addr}:${port}?congestion_control=bbr&alpn=h3&allow_insecure=1#${name}" ;;
        ss2022)   local E=$(echo -n "$PASSWORD"|base64 -w0 2>/dev/null||echo -n "$PASSWORD"|base64); LINK="ss://${E}@${addr}:${port}#${name}" ;;
        vless-ws) LINK="vless://${UUID}@${srv}:${port}?encryption=none&security=tls&sni=${DOMAIN}&type=ws&host=${DOMAIN}&path=${ws_path}#${name}" ;;
        vmess-ws) local J="{\"v\":\"2\",\"ps\":\"${name}\",\"add\":\"${srv}\",\"port\":\"${port}\",\"id\":\"${UUID}\",\"aid\":\"0\",\"scy\":\"auto\",\"net\":\"ws\",\"host\":\"${DOMAIN}\",\"path\":\"${ws_path}\",\"tls\":\"tls\",\"sni\":\"${DOMAIN}\"}"; LINK="vmess://$(echo -n "$J"|base64 -w0 2>/dev/null||echo -n "$J"|base64)" ;;
    esac

    # Clash Meta
    local CY=""
    case "$proto" in
        reality)  CY="  - name: ${name}\n    type: vless\n    server: ${srv}\n    port: ${port}\n    uuid: ${UUID}\n    network: tcp\n    tls: true\n    udp: true\n    flow: xtls-rprx-vision\n    servername: ${SNI}\n    client-fingerprint: chrome\n    reality-opts:\n      public-key: ${PUBLIC_KEY}\n      short-id: ${SHORT_ID}" ;;
        hysteria2) CY="  - name: ${name}\n    type: hysteria2\n    server: ${srv}\n    port: ${port}\n    password: ${PASSWORD}\n    sni: bing.com\n    skip-cert-verify: true\n    obfs: salamander\n    obfs-password: ${OBFS_PASSWORD}" ;;
        tuic)     CY="  - name: ${name}\n    type: tuic\n    server: ${srv}\n    port: ${port}\n    uuid: ${UUID}\n    password: ${PASSWORD}\n    alpn: [h3]\n    congestion-controller: bbr\n    reduce-rtt: true\n    skip-cert-verify: true" ;;
        ss2022)   local SP=$(echo "$PASSWORD"|sed 's/^2022-blake3-aes-256-gcm://'); CY="  - name: ${name}\n    type: ss\n    server: ${srv}\n    port: ${port}\n    cipher: 2022-blake3-aes-256-gcm\n    password: ${SP}" ;;
        vless-ws) CY="  - name: ${name}\n    type: vless\n    server: ${srv}\n    port: ${port}\n    uuid: ${UUID}\n    network: ws\n    tls: true\n    servername: ${DOMAIN}\n    ws-opts:\n      path: ${ws_path}\n      headers:\n        Host: ${DOMAIN}" ;;
        vmess-ws) CY="  - name: ${name}\n    type: vmess\n    server: ${srv}\n    port: ${port}\n    uuid: ${UUID}\n    alterId: 0\n    cipher: auto\n    network: ws\n    tls: true\n    servername: ${DOMAIN}\n    ws-opts:\n      path: ${ws_path}\n      headers:\n        Host: ${DOMAIN}" ;;
    esac

    # 打印
    {
        echo "分享链接:"
        echo "$LINK"
        echo ""
        echo "Clash Meta / Mihomo:"
        printf 'proxies:\n%b\n' "$CY"
    } | ui_box "${name}" "$B"

    # 二维码
    if command -v qrencode >/dev/null 2>&1 && [ -n "$LINK" ]; then
        printf '%s\n' "二维码:" | ui_box "${name}" "$Y"
        qrencode -t ANSIUTF8 "$LINK"
    fi

    # 写入文件（追加到 yaml 注释区）
    if [ -n "$CLASH_FILE" ]; then
        cat >> "$CLASH_FILE" <<EONODE

# ━━━ ${name} ━━━
# 分享链接: ${LINK}
EONODE
    fi
}

output_all() {
    mkdir -p "$INFO_DIR"
    SAFE_NAME=$(echo "$NODE_NAME"|tr ' ' '_'|tr -cd 'A-Za-z0-9._-')
    # 如果中文名被全部过滤掉了（只剩横杠或空），用时间戳命名
    local clean_name=$(echo "$SAFE_NAME" | tr -d '-._')
    if [ -z "$clean_name" ]; then
        SAFE_NAME="node_$(date '+%m%d_%H%M')"
    fi

    # 自由全协议模式
    if [ "$PROTOCOL" = "free-multi" ]; then
        output_free_multi_all
        return
    fi

    local outmode_desc="直连出站"
    [ "$OUTBOUND_MODE" = "warp" ] && outmode_desc="WARP出站"
    [ "$OUTBOUND_MODE" = "dual" ] && outmode_desc="双节点 (直连+WARP)"

    # 先创建 yaml 文件（后面 output_node 会往里追加分享链接）
    CLASH_FILE="$INFO_DIR/${SAFE_NAME}.yaml"
    echo "# 节点: ${NODE_NAME}" > "$CLASH_FILE"
    chmod 600 "$CLASH_FILE" 2>/dev/null || true

    cat >> "$CLASH_FILE" <<EOH
# 机器: ${MACHINE_MODE} | 协议: ${PROTOCOL} | 出站: ${outmode_desc}
# IP: ${SERVER_IP} | 端口: ${SERVER_PORT}
EOH
    [ "$MACHINE_MODE" = "nat" ] && echo "# 内部端口: ${INTERNAL_PORT}" >> "$CLASH_FILE"

    {
        echo "机器: ${MACHINE_MODE} | 协议: ${PROTOCOL} | 出站: ${outmode_desc}"
        echo "IP: ${SERVER_IP} | 端口: ${SERVER_PORT}"
        [ "$MACHINE_MODE" = "nat" ] && echo "内部端口: ${INTERNAL_PORT}"
    } | ui_box "安装完成 — ${NODE_NAME}" "$G"

    # ===== 出站模式大横幅 =====
    if [ "$OUTBOUND_MODE" = "direct" ]; then
        cat <<'EOF' | ui_box "直连出站模式" "$G"
显示 VPS 的 IP
流量路径: 你 → VPS → 网站
EOF
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        cat <<'EOF' | ui_box "WARP 出站模式" "$B"
显示 Cloudflare 的 IP
流量路径: 你 → VPS → WARP → CF → 网站
可解锁 ChatGPT / Netflix 等
EOF
    elif [ "$OUTBOUND_MODE" = "dual" ]; then
        {
            echo "节点1 (端口${DIRECT_PORT}): 直连 → VPS IP"
            echo "节点2 (端口${WARP_PORT}): WARP → Cloudflare IP"
            echo "客户端里两个节点按需切换"
        } | ui_box "双节点模式" "$P"
    fi

    if [ "$OUTBOUND_MODE" = "dual" ]; then
        output_node "$SERVER_IP" "$DIRECT_PORT" "${NODE_NAME}-直连" "direct"
        output_node "$SERVER_IP" "$WARP_PORT" "${NODE_NAME}-WARP" "warp"
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        # WARP 单节点: 节点名加后缀
        output_node "$SERVER_IP" "$SERVER_PORT" "${NODE_NAME}-WARP" "warp"
        cat <<'EOF' | ui_box "WARP 节点提示" "$Y"
此节点的分享链接和直连模式相同。
区别在服务器端(出站走WARP)，客户端连接方式不变。
访问外部 IP 检测服务时会看到 Cloudflare 的 IP，说明 WARP 已生效。
EOF
    else
        output_node "$SERVER_IP" "$SERVER_PORT" "${NODE_NAME}-直连" "direct"
    fi

    # ===== 生成完整 Clash Meta 配置文件 =====
    info "完整 Clash Meta / Mihomo 配置"

    # local CLASH_FILE=\"$INFO_DIR/${SAFE_NAME}_clash.yaml\" # 已合并到主文件
    local ws_path=""; [ -f "$WORK_DIR/ws_path.txt" ] && ws_path=$(cat "$WORK_DIR/ws_path.txt")
    local srv="$SERVER_IP"; [[ "$PROTOCOL" =~ ws$ ]] && srv="$DOMAIN"

    # 收集所有节点名
    local ALL_NAMES=""
    if [ "$OUTBOUND_MODE" = "dual" ]; then
        ALL_NAMES="${NODE_NAME}-直连, ${NODE_NAME}-WARP"
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        ALL_NAMES="${NODE_NAME}-WARP"
    else
        ALL_NAMES="${NODE_NAME}-直连"
    fi

    # 生成 proxies 部分
    local PROXIES_YAML=""
    gen_clash_proxy() {
        local n="$1" s="$2" p="$3"
        case "$PROTOCOL" in
            reality)  echo "  - name: ${n}
    type: vless
    server: ${s}
    port: ${p}
    uuid: ${UUID}
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: ${SNI}
    client-fingerprint: chrome
    reality-opts:
      public-key: ${PUBLIC_KEY}
      short-id: ${SHORT_ID}" ;;
            hysteria2) echo "  - name: ${n}
    type: hysteria2
    server: ${s}
    port: ${p}
    password: ${PASSWORD}
    sni: bing.com
    skip-cert-verify: true
    obfs: salamander
    obfs-password: ${OBFS_PASSWORD}" ;;
            tuic) echo "  - name: ${n}
    type: tuic
    server: ${s}
    port: ${p}
    uuid: ${UUID}
    password: ${PASSWORD}
    alpn: [h3]
    congestion-controller: bbr
    reduce-rtt: true
    skip-cert-verify: true" ;;
            ss2022) local SP=$(echo "$PASSWORD"|sed 's/^2022-blake3-aes-256-gcm://'); echo "  - name: ${n}
    type: ss
    server: ${s}
    port: ${p}
    cipher: 2022-blake3-aes-256-gcm
    password: ${SP}" ;;
            vless-ws) echo "  - name: ${n}
    type: vless
    server: ${srv}
    port: ${p}
    uuid: ${UUID}
    network: ws
    tls: true
    servername: ${DOMAIN}
    ws-opts:
      path: ${ws_path}
      headers:
        Host: ${DOMAIN}" ;;
            vmess-ws) echo "  - name: ${n}
    type: vmess
    server: ${srv}
    port: ${p}
    uuid: ${UUID}
    alterId: 0
    cipher: auto
    network: ws
    tls: true
    servername: ${DOMAIN}
    ws-opts:
      path: ${ws_path}
      headers:
        Host: ${DOMAIN}" ;;
        esac
    }

    # 构建 proxies
    if [ "$OUTBOUND_MODE" = "dual" ]; then
        PROXIES_YAML="$(gen_clash_proxy "${NODE_NAME}-直连" "$SERVER_IP" "$DIRECT_PORT")
$(gen_clash_proxy "${NODE_NAME}-WARP" "$SERVER_IP" "$WARP_PORT")"
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        PROXIES_YAML="$(gen_clash_proxy "${NODE_NAME}-WARP" "$SERVER_IP" "$SERVER_PORT")"
    else
        PROXIES_YAML="$(gen_clash_proxy "${NODE_NAME}-直连" "$SERVER_IP" "$SERVER_PORT")"
    fi

    # 构建节点列表（用于 proxy-groups）
    local PROXY_LIST=""
    if [ "$OUTBOUND_MODE" = "dual" ]; then
        PROXY_LIST="      - ${NODE_NAME}-直连
      - ${NODE_NAME}-WARP"
    elif [ "$OUTBOUND_MODE" = "warp" ]; then
        PROXY_LIST="      - ${NODE_NAME}-WARP"
    else
        PROXY_LIST="      - ${NODE_NAME}-直连"
    fi

    cat > "$CLASH_FILE" <<EOCLASH
# ============================================
# Clash Meta / Mihomo 完整配置
# 节点: ${NODE_NAME}
# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')
# ============================================

mixed-port: 7890
allow-lan: true
mode: rule
log-level: info
unified-delay: true
find-process-mode: strict

dns:
  enable: true
  listen: :1053
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - https://dns.alidns.com/dns-query
    - https://doh.pub/dns-query
  fallback:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  fallback-filter:
    geoip: true
    geoip-code: CN

proxies:
${PROXIES_YAML}

proxy-groups:
  - name: Proxy
    type: select
    proxies:
${PROXY_LIST}
      - DIRECT

rules:
  # 国内直连
  - GEOIP,LAN,DIRECT
  - GEOIP,CN,DIRECT
  - DOMAIN-SUFFIX,cn,DIRECT
  - DOMAIN-KEYWORD,baidu,DIRECT
  - DOMAIN-KEYWORD,bilibili,DIRECT
  - DOMAIN-SUFFIX,qq.com,DIRECT
  - DOMAIN-SUFFIX,taobao.com,DIRECT
  - DOMAIN-SUFFIX,jd.com,DIRECT
  - DOMAIN-SUFFIX,163.com,DIRECT
  - DOMAIN-SUFFIX,zhihu.com,DIRECT
  - DOMAIN-SUFFIX,douyin.com,DIRECT

  # 其余全部走代理
  - MATCH,Proxy
EOCLASH

    # 追加常用命令到 yaml 注释区
    cat >> "$CLASH_FILE" <<EOCMD

# ━━━ 常用命令 ━━━
# $([ "$SVC" = "openrc" ] && echo "rc-service sing-box status/restart" || echo "systemctl status/restart sing-box")
# cat ${CONFIG_FILE}
EOCMD
    chmod 600 "$CLASH_FILE" 2>/dev/null || true

    green "⬇️ 请复制下方配置 ⬇️"
    cat "$CLASH_FILE"
    green "⬆️ 请复制上方配置 ⬆️"

    green ">>> 所有信息已保存到: ${CLASH_FILE}"
    yellow ">>> 此内容可直接全选复制，保存为 .yaml 文件导入 FlClash"
    yellow ">>> 分享链接（vless://...）在最上方的注释里"
    [ "$MACHINE_MODE" = "nat" ] && yellow ">>> 面板需映射 TCP ${SERVER_PORT} -> ${INTERNAL_PORT}"
}

# 自由全协议模式：输出所有节点
output_free_multi_all() {
    mkdir -p "$INFO_DIR"
    SAFE_NAME=$(echo "$NODE_NAME"|tr ' ' '_'|tr -cd 'A-Za-z0-9._-')
    local clean_name=$(echo "$SAFE_NAME" | tr -d '-._')
    if [ -z "$clean_name" ]; then
        SAFE_NAME="node_$(date '+%m%d_%H%M')"
    fi

    local COMBINED_FILE="$INFO_DIR/${SAFE_NAME}_all.yaml"
    echo "# ============================================" > "$COMBINED_FILE"
    echo "# 节点: ${NODE_NAME} (全协议模式)" >> "$COMBINED_FILE"
    echo "# IP: ${SERVER_IP}" >> "$COMBINED_FILE"
    echo "# 共 ${#MULTI_PROTOCOLS[@]} 个节点" >> "$COMBINED_FILE"
    echo "# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "$COMBINED_FILE"
    echo "# ============================================" >> "$COMBINED_FILE"
    echo "" >> "$COMBINED_FILE"

    {
        echo "机器: ${MACHINE_MODE} | 模式: 自由全协议"
        echo "IP: ${SERVER_IP} | 节点数: ${#MULTI_PROTOCOLS[@]}"
    } | ui_box "安装完成 — ${NODE_NAME}" "$G"

    cat <<'EOF' | ui_box "自由全协议模式" "$P"
已生成多个独立节点，每个协议一个端口
所有节点信息已合并到一个文件中
EOF

    green ">>> 开始生成 ${#MULTI_PROTOCOLS[@]} 个节点的配置..."

    # 1. 写入所有节点分享链接作为注释
    for i in "${!MULTI_PROTOCOLS[@]}"; do
        local proto="${MULTI_PROTOCOLS[$i]}"
        local port="${MULTI_PORTS[$i]}"

        green ">>> 正在处理节点 $((i+1))/${#MULTI_PROTOCOLS[@]}: ${proto} (端口 ${port})"

        # 恢复密钥
        eval "UUID=\$MULTI_UUID_${i}"
        eval "PASSWORD=\$MULTI_PASSWORD_${i}"
        eval "PRIVATE_KEY=\$MULTI_PRIVATE_KEY_${i}"
        eval "PUBLIC_KEY=\$MULTI_PUBLIC_KEY_${i}"
        eval "SHORT_ID=\$MULTI_SHORT_ID_${i}"
        eval "OBFS_PASSWORD=\$MULTI_OBFS_PASSWORD_${i}"
        eval "DOMAIN=\$MULTI_DOMAIN_${i}"

        # 设置协议特定变量
        local proto_clean="${proto%-warp}"
        case "$proto_clean" in
            reality) SNI="www.bing.com" ;;
            vless-ws|vmess-ws) ;; # DOMAIN已恢复
            *) ;;
        esac

        local proto_display="${proto}"
        case "$proto" in
            reality) proto_display="Reality" ;;
            ss2022) proto_display="SS2022" ;;
            hysteria2) proto_display="Hysteria2" ;;
            tuic) proto_display="TUIC" ;;
            vless-ws) proto_display="VLESS-WS" ;;
            vmess-ws) proto_display="VMess-WS" ;;
            reality-warp) proto_display="Reality-WARP"; SNI="www.bing.com" ;;
        esac

        local node_name="${NODE_NAME}-${proto_display}"

        # 生成分享链接
        local link=$(generate_share_link "$SERVER_IP" "$port" "$node_name" "$proto")

        echo "# ━━━ ${node_name} ━━━" >> "$COMBINED_FILE"
        echo "# 协议: ${proto_display} | 端口: ${port}" >> "$COMBINED_FILE"
        echo "# 分享链接: ${link}" >> "$COMBINED_FILE"
        echo "" >> "$COMBINED_FILE"
    done

    echo "# ============================================" >> "$COMBINED_FILE"
    echo "# 以下为 Clash Meta / Mihomo 完整配置" >> "$COMBINED_FILE"
    echo "# ============================================" >> "$COMBINED_FILE"
    echo "" >> "$COMBINED_FILE"

    # 2. 生成并追加完整 Clash 配置
    generate_free_multi_clash_config >> "$COMBINED_FILE"

    CLASH_FILE="$COMBINED_FILE"
    chmod 600 "$CLASH_FILE" 2>/dev/null || true

    green "⬇️ 请复制下方配置 ⬇️"
    cat "$CLASH_FILE"
    green "⬆️ 请复制上方配置 ⬆️"

    green ">>> 所有节点链接与 Clash 配置已完美合并保存至: ${CLASH_FILE}"
    yellow ">>> 此文件可直接导入 FlClash，也可复制单节点链接（在文件开头注释区）"
}
generate_free_multi_clash_config() {
    # 不再创建独立文件，直接输出到stdout，让调用者追加到COMBINED_FILE
    local proxies_yaml=""
    local proxy_list=""

    # 生成所有节点的proxies和列表
    for i in "${!MULTI_PROTOCOLS[@]}"; do
        local proto="${MULTI_PROTOCOLS[$i]}"
        local port="${MULTI_PORTS[$i]}"

        # 恢复密钥
        eval "UUID=\$MULTI_UUID_${i}"
        eval "PASSWORD=\$MULTI_PASSWORD_${i}"
        eval "PRIVATE_KEY=\$MULTI_PRIVATE_KEY_${i}"
        eval "PUBLIC_KEY=\$MULTI_PUBLIC_KEY_${i}"
        eval "SHORT_ID=\$MULTI_SHORT_ID_${i}"
        eval "OBFS_PASSWORD=\$MULTI_OBFS_PASSWORD_${i}"
        eval "DOMAIN=\$MULTI_DOMAIN_${i}"

        local proto_display="${proto}"
        case "$proto" in
            reality) proto_display="Reality" ;;
            ss2022) proto_display="SS2022" ;;
            hysteria2) proto_display="Hysteria2" ;;
            tuic) proto_display="TUIC" ;;
            vless-ws) proto_display="VLESS-WS" ;;
            vmess-ws) proto_display="VMess-WS" ;;
            reality-warp) proto_display="Reality-WARP"; SNI="www.bing.com" ;;
        esac

        local node_name="${NODE_NAME}-${proto_display}"
        local srv="$SERVER_IP"
        [[ "$proto" =~ ws$ ]] && srv="$DOMAIN"

        # 生成proxy配置
        case "$proto" in
            reality|reality-warp)
                SNI="www.bing.com"
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: vless
    server: ${srv}
    port: ${port}
    uuid: ${UUID}
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: ${SNI}
    client-fingerprint: chrome
    reality-opts:
      public-key: ${PUBLIC_KEY}
      short-id: ${SHORT_ID}"
                ;;
            hysteria2)
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: hysteria2
    server: ${srv}
    port: ${port}
    password: ${PASSWORD}
    sni: bing.com
    skip-cert-verify: true
    obfs: salamander
    obfs-password: ${OBFS_PASSWORD}"
                ;;
            tuic)
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: tuic
    server: ${srv}
    port: ${port}
    uuid: ${UUID}
    password: ${PASSWORD}
    alpn: [h3]
    congestion-controller: bbr
    reduce-rtt: true
    skip-cert-verify: true"
                ;;
            ss2022)
                local SP=$(echo "$PASSWORD"|sed 's/^2022-blake3-aes-256-gcm://')
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: ss
    server: ${srv}
    port: ${port}
    cipher: 2022-blake3-aes-256-gcm
    password: ${SP}"
                ;;
            vless-ws)
                local ws_path=$(cat "$WORK_DIR/ws_path_${port}.txt" 2>/dev/null)
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: vless
    server: ${srv}
    port: ${port}
    uuid: ${UUID}
    network: ws
    tls: true
    servername: ${DOMAIN}
    ws-opts:
      path: ${ws_path}
      headers:
        Host: ${DOMAIN}"
                ;;
            vmess-ws)
                local ws_path=$(cat "$WORK_DIR/ws_path_${port}.txt" 2>/dev/null)
                [ $i -gt 0 ] && proxies_yaml+=$'\n'
                proxies_yaml+="  - name: ${node_name}
    type: vmess
    server: ${srv}
    port: ${port}
    uuid: ${UUID}
    alterId: 0
    cipher: auto
    network: ws
    tls: true
    servername: ${DOMAIN}
    ws-opts:
      path: ${ws_path}
      headers:
        Host: ${DOMAIN}"
                ;;
        esac

        proxy_list+="      - ${node_name}"$'\n'
    done

    # 去除最后的换行
    proxy_list="${proxy_list%$'\n'}"

    # 输出完整Clash配置到stdout
    cat <<EOCLASH
mixed-port: 7890
allow-lan: true
mode: rule
log-level: info
unified-delay: true
find-process-mode: strict

dns:
  enable: true
  listen: :1053
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver:
    - https://dns.alidns.com/dns-query
    - https://doh.pub/dns-query
  fallback:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  fallback-filter:
    geoip: true
    geoip-code: CN

proxies:
${proxies_yaml}

proxy-groups:
  - name: Proxy
    type: select
    proxies:
${proxy_list}
      - DIRECT

rules:
  # 国内直连
  - GEOIP,LAN,DIRECT
  - GEOIP,CN,DIRECT
  - DOMAIN-SUFFIX,cn,DIRECT
  - DOMAIN-KEYWORD,baidu,DIRECT
  - DOMAIN-KEYWORD,bilibili,DIRECT
  - DOMAIN-SUFFIX,qq.com,DIRECT
  - DOMAIN-SUFFIX,taobao.com,DIRECT
  - DOMAIN-SUFFIX,jd.com,DIRECT
  - DOMAIN-SUFFIX,163.com,DIRECT
  - DOMAIN-SUFFIX,zhihu.com,DIRECT
  - DOMAIN-SUFFIX,douyin.com,DIRECT

  # 其余全部走代理
  - MATCH,Proxy
EOCLASH
}

# ====================知识科普 ====================
show_cf_knowledge() {
    cat <<'EOF' | ui_box "Cloudflare 完整知识科普" "$P"
第一章: 两段路原理

你的手机 ──①入站──→ VPS ──②出站──→ 外部 IP 检测服务

① 入站 = 你怎么连到VPS（Reality / CDN中转 / 直连）
② 出站 = VPS怎么访问目标网站（直连 / WARP）

外部显示的 IP = 出站的 IP！
你客户端连接的 IP = 入站的 IP！

第二章: 三种组合对比

┌──────────────────┬──────────────┬──────────────┬────────────┐
│ 组合             │ 外部显示IP   │ 客户端连的IP │ 需要域名   │
├──────────────────┼──────────────┼──────────────┼────────────┤
│ Reality+直连     │ VPS IP       │ VPS IP       │ 否         │
│ Reality+WARP出站 │ CF WARP IP   │ VPS IP       │ 否         │
│ CDN中转+直连     │ VPS IP       │ CF CDN IP    │ 是         │
│ CDN中转+WARP出站 │ CF WARP IP   │ CF CDN IP    │ 是，最安全 │
└──────────────────┴──────────────┴──────────────┴────────────┘

CDN中转的价值: 隐藏VPS真实IP，GFW封不到你的VPS
WARP出站的价值: 解锁ChatGPT/Netflix等，外部显示 Cloudflare IP

第三章: 为什么CDN中转CF能看到明文？

你 ──TLS加密──→ CF边缘 ──解密,再加密──→ VPS
                  ↑
         CF是中间人，必须解密才能转发
         所以CF理论上能看到你的数据

对比 Reality:
你 ──端到端TLS加密──→ VPS（中间没人能看到）

第四章: Zero Trust / WARP 是什么

WARP        = CF的免费VPN (1.1.1.1 App)
Zero Trust  = CF企业安全平台 (50人以下免费)
Teams       = Zero Trust的旧名

在VPS上装WARP的流程:
1. CF Dashboard → Zero Trust → 创建组织 / Device enrollment 策略
2. VPS上安装 cloudflare-warp
3. 登录方式 1: warp-cli registration new <组织名> → 浏览器授权
4. 登录方式 2: 复制 com.cloudflare.warp://... Team Token URL → warp-cli registration token
5. 登录方式 3: Service Token + MDM (auth_client_id + auth_client_secret)
6. 最后切到 proxy 模式，127.0.0.1:40000 就是本地 WARP 代理

第五章: 流量路径图

【直连】             手机 → VPS → 网站
【WARP出站】         手机 → VPS → WARP → CF → 网站
【CDN中转】          手机 → CF边缘 → VPS → 网站
【CDN+WARP(终极)】   手机 → CF边缘 → VPS → WARP → CF → 网站
EOF
}

# ==================== 管理菜单 ====================
show_manage_menu() {
    while true; do
        trace_pause
        cat <<'EOF' | ui_box "综合代理部署脚本 v3.0" "$B"
1) 全新安装 / 重置
   - 从机器类型、协议、出站模式开始重新部署 sing-box
2) 查看当前节点信息
   - 打印已保存的节点链接、Clash/FlClash 配置和导入提示
3) 重启服务
   - 重启 sing-box，让现有配置重新生效
4) 卸载
   - 删除 sing-box 服务、配置目录和已保存节点信息
5) WARP 安装向导
   - WireGuard 模式 (推荐) / 传统 warp-cli 登录
6) WARP 状态检查
   - 查看 WireGuard / warp-cli 注册状态和出口 IP
7) 知识科普
   - 解释直连、WARP、CDN、Zero Trust 等概念

0) 退出
   - 关闭脚本，不做额外操作
EOF
        ui_read "请选择 [0-7]: " action
        trace_resume
        case "$action" in
            1) do_full_install ;;
            2) show_saved_info ;;
            3) restart_service ;;
            4) do_uninstall ;;
            5) install_warp_client ;;
            6) check_warp_status ;;
            7) show_cf_knowledge ;;
            0) exit 0 ;;
            *) red "无效" ;;
        esac
        continue
    done
}

show_saved_info() {
    if [ -d "$INFO_DIR" ]; then
        local found=0
        # 显示所有 yaml 文件
        for f in "$INFO_DIR"/*.yaml; do
            [ -f "$f" ] || continue
            found=1
            printf '%s\n' "$f" | ui_box "配置文件" "$B"
            # 打印全部内容（包括注释里的节点链接和下面的规则）
            green "⬇️ 配置文件内容 ⬇️"
            cat "$f"
            green "⬆️ 配置文件内容 ⬆️"
            green "完整文件路径: $f"
            yellow "导入方式: 直接复制上方所有内容（或者下载文件），导入到 FlClash"
        done
        # 兼容旧版 txt
        for f in "$INFO_DIR"/*.txt; do
            [ -f "$f" ] || continue
            found=1
            printf '%s\n' "$f" | ui_box "旧版节点信息" "$B"
            cat "$f"
            green "⬆️ 旧版节点信息结束 ⬆️"
        done
        [ $found -eq 0 ] && red "未找到节点信息"
    else
        red "未找到节点信息"
    fi
}

restart_service() {
    detect_os
    if [ "$SVC" = "openrc" ]; then rc-service sing-box restart && green "重启成功"
    else systemctl restart sing-box && green "重启成功"; fi
}

do_uninstall() {
    is_root
    detect_os
    cat <<'EOF' | ui_box "深度卸载确认" "$Y"
将深度卸载 sing-box / 节点配置 / WARP 客户端 / Cloudflare 源 / 脚本日志 / 临时文件 / 本机防火墙放行规则。
不会删除当前脚本文件；云厂商安全组仍需在控制台手动删除。
EOF
    ui_read "确认深度卸载? (y/n): " c
    [ "$c" = "y" ] || return

    close_proxy_firewall
    cleanup_proxy_artifacts
    cleanup_warp_client
    cleanup_common_dependencies

    green ">>> 深度卸载完成"
}

# ==================== 安装主流程（带返回功能） ====================
do_full_install() {
    is_root; detect_os

    # 第一步：机器类型（可返回）
    select_machine_mode || return

    # 第二步：协议（可返回→回到第一步）
    while true; do
        select_protocol
        local ret=$?
        [ $ret -eq 0 ] && break
        select_machine_mode || return
    done

    # 自由全协议模式跳过第三步（出站模式选择）
    if [ "$PROTOCOL" = "free-multi" ]; then
        OUTBOUND_MODE="direct"  # 占位，实际由每个协议决定
    else
        # 第三步：出站模式（可返回→回到第二步）
        while true; do
            select_outbound_mode
            local ret=$?
            [ $ret -eq 0 ] && break
            while true; do
                select_protocol
                local ret2=$?
                [ $ret2 -eq 0 ] && break
                select_machine_mode || return
            done
        done
    fi

    # 第四步：收集信息
    collect_info || return

    # 第五步：安装依赖 + sing-box
    install_singbox

    # WireGuard 注册（需要 sing-box 已安装）
    if [ "$WARP_WG_MODE" = "wireguard" ]; then
        if ! warp_wg_config_exists; then
            warp_register_wireguard || { red "WireGuard 注册失败，将回退到直连模式"; OUTBOUND_MODE="direct"; WARP_WG_MODE=""; }
        else
            green ">>> 已检测到 WireGuard 配置: $(warp_wg_config_file)"
        fi
    fi

    # 第六步：生成密钥（必须在安装依赖之后，因为需要 openssl）
    # 自由全协议模式在 generate_free_multi_config 中生成
    if [ "$PROTOCOL" != "free-multi" ]; then
        green ">>> 生成密钥..."
        trace_pause
        gen_uuid; gen_password; gen_short_id
        trace_resume
    fi

    # 第七步：系统性能优化
    optimize_tcp_stack
    optimize_system_limits

    # 第八步：生成配置
    generate_config
    /usr/local/bin/sing-box check -c "$CONFIG_FILE" || { red "配置校验失败"; cat "$CONFIG_FILE"; exit 1; }
    setup_service
    if [ "$PROTOCOL" = "free-multi" ] && [ ${#MULTI_PORTS[@]} -gt 0 ]; then
        for port in "${MULTI_PORTS[@]}"; do
            open_firewall "$port"
        done
    else
        open_firewall "${INTERNAL_PORT:-$SERVER_PORT}"
    fi
    [ "$OUTBOUND_MODE" = "dual" ] && open_firewall "$WARP_PORT"
    output_all
}

# ==================== 入口 ====================
show_manage_menu
