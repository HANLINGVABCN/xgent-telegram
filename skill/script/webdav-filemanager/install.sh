#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="webdav-filemanager"
APP_DIR="/opt/webdav-filemanager"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_PORT="8989"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
blue() { printf '\033[34m%s\033[0m\n' "$*"; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    yellow "需要 root 权限，正在尝试使用 sudo 重新执行..."
    exec sudo bash "$0" "$@"
  fi
}

ask() {
  local prompt="$1"
  local default_value="$2"
  local value=""
  if [[ -n "$default_value" ]]; then
    read -r -p "${prompt} [${default_value}]: " value
    printf '%s' "${value:-$default_value}"
  else
    read -r -p "${prompt}: " value
    printf '%s' "$value"
  fi
}

ask_password() {
  local prompt="$1"
  local value=""
  read -r -s -p "${prompt}: " value
  printf '\n' >&2
  printf '%s' "$value"
}

confirm() {
  local prompt="$1"
  local answer=""
  read -r -p "${prompt} [y/N]: " answer
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

validate_port() {
  local port="$1"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    red "端口号必须是 1-65535 的数字。"
    exit 1
  fi
}

shell_quote() {
  printf '%q' "$1"
}

print_header() {
  blue "========================================"
  blue " WebDAV 文件管理器 一键部署"
  blue "========================================"
  printf '\n'
}

# ----------------------------------------------------------
# Detect which process manager is currently managing the app
# Returns: "systemd", "pm2", or ""
# ----------------------------------------------------------
detect_manager() {
  # Check systemd first
  if command -v systemctl >/dev/null 2>&1; then
    if [[ -f "$SERVICE_FILE" ]] || systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
      printf 'systemd'
      return
    fi
  fi
  # Check pm2
  if command -v pm2 >/dev/null 2>&1; then
    if pm2 describe "$SERVICE_NAME" >/dev/null 2>&1; then
      printf 'pm2'
      return
    fi
  fi
  printf ''
}

# ----------------------------------------------------------
# Install
# ----------------------------------------------------------
install_app() {
  local src_dir
  src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if [[ ! -f "${src_dir}/server.py" || ! -f "${src_dir}/index.html" ]]; then
    red "未找到 server.py 或 index.html。请把 install.sh、server.py、index.html 放在同一目录后再运行。"
    exit 1
  fi

  command -v python3 >/dev/null 2>&1 || { red "未找到 python3，请先安装 Python 3。"; exit 1; }

  # Choose process manager
  local manager=""
  printf '请选择进程管理方式：\n'
  printf '  1) systemd（Linux 默认进程管理器）\n'
  printf '  2) pm2（推荐，Node.js 进程管理器，也可管理 Python 进程）\n\n'
  local mgr_choice=""
  read -r -p "请输入选项 [2]: " mgr_choice
  mgr_choice="${mgr_choice:-2}"

  case "$mgr_choice" in
    1)
      manager="systemd"
      command -v systemctl >/dev/null 2>&1 || { red "未找到 systemctl，本选项适用于 systemd 系统。请选择 pm2 或安装 systemd。"; exit 1; }
      ;;
    2)
      manager="pm2"
      command -v pm2 >/dev/null 2>&1 || { red "未找到 pm2，请先安装：npm install -g pm2"; exit 1; }
      ;;
    *)
      red "无效选项。"
      exit 1
      ;;
  esac

  local port username password root_dir
  port="$(ask "请输入端口号" "$DEFAULT_PORT")"
  validate_port "$port"

  username="$(ask "请输入登录账号" "admin")"
  if [[ -z "$username" ]]; then
    red "登录账号不能为空。"
    exit 1
  fi

  password="$(ask_password "请输入登录密码")"
  if [[ -z "$password" ]]; then
    red "登录密码不能为空。"
    exit 1
  fi

  root_dir="$(ask "请输入文件根目录" "/data/files")"
  if [[ -z "$root_dir" ]]; then
    red "文件根目录不能为空。"
    exit 1
  fi

  mkdir -p "$APP_DIR"
  mkdir -p "$root_dir"

  cp "${src_dir}/server.py" "$APP_DIR/server.py"
  cp "${src_dir}/index.html" "$APP_DIR/index.html"
  chmod 755 "$APP_DIR/server.py"
  # Security: restrict APP_DIR so other users cannot read credentials in start.sh
  chmod 700 "$APP_DIR"

  local python_bin
  python_bin="$(command -v python3)"

  cat > "$APP_DIR/start.sh" <<START_EOF
