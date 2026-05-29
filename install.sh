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
APT_UPDATED=0

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

ensure_apt_packages() {
    ensure_apt_available

    if [ "$APT_UPDATED" -eq 0 ]; then
        info "[信息] 正在执行 apt-get update..."
        run_privileged apt-get update
        APT_UPDATED=1
    fi

    info "[信息] 正在安装系统依赖: $*"
    run_privileged apt-get install -y "$@"
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
    status=$(python - <<'PY'
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
    python "$APP_ENTRY"
}

start_background() {
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

    nohup "$VENV_PYTHON" "$APP_ENTRY" > bot_output.log 2>&1 &
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
    ensure_pm2

    info "[运行] 正在使用 PM2 启动 Telegram AI Bot..."
    pm2 delete telegram-ai-bot 2>/dev/null || true
    pm2 start "$SCRIPT_DIR/$APP_ENTRY" \
        --name "telegram-ai-bot" \
        --cwd "$SCRIPT_DIR" \
        --interpreter "$VENV_PYTHON" \
        --stop-exit-codes 78 \
        --max-memory-restart 500M \
        --exp-backoff-restart-delay=100

    echo "   PM2 启动成功。"
    echo "   查看日志: pm2 logs telegram-ai-bot"
    echo "   停止命令: pm2 stop telegram-ai-bot"
    echo "   重启命令: pm2 restart telegram-ai-bot"
    setup_pm2_startup

    if pm2 save >/dev/null; then
        echo "   PM2 进程列表已保存，服务器重启后会自动恢复。"
    else
        warn "   PM2 进程列表保存失败。请手动执行: pm2 save"
    fi
}

show_menu() {
    echo ""
    echo "请选择启动方式:"
    echo "  1) 前台运行"
    echo "  2) 后台运行 (nohup)"
    echo "  3) PM2 守护运行 (未安装时自动补齐)"
    echo "  4) 仅检查环境"
    echo "  q) 退出"
    echo ""
}

main() {
    print_banner
    fix_line_endings
    ensure_python
    ensure_virtualenv
    activate_virtualenv
    install_requirements
    ensure_env_file
    validate_telegram_token
    check_database

    show_menu
    read -r -p "请输入选项 [1/2/3/4/q]: " choice

    case "$choice" in
        1)
            start_foreground
            ;;
        2)
            start_background
            ;;
        3)
            start_with_pm2
            ;;
        4)
            success "环境检查完成，未启动任何服务。"
            ;;
        q|Q)
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

main "$@"
