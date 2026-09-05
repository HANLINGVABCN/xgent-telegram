#!/usr/bin/env bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/venv"
VENV_ACTIVATE="$VENV_DIR/bin/activate"
VENV_PYTHON="$VENV_DIR/bin/python3"
APP_ENTRY="xgent_server.py"
PM2_APP_NAME="xgent-telegram"
LEGACY_PM2_APP_NAME="telegram-ai-bot"
LEGACY_APP_ENTRY="bot_server.py"
STATE_DIR="$SCRIPT_DIR/.install-state"
APT_STATE_FILE="$STATE_DIR/apt-packages.txt"
RUNTIME_MODE_FILE="$STATE_DIR/runtime-mode"
# 上次装依赖时 requirements.txt 的原样副本。重启路径靠它判断还要不要再联网装一遍。
REQUIREMENTS_SNAPSHOT="$STATE_DIR/requirements.installed"
APT_UPDATED=0
SKIP_CONFIRM=0
# ensure_virtualenv 这一次有没有真的新建/重建 venv。
VENV_CREATED=0

# ensure_python 挑出来的解释器绝对路径。空串代表还没挑过。
PYTHON_BIN=""

# 系统级保活（systemd）。unit 名固定，卸载时才敢删——按名字删自己写的那个，
# 不碰用户手工建的同类服务。
SYSTEMD_SERVICE_NAME="xgent"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SYSTEMD_SERVICE_NAME}.service"

LOCAL_API_CONTAINER="telegram-local-bot-api"
LOCAL_API_PORT="8081"
LOCAL_API_DATA_DIR="$SCRIPT_DIR/.local-api-data"
LOCAL_API_IMAGE="aiogram/telegram-bot-api:latest"
LOCAL_API_ALLOWED_IPS_FILE="$STATE_DIR/local-api-allowed-ips"

print_banner() {
    echo -e "${CYAN}"
    echo "========================================================"
    echo " XGent for Telegram 启动器"
    echo " 异步 SQLite + 快速流式输出部署助手"
    echo "========================================================"
    echo -e "${NC}"
}

info() {
    echo -e "${GREEN}$1${NC}"
}

warn() {
    echo -e "${YELLOW}$1${NC}"
}

error() {
    echo -e "${RED}$1${NC}"
}

success() {
    echo -e "   ${GREEN}[成功] $1${NC}"
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

ensure_state_dir() {
    mkdir -p "$STATE_DIR"
}

migrate_env_key() {
    local old_key="$1" new_key="$2" old_value="" new_value=""
    [ -f ".env" ] || return 0

    old_value="$(env_get "$old_key")"
    [ -n "$old_value" ] || return 0
    new_value="$(env_get "$new_key")"

    env_unset "$old_key"
    env_set "$new_key" "${new_value:-$old_value}"
    success "已迁移 .env 配置: ${old_key} -> ${new_key}"
}


migrate_legacy_installation() {
    migrate_env_key "TELEGRAM_AI_BOT_IP_MODE" "XGENT_IP_MODE"
    migrate_env_key "TELEGRAM_AI_BOT_SOCKS5_PROXY" "XGENT_SOCKS5_PROXY"
    migrate_env_key "TELEGRAM_AI_BOT_TRACE_LOG_FILE" "XGENT_TRACE_LOG_FILE"

    # Runtime database/storage/log migration is performed by the new Python
    # bootstrap before those paths are opened. Renaming only the PID marker here
    # lets the installer stop a legacy nohup process safely.
    if [ -f "bot.pid" ] && [ ! -e "xgent.pid" ]; then
        mv -- "bot.pid" "xgent.pid"
        success "已迁移旧进程标记: bot.pid -> xgent.pid"
    fi
}

get_ip_mode() {
    local mode=""

    mode="$(env_get XGENT_IP_MODE)"
    [ -n "$mode" ] || mode="$(env_get TELEGRAM_AI_BOT_IP_MODE)"
    mode="$(printf '%s' "$mode" | tr -d '[:space:]')"

    case "$mode" in
        ipv4|ipv6|sock5)
            printf '%s\n' "$mode"
            ;;
        *)
            printf 'default\n'
            ;;
    esac
}

# 读取 SOCKS5 代理地址（仅当 IP 模式为 sock5 时有效）
get_socks5_proxy() {
    local url
    url="$(env_get XGENT_SOCKS5_PROXY)"
    [ -n "$url" ] || url="$(env_get TELEGRAM_AI_BOT_SOCKS5_PROXY)"
    printf '%s\n' "$url"
}

ip_mode_label() {
    case "$(get_ip_mode)" in
        ipv4)
            printf '仅 IPv4'
            ;;
        ipv6)
            printf '仅 IPv6'
            ;;
        sock5)
            printf 'SOCKS5 代理'
            ;;
        *)
            printf '服务器默认'
            ;;
    esac
}

# .env 里有 BOT_TOKEN 就是权威判据（与 sync_deploy_mode_state 的判断逻辑一致），
# 不依赖 .install-state/deploy-mode——那个文件是同步出来的副本，单独读它会在
# "刚手动往 .env 填了 Token 但还没跑过安装流程"时显示过期的模式。
deploy_mode_label() {
    if [ -f ".env" ] && [ -n "$(env_get BOT_TOKEN)" ]; then
        printf 'Telegram + Web'
    else
        printf '仅 Web'
    fi
}

# 设置 IP 出站模式。ipv4 / ipv6 / sock5 / default 四选一，互斥。
# sock5 模式需额外传入代理地址（socks5://[user:pass@]host:port）。
# 切换到任一模式时，会先清除其它模式的环境变量，确保互斥。
set_ip_mode() {
    local mode="$1"
    local socks5_url="${2:-}"
    local ip_keys=(XGENT_IP_MODE XGENT_SOCKS5_PROXY
                   TELEGRAM_AI_BOT_IP_MODE TELEGRAM_AI_BOT_SOCKS5_PROXY)

    case "$mode" in
        ipv4|ipv6)
            env_unset "${ip_keys[@]}"
            env_set XGENT_IP_MODE "$mode"
            ;;
        sock5)
            if [ -z "$socks5_url" ]; then
                error "[错误] SOCKS5 模式需要提供代理地址"
                exit 1
            fi
            env_unset "${ip_keys[@]}"
            env_set XGENT_IP_MODE "$mode"
            env_set XGENT_SOCKS5_PROXY "$socks5_url"
            ;;
        default)
            env_unset "${ip_keys[@]}"
            ;;
        *)
            error "[错误] 无效 IP 模式: $mode"
            exit 1
            ;;
    esac
}

pythonpath_with_project() {
    if [ -n "${PYTHONPATH:-}" ]; then
        printf '%s:%s\n' "$SCRIPT_DIR" "$PYTHONPATH"
    else
        printf '%s\n' "$SCRIPT_DIR"
    fi
}

run_bot_python() {
    local mode pythonpath app_entry socks5_url
    mode="$(get_ip_mode)"
    pythonpath="$(pythonpath_with_project)"
    app_entry="${XGENT_APP_ENTRY:-${TELEGRAM_AI_BOT_APP_ENTRY:-$APP_ENTRY}}"

    case "$mode" in
        default)
            env -u XGENT_IP_MODE -u XGENT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
            ;;
        sock5)
            socks5_url="$(get_socks5_proxy)"
            if [ -z "$socks5_url" ]; then
                error "[错误] SOCKS5 模式已启用但未配置代理地址，请在 IP 出站模式中重新设置。"
                exit 1
            fi
            env -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_IP_MODE="$mode" XGENT_SOCKS5_PROXY="$socks5_url" \
                XGENT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
            ;;
        *)
            env -u XGENT_SOCKS5_PROXY -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_IP_MODE="$mode" \
                XGENT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
            ;;
    esac
}

ip_family_restrictor_python() {
    cat <<'PY'
import errno
import os
import socket

_ip_mode = (os.environ.get("XGENT_IP_MODE") or os.environ.get("TELEGRAM_AI_BOT_IP_MODE") or "").strip().lower()
_allowed_family = None
_blocked_family = None

# SOCKS5 代理模式：把代理地址写入标准代理环境变量，让 httpx / OpenAI SDK 自动走代理。
# 必须在任何 httpx 客户端创建之前完成，因此放在本预加载段执行。
if _ip_mode in {"sock5", "socks5"}:
    _socks5_url = (os.environ.get("XGENT_SOCKS5_PROXY") or os.environ.get("TELEGRAM_AI_BOT_SOCKS5_PROXY") or "").strip()
    if _socks5_url:
        # httpx 读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY
        for _proxy_key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                           "https_proxy", "http_proxy", "all_proxy"):
            os.environ[_proxy_key] = _socks5_url
        # 同时让 urllib / requests 系列也走代理
        os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "127.0.0.1,localhost")

if _ip_mode in {"ipv4", "4", "only_ipv4", "ipv4_only"}:
    _allowed_family = socket.AF_INET
    _blocked_family = socket.AF_INET6
elif _ip_mode in {"ipv6", "6", "only_ipv6", "ipv6_only"}:
    _allowed_family = socket.AF_INET6
    _blocked_family = socket.AF_INET

if _allowed_family is not None and _blocked_family is not None:
    _original_getaddrinfo = socket.getaddrinfo
    _original_socket = socket.socket

    def _family_name(family):
        if family == socket.AF_INET:
            return "IPv4"
        if family == socket.AF_INET6:
            return "IPv6"
        return str(family)

    def _blocked_error():
        return OSError(
            errno.EAFNOSUPPORT,
            f"{_family_name(_blocked_family)} is disabled by XGENT_IP_MODE={_ip_mode}",
        )

    def _restricted_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family in (0, socket.AF_UNSPEC):
            family = _allowed_family
        elif family == _blocked_family:
            raise socket.gaierror(socket.EAI_NONAME, str(_blocked_error()))

        results = _original_getaddrinfo(host, port, family, type, proto, flags)
        filtered = [item for item in results if item and item[0] == _allowed_family]
        if not filtered:
            raise socket.gaierror(
                socket.EAI_NONAME,
                f"no {_family_name(_allowed_family)} address available for {host!r}",
            )
        return filtered

    class _RestrictedSocket(_original_socket):
        def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
            if family == _blocked_family:
                raise _blocked_error()
            if fileno is None:
                super().__init__(family, type, proto)
            else:
                super().__init__(family, type, proto, fileno=fileno)

    socket.getaddrinfo = _restricted_getaddrinfo
    socket.socket = _RestrictedSocket
PY
}

bot_python_code() {
    ip_family_restrictor_python
    cat <<'PY'
import os
import runpy
from pathlib import Path

_app_entry = os.environ.get("XGENT_APP_ENTRY") or os.environ.get("TELEGRAM_AI_BOT_APP_ENTRY") or "xgent_server.py"
_app_path = Path(_app_entry)
if not _app_path.is_absolute():
    _app_path = Path.cwd() / _app_path
runpy.run_path(str(_app_path), run_name="__main__")
PY
}

configure_ip_mode() {
    local choice
    local current_label="$(ip_mode_label)"

    echo ""
    echo "当前 IP 出站模式: $current_label"
    echo "请选择 IP 出站模式 (以下四项互斥，只能同时生效一个):"
    echo "  1) 仅 IPv4 - 禁用 IPv6，Telegram 和 AI 服务等运行期请求只解析/连接 IPv4"
    echo "  2) 仅 IPv6 - 禁用 IPv4，Telegram 和 AI 服务等运行期请求只解析/连接 IPv6"
    echo "  3) SOCKS5 代理 - 所有出站流量走指定的 SOCKS5 代理"
    echo "  4) 撤回修改 - 取消限制，恢复服务器默认网络栈"
    echo "  5) 返回主菜单"
    echo ""
    read -r -p "请输入选项 [1/2/3/4/5，默认 5]: " choice

    case "$choice" in
        1)
            set_ip_mode ipv4
            success "已设置为仅 IPv4。请重启 Bot 后生效。"
            ;;
        2)
            set_ip_mode ipv6
            success "已设置为仅 IPv6。请重启 Bot 后生效。"
            ;;
        3)
            local proxy_url=""
            echo ""
            echo "SOCKS5 代理地址格式:"
            echo "  无认证:  socks5://127.0.0.1:1080"
            echo "  带认证:  socks5://user:pass@127.0.0.1:1080"
            echo ""
            read -r -p "请输入 SOCKS5 代理地址: " proxy_url
            if [ -z "$proxy_url" ]; then
                warn "未输入代理地址，已取消。"
                return 1
            fi
            if ! echo "$proxy_url" | grep -qE '^socks5://'; then
                error "[错误] 代理地址必须以 socks5:// 开头"
                return 1
            fi
            set_ip_mode sock5 "$proxy_url"
            success "已设置为 SOCKS5 代理: $proxy_url"
            warn "   请确保代理可用，并已安装 httpx[socks] 依赖（requirements.txt 已包含）。"
            warn "   请重启 Bot 后生效。"
            ;;
        4)
            set_ip_mode default
            success "已撤回 IP 限制，恢复服务器默认状态。请重启 Bot 后生效。"
            ;;
        ""|5)
            warn "已返回主菜单。"
            ;;
        *)
            error "[错误] 无效选项。"
            exit 1
            ;;
    esac
}

run_privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command_exists sudo; then
        sudo "$@"
    else
        error "[错误] 当前步骤需要 root 权限，请使用 root 运行，或先安装 sudo。"
        exit 1
    fi
}

ensure_apt_available() {
    if ! command_exists apt-get; then
        error "[错误] 自动补环境目前只支持 Debian/Ubuntu。"
        echo "请手动安装: python3 python3-venv python3-pip nodejs npm"
        exit 1
    fi
}

apt_package_installed() {
    command_exists dpkg-query || return 1
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

record_apt_packages() {
    local package

    [ "$#" -gt 0 ] || return
    ensure_state_dir
    touch "$APT_STATE_FILE"

    for package in "$@"; do
        if ! grep -Fxq "$package" "$APT_STATE_FILE"; then
            printf '%s\n' "$package" >> "$APT_STATE_FILE"
        fi
    done
}

ensure_apt_packages() {
    local package
    local missing_packages=()
    local newly_installed=()
    local before_file=""
    local after_file=""

    ensure_apt_available

    for package in "$@"; do
        if ! apt_package_installed "$package"; then
            missing_packages+=("$package")
        fi
    done

    if command_exists dpkg-query; then
        before_file="$(mktemp)"
        after_file="$(mktemp)"
        dpkg-query -W -f='${Package}\n' 2>/dev/null | sort > "$before_file" || true
    fi

    if [ "$APT_UPDATED" -eq 0 ]; then
        info "[信息] 正在执行 apt-get update..."
        run_privileged apt-get update
        APT_UPDATED=1
    fi

    info "[信息] 正在安装系统依赖: $*"
    run_privileged apt-get install -y "$@"

    if [ -n "$before_file" ] && [ -f "$before_file" ]; then
        dpkg-query -W -f='${Package}\n' 2>/dev/null | sort > "$after_file" || true
        while IFS= read -r package; do
            [ -n "$package" ] && newly_installed+=("$package")
        done < <(comm -13 "$before_file" "$after_file" 2>/dev/null || true)
        rm -f "$before_file" "$after_file"
    fi

    if [ "${#newly_installed[@]}" -gt 0 ]; then
        record_apt_packages "${newly_installed[@]}"
    elif [ "${#missing_packages[@]}" -gt 0 ]; then
        record_apt_packages "${missing_packages[@]}"
    fi
}

fix_line_endings() {
    warn "[修复] 正在统一换行符..."
    for file in *.sh *.py *.txt .env; do
        if [ -f "$file" ]; then
            sed -i 's/\r$//' "$file" 2>/dev/null || true
        fi
    done
    # 版本标记：部署目录不是 git 检出（zip 解压）时 CLI banner 拿不到哈希，
    # 这里写一份到 .xgent-version 兜底。CLI 读它显示 vXxx。
    git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null \
        | head -c 10 > "$SCRIPT_DIR/.xgent-version" || true
    [ -s "$SCRIPT_DIR/.xgent-version" ] || printf 'installed-%s' "$(date +%Y%m%d)" > "$SCRIPT_DIR/.xgent-version"
}

python_version_ok() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# 候选解释器文件名，按优先级从高到低。
#
# `python3` 排第一，而不是带版本号的那些：它是这台机器的"系统默认 Python"，
# 配套的 python3-venv / python3-pip 一定装齐了。反过来把 python3.13 排前面会
# 踩 Debian 的一个坑——`apt install python3.13` 不会带上 python3.13-venv，
# 于是我们挑中一个建不了 venv 的解释器，而自动补包又只会去装 python3-venv，
# 修的是另一个版本。只有当系统默认那个太旧时，才轮到带版本号的候选。
python_candidate_names() {
    printf '%s\n' python3 python3.14 python3.13 python3.12 python3.11 python3.10 python
}

# 要翻的目录：PATH 全部条目 + 几个常见安装位置，去重后保持顺序。
python_search_dirs() {
    local dir
    local IFS=:
    for dir in $PATH; do
        [ -n "$dir" ] && printf '%s\n' "$dir"
    done
    printf '%s\n' /usr/local/bin /usr/bin /bin
}

# 这个解释器是不是某个 venv 里的。
#
# 只能比对原始路径，不能先 readlink -f：venv/bin/python3 通常就是指向
# /usr/bin/pythonX.Y 的软链，解析完就认不出它来自 venv 了。
#
# `.venv` 也要认：uv / poetry 默认建的就是这个名字，在激活了 .venv 的 shell 里
# 跑本脚本，会原样复现下面 discover_python 注释里描述的那个坑。
is_project_venv_python() {
    case "$1" in
        "$VENV_DIR"/*) return 0 ;;
        */venv/bin/python*|*/.venv/bin/python*) return 0 ;;
    esac
    return 1
}