#!/usr/bin/env bash
set -Eeuo pipefail
exec $(shell_quote "$python_bin") $(shell_quote "$APP_DIR/server.py") -H 0.0.0.0 -p $(shell_quote "$port") -r $(shell_quote "$root_dir") -a $(shell_quote "${username}:${password}")
START_EOF
  chmod 700 "$APP_DIR/start.sh"

  # Register with chosen process manager
  if [[ "$manager" == "systemd" ]]; then
    _install_systemd
  else
    _install_pm2
  fi

  # Print success info
  local ip="服务器IP"
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -z "$ip" ]] && ip="服务器IP"
  fi

  printf '\n'
  green "部署完成！（进程管理：${manager}）"
  printf 'Web 页面：  http://%s:%s/\n' "$ip" "$port"
  printf 'WebDAV：    http://%s:%s/dav/\n' "$ip" "$port"
  printf '登录账号：  %s\n' "$username"
  printf '文件目录：  %s\n' "$root_dir"
  printf '\n'

  if [[ "$manager" == "systemd" ]]; then
    printf '查看状态：  systemctl status %s\n' "$SERVICE_NAME"
    printf '重启服务：  systemctl restart %s\n' "$SERVICE_NAME"
  else
    printf '查看状态：  pm2 show %s\n' "$SERVICE_NAME"
    printf '重启服务：  pm2 restart %s\n' "$SERVICE_NAME"
    printf '查看日志：  pm2 logs %s\n' "$SERVICE_NAME"
  fi
  printf '卸载程序：  sudo ./install.sh uninstall\n'
}

_install_systemd() {
  cat > "$SERVICE_FILE" <<SERVICE_EOF
[Unit]
Description=WebDAV File Manager
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/start.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE_EOF

  systemctl daemon-reload
  systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
  else
    systemctl enable --now "$SERVICE_NAME"
  fi
}

_install_pm2() {
  # Stop existing pm2 process if any
  pm2 delete "$SERVICE_NAME" >/dev/null 2>&1 || true

  pm2 start "$APP_DIR/start.sh" \
    --name "$SERVICE_NAME" \
    --interpreter bash \
    --cwd "$APP_DIR"

  pm2 save --force
  # Setup pm2 startup (auto-start on boot)
  # Security: avoid eval on pm2 output; run pm2 startup directly
  pm2 startup 2>/dev/null | grep -E '^sudo ' | head -n1 | bash >/dev/null 2>&1 || true
}

# ----------------------------------------------------------
# Uninstall
# ----------------------------------------------------------
uninstall_app() {
  local old_root=""
  if [[ -f "$APP_DIR/start.sh" ]]; then
    old_root="$(grep -oE ' -r ([^ ]+)' "$APP_DIR/start.sh" | head -n 1 | sed 's/^ -r //' || true)"
  fi

  local current_mgr
  current_mgr="$(detect_manager)"

  yellow "即将卸载 WebDAV 文件管理器。"
  if [[ -n "$current_mgr" ]]; then
    printf '当前进程管理方式：%s\n' "$current_mgr"
  fi
  printf '会删除：\n'
  if [[ "$current_mgr" == "systemd" ]]; then
    printf '  - %s\n' "$SERVICE_FILE"
  fi
  printf '  - %s\n' "$APP_DIR"
  if [[ "$current_mgr" == "pm2" ]]; then
    printf '  - pm2 进程 %s\n' "$SERVICE_NAME"
  fi
  printf '不会删除：你存入的文件根目录'
  if [[ -n "$old_root" ]]; then
    printf '（当前配置可能是 %s）' "$old_root"
  fi
  printf '\n\n'

  if ! confirm "确认卸载吗？"; then
    yellow "已取消卸载。"
    return 0
  fi

  # Clean up systemd
  if [[ "$current_mgr" == "systemd" ]] || [[ -f "$SERVICE_FILE" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
      systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
      rm -f "$SERVICE_FILE"
      systemctl daemon-reload
      systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
  fi

  # Clean up pm2
  if [[ "$current_mgr" == "pm2" ]]; then
    if command -v pm2 >/dev/null 2>&1; then
      pm2 delete "$SERVICE_NAME" >/dev/null 2>&1 || true
      pm2 save --force >/dev/null 2>&1 || true
    fi
  fi

  rm -rf "$APP_DIR"

  printf '\n'
  green "卸载完成。"
  printf '已删除服务配置和项目程序文件。\n'
  if [[ -n "$old_root" ]]; then
    printf '已保留文件根目录：%s\n' "$old_root"
  else
    printf '已保留你的文件根目录；脚本未删除任何存储目录。\n'
  fi
}

# ----------------------------------------------------------
# Menu
# ----------------------------------------------------------
show_menu() {
  print_header
  printf '请选择操作：\n'
  printf '  1) 安装 / 重新安装\n'
  printf '  2) 卸载：删除配置和项目文件，不删除存入的文件\n'
  printf '  3) 退出\n\n'

  local choice=""
  read -r -p "请输入选项 [1-3]: " choice
  case "${choice:-}" in
    1) install_app ;;
    2) uninstall_app ;;
    3) yellow "已退出。" ;;
    *) red "无效选项。"; exit 1 ;;
  esac
}

# ----------------------------------------------------------
# Main
# ----------------------------------------------------------
main() {
  need_root "$@"

  case "${1:-}" in
    install|--install) print_header; install_app ;;
    uninstall|--uninstall|remove|--remove) print_header; uninstall_app ;;
    "") show_menu ;;
    *) red "未知参数：$1"; printf '用法：sudo ./install.sh [install|uninstall]\n'; exit 1 ;;
  esac
}

main "$@"
