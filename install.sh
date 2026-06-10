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

    if [ -f "$IP_MODE_FILE" ]; then
        mode="$(tr -d '[:space:]' < "$IP_MODE_FILE" 2>/dev/null || true)"
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

    case "$mode" in
        ipv4|ipv6)
            ensure_state_dir
            printf '%s\n' "$mode" > "$IP_MODE_FILE"
            ;;
        default)
            rm -f "$IP_MODE_FILE"
            rmdir "$STATE_DIR" 2>/dev/null || true
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
        TELEGRAM_AI_BOT_IP_MODE= TELEGRAM_AI_BOT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
    else
        TELEGRAM_AI_BOT_IP_MODE="$mode" TELEGRAM_AI_BOT_APP_ENTRY="$app_entry" PYTHONPATH="$pythonpath" "$@"
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

    echo ""
    echo "当前 IP 限制: $(ip_mode_label)"
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
        grep -v "^${key}=" .env > "$tmp_file" || true
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
    mv "$tmp_file" .env
}

ensure_env_value() {
    local key="$1"
    local prompt="$2"
    local current_value=""
    local new_value=""

    if [ -f ".env" ]; then
        current_value="$(grep -E "^${key}=.+" .env | tail -n 1 | cut -d= -f2- || true)"
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
    set -e

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

    if [ -f "bot.pid" ]; then
        local old_pid
        old_pid="$(cat bot.pid)"
        if ps -p "$old_pid" >/dev/null 2>&1; then
            echo "   正在停止旧进程: $old_pid"
            kill "$old_pid" 2>/dev/null || true
            sleep 2
        fi
    fi

    echo "   IP 出站模式: $(ip_mode_label)"
    mode="$(get_ip_mode)"
    pythonpath="$(pythonpath_with_project)"
    code="$(bot_python_code)"
    if [ "$mode" = "default" ]; then
        TELEGRAM_AI_BOT_IP_MODE= TELEGRAM_AI_BOT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" nohup "$VENV_PYTHON" -c "$code" > bot_output.log 2>&1 &
    else
        TELEGRAM_AI_BOT_IP_MODE="$mode" TELEGRAM_AI_BOT_APP_ENTRY="$APP_ENTRY" PYTHONPATH="$pythonpath" nohup "$VENV_PYTHON" -c "$code" > bot_output.log 2>&1 &
    fi
    local pid=$!
    echo "$pid" > bot.pid
    sleep 2

    if ps -p "$pid" >/dev/null 2>&1; then
        echo "   后台启动成功，PID: $pid"
        echo "   日志文件: bot_output.log"
        echo "   停止命令: kill \$(cat bot.pid)"
        echo "   查看日志: tail -f bot_output.log"
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
    pm2 delete "$PM2_APP_NAME" 2>/dev/null || true
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
    setup_pm2_startup

    if pm2 save >/dev/null; then
        echo "   PM2 进程列表已保存，服务器重启后会自动恢复。"
    else
        warn "   PM2 进程列表保存失败。请手动执行: pm2 save"
    fi
}

show_menu() {
    echo ""
    echo "请选择操作:"
    echo "  1) 前台运行"
    echo "  2) 后台运行 (nohup)"
    echo "  3) PM2 守护运行 (未安装时自动补齐)"
    echo "  4) 仅检查环境"
    echo "  5) IP 出站模式 ($(ip_mode_label))"
    echo "  6) 卸载本脚本安装的运行内容"
    echo "  7) 退出"
    echo ""
}

show_usage() {
    echo "用法:"
    echo "  ./install.sh                 打开数字菜单"
    echo "  ./install.sh install         打开数字菜单"
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
    read -r -p "请输入选项 [1/2/3/4/5/6/7，默认 1]: " choice

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
            configure_ip_mode
            exit 0
            ;;
        6)
            uninstall_app --no-banner
            exit 0
            ;;
        7)
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
