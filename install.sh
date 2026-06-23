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
APP_ENTRY="bot_server.py"
PM2_APP_NAME="telegram-ai-bot"
STATE_DIR="$SCRIPT_DIR/.install-state"
APT_STATE_FILE="$STATE_DIR/apt-packages.txt"
IP_MODE_FILE="$STATE_DIR/ip-mode"
APT_UPDATED=0
SKIP_CONFIRM=0

LOCAL_API_CONTAINER="telegram-local-bot-api"
LOCAL_API_PORT="8081"
LOCAL_API_DATA_DIR="$SCRIPT_DIR/.local-api-data"
LOCAL_API_IMAGE="aiogram/telegram-bot-api:latest"
LOCAL_API_ALLOWED_IPS_FILE="$STATE_DIR/local-api-allowed-ips"

print_banner() {
    echo -e "${CYAN}"
    echo "========================================================"
    echo " Telegram AI Bot 启动器"
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

get_ip_mode() {
    local mode=""

    if [ -f ".env" ]; then
        mode="$(grep -E "^TELEGRAM_AI_BOT_IP_MODE=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' || true)"
    fi

    case "$mode" in
        ipv4|ipv6)
            printf '%s\n' "$mode"
            ;;
        *)
            printf 'default\n'
            ;;
    esac
}

ip_mode_label() {
    case "$(get_ip_mode)" in
        ipv4)
            printf '仅 IPv4'
            ;;
        ipv6)
            printf '仅 IPv6'
            ;;
        *)
            printf '服务器默认'
            ;;
    esac
}

set_ip_mode() {
    local mode="$1"
    local tmp_file

    case "$mode" in
        ipv4|ipv6)
            tmp_file="$(mktemp)"
            if [ -f ".env" ]; then
                grep -v "^TELEGRAM_AI_BOT_IP_MODE=" .env > "$tmp_file" || true
            fi
            printf 'TELEGRAM_AI_BOT_IP_MODE=%s\n' "$mode" >> "$tmp_file"
            mv "$tmp_file" .env
            ;;
        default)
            if [ -f ".env" ]; then
                tmp_file="$(mktemp)"
                grep -v "^TELEGRAM_AI_BOT_IP_MODE=" .env > "$tmp_file" || true
                mv "$tmp_file" .env
            fi
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
    local mode pythonpath app_entry
    mode="$(get_ip_mode)"
    pythonpath="$(pythonpath_with_project)"
    app_entry="${TELEGRAM_AI_BOT_APP_ENTRY:-$APP_ENTRY}"

    if [ "$mode" = "default" ]; then
        env -u TELEGRAM_AI_BOT_IP_MODE TELEGRAM_AI_BOT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
    else
        env TELEGRAM_AI_BOT_IP_MODE="$mode" TELEGRAM_AI_BOT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
    fi
}

ip_family_restrictor_python() {
    cat <<'PY'
import errno
import os
import socket

_ip_mode = (os.environ.get("TELEGRAM_AI_BOT_IP_MODE") or "").strip().lower()
_allowed_family = None
_blocked_family = None

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
            f"{_family_name(_blocked_family)} is disabled by TELEGRAM_AI_BOT_IP_MODE={_ip_mode}",
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

_app_entry = os.environ.get("TELEGRAM_AI_BOT_APP_ENTRY") or "bot_server.py"
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
    echo "当前 IP 限制: $current_label"
    echo "请选择 IP 出站模式:"
    echo "  1) 仅 IPv4 - 禁用 IPv6，Telegram 和 AI 服务等运行期请求只解析/连接 IPv4"
    echo "  2) 仅 IPv6 - 禁用 IPv4，Telegram 和 AI 服务等运行期请求只解析/连接 IPv6"
    echo "  3) 撤回修改 - 取消限制，恢复服务器默认网络栈"
    echo "  4) 返回主菜单"
    echo ""
    read -r -p "请输入选项 [1/2/3/4，默认 4]: " choice

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
            set_ip_mode default
            success "已撤回 IP 限制，恢复服务器默认状态。请重启 Bot 后生效。"
            ;;
        ""|4)
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
}