# 找一个够新、而且不在任何 venv 里的解释器。
#
# 直接用 `python3` 会踩一个很难自证的坑：只要 PATH 首位落在某个 venv 的 bin
# 目录（在激活了 venv 的 shell 里跑、或者从一个本身就跑在 venv 里的进程里调
# 起本脚本——比如让 Bot 自己执行 install.sh），`python3` 解析到的就是那个
# venv 的解释器。而 venv 解释器的版本被钉死在它**被创建的那一刻**，可能远比
# 系统里现有的旧。于是同一台机器上，SSH 手敲能装、从 Bot 里调起就报"需要
# Python 3.10"，看起来像"时好时坏"，实际只是 PATH 差异。
#
# 光换成 `command -v` 也不够：它只返回第一个命中，venv 排在 PATH 首位时后面
# 所有同名解释器全被挡住。所以这里按"文件名优先级 × 全部搜索目录"逐个试，
# 并跳过 venv 里的那些。第一个够新的就是答案——文件名顺序已经表达了偏好，
# 没必要再去比较版本号大小（那会引入"挑了个更新但缺 venv 模块"的新问题）。
discover_python() {
    local name dir candidate dirs

    if [ -n "${XGENT_PYTHON:-}" ]; then
        candidate="$(command -v "$XGENT_PYTHON" 2>/dev/null || true)"
        if [ -n "$candidate" ] && python_version_ok "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
        # 用户明确指定了却用不了，就该报错，而不是悄悄换一个——那样他会
        # 以为自己的指定生效了。
        return 1
    fi

    dirs="$(python_search_dirs | awk '!seen[$0]++')"
    while IFS= read -r name; do
        while IFS= read -r dir; do
            candidate="$dir/$name"
            [ -x "$candidate" ] || continue
            is_project_venv_python "$candidate" && continue
            python_version_ok "$candidate" || continue
            printf '%s\n' "$candidate"
            return 0
        done <<< "$dirs"
    done < <(python_candidate_names)
    return 1
}

ensure_python() {
    info "[检查] 正在检查 Python 3..."

    # 先探测，再谈补包。这里原来反着来：先用硬编码的 python3/python3.12/python3.11
    # 判断"要不要 apt 补一遍"，之后才 discover_python。于是只装了 python3.13、
    # 没有 python3 软链的机器会先进 apt 分支，而 ensure_apt_available 在非
    # Debian 系统上直接 exit 1——明明有一个完全可用的解释器，连用户显式给的
    # XGENT_PYTHON 都没机会生效。
    PYTHON_BIN="$(discover_python || true)"

    if [ -z "$PYTHON_BIN" ] && [ -z "${XGENT_PYTHON:-}" ]; then
        warn "   未找到 3.10 及以上的 Python，尝试自动安装..."
        ensure_apt_packages python3 python3-pip python3-venv
        PYTHON_BIN="$(discover_python || true)"
    fi

    if [ -z "$PYTHON_BIN" ]; then
        error "[错误] 需要 Python 3.10 或更高版本，但当前系统里找不到可用的。"
        if [ -n "${XGENT_PYTHON:-}" ]; then
            error "       XGENT_PYTHON=${XGENT_PYTHON} 指向的解释器不可用或版本过低。"
        fi
        if command_exists python3; then
            error "       当前 PATH 里的 python3: $(command -v python3) ($(python3 --version 2>&1))"
            error "       注意：venv 里的解释器已被刻意跳过，它的版本停留在创建时那一刻。"
        fi
        error "       装好新版本后重跑本脚本，或用 XGENT_PYTHON=/path/to/python3.12 指定。"
        exit 1
    fi

    if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
        warn "   缺少 python3-venv，尝试自动安装..."
        ensure_apt_packages python3-venv python3-pip
    fi

    if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
        warn "   缺少 pip，尝试自动安装..."
        ensure_apt_packages python3-pip
    fi

    echo "   使用解释器: $PYTHON_BIN"
    echo "   Python 版本: $("$PYTHON_BIN" --version 2>&1)"
    success "Python 版本满足要求 (>= 3.10)"
}

resolve_path() {
    local target="$1"
    echo "$(cd "$(dirname "$target")" && pwd -P)/$(basename "$target")"
}

# 删 venv 前的最后一道闸。
#
# 原来这里比的是 resolve_path "$SCRIPT_DIR/venv" 和 resolve_path "$VENV_DIR"，
# 而 VENV_DIR 就是 "$SCRIPT_DIR/venv"——两个操作数是同一个表达式，判据永远成立，
# 等于给一句 rm -rf 装了个假的保险。改成校验解析后的真实路径本身：必须直接落在
# $SCRIPT_DIR 下面，且目录名就叫 venv。
safe_remove_venv() {
    local resolved script_root
    resolved="$(resolve_path "$VENV_DIR")"
    script_root="$(cd "$SCRIPT_DIR" && pwd -P)"

    if [ "$(basename "$resolved")" != "venv" ] || [ "$(dirname "$resolved")" != "$script_root" ]; then
        error "[错误] 安全保护触发，拒绝删除非预期目录: $resolved"
        exit 1
    fi

    rm -rf -- "$resolved"
}

confirm_uninstall() {
    local answer

    if [ "$SKIP_CONFIRM" -eq 1 ]; then
        return
    fi

    warn "即将卸载本安装脚本创建的运行环境和服务记录。"
    echo "   会清理: venv、本项目 nohup/PM2 进程、systemd 服务 (${SYSTEMD_SERVICE_NAME})、"
    echo "           本地 API 容器 (${LOCAL_API_CONTAINER})、xgent.pid、xgent 命令链接、"
    echo "           保活方式记录、IP 出站模式设置。"
    echo "   会保留: 项目文件、.env、数据库、日志、${LOCAL_API_DATA_DIR##*/}/、"
    echo "           skill-public/ 下脚本服务、PM2 程序本体。"
    echo "   会单独再问一次: 本脚本装过的 apt 包要不要 purge（这一步不可逆，会连带"
    echo "                   删掉依赖它们的包，届时会先把 apt 的完整清单摊开给你看）。"
    echo ""
    read -r -p "确认继续？输入 y 继续: " answer
    if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
        warn "已取消卸载。"
        exit 0
    fi
}

stop_background_process() {
    local pid cmdline
    local found_extra=0

    if [ -f "xgent.pid" ]; then
        pid="$(cat xgent.pid 2>/dev/null || true)"
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            warn "   xgent.pid 内容不是有效 PID，已移除 PID 文件。"
            rm -f xgent.pid
        elif ! ps -p "$pid" >/dev/null 2>&1; then
            rm -f xgent.pid
            success "已清理过期 xgent.pid"
        else
            cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
            if { [[ "$cmdline" == *"$APP_ENTRY"* ]] || [[ "$cmdline" == *"$LEGACY_APP_ENTRY"* ]]; } && { [[ "$cmdline" == *"$SCRIPT_DIR"* ]] || [[ "$cmdline" == *"$VENV_PYTHON"* ]]; }; then
                info "[卸载] 正在停止 nohup 后台进程: $pid"
                kill "$pid" 2>/dev/null || true
                sleep 2
                if ps -p "$pid" >/dev/null 2>&1; then
                    # kill 默认发的就是 TERM，这里再发一次 TERM 等于原地打转。
                    # 卡住的进程只能靠 KILL 收掉——否则下面照样 rm 掉 pid 文件，
                    # 那个进程就此失联，还占着端口和数据库。
                    kill -9 "$pid" 2>/dev/null || true
                fi
                rm -f xgent.pid
                success "nohup 后台进程已停止"
            else
                warn "   xgent.pid 指向的进程不像本项目进程，已跳过停止: $pid"
            fi
        fi
    fi

    while IFS= read -r line; do
        pid="${line%% *}"
        cmdline="${line#* }"

        if ! [[ "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -eq "$$" ]; then
            continue
        fi

        # 第二组判据必须是"这个进程属于本目录"。这里曾经还有一个 or 分支
        # ——cmdline 含 XGENT_APP_ENTRY / TELEGRAM_AI_BOT_APP_ENTRY 就算命中，
        # 而 bot_python_code 生成的内联 Python 本身就含这两个字面量，于是同机
        # 每一份副本的 argv 都命中，$SCRIPT_DIR 这层限定被完全短路：在
        # /tmp/xgt 里跑一次 stop，会顺手杀掉另一个目录下正在服务的进程。
        if { [[ "$cmdline" == *"$APP_ENTRY"* ]] || [[ "$cmdline" == *"$LEGACY_APP_ENTRY"* ]]; } \
            && { [[ "$cmdline" == *"$SCRIPT_DIR"* ]] || [[ "$cmdline" == *"$VENV_PYTHON"* ]]; }; then
            found_extra=1
            info "[卸载] 正在停止残留进程: $pid"
            kill "$pid" 2>/dev/null || true
        fi
    done < <(ps -eo pid=,args= 2>/dev/null | sed 's/^ *//' || true)

    if [ "$found_extra" -eq 1 ]; then
        sleep 2
        success "残留进程已处理"
    fi
}

remove_pm2_process() {
    local app_name found=0
    if ! command_exists pm2; then
        warn "   未检测到 PM2，跳过 PM2 进程清理。"
        return
    fi

    for app_name in "$PM2_APP_NAME" "$LEGACY_PM2_APP_NAME"; do
        if pm2 describe "$app_name" >/dev/null 2>&1; then
            found=1
            info "[卸载] 正在移除 PM2 进程: $app_name"
            pm2 delete "$app_name" >/dev/null 2>&1 || warn "   PM2 进程移除失败，请手动执行: pm2 delete $app_name"
        fi
    done

    if [ "$found" -eq 1 ]; then
        if pm2 save >/dev/null 2>&1; then
            success "PM2 进程记录已更新"
        else
            warn "   PM2 进程列表保存失败，请手动执行: pm2 save"
        fi
    else
        echo "   未发现 PM2 进程: $PM2_APP_NAME / $LEGACY_PM2_APP_NAME"
    fi
}
remove_virtualenv() {
    if [ -e "$VENV_DIR" ]; then
        info "[卸载] 正在删除虚拟环境: $VENV_DIR"
        safe_remove_venv
        success "虚拟环境已删除"
    else
        echo "   未发现虚拟环境: $VENV_DIR"
    fi
}

remove_recorded_apt_packages() {
    local package
    local packages=()
    local kept_packages=()

    if [ ! -f "$APT_STATE_FILE" ]; then
        warn "   未发现 apt 安装记录，跳过系统包卸载。"
        echo "   说明: 只有本脚本记录过的新装 apt 包才会被卸载。"
        rmdir "$STATE_DIR" 2>/dev/null || true
        return
    fi

    if ! command_exists apt-get; then
        warn "   当前系统没有 apt-get，跳过系统包卸载。"
        return
    fi

    while IFS= read -r package; do
        if [ -z "$package" ] || ! [[ "$package" =~ ^[A-Za-z0-9.+:-]+$ ]] || ! apt_package_installed "$package"; then
            continue
        fi

        case "$package" in
            pm2|nodejs|npm|node-*|libnode*|libuv*|libc-ares*)
                kept_packages+=("$package")
                ;;
            # python3 系是系统基础包：Debian 上一大票东西依赖它，purge 掉会级联
            # 把它们一起删走。我们"装过"它只是因为当时机器上没有，卸载时宁可留着
            # ——留一个没人用的解释器，比拆掉别人正在用的依赖便宜得多。
            python3|python3-*|python3.*|libpython3*)
                kept_packages+=("$package")
                ;;
            *)
                packages+=("$package")
                ;;
        esac
    done < <(sort -u "$APT_STATE_FILE")

    if [ "${#kept_packages[@]}" -gt 0 ]; then
        warn "   以下包属于 PM2/Node 或 Python 基础环境，一律跳过不卸:"
        printf '   %s\n' "${kept_packages[@]}"
    fi

    if [ "${#packages[@]}" -eq 0 ]; then
        echo "   apt 安装记录中没有仍处于安装状态的包。"
        rm -f "$APT_STATE_FILE"
        rmdir "$STATE_DIR" 2>/dev/null || true
        return
    fi

    info "[卸载] 本脚本记录过的新装 apt 包:"
    printf '   %s\n' "${packages[@]}"

    # 先 simulate 一遍。ensure_apt_packages 记的是 dpkg 全量列表的差集，里面混着
    # apt 自己拉进来的传递依赖；purge 它们时 apt 又会把"现在依赖这些包的东西"
    # 一起级联删掉。所以真正被删的往往远多于上面这几行——不摊开给人看就按 -y
    # 冲下去，是这个脚本里最不可逆的一步。
    local removal_preview=""
    if removal_preview="$(apt-get -s purge "${packages[@]}" 2>/dev/null)"; then
        removal_preview="$(printf '%s\n' "$removal_preview" | awk '/^Remv /{print $2}' | sort -u)"
    else
        removal_preview=""
    fi
    if [ -n "$removal_preview" ]; then
        warn "   apt 实际会移除以下包（含被级联带走的依赖）:"
        while IFS= read -r package; do
            # 用 if 而不是 `[ -n "$package" ] && echo`：后者在最后一轮判据为假时
            # 会把整个 while 的退出码带成 1，在 set -e 下当场中断卸载。
            if [ -n "$package" ]; then
                echo "   - $package"
            fi
        done <<< "$removal_preview"
    else
        warn "   无法预演 apt 的移除清单（apt-get -s 失败），下面这一步的影响面未知。"
    fi

    if [ "$SKIP_CONFIRM" -ne 1 ]; then
        local answer=""
        echo ""
        read -r -p "   确认执行 apt-get purge？输入 y 继续，其它任意键跳过: " answer
        if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
            warn "   已跳过 apt 包卸载。记录留在 $APT_STATE_FILE，需要时可手动处理。"
            return
        fi
    fi

    run_privileged apt-get purge -y "${packages[@]}" || warn "   部分 apt 包卸载失败，请根据上方输出手动检查。"
    # autoremove --purge 会清掉系统上**所有**它认为孤立的包，不限于我们装的那批。
    # 默认不跑，交给知道自己在做什么的人显式开。
    if [ "${XGENT_APT_AUTOREMOVE:-0}" = "1" ]; then
        run_privileged apt-get autoremove --purge -y || true
    else
        warn "   已跳过 apt-get autoremove --purge（它会连带清掉系统上其它孤立包）。"
        echo "   确实需要时手动执行，或用 XGENT_APT_AUTOREMOVE=1 重跑卸载。"
    fi
    run_privileged apt-get clean || true

    rm -f "$APT_STATE_FILE"
    rmdir "$STATE_DIR" 2>/dev/null || true
    success "已处理 apt 安装记录"
}

remove_ip_mode_state() {
    # 只清 .env 里那四个键。原来这里还删一个 .install-state/ip-mode 文件，
    # 但全脚本没有任何地方写它——早期设计的残留。
    if [ -f ".env" ] && grep -qE "^(XGENT_IP_MODE|XGENT_SOCKS5_PROXY|TELEGRAM_AI_BOT_IP_MODE|TELEGRAM_AI_BOT_SOCKS5_PROXY)=" .env 2>/dev/null; then
        info "[卸载] 正在清理 .env 中的 IP 出站模式 / SOCKS5 代理设置"
        env_unset XGENT_IP_MODE XGENT_SOCKS5_PROXY \
            TELEGRAM_AI_BOT_IP_MODE TELEGRAM_AI_BOT_SOCKS5_PROXY
        success "IP 出站模式设置已清理"
    fi
    # 末尾曾是 `[ "$cleaned" -eq 1 ] && success ...`：没清到东西时整个函数返回 1，
    # 而 uninstall_app 在 set -e 下不带判据地调它——于是"本来就没设过 IP 模式"这种
    # 最常见的情形会让卸载在这里静静中断，后面的 remove_xgent_command 压根没跑到。
    rmdir "$STATE_DIR" 2>/dev/null || true
}

remove_runtime_mode_state() {
    [ -f "$RUNTIME_MODE_FILE" ] || return 0
    info "[卸载] 正在清理保活方式记录"
    rm -f "$RUNTIME_MODE_FILE"
    success "保活方式记录已清理"
}

uninstall_app() {
    if [ "${1:-}" != "--no-banner" ]; then
        print_banner
    fi

    confirm_uninstall

    # 容器要第一个关。它是 --restart unless-stopped 起的，不关掉的话卸载完还在跑、
    # 机器重启还会自己回来，端口一直占着，白名单里加过 0.0.0.0 就一直对公网开着。
    # 顺带把 token 从本地 server 登出交还官方，这一步也只有现在做才有意义。
    stop_local_api_container no-restart

    remove_pm2_process
    remove_systemd_service
    stop_background_process
    remove_virtualenv
    remove_recorded_apt_packages
    remove_runtime_mode_state
    remove_ip_mode_state
    remove_xgent_command

    echo ""
    echo -e "${CYAN}========================================================${NC}"
    echo -e "${CYAN} XGent for Telegram 主安装内容已卸载。${NC}"
    echo -e "${CYAN} 已保留项目文件、配置、数据库、日志、skill-public 服务和 PM2 本体。${NC}"
    echo -e "${CYAN}========================================================${NC}"
}

ensure_virtualenv() {
    info "[检查] 正在检查虚拟环境..."

    [ -n "$PYTHON_BIN" ] || ensure_python

    if [ -x "$VENV_PYTHON" ] && [ -f "$VENV_ACTIVATE" ]; then
        if python_version_ok "$VENV_PYTHON"; then
            success "现有虚拟环境可正常使用（$("$VENV_PYTHON" --version 2>&1)）"
            return
        fi
        # venv 的解释器版本钉死在它被创建的那一刻。系统后来升级了 Python，
        # 旧 venv 不会跟着升——留着它只会让"系统 3.12 但项目跑在 3.9 上"这种
        # 组合一直存在，而且是**运行期**才炸。这里直接重建。
        warn "   现有 venv 的 Python 版本过低（$("$VENV_PYTHON" --version 2>&1)），正在用 $PYTHON_BIN 重建..."
        safe_remove_venv
    elif [ -e "$VENV_DIR" ]; then
        warn "   检测到损坏或不完整的 venv，正在重建..."
        safe_remove_venv
    fi

    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        warn "   创建虚拟环境失败，正在安装 python3-venv 后重试..."
        ensure_apt_packages python3-venv python3-pip
        if [ -e "$VENV_DIR" ]; then
            safe_remove_venv
        fi
        "$PYTHON_BIN" -m venv "$VENV_DIR"
    fi

    if [ ! -x "$VENV_PYTHON" ] || [ ! -f "$VENV_ACTIVATE" ]; then
        error "[错误] 无法创建可用的虚拟环境。"
        exit 1
    fi

    # 让 install_requirements 知道这是个全新的 venv：里面什么都没有，
    # 必须真的装一遍，不能走"依赖没变就跳过"那条快路。
    VENV_CREATED=1
    success "虚拟环境创建完成（$("$VENV_PYTHON" --version 2>&1)）"
}

activate_virtualenv() {
    if [ ! -f "$VENV_ACTIVATE" ]; then
        error "[错误] 找不到激活脚本: $VENV_ACTIVATE"
        exit 1
    fi

    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
}

# 依赖装过没有、而且装的是不是眼下这一份 requirements.txt。
# 比对内容而不是 mtime：fix_line_endings 的 sed -i 每次都会把 mtime 刷新一遍。
requirements_up_to_date() {
    command_exists cmp || return 1
    [ -f "$REQUIREMENTS_SNAPSHOT" ] || return 1
    cmp -s requirements.txt "$REQUIREMENTS_SNAPSHOT"
}

core_imports_ok() {
    python -c 'import aiosqlite, telegram, openai' >/dev/null 2>&1
}

print_core_versions() {
    python -c "import aiosqlite; print('   aiosqlite 版本:', aiosqlite.__version__)"
    python -c "import telegram; print('   python-telegram-bot 版本:', telegram.__version__)"
    python -c "import openai; print('   openai 版本:', openai.__version__)"
}

install_requirements() {
    # 重启也会走到这里（restart → prepare_environment → 本函数）。而 pip 每次都
    # 要联网——`--upgrade pip` 必然去问一次 PyPI——网络一断就被 set -e 掀掉整个
    # 重启，可"重启一个已经装好的服务"跟装依赖本来是两件事。所以：requirements.txt
    # 与上次装的那份一致、三个核心包又都 import 得动，就直接跳过。
    # 两个条件都要满足，venv 被人删过半截时才还能自愈。
    if [ "$VENV_CREATED" -eq 0 ] && requirements_up_to_date && core_imports_ok; then
        info "[pip] 依赖与上次安装一致，跳过（重启路径不联网）。"
        print_core_versions
        return
    fi

    # pip 自身只在新建 venv 时升一次，失败也不致命：它是顺手做的好事，
    # 不是装依赖的前提。
    if [ "$VENV_CREATED" -eq 1 ]; then
        info "[pip] 正在升级 pip..."
        python -m pip install --upgrade pip -q || warn "   pip 自身升级失败，继续用现有版本。"
    fi

    info "[pip] 正在安装 Python 依赖..."
    python -m pip install -r requirements.txt -q

    ensure_state_dir
    cp -f requirements.txt "$REQUIREMENTS_SNAPSHOT" 2>/dev/null || true
    success "依赖安装完成"
    print_core_versions
}

# ==========================================================================
# .env 读写：只走 env_get / env_set / env_unset
# ==========================================================================
# 以前这里有七份各写一遍的实现，踩过的坑各修在其中一两份里：行首没锚定会顺手
# 删掉 XGENT_BOT_TOKEN（grep -vF "BOT_TOKEN=" 匹配行内任意位置）、值末尾的 \r
# 会让 AUTHORIZED_USER_ID 被判成非数字、临时文件落在 /tmp 时跨文件系统 mv 会
# 退化成复制+删除并可能截断整个 .env。三条一次性钉在这里，别再另写第八份。

# 读一个键的值，不存在则输出空。
#
# 末尾的 \r 必须去掉：用户在 Windows 上编辑过 .env 再传到服务器是很常见的，
# 那样每个值都会多带一个回车符。fix_line_endings 只在安装流程里跑，而
# `install.sh status`、组件清单这些路径会先读 .env——不去掉的话
# AUTHORIZED_USER_ID 会被判成"不是合法数字"，BOT_TOKEN 则会带着 \r 发给
# Telegram 直接鉴权失败，两种症状都完全看不出根因在换行符上。
env_get() {
    local key="$1"
    [ -f ".env" ] || return 0
    awk -F= -v k="$key" '$1==k{print substr($0,length(k)+2)}' .env 2>/dev/null \
        | tail -n 1 | tr -d '\r' || true
}

# 写入/覆盖一个键。
env_set() {
    local key="$1" value="$2" tmp_file

    # 临时文件建在 .env 同目录：mktemp 默认落在 /tmp，跨文件系统时 mv 会退化成
    # 复制+删除，复制中途失败会把含 BOT_TOKEN 和全部 api_key 的 .env 截断且无备份。
    # 同目录下 mv 是原子 rename。
    tmp_file="$(mktemp ./.env.XXXXXX)"
    chmod 600 "$tmp_file"
    if [ -f ".env" ]; then
        # 必须锚定到行首：grep -vF "KEY=" 会匹配行内任意位置，
        # 写 BOT_TOKEN 会顺手删掉 XGENT_BOT_TOKEN 等所有含该串的行。
        grep -v "^${key}=" .env > "$tmp_file" || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
    mv "$tmp_file" .env
    chmod 600 .env
}

# 删掉一个或多个键。
env_unset() {
    local pattern tmp_file
    [ -f ".env" ] || return 0
    [ "$#" -gt 0 ] || return 0

    pattern="$(printf '%s|' "$@")"
    pattern="^(${pattern%|})="

    tmp_file="$(mktemp ./.env.XXXXXX)"
    chmod 600 "$tmp_file"
    grep -vE "$pattern" .env > "$tmp_file" || true
    mv "$tmp_file" .env
    chmod 600 .env
}

# 容器运行状态：running / exited / missing / docker-unavailable
local_api_container_status() {
    if ! command_exists docker; then
        printf 'docker-unavailable\n'
        return
    fi

    if ! docker info >/dev/null 2>&1; then
        printf 'docker-unavailable\n'
        return
    fi

    local state
    state="$(docker inspect -f '{{.State.Status}}' "$LOCAL_API_CONTAINER" 2>/dev/null || true)"
    case "$state" in
        running|exited|paused|restarting|created|dead)
            printf '%s\n' "$state"
            ;;
        *)
            printf 'missing\n'
            ;;
    esac
}

# bot 是否走本地 API：enabled / disabled（依据 .env 中 TELEGRAM_API_URL 非空）
local_api_enabled_for_bot() {
    local url
    url="$(env_get TELEGRAM_API_URL)"
    if [ -n "$url" ]; then
        printf 'enabled\n'
    else
        printf 'disabled\n'
    fi
}

# 读取允许访问本地 API 的 IP 白名单。
# 文件不存在或为空时，返回默认 127.0.0.1（仅本机）。
# 每行一个 IP，忽略空行和 # 注释。
get_allowed_ips() {
    if [ -f "$LOCAL_API_ALLOWED_IPS_FILE" ]; then
        local ips
        ips="$(grep -vE '^\s*(#|$)' "$LOCAL_API_ALLOWED_IPS_FILE" 2>/dev/null || true)"
        if [ -n "$ips" ]; then
            printf '%s\n' "$ips"
            return
        fi
    fi
    printf '127.0.0.1\n'
}

# 简单 IPv4 校验（4 段 0-255）。IPv6 不做严格校验，交给 docker 判断。
is_valid_ipv4() {
    local ip="$1"
    if [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
        local i
        for i in 1 2 3 4; do
            local seg="${BASH_REMATCH[i]}"
            if [ "$seg" -gt 255 ]; then
                return 1
            fi
        done
        return 0
    fi
    return 1
}

# 把一个 IP 追加到白名单（去重）
add_allowed_ip() {
    local ip="$1"
    ensure_state_dir
    touch "$LOCAL_API_ALLOWED_IPS_FILE"
    if grep -Fxq -- "$ip" "$LOCAL_API_ALLOWED_IPS_FILE" 2>/dev/null; then
        return 0
    fi
    printf '%s\n' "$ip" >> "$LOCAL_API_ALLOWED_IPS_FILE"
}

# 从白名单删除一个 IP
remove_allowed_ip() {
    local ip="$1"
    if [ ! -f "$LOCAL_API_ALLOWED_IPS_FILE" ]; then
        return 0
    fi
    local tmp_file
    tmp_file="$(mktemp)"
    grep -Fxv -- "$ip" "$LOCAL_API_ALLOWED_IPS_FILE" > "$tmp_file" 2>/dev/null || true
    mv "$tmp_file" "$LOCAL_API_ALLOWED_IPS_FILE"
}

ensure_docker() {
    # 已安装且守护进程可用
    if command_exists docker && docker info >/dev/null 2>&1; then
        return 0
    fi

    # 命令存在但守护进程没起 → 尝试启动
    if command_exists docker; then
        warn "   检测到 docker 命令但守护进程未运行，尝试启动..."
        if command_exists systemctl; then
            run_privileged systemctl enable --now docker >/dev/null 2>&1 || true
        elif command_exists service; then
            run_privileged service docker start >/dev/null 2>&1 || true
        fi
        sleep 3
        if docker info >/dev/null 2>&1; then
            success "Docker 守护进程已启动"
            return 0
        fi
        error "[错误] Docker 已安装但守护进程仍无法运行。"
        echo "   请手动启动 docker 服务或将当前用户加入 docker 组后重试。"
        return 1
    fi

    # 完全没装 → 自动安装
    warn "   未检测到 docker，正在自动安装（使用 get.docker.com 官方脚本）..."
    if ! command_exists curl && ! command_exists wget; then
        # 先确保有下载工具
        ensure_apt_packages curl ca-certificates 2>/dev/null || true
    fi

    local installer
    installer="$(mktemp)"
    if command_exists curl; then
        if ! curl -fsSL https://get.docker.com -o "$installer"; then
            error "[错误] 下载 Docker 安装脚本失败，请检查网络。"
            rm -f "$installer"
            return 1
        fi
    elif command_exists wget; then
        if ! wget -qO "$installer" https://get.docker.com; then
            error "[错误] 下载 Docker 安装脚本失败，请检查网络。"
            rm -f "$installer"
            return 1
        fi
    else
        error "[错误] 需要 curl 或 wget 来下载 Docker 安装脚本。"
        return 1
    fi

    info "[信息] 正在执行 Docker 安装（需要 root 权限，可能耗时几分钟）..."
    if ! run_privileged sh "$installer"; then
        error "[错误] Docker 自动安装失败。"
        echo "   请参考官方文档手动安装: https://docs.docker.com/engine/install/"
        rm -f "$installer"
        return 1
    fi
    rm -f "$installer"
    hash -r 2>/dev/null || true

    # 启动守护进程
    if command_exists systemctl; then
        run_privileged systemctl enable --now docker >/dev/null 2>&1 || true
    elif command_exists service; then
        run_privileged service docker start >/dev/null 2>&1 || true
    fi
    sleep 3

    if ! command_exists docker; then
        error "[错误] Docker 安装完成但命令未进入 PATH。"
        echo "   请重新登录后重试，或检查 PATH 配置。"
        return 1
    fi

    if ! docker info >/dev/null 2>&1; then
        error "[错误] Docker 已安装但守护进程未运行。"
        echo "   请手动执行: sudo systemctl start docker"
        return 1
    fi

    # 把当前用户加入 docker 组（避免每次都要 sudo）
    local current_user
    current_user="$(id -un)"
    if [ "$(id -u)" -ne 0 ] && [ -n "$current_user" ]; then
        if getent group docker >/dev/null 2>&1; then
            if id -nG | tr ' ' '\n' | grep -qx docker; then
                success "当前用户已在 docker 组"
            else
                info "[信息] 正在将当前用户加入 docker 组..."
                if run_privileged usermod -aG docker "$current_user" 2>/dev/null; then
                    warn "   已将 $current_user 加入 docker 组。"
                    echo "   注意: 需重新登录后才能免 sudo 使用 docker；本次将临时用 sudo 操作容器。"
                fi
            fi
        fi
    fi

    success "Docker 安装完成并已运行"
    return 0
}

# 对指定 base 调用 Telegram Bot API logOut，把 token 从该 server 登出。
# 用法: telegram_log_out <base_url>
#   base_url 形如 "https://api.telegram.org" 或 "http://localhost:8081"
# logOut 只对 token 当前真正登录的 server 有效；对未登录的 server 调用会返回错误，
# 这里按“尝试登出，失败仅警告”处理（幂等、不阻断流程）。
telegram_log_out() {
    local base="$1"
    local token log_out_url

    token="$(env_get BOT_TOKEN)"
    if [ -z "$token" ]; then
        warn "   .env 中没有 BOT_TOKEN，跳过 logOut。"
        return 1
    fi

    base="${base%%/}"

    info "[本地 API] 正在调用 logOut (从 ${base} 登出 bot token)..."
    # telegram logOut 成功返回 {"ok":true,...}，未登录/网络错误返回非 0 或 {"ok":false}
    local http_code body
    # URL 里带着 BOT_TOKEN，不能直接放进命令行——同机任何用户 ps aux 都能看到。
    # curl 走 --config 从文件读 URL；wget 没有等价能力，退化用环境变量。
    local url_file
    url_file="$(mktemp)"
    chmod 600 "$url_file"
    set +e
    if command_exists curl; then
        printf 'url = "%s/bot%s/logOut"\n' "$base" "$token" > "$url_file"
        body="$(curl -sS --max-time 15 -X POST -d '' --config "$url_file" 2>/dev/null || true)"
    elif command_exists wget; then
        # wget 没有从文件读 URL 的能力，token 仍会出现在 argv。
        # 这条只是 curl 不存在时的降级路径。
        warn "   未找到 curl，改用 wget；此路径下 token 会短暂出现在进程列表中。"
        body="$(wget -qO- --timeout=15 --post-data='' "${base}/bot${token}/logOut" 2>/dev/null || true)"
    else
        rm -f "$url_file"
        set -euo pipefail
        warn "   未找到 curl/wget，跳过 logOut。"
        return 1
    fi
    rm -f "$url_file"
    set -euo pipefail

    if [ -n "$body" ] && printf '%s' "$body" | grep -q '"ok":true'; then
        success "logOut 成功 (token 已从 ${base} 登出)。"
        return 0
    fi

    warn "   logOut 未能确认成功 (返回: ${body:-空})。"
    echo "   常见原因: token 本就不在该 server 上（无需登出）、或网络不通。"
    return 1
}

ensure_env_value() {
    local key="$1"
    local prompt="$2"
    # mode: 空=普通文本 / secret=输入不回显 / int=必须是纯数字，非法值反复重问
    #
    # 加 int 模式是因为一次真实用户报告过的连锁事故：AUTHORIZED_USER_ID 从
    # 未填过，之前又手动往 .env 塞过一行中文说明（例如“系统信息”这类误粘贴
    # 的占位文字），awk 抓到的 current_value 非空、这里直接 return 放行，
    # 于是错误值一路带到运行时——core.py 的 int() 转换失败，AUTHORIZED_USER_ID
    # 静默退化成 0，鉴权直接判死，Bot 陷入 PM2 重启死循环，且日志文案是英文
    # 逻辑变量名，用户完全看不出问题出在 .env 里那一行。这里把校验挪到装的
    # 时候做，比等运行时崩溃、翻日志排查省事得多。
    local mode="${3:-}"
    local current_value=""
    local new_value=""

    current_value="$(env_get "$key")"

    if [ "$mode" = "int" ] && [ -n "$current_value" ]; then
        case "$current_value" in
            *[!0-9]* | "")
                warn "   .env 中的 $key 当前值不是合法数字（值：$current_value），需要重新输入。"
                current_value=""
                ;;
        esac
    fi

    if [ -n "$current_value" ]; then
        return
    fi

    warn "   .env 中缺少 $key，请现在输入。"
    while [ -z "$new_value" ]; do
        if [ "$mode" = "secret" ]; then
            # 密钥类输入不回显，避免留在终端回滚缓冲和录屏里
            read -r -s -p "$prompt: " new_value
            echo
        else
            read -r -p "$prompt: " new_value
        fi
        if [ "$mode" = "int" ] && [ -n "$new_value" ]; then
            case "$new_value" in
                *[!0-9]* | "")
                    warn "   请只输入数字，重新输入。"
                    new_value=""
                    ;;
            esac
        fi
    done
    env_set "$key" "$new_value"
}