ensure_python() {
    info "[检查] 正在检查 Python 3..."

    if ! command_exists python3; then
        warn "   未检测到 Python 3，尝试自动安装..."
        ensure_apt_packages python3 python3-pip python3-venv
    fi

    if ! command_exists python3; then
        error "[错误] 自动安装后仍然无法使用 Python 3。"
        exit 1
    fi

    if ! python3 -m venv --help >/dev/null 2>&1; then
        warn "   缺少 python3-venv，尝试自动安装..."
        ensure_apt_packages python3-venv python3-pip
    fi

    if ! python3 -m pip --version >/dev/null 2>&1; then
        warn "   缺少 pip，尝试自动安装..."
        ensure_apt_packages python3-pip
    fi

    echo "   Python 版本: $(python3 --version)"

    if python3 -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)"; then
        success "Python 版本满足要求 (>= 3.8)"
    else
        error "[错误] 需要 Python 3.8 或更高版本。"
        exit 1
    fi
}

resolve_path() {
    local target="$1"
    echo "$(cd "$(dirname "$target")" && pwd -P)/$(basename "$target")"
}

safe_remove_venv() {
    local expected resolved
    expected="$(resolve_path "$SCRIPT_DIR/venv")"
    resolved="$(resolve_path "$VENV_DIR")"

    if [ "$resolved" != "$expected" ]; then
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
    echo "   会清理: venv、本项目 nohup/PM2 进程、bot.pid、脚本记录的 apt 下载包。"
    echo "   会保留: 项目文件、.env、数据库、日志、skill/ 下脚本服务、PM2 程序本体。"
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

    if [ -f "bot.pid" ]; then
        pid="$(cat bot.pid 2>/dev/null || true)"
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            warn "   bot.pid 内容不是有效 PID，已移除 PID 文件。"
            rm -f bot.pid
        elif ! ps -p "$pid" >/dev/null 2>&1; then
            rm -f bot.pid
            success "已清理过期 bot.pid"
        else
            cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
            if [[ "$cmdline" == *"$APP_ENTRY"* ]] && { [[ "$cmdline" == *"$SCRIPT_DIR"* ]] || [[ "$cmdline" == *"$VENV_PYTHON"* ]]; }; then
                info "[卸载] 正在停止 nohup 后台进程: $pid"
                kill "$pid" 2>/dev/null || true
                sleep 2
                if ps -p "$pid" >/dev/null 2>&1; then
                    kill -TERM "$pid" 2>/dev/null || true
                fi
                rm -f bot.pid
                success "nohup 后台进程已停止"
            else
                warn "   bot.pid 指向的进程不像本项目进程，已跳过停止: $pid"
            fi
        fi
    fi

    while IFS= read -r line; do
        pid="${line%% *}"
        cmdline="${line#* }"

        if ! [[ "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -eq "$$" ]; then
            continue
        fi

        if [[ "$cmdline" == *"$APP_ENTRY"* ]] && { [[ "$cmdline" == *"$SCRIPT_DIR"* ]] || [[ "$cmdline" == *"$VENV_PYTHON"* ]] || [[ "$cmdline" == *"TELEGRAM_AI_BOT_APP_ENTRY"* ]]; }; then
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
    if ! command_exists pm2; then
        warn "   未检测到 PM2，跳过 PM2 进程清理。"
        return
    fi

    if pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1; then
        info "[卸载] 正在移除 PM2 进程: $PM2_APP_NAME"
        pm2 delete "$PM2_APP_NAME" >/dev/null 2>&1 || warn "   PM2 进程移除失败，请手动执行: pm2 delete $PM2_APP_NAME"
        if pm2 save >/dev/null 2>&1; then
            success "PM2 进程记录已更新"
        else
            warn "   PM2 进程列表保存失败，请手动执行: pm2 save"
        fi
    else
        echo "   未发现 PM2 进程: $PM2_APP_NAME"
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
            *)
                packages+=("$package")
                ;;
        esac
    done < <(sort -u "$APT_STATE_FILE")

    if [ "${#kept_packages[@]}" -gt 0 ]; then
        warn "   为保留 PM2，已跳过这些 Node/npm 相关包:"
        printf '   %s\n' "${kept_packages[@]}"
    fi

    if [ "${#packages[@]}" -eq 0 ]; then
        echo "   apt 安装记录中没有仍处于安装状态的包。"
        rm -f "$APT_STATE_FILE"
        rmdir "$STATE_DIR" 2>/dev/null || true
        return
    fi

    info "[卸载] 正在卸载本脚本记录的新装 apt 包:"
    printf '   %s\n' "${packages[@]}"
    run_privileged apt-get purge -y "${packages[@]}" || warn "   部分 apt 包卸载失败，请根据上方输出手动检查。"
    run_privileged apt-get autoremove --purge -y || true
    run_privileged apt-get clean || true

    rm -f "$APT_STATE_FILE"
    rmdir "$STATE_DIR" 2>/dev/null || true
    success "已处理 apt 安装记录"
}

remove_ip_mode_state() {
    if [ -f "$IP_MODE_FILE" ]; then
        info "[卸载] 正在清理 IP 出站模式设置"
        rm -f "$IP_MODE_FILE"
        rmdir "$STATE_DIR" 2>/dev/null || true
        success "IP 出站模式设置已清理"
    fi
}

uninstall_app() {
    if [ "${1:-}" != "--no-banner" ]; then
        print_banner
    fi

    confirm_uninstall

    remove_pm2_process
    stop_background_process
    remove_virtualenv
    remove_recorded_apt_packages
    remove_ip_mode_state

    echo ""
    echo -e "${CYAN}========================================================${NC}"
    echo -e "${CYAN} Telegram AI Bot 主安装内容已卸载。${NC}"
    echo -e "${CYAN} 已保留项目文件、配置、数据库、日志、skill 服务和 PM2 本体。${NC}"
    echo -e "${CYAN}========================================================${NC}"
}

ensure_virtualenv() {
    info "[检查] 正在检查虚拟环境..."

    if [ -x "$VENV_PYTHON" ] && [ -f "$VENV_ACTIVATE" ]; then
        success "现有虚拟环境可正常使用"
        return
    fi

    if [ -e "$VENV_DIR" ]; then
        warn "   检测到损坏或不完整的 venv，正在重建..."
        safe_remove_venv
    fi

    if ! python3 -m venv "$VENV_DIR"; then
        warn "   创建虚拟环境失败，正在安装 python3-venv 后重试..."
        ensure_apt_packages python3-venv python3-pip
        if [ -e "$VENV_DIR" ]; then
            safe_remove_venv
        fi
        python3 -m venv "$VENV_DIR"
    fi

    if [ ! -x "$VENV_PYTHON" ] || [ ! -f "$VENV_ACTIVATE" ]; then
        error "[错误] 无法创建可用的虚拟环境。"
        exit 1
    fi

    success "虚拟环境创建完成"
}

activate_virtualenv() {
    if [ ! -f "$VENV_ACTIVATE" ]; then
        error "[错误] 找不到激活脚本: $VENV_ACTIVATE"
        exit 1
    fi

    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
}

install_requirements() {
    info "[pip] 正在升级 pip..."
    python -m pip install --upgrade pip -q

    info "[pip] 正在安装 Python 依赖..."
    python -m pip install -r requirements.txt -q

    success "依赖安装完成"
    python -c "import aiosqlite; print('   aiosqlite 版本:', aiosqlite.__version__)"
    python -c "import telegram; print('   python-telegram-bot 版本:', telegram.__version__)"
    python -c "import openai; print('   openai 版本:', openai.__version__)"
}

upsert_env_value() {
    local key="$1"
    local value="$2"
    local tmp_file

    tmp_file="$(mktemp)"
    if [ -f ".env" ]; then
        grep -vF "${key}=" .env > "$tmp_file" || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
    mv "$tmp_file" .env
    chmod 600 .env
}

remove_env_value() {
    local key="$1"

    if [ ! -f ".env" ]; then
        return
    fi

    local tmp_file
    tmp_file="$(mktemp)"
    grep -vF "${key}=" .env > "$tmp_file" || true
    mv "$tmp_file" .env
    chmod 600 .env
}

# 读取 .env 中某个键的值（不存在则空）
local_api_env_value() {
    local key="$1"
    if [ -f ".env" ]; then
        awk -F= -v k="$key" '$1==k{print substr($0,length(k)+2)}' .env 2>/dev/null | tail -n 1 || true
    fi
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
    url="$(local_api_env_value TELEGRAM_API_URL)"
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

# 把白名单 IP 列表转成 docker run 的端口映射参数（每个 IP 一条 -p）。
# 输出形如：-p 127.0.0.1:8081:8081 -p 1.2.3.4:8081:8081
build_local_api_port_args() {
    local ip args=""
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        # 简单格式校验：只允许 IPv4/IPv6/主机名常见字符
        if [[ "$ip" =~ ^[A-Za-z0-9._:-]+$ ]]; then
            args="${args} -p ${ip}:${LOCAL_API_PORT}:8081"
        fi
    done < <(get_allowed_ips)
    if [ -z "$args" ]; then
        args="-p 127.0.0.1:${LOCAL_API_PORT}:8081"
    fi
    printf '%s' "${args# }"
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

    token="$(local_api_env_value BOT_TOKEN)"
    if [ -z "$token" ]; then
        warn "   .env 中没有 BOT_TOKEN，跳过 logOut。"
        return 1
    fi

    base="${base%%/}"

    info "[本地 API] 正在调用 logOut (从 ${base} 登出 bot token)..."
    # telegram logOut 成功返回 {"ok":true,...}，未登录/网络错误返回非 0 或 {"ok":false}
    local http_code body
    set +e
    if command_exists curl; then
        body="$(curl -sS --max-time 15 -X POST -d '' "${base}/bot${token}/logOut" 2>/dev/null || true)"
    elif command_exists wget; then
        body="$(wget -qO- --timeout=15 --post-data='' "${base}/bot${token}/logOut" 2>/dev/null || true)"
    else
        set -euo pipefail
        warn "   未找到 curl/wget，跳过 logOut。"
        return 1
    fi
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
    local current_value=""
    local new_value=""

    if [ -f ".env" ]; then
        current_value="$(awk -F= -v k="$key" '$1==k && length($0)>length(k)+1{print substr($0,length(k)+2)}' .env | tail -n 1 || true)"
    fi

    if [ -n "$current_value" ]; then
        return
    fi

    warn "   .env 中缺少 $key，请现在输入。"
    while [ -z "$new_value" ]; do
        read -r -p "$prompt: " new_value
    done
    upsert_env_value "$key" "$new_value"
}

ensure_env_file() {
    info "[检查] 正在检查 .env..."

    if [ ! -f ".env" ]; then
        warn "   未找到 .env，正在创建新文件..."
        : > .env
    fi

    ensure_env_value "BOT_TOKEN" "请输入 Telegram Bot Token"
    ensure_env_value "AUTHORIZED_USER_ID" "请输入 Telegram 用户 ID"

    success ".env 配置已就绪"
}

validate_telegram_token() {
    info "[检查] 正在验证 Telegram Bot Token..."

    local status
    set +e
    status=$(run_bot_python python - <<PY
$(ip_family_restrictor_python)
import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import InvalidToken, TelegramError

token = ""
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("BOT_TOKEN="):
        token = line.split("=", 1)[1].strip()
        break

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
        2)
            warn "   无法完成在线校验，可能是网络问题；将继续后续步骤。"
            ;;
        *)
            error "[错误] Telegram Bot Token 校验失败，请检查 .env 内容。"
            exit 1
            ;;
    esac
}

check_database() {
    info "[检查] 正在检查数据库..."

    if [ -f "bot_memory.db" ]; then
        local db_size
        db_size="$(du -h bot_memory.db | cut -f1)"
        echo "   数据库已存在，大小: $db_size"
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

ensure_pm2() {
    if command_exists pm2; then
        success "PM2 已可用"
        return
    fi

    ensure_npm

    info "[pm2] 正在全局安装 PM2..."
    run_privileged npm install -g pm2
    hash -r

    if ! command_exists pm2; then
        error "[错误] PM2 安装完成，但命令仍未进入 PATH。"
        exit 1
    fi

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
    info "[运行] 正在前台启动 Telegram AI Bot..."
    echo "   IP 出站模式: $(ip_mode_label)"
    run_bot_python python -c "$(bot_python_code)"
}

start_background() {
    local mode pythonpath code

    info "[运行] 正在后台启动 Telegram AI Bot..."

    # 彻底停止旧进程
    if [ -f "bot.pid" ]; then
        local old_pid
        old_pid="$(cat bot.pid)"
        if [[ "$old_pid" =~ ^[0-9]+$ ]] && ps -p "$old_pid" >/dev/null 2>&1; then
            echo "   正在停止旧进程: $old_pid"
            kill "$old_pid" 2>/dev/null || true
            sleep 2
            if ps -p "$old_pid" >/dev/null 2>&1; then
                kill -9 "$old_pid" 2>/dev/null || true
            fi
        fi
        rm -f bot.pid
    fi

    echo "   IP 出站模式: $(ip_mode_label)"
    mode="$(get_ip_mode)"
    pythonpath="$(pythonpath_with_project)"
    code="$(bot_python_code)"
    if [ "$mode" = "default" ]; then
        env -u TELEGRAM_AI_BOT_IP_MODE TELEGRAM_AI_BOT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" nohup "$VENV_PYTHON" -c "$code" > bot_output.log 2>&1 &
    else
        env TELEGRAM_AI_BOT_IP_MODE="$mode" TELEGRAM_AI_BOT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" nohup "$VENV_PYTHON" -c "$code" > bot_output.log 2>&1 &
    fi
    local pid=$!
    echo "$pid" > bot.pid
    sleep 2

    if ps -p "$pid" >/dev/null 2>&1; then
        echo "   后台启动成功，PID: $pid"
        echo "   日志文件: bot_output.log"
        echo "   停止命令: kill \$(cat bot.pid)"
        echo "   查看日志: tail -f bot_output.log"
        echo "   彻底重启: bash install.sh restart"
    else
        error "[错误] 后台启动失败，请查看 bot_output.log。"
        tail -20 bot_output.log || true
        exit 1
    fi
}

start_with_pm2() {
    local code

    ensure_pm2

    info "[运行] 正在使用 PM2 启动 Telegram AI Bot..."
    echo "   IP 出站模式: $(ip_mode_label)"

    # 彻底删除旧进程，确保不会保留旧配置
    pm2 delete "$PM2_APP_NAME" 2>/dev/null || true
    sleep 1

    code="$(bot_python_code)"
    run_bot_python pm2 start "$VENV_PYTHON" \
        --name "$PM2_APP_NAME" \
        --cwd "$SCRIPT_DIR" \
        --interpreter none \
        --stop-exit-codes 78 \
        --max-memory-restart 500M \
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
    local helper log_file mode
    helper="$(mktemp /tmp/${PM2_APP_NAME}-restart.XXXXXX.sh)"
    log_file="/tmp/${PM2_APP_NAME}-restart.log"
    mode="$(get_ip_mode)"

    cat > "$helper" <<EOF
#!/usr/bin/env bash
set -u
{
  echo "===== $PM2_APP_NAME detached restart started at \$(date '+%F %T') ====="
  cd "$SCRIPT_DIR" || exit 1

  # 彻底停止旧进程
  if pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1; then
    echo "Stopping PM2 process: $PM2_APP_NAME"
    pm2 delete "$PM2_APP_NAME" >/dev/null 2>&1 || true
    sleep 1
  fi

  # 设置 IP 模式环境变量
  if [ "$mode" = "default" ]; then
    unset TELEGRAM_AI_BOT_IP_MODE || true
  else
    export TELEGRAM_AI_BOT_IP_MODE="$mode"
  fi

  # 彻底重启：重新准备环境并启动
  bash "$SCRIPT_DIR/install.sh" pm2-start-internal
  status=\$?
  pm2 save || true
  echo "===== $PM2_APP_NAME detached restart finished with status \$status at \$(date '+%F %T') ====="
  exit \$status
} >> "$log_file" 2>&1
EOF
    chmod +x "$helper"

    nohup bash -c "sleep 2; '$helper'" >/dev/null 2>&1 &
    echo "   已启动脱离当前会话的 PM2 彻底重启任务。"
    echo "   日志文件: $log_file"
    echo "   如果当前 Bot 短暂断开，请等待 5-10 秒后重新 /start。"
}

restart_app() {
    info "[重启] 正在彻底重启 Telegram AI Bot..."

    if command_exists pm2 && pm2 describe "$PM2_APP_NAME" >/dev/null 2>&1; then
        echo "   检测到 PM2 进程，将彻底删除并重新创建以确保加载最新配置。"
        restart_pm2_detached
        return
    fi

    if [ -f "bot.pid" ]; then
        echo "   检测到 nohup 进程，将彻底停止并重新启动以确保加载最新配置。"
        stop_background_process
        sleep 1
        start_background
        return
    fi

    warn "   未检测到正在运行的 PM2/nohup 进程。"
    echo "   请先选择 2) 后台运行 或 3) PM2 守护运行 启动一次。"
    return 1
}

# 收集本地 API 所需 .env 变量；已有值则显示并允许回车保留
prompt_local_api_env() {
    local current_url current_id current_hash
    local input_url input_id input_hash

    current_url="$(local_api_env_value TELEGRAM_API_URL)"
    current_id="$(local_api_env_value TELEGRAM_API_ID)"
    current_hash="$(local_api_env_value TELEGRAM_API_HASH)"

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

    upsert_env_value "TELEGRAM_API_URL" "$input_url"
    upsert_env_value "TELEGRAM_API_ID" "$input_id"
    upsert_env_value "TELEGRAM_API_HASH" "$input_hash"
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

    api_id="$(local_api_env_value TELEGRAM_API_ID)"
    api_hash="$(local_api_env_value TELEGRAM_API_HASH)"
    port="$LOCAL_API_PORT"

    info "[本地 API] 正在准备容器..."
    mkdir -p "$LOCAL_API_DATA_DIR"

    # 清掉可能存在的旧容器
    if docker inspect "$LOCAL_API_CONTAINER" >/dev/null 2>&1; then
        info "[本地 API] 正在移除旧容器: $LOCAL_API_CONTAINER"
        docker rm -f "$LOCAL_API_CONTAINER" >/dev/null
    fi

    info "[本地 API] 正在拉取镜像 $LOCAL_API_IMAGE（首次较慢）..."
    if ! docker pull "$LOCAL_API_IMAGE"; then
        error "[错误] 拉取镜像失败，请检查网络或手动执行: docker pull $LOCAL_API_IMAGE"
        return 1
    fi

    info "[本地 API] 正在启动容器..."
    # 按白名单 IP 生成端口绑定参数（默认仅 127.0.0.1，可随时加白名单）
    local -a port_args=()
    local ip
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
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
    helper="$(mktemp /tmp/${PM2_APP_NAME}-api-switch-restart.XXXXXX.sh)"
    log_file="/tmp/${PM2_APP_NAME}-api-switch-restart.log"

    cat > "$helper" <<EOF
#!/usr/bin/env bash
set -u
{
  echo "===== $PM2_APP_NAME api-switch restart at \$(date '+%F %T') ====="
  cd "$SCRIPT_DIR" || exit 1
  bash "$SCRIPT_DIR/install.sh" restart
  echo "===== finished at \$(date '+%F %T') ====="
} >> "$log_file" 2>&1
EOF
    chmod +x "$helper"

    nohup bash -c "sleep 2; '$helper'" >/dev/null 2>&1 &
    echo "   已在后台启动 bot 重启任务（独立进程，不阻塞当前菜单）。"
    echo "   日志文件: $log_file"
    echo "   如果当前 Bot 短暂断开，请等待 5-10 秒后重新 /start。"
}

stop_local_api_container() {
    local local_base changed=0

    if ! command_exists docker; then
        warn "   未检测到 docker，跳过容器操作。"
    else
        if docker inspect "$LOCAL_API_CONTAINER" >/dev/null 2>&1; then
            # 容器还在运行时，先把 token 从本地 server 登出，让它回到官方
            local_base="$(local_api_env_value TELEGRAM_API_URL)"
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
    if [ -n "$(local_api_env_value TELEGRAM_API_URL)" ]; then
        remove_env_value "TELEGRAM_API_URL"
        success ".env 中 TELEGRAM_API_URL 已清除，bot 重启后将回连官方 api.telegram.org。"
        echo "   TELEGRAM_API_ID / TELEGRAM_API_HASH 已保留，下次启用可直接复用。"
        changed=1
    else
        echo "   .env 中本就没有 TELEGRAM_API_URL，无需清理。"
    fi

    # 只有发生过实际变更（关了容器或清了配置）才重启 bot
    if [ "$changed" -eq 1 ]; then
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

show_menu() {
    local current_mode_label="$(ip_mode_label)"
    echo ""
    echo "请选择操作:"
    echo "  1) 前台运行"
    echo "  2) 后台运行 (nohup)"
    echo "  3) PM2 守护运行 (未安装时自动补齐)"
    echo "  4) 仅检查环境"
    echo "  5) 重启 Bot (当前: $current_mode_label)"
    echo "  6) IP 出站模式 (当前: $current_mode_label)"
    echo "  7) 卸载本脚本安装的运行内容"
    echo "  8) 本地 API 容器 (Docker) - 启动/关闭本地 Telegram Bot API server"
    echo "  9) 退出"
    echo ""
}

show_usage() {
    echo "用法:"
    echo "  ./install.sh                 打开数字菜单"
    echo "  ./install.sh install         打开数字菜单"
    echo "  ./install.sh restart         重启当前 PM2/nohup Bot 进程"
    echo "  ./install.sh uninstall       卸载本脚本安装的运行内容"
    echo "  ./install.sh uninstall -y    跳过确认直接卸载"
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

prepare_environment() {
    fix_line_endings
    ensure_python
    ensure_virtualenv
    activate_virtualenv
    install_requirements
    ensure_env_file
    validate_telegram_token
    check_database
}

main() {
    print_banner

    show_menu
    read -r -p "请输入选项 [1/2/3/4/5/6/7/8/9，默认 1]: " choice

    case "$choice" in
        ""|1)
            prepare_environment
            start_foreground
            ;;
        2)
            prepare_environment
            start_background
            ;;
        3)
            prepare_environment
            start_with_pm2
            ;;
        4)
            prepare_environment
            success "环境检查完成，未启动任何服务。"
            ;;
        5)
            prepare_environment
            restart_app
            ;;
        6)
            configure_ip_mode
            exit 0
            ;;
        7)
            uninstall_app --no-banner
            exit 0
            ;;
        8)
            manage_local_api
            exit 0
            ;;
        9)
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
    echo -e "${CYAN} Telegram AI Bot 已准备就绪。${NC}"
    echo -e "${CYAN} 已增强: 自动补环境、venv 自愈、PM2 自动安装${NC}"
    echo -e "${CYAN}========================================================${NC}"
}

case "${1:-}" in
    install|--install)
        main
        ;;
    pm2-start-internal)
        prepare_environment
        start_with_pm2
        ;;
    restart|--restart)
        print_banner
        prepare_environment
        restart_app
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