# 「切换部署模式」的入口（install.sh switch-mode）。真正的改法已经收进
# 组件清单里的 bot 一项——填 Token / 换授权 ID / 移除 Bot 都在那里，
# 这里只是把老命令名接过去，免得再维护第二份提示与校验逻辑。
switch_deploy_mode() {
    local do_restart=""

    echo ""
    echo "当前模式：$(deploy_mode_label)"
    deploy_bot
    sync_deploy_mode_state

    echo ""
    read -r -p "是否现在重启服务以生效？[Y/n]: " do_restart
    case "$do_restart" in
        n|N)
            warn "请稍后手动重启（主菜单第 3 项，或 bash install.sh restart）。"
            ;;
        *)
            prepare_environment
            restart_app || warn "自动重启未成功，请手动在主菜单里选择启动方式。"
            ;;
    esac
}

# install.sh 侧读写 Web 配置的唯一通道。
#
# 这里以前是五段各自抄了一遍 bootstrap 的内联 Python（set_web_password、
# set_web_port 的读和写、load_web_state、set_web_enabled），连"把加载期日志丢进
# 黑洞"那段注释都抄了五遍，约 120 行重复。现在统一交给 tools/xgent_config.py，
# 一个子命令对应一件事。
run_config_py() {
    run_bot_python "$VENV_PYTHON" tools/xgent_config.py "$@"
}

set_web_password() {
    # Web 服务没有密码就不会启动（start_web_chat_if_enabled 直接跳过），
    # 仅 Web 模式下这等于装完连不上。所以密码是 web 组件的安装步骤本身，
    # 不是"可选的加固项"。
    local password="" password_confirm="" exit_code

    info "[web] 设置访问密码"
    while true; do
        read -r -s -p "   请输入 Web 访问密码（至少 6 位，输入时不显示）: " password
        echo
        if [ "${#password}" -lt 6 ]; then
            warn "   密码至少 6 位，请重新输入。"
            continue
        fi
        read -r -s -p "   请再次输入以确认: " password_confirm
        echo
        if [ "$password" != "$password_confirm" ]; then
            warn "   两次输入不一致，请重新输入。"
            continue
        fi
        break
    done

    set +e
    # 密码走环境变量传给子进程，不进 argv——argv 在同机任何用户的 ps 里都看得见。
    XGENT_WEB_PASSWORD="$password" run_config_py set-password >/dev/null
    exit_code=$?
    set -euo pipefail
    # 密码变量只在子进程环境里短暂存在，这里主动清掉，避免残留在当前 shell。
    unset password password_confirm

    if [ "$exit_code" -ne 0 ]; then
        error "[错误] 设置 Web 访问密码失败，请查看上方输出。"
        exit 1
    fi
    reset_web_state_cache
    success "Web 访问密码已设置。"
}

set_web_port() {
    # 用户明确要的是"纯本地端口访问"，装完不告诉端口、也不给改端口的机会，
    # 等于让人对着一个不知道连哪的服务发呆。和 set_web_password 对称。
    local current_from_db exit_code

    info "[web] 设置监听端口"

    set +e
    current_from_db=$(run_config_py get-port)
    exit_code=$?
    set -euo pipefail

    if [ "$exit_code" -ne 0 ]; then
        error "[错误] 读取 Web 端口失败，请查看上方输出。"
        exit 1
    fi

    echo "   当前监听端口: $current_from_db（默认 8790，只监听 127.0.0.1，不会自行暴露到公网）"
    local port_input=""
    read -r -p "   回车保持不变，或输入 1024-65535 之间的自定义端口: " port_input

    if [ -z "$port_input" ]; then
        success "Web 端口保持为 $current_from_db。"
        return
    fi

    local set_output
    set +e
    set_output=$(run_config_py set-port "$port_input")
    exit_code=$?
    set -euo pipefail

    if [ "$exit_code" -ne 0 ]; then
        error "[错误] 设置 Web 端口失败：${set_output:-未知错误}"
        exit 1
    fi
    reset_web_state_cache
    # 报 set_output（Python 规范化后的端口）而不是 port_input：万一 parse_web_port
    # 做了修正，照着原始输入喊一句"已设置为 xxx"就是在骗人。
    success "Web 端口已设置为 ${set_output}。"
}

validate_telegram_token() {
    if [ "${DEPLOY_MODE:-telegram}" = "web-only" ]; then
        info "[检查] 仅 Web 模式，跳过 Telegram Bot Token 校验。"
        return
    fi
    if ! venv_ready; then
        warn "   运行环境（venv）还没建好，跳过 Token 校验。"
        return
    fi
    info "[检查] 正在验证 Telegram Bot Token..."

    local status
    set +e
    status=$(XGENT_VALIDATE_TOKEN="$(env_get BOT_TOKEN)" run_bot_python "$VENV_PYTHON" - <<PY
$(ip_family_restrictor_python)
import asyncio
import os

from telegram import Bot
from telegram.error import InvalidToken, TelegramError

# Token 由 shell 侧的 env_get 读好后经环境变量传进来。以前这里自己
# 按 startswith("BOT_TOKEN=") 扒一遍 .env——多一份读法就多一种和
# env_get 不一致的可能（尤其是值末尾的 \r），判断"有没有配 Token"这件事
# 必须只有一个答案。
token = (os.environ.get("XGENT_VALIDATE_TOKEN") or "").strip()


async def main():
    if not token:
        print("missing")
        raise SystemExit(1)
    try:
        bot = Bot(token)
        await bot.get_me()
        print("valid")
    except InvalidToken:
        print("invalid")
        raise SystemExit(78)
    except TelegramError:
        print("network")
        raise SystemExit(2)

asyncio.run(main())
PY
)
    local exit_code=$?
    set -euo pipefail

    case "$exit_code" in
        0)
            success "Telegram Bot Token 校验通过"
            ;;
        78)
            error "[错误] 当前 .env 中的 BOT_TOKEN 无效或已失效。"
            echo "请先到 BotFather 重新生成 Token，更新 .env 后再启动。"
            exit 78
            ;;
        *)
            # 除了 78（Token 明确无效）之外，其它都只说明"校验本身没跑成"：
            # 网络不通、依赖半坏、临时故障都会落到这里。为此中断启动的代价比
            # 放它过去大得多——restart 路径也走这个函数，而真的 Token 有问题时
            # 应用自己会以 78 退出，PM2 的 --stop-exit-codes 和 systemd 的
            # RestartPreventExitStatus 都认这个码，不会陷入重启死循环。
            warn "   Token 在线校验未能完成（退出码 ${exit_code}，标记: ${status:-无}），将继续启动。"
            ;;
    esac
}

check_database() {
    info "[检查] 正在检查数据库..."

    if [ -f "xgent_memory.db" ]; then
        local db_size
        db_size="$(du -h xgent_memory.db | cut -f1)"
        echo "   数据库已存在，大小: $db_size"
    elif [ -f "bot_memory.db" ]; then
        echo "   检测到旧版数据库，将在 XGent for Telegram 下次启动前自动迁移。"
    else
        echo "   数据库会在首次运行时自动创建。"
    fi
}

ensure_npm() {
    if command_exists npm; then
        return
    fi

    warn "   未检测到 npm，尝试自动安装 nodejs 和 npm..."
    ensure_apt_packages nodejs npm

    if ! command_exists npm; then
        error "[错误] 自动安装后仍然无法使用 npm。"
        exit 1
    fi
}

prepend_path_dir() {
    local directory="${1:-}"

    [ -n "$directory" ] && [ -d "$directory" ] || return 0
    case ":$PATH:" in
        *":$directory:"*) ;;
        *)
            PATH="$directory:$PATH"
            export PATH
            ;;
    esac
}

refresh_npm_global_bin_path() {
    local npm_prefix="" npm_root="" npm_bin=""

    command_exists npm || return 0

    # npm 的全局可执行文件通常位于 "$(npm prefix -g)/bin"。部分托管
    # 容器会把 prefix 改到 /app 或用户目录，却没有同步更新 PATH。
    npm_prefix="$(npm prefix -g 2>/dev/null || true)"
    if [ -n "$npm_prefix" ]; then
        prepend_path_dir "$npm_prefix/bin"
    fi

    # 兼容只提供 npm root -g 或 prefix 输出异常的环境。
    npm_root="$(npm root -g 2>/dev/null || true)"
    if [ -n "$npm_root" ]; then
        npm_bin="$(dirname "$(dirname "$npm_root")")/bin"
        prepend_path_dir "$npm_bin"
    fi

    hash -r
}

install_xgent_command() {
    local launcher="$SCRIPT_DIR/bin/xgent"
    local target_dir="${XGENT_CMD_LINK_DIR:-/usr/local/bin}"
    local target="$target_dir/xgent"

    [ -f "$launcher" ] || return 0
    chmod +x "$launcher" 2>/dev/null || true

    # 只在 /usr/local/bin 本来就在 PATH 里时建链接——否则建了也敲不到，
    # 反而让用户以为装好了。与 persist_pm2_command 的判断保持一致。
    case ":$PATH:" in
        *":$target_dir:"*) ;;
        *)
            warn "   $target_dir 不在 PATH 中，跳过注册 xgent 命令。"
            warn "   可直接运行: $launcher"
            return 0
            ;;
    esac

    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$(readlink -f "$target" 2>/dev/null || true)" = "$(readlink -f "$launcher" 2>/dev/null || true)" ]; then
            success "xgent 命令已就绪: $target"
            return 0
        fi
        warn "   $target 已存在且指向别处，已保留原文件。"
        warn "   可直接运行: $launcher"
        return 0
    fi

    if run_privileged ln -s -- "$launcher" "$target"; then
        hash -r
        success "已注册 xgent 命令: $target（任意目录输入 xgent 打开终端界面）"
    else
        warn "   无法创建 $target；可直接运行: $launcher"
    fi
}

remove_xgent_command() {
    local target_dir="${XGENT_CMD_LINK_DIR:-/usr/local/bin}"
    local target="$target_dir/xgent"
    local launcher="$SCRIPT_DIR/bin/xgent"

    [ -L "$target" ] || return 0
    # 只删我们自己建的那个软链，指向别处的同名命令一律不碰。
    if [ "$(readlink -f "$target" 2>/dev/null || true)" = "$(readlink -f "$launcher" 2>/dev/null || true)" ]; then
        run_privileged rm -f -- "$target" && success "已移除 xgent 命令链接: $target"
    fi
}

persist_pm2_command() {
    local pm2_bin target_dir="${XGENT_PM2_LINK_DIR:-/usr/local/bin}" target

    pm2_bin="$(command -v pm2 2>/dev/null || true)"
    [ -n "$pm2_bin" ] || return 1

    # 标准目录中的 PM2 不需要额外处理。
    case "$pm2_bin" in
        /usr/bin/*|/usr/local/bin/*)
            return 0
            ;;
    esac

    # 仅当 /usr/local/bin 本来就在 PATH 中时创建兼容链接。这样安装脚本
    # 退出后，新终端和用户直接执行 pm2 也不会再次遇到 PATH 问题。
    case ":$PATH:" in
        *":$target_dir:"*) ;;
        *) return 0 ;;
    esac

    target="$target_dir/pm2"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$(readlink -f "$target" 2>/dev/null || true)" = "$(readlink -f "$pm2_bin" 2>/dev/null || true)" ]; then
            return 0
        fi
        warn "   $target 已存在且不是当前 PM2，已保留原文件。"
        return 0
    fi

    if run_privileged ln -s -- "$pm2_bin" "$target"; then
        hash -r
        success "已创建 PM2 命令链接: $target"
    else
        warn "   无法创建 $target；安装脚本内仍可正常使用 PM2。"
    fi
}

ensure_pm2() {
    if command_exists pm2; then
        persist_pm2_command
        success "PM2 已可用"
        return
    fi

    ensure_npm
    refresh_npm_global_bin_path

    # PM2 可能已经安装在非标准 npm prefix 下，只是尚未进入 PATH。
    if command_exists pm2; then
        persist_pm2_command
        success "PM2 已可用（已修复 npm 全局命令 PATH）"
        return
    fi

    info "[pm2] 正在全局安装 PM2..."
    run_privileged npm install -g pm2
    refresh_npm_global_bin_path

    if ! command_exists pm2; then
        local npm_prefix
        npm_prefix="$(npm prefix -g 2>/dev/null || true)"
        error "[错误] PM2 安装完成，但仍无法定位命令。"
        [ -n "$npm_prefix" ] && echo "   npm 全局前缀: $npm_prefix"
        echo "   请确认该目录下的 bin/pm2 是否存在。"
        exit 1
    fi

    persist_pm2_command
    success "PM2 安装完成"
}

setup_pm2_startup() {
    if ! command_exists systemctl; then
        warn "   未检测到 systemd，已跳过 PM2 开机自启配置。可手动执行: pm2 startup"
        return
    fi

    local service_user service_home path_for_pm2 pm2_bin node_bin
    service_user="${SUDO_USER:-$(id -un)}"
    service_home="$HOME"

    if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
        service_home="$(eval echo "~$SUDO_USER")"
    fi

    pm2_bin="$(command -v pm2 || true)"
    node_bin="$(command -v node || true)"
    path_for_pm2="$PATH"

    if [ -n "$pm2_bin" ]; then
        case ":$path_for_pm2:" in
            *":$(dirname "$pm2_bin"):"*) ;;
            *) path_for_pm2="$(dirname "$pm2_bin"):$path_for_pm2" ;;
        esac
    fi

    if [ -n "$node_bin" ]; then
        case ":$path_for_pm2:" in
            *":$(dirname "$node_bin"):"*) ;;
            *) path_for_pm2="$(dirname "$node_bin"):$path_for_pm2" ;;
        esac
    fi

    info "[pm2] 正在配置开机自启..."
    if run_privileged env PATH="$path_for_pm2" pm2 startup systemd -u "$service_user" --hp "$service_home" >/dev/null; then
        success "PM2 开机自启配置完成"
    else
        warn "   PM2 开机自启配置失败。服务仍已启动，可手动执行: pm2 startup && pm2 save"
    fi
}

start_foreground() {
    info "[运行] 正在前台启动 XGent for Telegram..."
    echo "   IP 出站模式: $(ip_mode_label)"
    XGENT_APP_ENTRY="$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \
        run_bot_python python -c "$(bot_python_code)"
}

start_background() {
    local mode pythonpath code socks5_url

    info "[运行] 正在后台启动 XGent for Telegram..."

    # 彻底停止旧进程
    if [ -f "xgent.pid" ]; then
        local old_pid
        old_pid="$(cat xgent.pid)"
        if [[ "$old_pid" =~ ^[0-9]+$ ]] && ps -p "$old_pid" >/dev/null 2>&1; then
            echo "   正在停止旧进程: $old_pid"
            kill "$old_pid" 2>/dev/null || true
            sleep 2
            if ps -p "$old_pid" >/dev/null 2>&1; then
                kill -9 "$old_pid" 2>/dev/null || true
            fi
        fi
        rm -f xgent.pid
    fi

    echo "   IP 出站模式: $(ip_mode_label)"
    mode="$(get_ip_mode)"
    pythonpath="$(pythonpath_with_project)"
    code="$(bot_python_code)"
    case "$mode" in
        default)
            env -u XGENT_IP_MODE -u XGENT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" \
                nohup "$VENV_PYTHON" -c "$code" > xgent_output.log 2>&1 &
            ;;
        sock5)
            socks5_url="$(get_socks5_proxy)"
            if [ -z "$socks5_url" ]; then
                error "[错误] SOCKS5 模式已启用但未配置代理地址。"
                exit 1
            fi
            env -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_IP_MODE="$mode" XGENT_SOCKS5_PROXY="$socks5_url" \
                XGENT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" \
                nohup "$VENV_PYTHON" -c "$code" > xgent_output.log 2>&1 &
            ;;
        *)
            env -u XGENT_SOCKS5_PROXY -u TELEGRAM_AI_BOT_IP_MODE -u TELEGRAM_AI_BOT_SOCKS5_PROXY \
                -u TELEGRAM_AI_BOT_APP_ENTRY \
                XGENT_IP_MODE="$mode" \
                XGENT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" \
                nohup "$VENV_PYTHON" -c "$code" > xgent_output.log 2>&1 &
            ;;
    esac
    local pid=$!
    echo "$pid" > xgent.pid
    sleep 2

    if ps -p "$pid" >/dev/null 2>&1; then
        echo "   后台启动成功，PID: $pid"
        echo "   日志文件: xgent_output.log"
        echo "   停止命令: kill \$(cat xgent.pid)"
        echo "   查看日志: tail -f xgent_output.log"
        echo "   彻底重启: bash install.sh restart"
    else
        error "[错误] 后台启动失败，请查看 xgent_output.log。"
        tail -20 xgent_output.log || true
        exit 1
    fi
}

migrate_legacy_pm2_process() {
    if command_exists pm2 \
        && ! pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1 \
        && pm2 describe "$LEGACY_PM2_APP_NAME" >/dev/null 2>&1; then
        info "[迁移] 正在把 PM2 进程 ${LEGACY_PM2_APP_NAME} 迁移为 ${PM2_APP_NAME}..."
        pm2 delete "$LEGACY_PM2_APP_NAME" >/dev/null 2>&1 || true
        success "旧 PM2 进程记录已移除，将使用新名称重建"
    fi
}

start_with_pm2() {
    local code mode env_mode socks5_url

    ensure_pm2
    migrate_legacy_pm2_process

    info "[运行] 正在使用 PM2 启动 XGent for Telegram..."
    echo "   IP 出站模式: $(ip_mode_label)"

    # 进程已存在时原地重启（pm_id 不变），并用 --update-env 刷新环境变量。
    # 变量设为空串而不是 unset：Bot 侧把空串视为默认模式，
    # 这样 --update-env 才能覆盖掉旧进程里残留的模式设置。
    if pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1; then
        mode="$(get_ip_mode)"
        env_mode="$mode"
        [ "$mode" = "default" ] && env_mode=""
        socks5_url=""
        [ "$mode" = "sock5" ] && socks5_url="$(get_socks5_proxy)"
        if env XGENT_IP_MODE="$env_mode" XGENT_SOCKS5_PROXY="$socks5_url" \
            TELEGRAM_AI_BOT_IP_MODE="" TELEGRAM_AI_BOT_SOCKS5_PROXY="" \
            TELEGRAM_AI_BOT_APP_ENTRY="" XGENT_APP_ENTRY="$APP_ENTRY" \
            PYTHONPATH="$(pythonpath_with_project)" \
            pm2 restart "$PM2_APP_NAME" --update-env; then
            echo "   PM2 原地重启成功（进程 ID 保持不变）。"
            echo "   查看日志: pm2 logs $PM2_APP_NAME"
            if pm2 save >/dev/null; then
                echo "   PM2 进程列表已保存。"
            fi
            return
        fi
        # restart 失败通常是 PM2 状态不一致（dump 记录了进程但实际不存在）。
        # 清理残留记录后 fallback 到 start 分支重新创建。
        warn "   PM2 restart 失败（进程状态不一致），正在清理残留并重新启动..."
        pm2 delete "$PM2_APP_NAME" 2>/dev/null || true
        sleep 1
    fi

    code="$(bot_python_code)"
    XGENT_APP_ENTRY="$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \
        run_bot_python pm2 start "$VENV_PYTHON" \
        --name "$PM2_APP_NAME" \
        --cwd "$SCRIPT_DIR" \
        --interpreter none \
        --stop-exit-codes 78 \
        --max-memory-restart 1G \
        --exp-backoff-restart-delay=100 \
        -- -c "$code"

    echo "   PM2 启动成功。"
    echo "   查看日志: pm2 logs $PM2_APP_NAME"
    echo "   停止命令: pm2 stop $PM2_APP_NAME"
    echo "   重启命令: pm2 restart $PM2_APP_NAME"
    echo "   彻底重启: bash install.sh restart"
    setup_pm2_startup

    if pm2 save >/dev/null; then
        echo "   PM2 进程列表已保存，服务器重启后会自动恢复。"
    else
        warn "   PM2 进程列表保存失败。请手动执行: pm2 save"
    fi
}

restart_pm2_detached() {
    local helper log_file mode socks5_url

    ensure_state_dir
    log_file="$STATE_DIR/pm2-restart.log"
    helper="$(mktemp "$STATE_DIR/pm2-restart.XXXXXX.sh")"
    mode="$(get_ip_mode)"
    if [ "$mode" = "sock5" ]; then
        socks5_url="$(get_socks5_proxy)"
    else
        socks5_url=""
    fi

    # helper 用 <<'EOF' 写，正文里一个变量都不插值。原来用的是不加引号的 <<EOF，
    # 把 $socks5_url 直接拼进脚本正文——代理密码里一个反引号或 $( 就成了在 helper
    # 里执行的代码，而那个值只过了 "^socks5://" 前缀检查。要带进去的东西一律走
    # 环境变量，顺带也不再怕 SCRIPT_DIR 里有引号或空格。
    cat > "$helper" <<'EOF'
#!/usr/bin/env bash
set -u
# 用完自删。否则每次重启都在磁盘上多留一个文件，而 sock5 模式下它带着代理凭据。
trap 'rm -f -- "$0"' EXIT

echo "===== detached restart started at $(date '+%F %T') ====="
cd "$XGENT_RESTART_DIR" || exit 1

# 设置 IP 模式环境变量（ipv4 / ipv6 / sock5 互斥）
unset XGENT_SOCKS5_PROXY TELEGRAM_AI_BOT_IP_MODE TELEGRAM_AI_BOT_SOCKS5_PROXY || true
unset XGENT_APP_ENTRY TELEGRAM_AI_BOT_APP_ENTRY || true
case "$XGENT_RESTART_MODE" in
    default) unset XGENT_IP_MODE || true ;;
    sock5)
        export XGENT_IP_MODE="$XGENT_RESTART_MODE"
        export XGENT_SOCKS5_PROXY="$XGENT_RESTART_SOCKS5"
        ;;
    *) export XGENT_IP_MODE="$XGENT_RESTART_MODE" ;;
esac

# 不做 pm2 delete：由 start_with_pm2 对已存在的进程做原地 restart，
# 复用原 pm_id，避免进程 ID 不断增加。
bash "$XGENT_RESTART_DIR/install.sh" pm2-start-internal
status=$?
pm2 save || true
echo "===== detached restart finished with status $status at $(date '+%F %T') ====="
exit $status
EOF
    chmod +x "$helper"

    # bash -c '... "$0"' <helper> 里 $0 就是 helper 路径，不必把它拼进命令字符串。
    nohup env XGENT_RESTART_DIR="$SCRIPT_DIR" \
        XGENT_RESTART_MODE="$mode" \
        XGENT_RESTART_SOCKS5="$socks5_url" \
        bash -c 'sleep 2; exec "$0"' "$helper" >> "$log_file" 2>&1 &
    echo "   已启动脱离当前会话的 PM2 彻底重启任务。"
    echo "   日志文件: $log_file"
    echo "   如果当前 Bot 短暂断开，请等待 5-10 秒后重新 /start。"
}

# 彻底重建 PM2 进程：删除后重新创建，会重新加载 PM2 启动参数
# （注意：pm_id 会 +1，日常重启请用 restart / 菜单选项 5）
rebuild_pm2_app() {
    if command_exists pm2 && { pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1 || pm2 describe "$LEGACY_PM2_APP_NAME" >/dev/null 2>&1; }; then
        pm2 delete "$PM2_APP_NAME" 2>/dev/null || true
        pm2 delete "$LEGACY_PM2_APP_NAME" 2>/dev/null || true
        sleep 1
        start_with_pm2
    else
        restart_app
    fi
}

restart_app() {
    info "[重启] 正在彻底重启 XGent for Telegram..."

    # 分发依据必须和 start_service / stop_service 一致——都只看 runtime mode。
    # 以前这里第二步是"**存在** PM2 进程就走 PM2"：把保活方式改成 nohup 之后，
    # 只要那条 PM2 记录还在，restart 就又从 PM2 起来，和用户刚选的方式正好相反。
    case "$(get_runtime_mode)" in
        systemd)
            echo "   保活方式 systemd：将重写 unit 并重启（IP 模式等改动一并生效）。"
            start_with_systemd
            ;;
        pm2)
            # 统一走 detached：install.sh restart 有可能是被 PM2 自己管着的进程
            # 调起来的（Bot 里的重启入口就是），在自己的进程里 pm2 restart 会把
            # 调用方一起杀掉，重启就半途而废。start_with_pm2 内部已经区分了
            # "进程已存在→原地 restart" 和 "不存在→新建"。
            echo "   保活方式 PM2：脱离当前会话重启（进程已存在则原地重启，ID 不变）。"
            restart_pm2_detached
            ;;
        nohup)
            if [ -f "xgent.pid" ]; then
                echo "   将彻底停止后台进程再重新启动，确保加载最新配置。"
                stop_background_process
                sleep 1
            fi
            start_background
            ;;
        *)
            # foreground 的"重启"就是在当前终端重新跑一次；mode 还是 none 时由
            # start_service 去问一次保活方式（只有交互式菜单会走到这条）。
            if [ -f "xgent.pid" ]; then
                stop_background_process
                sleep 1
            fi
            echo "   将按当前保活方式（$(runtime_mode_label)）启动一次。"
            start_service
            ;;
    esac
}

# 收集本地 API 所需 .env 变量；已有值则显示并允许回车保留
prompt_local_api_env() {
    local current_url current_id current_hash
    local input_url input_id input_hash

    current_url="$(env_get TELEGRAM_API_URL)"
    current_id="$(env_get TELEGRAM_API_ID)"
    current_hash="$(env_get TELEGRAM_API_HASH)"

    echo ""
    warn "本地 Telegram Bot API server 需要 api_id 和 api_hash。"
    echo "   如尚未申请，请到 https://my.telegram.org 登录后在 API development 工具中创建获取。"
    echo ""

    if [ -n "$current_url" ]; then
        read -r -p "请输入 TELEGRAM_API_URL [当前: $current_url，回车保留]: " input_url
        input_url="${input_url:-$current_url}"
    else
        input_url=""
        while [ -z "$input_url" ]; do
            read -r -p "请输入 TELEGRAM_API_URL (例如 http://localhost:${LOCAL_API_PORT}): " input_url
        done
    fi

    if [ -n "$current_id" ]; then
        read -r -p "请输入 TELEGRAM_API_ID [当前: $current_id，回车保留]: " input_id
        input_id="${input_id:-$current_id}"
    else
        input_id=""
        while [ -z "$input_id" ]; do
            read -r -p "请输入 TELEGRAM_API_ID: " input_id
        done
    fi
    if ! [[ "$input_id" =~ ^[0-9]+$ ]]; then
        error "[错误] TELEGRAM_API_ID 必须是纯数字。"
        return 1
    fi

    if [ -n "$current_hash" ]; then
        read -r -p "请输入 TELEGRAM_API_HASH [当前: $current_hash，回车保留]: " input_hash
        input_hash="${input_hash:-$current_hash}"
    else
        input_hash=""
        while [ -z "$input_hash" ]; do
            read -r -p "请输入 TELEGRAM_API_HASH: " input_hash
        done
    fi

    env_set "TELEGRAM_API_URL" "$input_url"
    env_set "TELEGRAM_API_ID" "$input_id"
    env_set "TELEGRAM_API_HASH" "$input_hash"
    success ".env 已写入 TELEGRAM_API_URL / TELEGRAM_API_ID / TELEGRAM_API_HASH"
}

start_local_api_container() {
    local api_id api_hash port

    if ! ensure_docker; then
        return 1
    fi

    if ! prompt_local_api_env; then
        return 1
    fi

    api_id="$(env_get TELEGRAM_API_ID)"
    api_hash="$(env_get TELEGRAM_API_HASH)"
    port="$LOCAL_API_PORT"

    info "[本地 API] 正在准备容器..."
    mkdir -p "$LOCAL_API_DATA_DIR"

    # 先拉镜像再删旧容器：反过来的话拉取失败就会留下「旧容器已删、新容器没起」
    # 的空窗，本地 Bot API 直接停摆，而此时 token 已经还给官方 API 了。
    info "[本地 API] 正在拉取镜像 $LOCAL_API_IMAGE（首次较慢）..."
    if ! docker pull "$LOCAL_API_IMAGE"; then
        error "[错误] 拉取镜像失败，请检查网络或手动执行: docker pull $LOCAL_API_IMAGE"
        error "       旧容器未改动，本地 API 仍按原状运行。"
        return 1
    fi

    # 镜像就绪后再清掉旧容器
    if docker inspect "$LOCAL_API_CONTAINER" >/dev/null 2>&1; then
        info "[本地 API] 正在移除旧容器: $LOCAL_API_CONTAINER"
        docker rm -f "$LOCAL_API_CONTAINER" >/dev/null
    fi

    info "[本地 API] 正在启动容器..."
    # 按白名单 IP 生成端口绑定参数（默认仅 127.0.0.1，可随时加白名单）。
    # 白名单文件是纯文本、可能被手工编辑过，所以这里再过一遍格式：不合法的行
    # 直接跳掉，而不是拼进 docker run 让它报一句难懂的参数错误。
    local -a port_args=()
    local ip
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        if ! [[ "$ip" =~ ^[A-Za-z0-9._:-]+$ ]]; then
            warn "   白名单里这一行不像 IP，已跳过: $ip"
            continue
        fi
        port_args+=("-p" "${ip}:${LOCAL_API_PORT}:8081")
    done < <(get_allowed_ips)
    if [ "${#port_args[@]}" -eq 0 ]; then
        port_args+=("-p" "127.0.0.1:${LOCAL_API_PORT}:8081")
    fi

    local allowed_summary
    allowed_summary="$(get_allowed_ips | tr '\n' ' ' | sed 's/ *$//')"
    info "[本地 API] 端口 ${LOCAL_API_PORT} 允许访问的 IP: ${allowed_summary:-127.0.0.1}"

    if ! docker run -d \
        --name "$LOCAL_API_CONTAINER" \
        --restart unless-stopped \
        -v "$LOCAL_API_DATA_DIR:/var/lib/telegram-bot-api" \
        -e TELEGRAM_API_ID="$api_id" \
        -e TELEGRAM_API_HASH="$api_hash" \
        -e TELEGRAM_LOCAL=true \
        "${port_args[@]}" \
        "$LOCAL_API_IMAGE"; then
        error "[错误] 启动容器失败。"
        return 1
    fi

    sleep 3
    local status
    status="$(local_api_container_status)"
    if [ "$status" = "running" ]; then
        success "本地 API 容器已启动，端口 ${port}。"
        echo "   容器名: $LOCAL_API_CONTAINER"
        echo "   数据目录: $LOCAL_API_DATA_DIR"
        echo "   查看日志: docker logs -f $LOCAL_API_CONTAINER"

        # token 之前若登录在官方 server，需先 logOut，本地 server 才能接管
        echo ""
        warn "正在将 bot token 从官方 api.telegram.org 登出，交由本地 server 接管..."
        telegram_log_out "https://api.telegram.org" || true

        echo ""
        success "本地 API 配置完成，正在自动重启 bot 以通过本地 API 通信..."
        restart_bot_after_api_switch
    else
        error "[错误] 容器启动后状态异常: $status"
        echo "   请查看日志: docker logs $LOCAL_API_CONTAINER"
        return 1
    fi
}

# 在本地 API 启用/关闭后，自动重启 bot 以加载新配置。
# 采用 detached 子进程方式：不阻塞当前交互菜单，避免 nohup/PM2 路径对 venv 状态的依赖。
restart_bot_after_api_switch() {
    local helper log_file

    ensure_state_dir
    log_file="$STATE_DIR/api-switch-restart.log"
    helper="$(mktemp "$STATE_DIR/api-switch-restart.XXXXXX.sh")"

    # 同 restart_pm2_detached：正文不插值，路径走环境变量，用完自删。
    cat > "$helper" <<'EOF'
#!/usr/bin/env bash
set -u
trap 'rm -f -- "$0"' EXIT

echo "===== api-switch restart at $(date '+%F %T') ====="
cd "$XGENT_RESTART_DIR" || exit 1
bash "$XGENT_RESTART_DIR/install.sh" restart
echo "===== finished at $(date '+%F %T') ====="
EOF
    chmod +x "$helper"

    nohup env XGENT_RESTART_DIR="$SCRIPT_DIR" \
        bash -c 'sleep 2; exec "$0"' "$helper" >> "$log_file" 2>&1 &
    echo "   已在后台启动 bot 重启任务（独立进程，不阻塞当前菜单）。"
    echo "   日志文件: $log_file"
    echo "   如果当前 Bot 短暂断开，请等待 5-10 秒后重新 /start。"
}

stop_local_api_container() {
    # restart_after=no-restart 时不拉重启任务。卸载流程要用这个：那时正在把服务
    # 一件件拆掉，再顺手起一个 detached 重启就是在跟自己打架。
    local restart_after="${1:-restart}"
    local local_base changed=0

    if ! command_exists docker; then
        warn "   未检测到 docker，跳过容器操作。"
    else
        if docker inspect "$LOCAL_API_CONTAINER" >/dev/null 2>&1; then
            # 容器还在运行时，先把 token 从本地 server 登出，让它回到官方
            local_base="$(env_get TELEGRAM_API_URL)"
            local_base="${local_base%%/}"
            if [ -n "$local_base" ]; then
                echo ""
                warn "正在将 bot token 从本地 server (${local_base}) 登出，交还官方..."
                telegram_log_out "$local_base" || true
            fi

            info "[本地 API] 正在关闭并移除容器: $LOCAL_API_CONTAINER"
            docker stop "$LOCAL_API_CONTAINER" >/dev/null 2>&1 || true
            docker rm "$LOCAL_API_CONTAINER" >/dev/null 2>&1 || true
            success "本地 API 容器已关闭。"
            changed=1
        else
            echo "   未发现本地 API 容器: $LOCAL_API_CONTAINER"
        fi
    fi

    # 清除 TELEGRAM_API_URL，让 bot 回连官方 api.telegram.org；
    # 保留 TELEGRAM_API_ID / TELEGRAM_API_HASH，下次启用可直接复用。
    if [ -n "$(env_get TELEGRAM_API_URL)" ]; then
        env_unset "TELEGRAM_API_URL"
        success ".env 中 TELEGRAM_API_URL 已清除，bot 重启后将回连官方 api.telegram.org。"
        echo "   TELEGRAM_API_ID / TELEGRAM_API_HASH 已保留，下次启用可直接复用。"
        changed=1
    else
        echo "   .env 中本就没有 TELEGRAM_API_URL，无需清理。"
    fi

    # 只有发生过实际变更（关了容器或清了配置）才重启 bot
    if [ "$changed" -eq 1 ] && [ "$restart_after" = "restart" ]; then
        echo ""
        success "本地 API 已关闭，正在自动重启 bot 以回连官方 api.telegram.org..."
        restart_bot_after_api_switch
    fi
}

# 管理本地 API 端口白名单 IP（查看/添加/删除）。
# 默认 127.0.0.1；可随时增删，改完需重启容器（子选项 1）才生效。
manage_allowed_ips() {
    local choice ip current_ips

    while true; do
        echo ""
        echo -e "${CYAN}--- 本地 API 白名单 IP ---${NC}"
        echo "当前允许访问端口 ${LOCAL_API_PORT} 的 IP:"
        current_ips="$(get_allowed_ips)"
        if [ -z "$current_ips" ]; then
            echo "  (空，将默认 127.0.0.1)"
        else
            while IFS= read -r ip; do
                [ -n "$ip" ] && echo "  - $ip"
            done <<< "$current_ips"
        fi
        echo ""
        echo "请选择操作:"
        echo "  1) 添加 IP 到白名单"
        echo "  2) 从白名单删除 IP"
        echo "  3) 返回本地 API 菜单"
        echo ""
        read -r -p "请输入选项 [1/2/3，默认 3]: " choice

        case "$choice" in
            1)
                read -r -p "请输入要添加的 IPv4 地址（如 1.2.3.4 或 0.0.0.0 表示全部）: " ip
                if [ -z "$ip" ]; then
                    warn "未输入，已取消。"
                    continue
                fi
                if [ "$ip" = "0.0.0.0" ]; then
                    warn "⚠️  0.0.0.0 表示对所有 IP 开放（公网可访问），请确认你知道风险。"
                    read -r -p "确认添加 0.0.0.0？输入 y 继续: " confirm
                    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
                        warn "已取消。"
                        continue
                    fi
                elif ! is_valid_ipv4 "$ip"; then
                    error "[错误] 不是合法的 IPv4 地址: $ip"
                    continue
                fi
                add_allowed_ip "$ip"
                success "已添加: $ip"
                warn "   改动需重启容器后生效：返回本地 API 菜单选择 1) 启动/重新安装启动。"
                ;;
            2)
                read -r -p "请输入要删除的 IP: " ip
                if [ -z "$ip" ]; then
                    warn "未输入，已取消。"
                    continue
                fi
                if ! grep -Fxq -- "$ip" <(get_allowed_ips); then
                    warn "白名单中没有: $ip"
                    continue
                fi
                remove_allowed_ip "$ip"
                success "已删除: $ip"
                warn "   改动需重启容器后生效：返回本地 API 菜单选择 1) 启动/重新安装启动。"
                ;;
            ""|3)
                warn "已返回本地 API 菜单。"
                break
                ;;
            *)
                error "[错误] 无效选项。"
                ;;
        esac
    done
}

manage_local_api() {
    local choice
    local container_status bot_enabled

    container_status="$(local_api_container_status)"
    bot_enabled="$(local_api_enabled_for_bot)"

    echo ""
    echo -e "${CYAN}--- 本地 API 容器 (Docker) ---${NC}"
    echo "容器状态: $container_status"
    echo "bot 启用本地 API: $bot_enabled"
    echo "允许访问端口 ${LOCAL_API_PORT} 的 IP: $(get_allowed_ips | tr '\n' ' ' | sed 's/ *$//')"
    echo ""
    echo "请选择操作:"
    echo "  1) 启动/重新安装启动本地 API 容器 (Docker)"
    echo "  2) 关闭本地 API 容器"
    echo "  3) 管理白名单 IP (默认仅 127.0.0.1)"
    echo "  4) 返回主菜单"
    echo ""
    read -r -p "请输入选项 [1/2/3/4，默认 4]: " choice

    case "$choice" in
        1)
            start_local_api_container
            ;;
        2)
            stop_local_api_container
            ;;
        3)
            manage_allowed_ips
            ;;
        ""|4)
            warn "已返回主菜单。"
            ;;
        *)
            error "[错误] 无效选项。"
            ;;
    esac
}

# ==========================================================================
# 保活方式（runtime mode）
# ==========================================================================
# 服务以什么方式常驻，和"装了哪些组件"是两个正交的问题：同一套 web+bot
# 可以跑在 systemd 下，也可以跑在 PM2 下。所以它单独存一份状态，只在第一次
# 安装时问一次，之后从主菜单改。

get_runtime_mode() {
    local mode=""
    if [ -f "$RUNTIME_MODE_FILE" ]; then
        mode="$(tr -d '[:space:]' < "$RUNTIME_MODE_FILE" 2>/dev/null || true)"
    fi
    case "$mode" in
        systemd|pm2|nohup|foreground) printf '%s\n' "$mode" ;;
        *) printf 'none\n' ;;
    esac
}

runtime_mode_label() {
    case "$(get_runtime_mode)" in
        systemd)    printf '系统级保活 (systemd)' ;;
        pm2)        printf 'PM2 保活' ;;
        nohup)      printf '后台运行 (nohup)' ;;
        foreground) printf '前台运行' ;;
        *)          printf '尚未选择' ;;
    esac
}

set_runtime_mode() {
    ensure_state_dir
    printf '%s\n' "$1" > "$RUNTIME_MODE_FILE"
}

choose_runtime_mode() {
    local choice="" previous_mode
    previous_mode="$(get_runtime_mode)"

    echo ""
    echo "请选择保活方式（决定服务以什么方式常驻，之后可以随时改）:"
    if systemd_available; then
        echo "  1) 系统级保活 (systemd)   开机自启、崩溃自动拉起；需要 root/sudo"
    else
        echo "  1) 系统级保活 (systemd)   当前系统不可用（没检测到 systemd）"
    fi
    echo "  2) PM2 保活               Node 进程守护，开机自启，pm2 logs 看日志"
    echo "  3) 后台运行 (nohup)       最轻量，但不会开机自启"
    echo "  4) 前台运行               调试用，关掉终端服务就停"
    echo ""
    read -r -p "请输入选项 [1-4，默认 2]: " choice

    case "$choice" in
        1)
            if ! systemd_available; then
                error "   当前系统没有可用的 systemd，请改选 2/3/4。"
                choose_runtime_mode
                return
            fi
            set_runtime_mode systemd
            ;;
        3) set_runtime_mode nohup ;;
        4) set_runtime_mode foreground ;;
        ""|2) set_runtime_mode pm2 ;;
        *)
            error "   无效选项，请重新选择。"
            choose_runtime_mode
            return
            ;;
    esac

    # 换了保活方式，就得先把原来那套停掉。以前这里改完记录就直接返回，调用方紧接着
    # start_service——于是 PM2 里的旧进程还 online，systemd 又拉起一个新的：同一个
    # SQLite、同一个 Web 端口、同一个 token 长轮询（Telegram 直接回 409 Conflict），
    # 而两边看起来都"启动成功了"，最难查的那种。
    if [ "$previous_mode" != "none" ] && [ "$previous_mode" != "$(get_runtime_mode)" ]; then
        stop_service_in_mode "$previous_mode"
    fi
    success "保活方式: $(runtime_mode_label)"
}

# ==========================================================================
# 系统级保活：systemd
# ==========================================================================

systemd_available() {
    command_exists systemctl && [ -d /run/systemd/system ]
}

systemd_unit_installed() {
    [ -f "$SYSTEMD_UNIT_PATH" ]
}

write_systemd_unit() {
    local service_user unit_tmp
    service_user="${SUDO_USER:-$(id -un)}"
    unit_tmp="$(mktemp)"

    # unit 里刻意不写具体启动参数，只写 "bash install.sh service-exec"：
    # IP 出站模式、PYTHONPATH、入口文件这些都由 run_bot_python 统一决定，
    # 写进 unit 就等于抄第二份——用户改完 IP 模式还得记得重写 unit 才生效。
    # 顺带躲开在 unit 文件里给一大段内联 Python 加引号这件事。
    cat > "$unit_tmp" <<EOF
[Unit]
Description=XGent for Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$service_user
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/env bash $SCRIPT_DIR/install.sh service-exec
Restart=always
RestartSec=3
# 78 是应用主动要求"别再拉起来了"的退出码，与 PM2 的 --stop-exit-codes 一致。
RestartPreventExitStatus=78
KillSignal=SIGINT
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    run_privileged install -m 0644 "$unit_tmp" "$SYSTEMD_UNIT_PATH"
    rm -f "$unit_tmp"
}

start_with_systemd() {
    if ! systemd_available; then
        error "[错误] 当前系统没有可用的 systemd，请在主菜单里改用 PM2 / nohup。"
        exit 1
    fi

    info "[运行] 正在用 systemd 启动 XGent for Telegram..."
    echo "   IP 出站模式: $(ip_mode_label)"

    write_systemd_unit
    run_privileged systemctl daemon-reload
    if ! run_privileged systemctl enable "$SYSTEMD_SERVICE_NAME" >/dev/null 2>&1; then
        warn "   开机自启配置失败，可手动执行: systemctl enable $SYSTEMD_SERVICE_NAME"
    fi
    run_privileged systemctl restart "$SYSTEMD_SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SYSTEMD_SERVICE_NAME"; then
        success "systemd 服务已启动: $SYSTEMD_SERVICE_NAME"
        echo "   查看日志: journalctl -u $SYSTEMD_SERVICE_NAME -f"
        echo "   停止命令: systemctl stop $SYSTEMD_SERVICE_NAME"
        echo "   彻底重启: bash install.sh restart"
    else
        error "[错误] systemd 服务启动失败，下面是最近的状态输出:"
        run_privileged systemctl status "$SYSTEMD_SERVICE_NAME" --no-pager -l 2>&1 | head -30 || true
        exit 1
    fi
}

stop_systemd_service() {
    systemd_unit_installed || return 0
    run_privileged systemctl stop "$SYSTEMD_SERVICE_NAME" >/dev/null 2>&1 || true
}

remove_systemd_service() {
    if ! systemd_unit_installed; then
        echo "   未发现 systemd 服务: $SYSTEMD_SERVICE_NAME"
        return 0
    fi
    info "[卸载] 正在移除 systemd 服务: $SYSTEMD_SERVICE_NAME"
    run_privileged systemctl disable --now "$SYSTEMD_SERVICE_NAME" >/dev/null 2>&1 || true
    run_privileged rm -f -- "$SYSTEMD_UNIT_PATH"
    run_privileged systemctl daemon-reload >/dev/null 2>&1 || true
    success "systemd 服务已移除"
}

# systemd / 前台守护的实际进入点：不做任何交互，直接把服务跑起来。
exec_app_process() {
    if [ ! -x "$VENV_PYTHON" ]; then
        error "[错误] 找不到虚拟环境解释器: $VENV_PYTHON"
        error "       请先运行 ./install.sh 完成安装。"
        exit 1
    fi
    XGENT_APP_ENTRY="$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \
        run_bot_python "$VENV_PYTHON" -c "$(bot_python_code)"
}

# ==========================================================================
# 服务启停：按保活方式分发
# ==========================================================================

start_service() {
    case "$(get_runtime_mode)" in
        systemd)    start_with_systemd ;;
        nohup)      start_background ;;
        foreground) start_foreground ;;
        pm2)        start_with_pm2 ;;
        *)
            choose_runtime_mode
            start_service
            ;;
    esac
}

stop_service() {
    case "$(get_runtime_mode)" in
        systemd)
            info "[停止] 正在停止 systemd 服务..."
            stop_systemd_service
            success "已停止: $SYSTEMD_SERVICE_NAME"
            ;;
        pm2)
            if command_exists pm2 && pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1; then
                info "[停止] 正在停止 PM2 进程..."
                pm2 stop "$PM2_APP_NAME" >/dev/null 2>&1 || true
                success "已停止: $PM2_APP_NAME"
            else
                warn "   未发现正在运行的 PM2 进程。"
            fi
            ;;
        *)
            info "[停止] 正在停止后台进程..."
            stop_background_process
            ;;
    esac
}

# 按**指定**的保活方式停一次服务。
#
# stop_service 停的是"当前记录的那一套"，而切换保活方式时要停的恰好是切换前那套，
# 记录已经改掉了，所以单独抽一个按参数停的版本。没有对应进程就静静返回。
stop_service_in_mode() {
    case "$1" in
        systemd)
            systemd_unit_installed || return 0
            info "[切换] 正在停止原来的 systemd 服务..."
            stop_systemd_service
            ;;
        pm2)
            command_exists pm2 || return 0
            pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1 || return 0
            info "[切换] 正在移除原来的 PM2 进程..."
            # 用 delete 而不是 stop：用户已经不用 PM2 了，留一条 stopped 记录只会
            # 让 service_running / pm2 save 继续把它当自己人。
            pm2 delete "$PM2_APP_NAME" >/dev/null 2>&1 || true
            pm2 save >/dev/null 2>&1 || true
            ;;
        nohup|foreground)
            [ -f "xgent.pid" ] || return 0
            info "[切换] 正在停止原来的后台进程..."
            stop_background_process
            ;;
    esac
}

service_running() {
    local pid
    case "$(get_runtime_mode)" in
        systemd)
            command_exists systemctl || return 1
            systemd_unit_installed || return 1
            systemctl is-active --quiet "$SYSTEMD_SERVICE_NAME" 2>/dev/null
            ;;
        pm2)
            command_exists pm2 && pm2 describe "$PM2_APP_NAME" 2>/dev/null | grep -q "online"
            ;;
        *)
            [ -f "xgent.pid" ] || return 1
            pid="$(cat xgent.pid 2>/dev/null || true)"
            case "$pid" in
                ""|*[!0-9]*) return 1 ;;
            esac
            ps -p "$pid" >/dev/null 2>&1
            ;;
    esac
}

# ==========================================================================
# 组件清单：cli / web / bot
# ==========================================================================
# 三个组件共用一套代码和一份数据库，但"装没装"的判据各不相同：
#   cli 看运行环境和 xgent 命令链接（文件系统）；
#   web 看数据库里有没有访问密码（没密码 Web 服务根本不会启动）；
#   bot 看 .env 里的 BOT_TOKEN 和 AUTHORIZED_USER_ID。
# 每个探测函数统一输出 "状态|说明"，状态是 installed / missing / error 三选一。

is_number() {
    case "${1:-}" in
        ""|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

venv_ready() {
    [ -x "$VENV_PYTHON" ] && [ -f "$VENV_ACTIVATE" ]
}

# 端口上有没有人在听。0 = 在听，或者查不出来（不敢下结论）；1 = 确定没人在听。
#
# 判据顺序是刻意的：先直连一次端口，这是唯一不依赖 /proc/net 的判据，也正好就是
# 我们真想知道的那件事。Android 不给普通应用（含 Termux）读 /proc/net/tcp，ss 和
# netstat 在那儿要么 Permission denied、要么只吐一行表头；原来只看 grep 有没有命中，
# 于是"查不了"被当成了"没监听"，装好的机器固定报一句黄色的"端口没有监听"，而服务
# 其实好得很。连接是内核层面的事，那条限制管不着它。
port_is_listening() {
    local port="$1" probe_rc=0 out=""

    if venv_ready; then
        run_config_py probe-port "$port" >/dev/null 2>&1 || probe_rc=$?
        case "$probe_rc" in
            0) return 0 ;;   # 连上了
            1) return 1 ;;   # ECONNREFUSED，确定没人在听
        esac                 # 2 或其它：探测本身没跑成，往下退
    fi

    # 退回 ss / netstat，但只在它们**真的答上话**时才采信否定结论：退出码为 0 且
    # 有输出，才算"查到了、里面没这个端口"。原来的写法把 ss 的退出码丢进管道，
    # "被拒"和"没命中"塌成了同一个 1，那正是这个 bug 的根。
    if command_exists ss; then
        out="$(ss -ltn 2>/dev/null)" || out=""
        if [ -n "$out" ]; then
            if printf '%s\n' "$out" | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
                return 0
            fi
            return 1
        fi
    fi
    if command_exists netstat; then
        out="$(netstat -ltn 2>/dev/null)" || out=""
        if [ -n "$out" ]; then
            if printf '%s\n' "$out" | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
                return 0
            fi
            return 1
        fi
    fi

    # 谁都没答上话就别下"异常"的结论——查不到不等于没监听。
    return 0
}

WEB_STATE_LOADED=0
WEB_ENABLED="no"
WEB_HAS_PASSWORD="no"
WEB_PORT=""

reset_web_state_cache() {
    WEB_STATE_LOADED=0
    # 只清 LOADED 标志不够：load_web_state 失败时会提前 return，把上一次的值
    # 留在原地，读到的就是过期状态。四个一起清，谁都别想读到脏值。
    WEB_ENABLED="no"
    WEB_HAS_PASSWORD="no"
    WEB_PORT=""
}

# Web 配置探测。跑一次要加载全部 section（约一两秒），所以结果缓存起来，
# 只有改过密码/端口之后才 reset。
load_web_state() {
    local output exit_code

    [ "$WEB_STATE_LOADED" -eq 1 ] && return 0
    venv_ready || return 1

    set +e
    output=$(run_config_py get-web-state 2>/dev/null)
    exit_code=$?
    set -euo pipefail

    [ "$exit_code" -eq 0 ] || return 1

    WEB_HAS_PASSWORD="$(printf '%s\n' "$output" | grep '^password=' | tail -n 1 | cut -d= -f2)"
    WEB_PORT="$(printf '%s\n' "$output" | grep '^port=' | tail -n 1 | cut -d= -f2)"
    WEB_ENABLED="$(printf '%s\n' "$output" | grep '^enabled=' | tail -n 1 | cut -d= -f2)"
    [ -n "$WEB_PORT" ] || return 1
    WEB_STATE_LOADED=1
    return 0
}

component_state_cli() {
    local launcher target

    if ! venv_ready; then
        printf 'missing|尚未创建运行环境（venv）\n'
        return
    fi
    if ! python_version_ok "$VENV_PYTHON"; then
        printf 'error|venv 里是 %s，低于 3.10，重新安装即可修复\n' "$("$VENV_PYTHON" --version 2>&1)"
        return
    fi

    launcher="$SCRIPT_DIR/bin/xgent"
    target="${XGENT_CMD_LINK_DIR:-/usr/local/bin}/xgent"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$(readlink -f "$target" 2>/dev/null || true)" = "$(readlink -f "$launcher" 2>/dev/null || true)" ]; then
            printf 'installed|终端命令 xgent -> %s\n' "$target"
        else
            printf 'error|%s 已被别的文件占用，xgent 命令没指向本项目\n' "$target"
        fi
        return
    fi
    printf 'missing|终端命令 xgent 尚未注册\n'
}

component_state_web() {
    if ! venv_ready; then
        printf 'missing|尚未创建运行环境（venv）\n'
        return
    fi
    if ! load_web_state; then
        printf 'error|读取 Web 配置失败（数据库或依赖有问题）\n'
        return
    fi
    if [ "$WEB_HAS_PASSWORD" != "yes" ]; then
        printf 'missing|尚未设置访问密码，Web 服务不会启动\n'
        return
    fi
    # 开关关着是"密码设了但端口不监听"最常见的原因，尤其是从老版本升上来的：
    # 那会儿 web 是 bot 菜单里的一个开关，默认关着，装了 web 组件也不会自动开。
    # 必须排在端口检查前面——否则用户只看到"端口未监听"，根本不知道去哪儿开。
    if [ "$WEB_ENABLED" != "yes" ]; then
        printf 'error|已在 bot 内关闭 Web 功能，端口 %s 未监听（本页第 3 项可开启）\n' "$WEB_PORT"
        return
    fi
    if service_running && ! port_is_listening "$WEB_PORT"; then
        printf 'error|Web 已开启，但服务在运行、端口 %s 没有监听（查日志排查）\n' "$WEB_PORT"
        return
    fi
    printf 'installed|http://127.0.0.1:%s（密码已设置，Web 已开启）\n' "$WEB_PORT"
}

component_state_bot() {
    local token id

    token="$(env_get BOT_TOKEN)"
    id="$(env_get AUTHORIZED_USER_ID)"

    if [ -z "$token" ]; then
        printf 'missing|未配置 Bot Token，当前是仅 Web 模式\n'
        return
    fi
    if ! is_number "$id"; then
        printf 'error|Token 已填，但 AUTHORIZED_USER_ID 不是合法数字（当前: %s）\n' "${id:-空}"
        return
    fi
    if [ "$id" = "1" ]; then
        printf 'error|AUTHORIZED_USER_ID 还是仅 Web 模式留下的占位值 1，Bot 会拒绝所有人\n'
        return
    fi
    printf 'installed|Telegram Bot 已配置（授权用户 ID %s）\n' "$id"
}

component_icon() {
    case "$1" in
        installed) printf '%b' "${GREEN}✅${NC}" ;;
        error)     printf '%b' "${YELLOW}🟡${NC}" ;;
        *)         printf '%b' "${RED}❌${NC}" ;;
    esac
}

component_state_text() {
    case "$1" in
        installed) printf '已安装' ;;
        error)     printf '状态异常' ;;
        *)         printf '未安装' ;;
    esac
}

render_component_board() {
    local row state detail index name
    local names=("cli" "web" "bot")

    echo ""
    echo "部署组件:"
    index=0
    for name in "${names[@]}"; do
        index=$((index + 1))
        row="$(component_state_"$name")"
        state="${row%%|*}"
        detail="${row#*|}"
        printf '  %b %d %s （%s）  %s\n' \
            "$(component_icon "$state")" "$index" "$name" \
            "$(component_state_text "$state")" "$detail"
    done
}

deployment_configured() {
    [ -n "$(env_get BOT_TOKEN)" ] && return 0
    load_web_state || return 1
    [ "$WEB_HAS_PASSWORD" = "yes" ]
}

require_deployment() {
    deployment_configured && return 0
    error "[错误] 还没有配置 web 或 bot，服务没有任何可用入口。"
    error "       请先运行 ./install.sh，选择 1) 安装 / 部署 / 配置。"
    exit 1
}

sync_deploy_mode_state() {
    ensure_state_dir
    if [ -n "$(env_get BOT_TOKEN)" ]; then
        DEPLOY_MODE="telegram"
    else
        DEPLOY_MODE="web-only"
    fi
    printf '%s\n' "$DEPLOY_MODE" > "$STATE_DIR/deploy-mode"
}

# ==========================================================================
# 组件安装 / 配置
# ==========================================================================

deploy_cli() {
    local row state choice=""

    echo ""
    info "── 1  cli · 本地终端客户端 ──────────────────────────"
    row="$(component_state_cli)"
    state="${row%%|*}"

    if [ "$state" = "installed" ]; then
        echo "   状态: 已安装 — ${row#*|}"
        echo "   cli 本身没有独立配置：模型、提示词、记忆都和 web/bot 共用同一份数据库，"
        echo "   在 xgent 里用 /config、/providers 这些命令改就行。"
        echo ""
        echo "   1) 重新注册 xgent 命令"
        echo "   2) 移除 xgent 命令"
        echo "   3) 返回"
        read -r -p "   请选择 [1-3，默认 3]: " choice
        case "$choice" in
            1) remove_xgent_command; install_xgent_command ;;
            2) remove_xgent_command ;;
            *) : ;;
        esac
        return
    fi

    if [ "$state" = "error" ]; then
        warn "   ${row#*|}"
    fi
    install_xgent_command
    echo "   在任意目录敲 xgent 就能打开终端界面。"
}

deploy_web() {
    local row state choice=""

    echo ""
    info "── 2  web · 浏览器网页访问 ──────────────────────────"
    ensure_identity_placeholder
    # 必须在当前 shell 里先加载一次。下面 component_state_web 是在 $(...) 里跑的，
    # 那是子 shell——它内部 load_web_state 对 WEB_ENABLED / WEB_HAS_PASSWORD 的
    # 赋值随子 shell 一起消失，父 shell 读到的永远是初值 no。后果是本页第 3 项
    # 恒显示"开启 Web 功能（当前: 已关闭）"，点下去 toggle_web_enabled 又以为没设
    # 过密码——装好之后没有任何路径能把 Web 关掉。
    load_web_state || true
    row="$(component_state_web)"
    state="${row%%|*}"

    if [ "$state" = "installed" ] || [ "$state" = "error" ]; then
        echo "   状态: $(component_state_text "$state") — ${row#*|}"
        echo ""
        echo "   1) 修改访问密码"
        echo "   2) 修改监听端口"
        if [ "$WEB_ENABLED" = "yes" ]; then
            echo "   3) 关闭 Web 功能（当前: 已开启）"
        else
            echo "   3) 开启 Web 功能（当前: 已关闭）"
        fi
        echo "   4) 返回"
        read -r -p "   请选择 [1-4，默认 4]: " choice
        case "$choice" in
            1) set_web_password ;;
            2) set_web_port ;;
            3) toggle_web_enabled ;;
            *) : ;;
        esac
        return
    fi

    warn "   Web 服务必须先有访问密码才会启动，下面把密码和端口一次设好。"
    set_web_password
    set_web_port
    # 装完就打开：用户在组件清单里选了 web，意思就是"我要用网页"，没道理让他
    # 装完再去 bot 菜单里手动开一次。从老版本升上来的人最容易踩这个——那时
    # web 是 bot 菜单里的一个开关，默认关着。
    if [ "$WEB_ENABLED" != "yes" ]; then
        set_web_enabled 1 && success "Web 功能已开启"
    fi
}

# 读写的是 bot 菜单里那个开关的同一个 key：UserDataManager 的 web_enabled，
# callbacks.py:683 的 toggle_web_enabled 写的也是它。两边共用一份配置，
# 在哪边改都一样。
set_web_enabled() {
    local want="$1"

    venv_ready || { error "   [错误] 运行环境未就绪，无法修改。"; return 1; }
    if ! run_config_py set-web-enabled "$want" >/dev/null 2>&1; then
        error "   [错误] 写入配置失败。"
        return 1
    fi
    reset_web_state_cache
    load_web_state || true
    return 0
}

toggle_web_enabled() {
    # 自己先确保状态是新鲜的，不依赖调用方替我们加载过——这个函数读的两个变量
    # 都是全局缓存，而缓存最容易在 $(...) 子shell 里被填成"看起来有值"的旧值。
    load_web_state || true

    if [ "$WEB_ENABLED" = "yes" ]; then
        set_web_enabled 0 && success "Web 功能已关闭"
    else
        # 和电报端 toggle_web_enabled 的校验保持对称。没密码就开启会留下一个
        # "开关是开的、服务却从没起来"的状态：菜单里照常显示打开按钮，点下去
        # 那个地址上没人监听，用户看到的就是毫无反应。
        if [ "$WEB_HAS_PASSWORD" != "yes" ]; then
            warn "   [跳过] 还没有设置访问密码，Web 服务不会启动。"
            warn "   请先用「设置访问密码」设好密码再开启。"
            return 1
        fi
        set_web_enabled 1 && success "Web 功能已开启"
    fi
    warn "   改动需要重启服务才生效（主菜单 3) 重启服务）。"
}

deploy_bot() {
    local row state choice="" confirm=""

    echo ""
    info "── 3  bot · Telegram Bot ────────────────────────────"
    row="$(component_state_bot)"
    state="${row%%|*}"

    if [ "$state" = "installed" ]; then
        echo "   状态: 已安装 — ${row#*|}"
        echo ""
        echo "   1) 更换 Bot Token"
        echo "   2) 更换授权用户 ID"
        echo "   3) 移除 Bot（切换为仅 Web 模式）"
        echo "   4) 返回"
        read -r -p "   请选择 [1-4，默认 4]: " choice
        case "$choice" in
            1)
                env_unset "BOT_TOKEN"
                ensure_env_value "BOT_TOKEN" "   请输入 Telegram Bot Token（输入时不显示）" secret
                sync_deploy_mode_state
                validate_telegram_token
                ;;
            2)
                prompt_authorized_user_id force
                ;;
            3)
                read -r -p "   确认移除 Bot Token、切换为仅 Web 模式？[y/N]: " confirm
                case "$confirm" in
                    y|Y)
                        env_unset "BOT_TOKEN"
                        sync_deploy_mode_state
                        success "已切换为仅 Web 模式（历史记录和模型配置都保留）。"
                        ;;
                    *) warn "   已取消。" ;;
                esac
                ;;
            *) : ;;
        esac
        return
    fi

    if [ "$state" = "error" ]; then
        warn "   ${row#*|}"
    fi
    ensure_env_value "BOT_TOKEN" "   请输入 Telegram Bot Token（输入时不显示）" secret
    # 这里一律强制重问 ID：仅 Web 模式会把 AUTHORIZED_USER_ID 填成占位数字，
    # 它是"合法数字"，ensure_env_value 会当成已配置直接放行——于是 Bot 装完
    # 谁都用不了，日志里只有一句看不懂的鉴权失败。
    prompt_authorized_user_id force
    sync_deploy_mode_state
    validate_telegram_token
}

# 仅 Web 模式也需要 AUTHORIZED_USER_ID（当身份标识用），但那只是个占位数字，
# 没必要专门问用户一次。缺了才补，已有真实 ID 时绝不覆盖。
ensure_identity_placeholder() {
    local current
    current="$(env_get AUTHORIZED_USER_ID)"
    is_number "$current" && return 0
    env_set "AUTHORIZED_USER_ID" "1"
    info "   已把 AUTHORIZED_USER_ID 设为占位值 1（仅 Web 模式下它只是身份标识）。"
}

prompt_authorized_user_id() {
    local force="${1:-}" current="" new_value=""

    current="$(env_get AUTHORIZED_USER_ID)"
    if [ "$force" != "force" ] && is_number "$current" && [ "$current" != "1" ]; then
        return 0
    fi
    if [ "$current" = "1" ]; then
        warn "   当前的 AUTHORIZED_USER_ID=1 只是仅 Web 模式下的占位数字，"
        warn "   不能当成真实 Telegram ID 沿用，请重新输入。"
    elif is_number "$current"; then
        read -r -p "   当前授权用户 ID 是 $current，回车保留，或输入新的纯数字 ID: " new_value
        if [ -z "$new_value" ]; then
            return 0
        fi
    fi

    while ! is_number "$new_value"; do
        read -r -p "   请输入 Telegram 用户 ID（纯数字，在 @userinfobot 里查看）: " new_value
        is_number "$new_value" || warn "   请只输入数字。"
    done
    env_set "AUTHORIZED_USER_ID" "$new_value"
    success "授权用户 ID 已设置为 $new_value"
}

component_flow() {
    local answer token pick_cli pick_web pick_bot invalid

    while true; do
        render_component_board
        echo ""
        echo "请输入要部署或配置的选项（可多选，空格隔开，例如: 1 2 3）"
        answer=""
        read -r -p "直接回车结束安装/配置: " answer
        if [ -z "$answer" ]; then
            return 0
        fi

        pick_cli=0; pick_web=0; pick_bot=0; invalid=0
        for token in $answer; do
            case "$token" in
                1) pick_cli=1 ;;
                2) pick_web=1 ;;
                3) pick_bot=1 ;;
                *) error "   无法识别的选项: $token（只能是 1 / 2 / 3）"; invalid=1 ;;
            esac
        done
        if [ "$invalid" -eq 1 ]; then
            continue
        fi

        # 固定按 1 -> 2 -> 3 执行，不按用户输入的顺序：web 会给
        # AUTHORIZED_USER_ID 落一个占位值，bot 必须排在它后面才能把占位值
        # 换成真实 ID；反过来的话真实 ID 会被占位值盖掉。
        if [ "$pick_cli" -eq 1 ]; then deploy_cli; fi
        if [ "$pick_web" -eq 1 ]; then deploy_web; fi
        if [ "$pick_bot" -eq 1 ]; then deploy_bot; fi
        reset_web_state_cache
    done
}

install_flow() {
    # 保活方式只在第一次问。已经装过的用户再点安装，看到的应该是同一套组件
    # 清单，而不是又被问一遍"你想用 systemd 还是 PM2"——那个问题他上次已经
    # 回答过了，答案就存在 .install-state/runtime-mode 里。
    if [ "$(get_runtime_mode)" = "none" ]; then
        choose_runtime_mode
    else
        info "[保活] 当前保活方式: $(runtime_mode_label)（在主菜单第 6 项可以更改）"
    fi

    prepare_base_environment
    success "环境检查通过。"

    component_flow
    sync_deploy_mode_state

    if ! deployment_configured; then
        echo ""
        warn "web 和 bot 都还没配置，服务没有可提供的入口，已跳过启动。"
        warn "（只装 cli 也能用：直接敲 xgent 打开终端界面。）"
        return 0
    fi

    echo ""
    local answer=""
    read -r -p "现在按「$(runtime_mode_label)」启动/重启服务？[Y/n]: " answer
    case "$answer" in
        n|N) warn "已跳过启动。之后在主菜单选 2) 启动服务 即可。" ;;
        *) start_service ;;
    esac
}

show_status() {
    echo ""
    echo "保活方式:   $(runtime_mode_label)"
    if service_running; then
        info "服务状态:   运行中"
    else
        warn "服务状态:   已停止"
    fi
    echo "部署模式:   $(deploy_mode_label)"
    echo "IP 出站模式: $(ip_mode_label)"
    render_component_board
}

show_menu() {
    echo ""
    echo "请选择操作:"
    echo "  1) 安装 / 部署 / 配置      检查环境后进入组件清单 (cli / web / bot)"
    echo "  2) 启动服务                当前保活方式: $(runtime_mode_label)"
    echo "  3) 重启服务"
    echo "  4) 停止服务"
    echo "  5) 运行状态"
    echo "  6) 更改保活方式            当前: $(runtime_mode_label)"
    echo "  7) IP 出站模式             当前: $(ip_mode_label)"
    echo "  8) 本地 API 容器 (Docker)  启动/关闭本地 Telegram Bot API server"
    echo "  9) 彻底重建 PM2 进程       重新加载 PM2 启动参数，进程 ID 会 +1"
    echo " 10) 卸载本脚本安装的运行内容"
    echo "  0) 退出"
    echo ""
}

show_usage() {
    echo "用法:"
    echo "  ./install.sh                 打开数字菜单"
    echo "  ./install.sh install         打开数字菜单"
    echo "  ./install.sh status          打印保活方式、服务状态和组件清单"
    echo "  ./install.sh start           按当前保活方式启动服务"
    echo "  ./install.sh stop            按当前保活方式停止服务"
    echo "  ./install.sh restart         重启服务 (PM2 为原地重启，进程 ID 不变)"
    echo "  ./install.sh rebuild         彻底重建 PM2 进程 (重新加载 PM2 启动参数，进程 ID 会 +1)"
    echo "  ./install.sh switch-mode     配置 / 移除 Telegram Bot（等价于组件清单里的第 3 项）"
    echo "  ./install.sh uninstall       卸载本脚本安装的运行内容"
    echo "  ./install.sh uninstall -y    跳过确认直接卸载"
    echo ""
    echo "安装后可用命令:"
    echo "  xgent                        打开本地终端客户端（与 Telegram/Web 共用同一套对话核心）"
}

parse_uninstall_options() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -y)
                SKIP_CONFIRM=1
                ;;
            *)
                error "[错误] 未知卸载参数: $1"
                show_usage
                exit 1
                ;;
        esac
        shift
    done
}

prepare_base_environment() {
    fix_line_endings
    migrate_legacy_installation
    ensure_python
    ensure_virtualenv
    activate_virtualenv
    install_requirements
    ensure_env_skeleton
    check_database
}

# 保证 .env 存在，并把部署模式状态和 .env 的实际内容对齐。不问任何问题。
ensure_env_skeleton() {
    info "[检查] 正在检查 .env..."
    if [ ! -f ".env" ]; then
        warn "   未找到 .env，正在创建新文件..."
        : > .env
        chmod 600 .env 2>/dev/null || true
    fi
    sync_deploy_mode_state
    success ".env 已就绪（部署模式: $(deploy_mode_label)）"
}

# restart / rebuild / pm2-start-internal 用的准备流程：**绝不发问**。
# 这几条路里有的是脱离终端跑的（restart_pm2_detached 起的后台 helper、
# systemd 的 ExecStart），一旦弹出 read 提示就是无声挂起——没有人能回答它，
# 而调用方只会看到"重启没生效"。要填的配置一律走 install_flow。
prepare_environment() {
    # 先做一次不需要 venv 的粗判。deployment_configured 要读数据库，得等 venv
    # 就绪才能问；但"连 .env 和 venv 都没有"是全新机器的确凿信号，没必要先花
    # 几分钟建 venv、装依赖，最后才告诉用户"你还没配置任何组件"。
    if [ ! -f ".env" ] && [ ! -x "$VENV_PYTHON" ]; then
        error "[错误] 还没安装过。"
        error "       请先运行 ./install.sh，选择 1) 安装 / 部署 / 配置。"
        exit 1
    fi
    prepare_base_environment
    install_xgent_command
    require_deployment
    validate_telegram_token
}

main() {
    local choice=""

    print_banner
    show_menu
    read -r -p "请输入选项 [0-10，默认 1]: " choice

    case "$choice" in
        ""|1)
            install_flow
            ;;
        2)
            prepare_environment
            start_service
            ;;
        3)
            prepare_environment
            restart_app
            ;;
        4)
            stop_service
            exit 0
            ;;
        5)
            show_status
            exit 0
            ;;
        6)
            choose_runtime_mode
            echo ""
            local restart_now=""
            read -r -p "现在按新的保活方式重新启动服务？[Y/n]: " restart_now
            case "$restart_now" in
                n|N) warn "已跳过。之后在主菜单选 2) 启动服务 即可。" ;;
                *) prepare_environment; start_service ;;
            esac
            exit 0
            ;;
        7)
            configure_ip_mode
            exit 0
            ;;
        8)
            manage_local_api
            exit 0
            ;;
        9)
            prepare_environment
            rebuild_pm2_app
            ;;
        10)
            uninstall_app --no-banner
            exit 0
            ;;
        0|q|Q)
            warn "已退出。"
            exit 0
            ;;
        *)
            error "[错误] 无效选项。"
            exit 1
            ;;
    esac

    echo ""
    echo -e "${CYAN}========================================================${NC}"
    echo -e "${CYAN} XGent for Telegram 已准备就绪。${NC}"
    echo -e "${CYAN} 保活方式: $(runtime_mode_label)${NC}"
    echo -e "${CYAN} 本地终端: 输入 ${GREEN}xgent${CYAN} 随时打开命令行客户端${NC}"
    echo -e "${CYAN}========================================================${NC}"
    print_web_only_access_hint
}

# 仅 Web 模式下，装完必须直接把访问地址打出来——用户明确要求的是"纯本地
# 端口访问"，如果只在设端口那一步一闪而过提过端口号，装完后什么都不显示，
# 等于让用户自己去翻代码猜端口。端口读的是 load_web_state 那份缓存，和
# set_web_port / 组件清单用的是同一个配置源，不会各说各话。
print_web_only_access_hint() {
    if [ "${DEPLOY_MODE:-telegram}" != "web-only" ]; then
        return
    fi
    load_web_state || return 0
    [ "$WEB_HAS_PASSWORD" = "yes" ] || return 0

    echo ""
    echo -e "${GREEN}访问地址：http://127.0.0.1:${WEB_PORT}${NC}"
    echo "（只监听 127.0.0.1，同机浏览器直接打开；远程访问需要自己配置端口转发或反向代理。）"
}

# 被 source 且设了 XGENT_INSTALL_SH_LIB=1 时只提供函数、不跑菜单。
# 这样测试可以单独调用 is_number / component_state_bot / discover_python 这些
# 判定逻辑——它们的 bug（比如"venv 里的 python3 抢了系统 python3"）只有真的
# 跑一遍才看得出来，靠读脚本正则是抓不到的。
if [ "${XGENT_INSTALL_SH_LIB:-}" = "1" ]; then
    return 0
fi

case "${1:-}" in
    install|--install)
        main
        ;;
    pm2-start-internal)
        prepare_environment
        start_with_pm2
        ;;
    service-exec)
        # systemd 的 ExecStart 进入点。刻意不做环境检查、不发问、不打横幅：
        # 这里是服务进程本身，任何一次 read 提示都会变成"服务起不来但也不
        # 报错"。环境准备是安装时的事，跑服务时只管跑。
        exec_app_process
        ;;
    status|--status)
        print_banner
        show_status
        ;;
    start|--start)
        print_banner
        prepare_environment
        start_service
        ;;
    stop|--stop)
        stop_service
        ;;
    restart|--restart)
        print_banner
        prepare_environment
        restart_app
        ;;
    rebuild|--rebuild)
        print_banner
        prepare_environment
        rebuild_pm2_app
        ;;
    switch-mode|--switch-mode)
        print_banner
        switch_deploy_mode
        ;;
    uninstall|--uninstall|remove|--remove)
        shift
        parse_uninstall_options "$@"
        uninstall_app
        ;;
    help|--help|-h)
        show_usage
        ;;
    "")
        main
        ;;
    *)
        error "[错误] 未知参数: $1"
        show_usage
        exit 1
        ;;
esac
