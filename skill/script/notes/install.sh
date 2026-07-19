#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="simple-notes"
APP_DIR="/opt/simple-notes"
CONFIG_FILE="${APP_DIR}/config.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_PORT="8899"
DEFAULT_DATA_DIR="${APP_DIR}/data"

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
  local prompt="$1" default_value="$2" value=""
  if [[ -n "$default_value" ]]; then
    read -r -p "${prompt} [${default_value}]: " value
    printf '%s' "${value:-$default_value}"
  else
    read -r -p "${prompt}: " value
    printf '%s' "$value"
  fi
}

ask_password() {
  local prompt="$1" value=""
  read -r -s -p "${prompt}: " value
  printf '\n' >&2
  printf '%s' "$value"
}

confirm() {
  local answer=""
  read -r -p "$1 [y/N]: " answer
  case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

validate_port() {
  local port="$1"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    red "端口号必须是 1-65535 的数字。"
    exit 1
  fi
}

validate_username() {
  local username="$1"
  if [[ -z "$username" || "$username" == *:* || "$username" =~ [[:cntrl:]] ]]; then
    red "登录账号不能为空，不能包含冒号或控制字符。"
    exit 1
  fi
}

shell_quote() { printf '%q' "$1"; }

quote_value() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//\$/\\\$}"
  s="${s//\`/\\\`}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  printf '"%s"' "$s"
}

print_header() {
  blue "========================================"
  blue " 私有便签 一键部署"
  blue "========================================"
  printf '\n'
}

systemd_unit_exists() {
  command -v systemctl >/dev/null 2>&1 || return 1
  [[ -e "$SERVICE_FILE" || -L "$SERVICE_FILE" ]] && return 0
  systemctl list-unit-files --no-legend "${SERVICE_NAME}.service" 2>/dev/null | awk '{print $1}' | grep -Fxq "${SERVICE_NAME}.service"
}

copy_program_files() {
  local src_dir="$1"
  cp "${src_dir}/server.py" "$APP_DIR/server.py.new"
  chmod 755 "$APP_DIR/server.py.new"
  mv -f "$APP_DIR/server.py.new" "$APP_DIR/server.py"
}

write_runtime_files() {
  local port="$1" data_dir="$2" username="$3" password="${4:-}" host="${5:-0.0.0.0}"
  local python_bin
  python_bin="$(command -v python3)"

  cat > "$CONFIG_FILE" <<CONFIG_EOF
NOTES_HOST=$(quote_value "$host")
NOTES_PORT=$(quote_value "$port")
NOTES_DATA_DIR=$(quote_value "$data_dir")
CONFIG_EOF
  chmod 600 "$CONFIG_FILE"

  cat > "$APP_DIR/start.sh" <<START_EOF
#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="\${SCRIPT_DIR}/config.env"

NOTES_HOST=$(shell_quote "$host")
NOTES_PORT=$(shell_quote "$port")
NOTES_DATA_DIR=$(shell_quote "$data_dir")

if [[ -f "\$CONFIG_FILE" ]]; then
  while IFS='=' read -r _cfg_key _cfg_value || [[ -n "\$_cfg_key" ]]; do
    case "\$_cfg_key" in
      NOTES_HOST|NOTES_PORT|NOTES_DATA_DIR)
        if [[ "\$_cfg_value" == \"*\" ]]; then
          eval "_cfg_resolved=\$_cfg_value"
        else
          _cfg_resolved="\$_cfg_value"
        fi
        ;;
    esac
    case "\$_cfg_key" in
      NOTES_HOST) NOTES_HOST="\$_cfg_resolved" ;;
      NOTES_PORT) NOTES_PORT="\$_cfg_resolved" ;;
      NOTES_DATA_DIR) NOTES_DATA_DIR="\$_cfg_resolved" ;;
    esac
  done < "\$CONFIG_FILE"
fi

exec $(shell_quote "$python_bin") $(shell_quote "$APP_DIR/server.py") -H "\$NOTES_HOST" -p "\$NOTES_PORT" -d "\$NOTES_DATA_DIR"
START_EOF
  chmod 700 "$APP_DIR/start.sh"

  if [[ -n "$password" ]]; then
    "$python_bin" "$APP_DIR/server.py" -d "$data_dir" -u "$username" -P "$password" --init-auth
  fi
}

RUNTIME_HOST=""
RUNTIME_PORT=""
RUNTIME_DATA_DIR=""

load_runtime_config() {
  RUNTIME_HOST="0.0.0.0"
  RUNTIME_PORT=""
  RUNTIME_DATA_DIR=""
  if [[ ! -f "$CONFIG_FILE" ]]; then
    return 0
  fi
  local _key _value _resolved
  while IFS='=' read -r _key _value || [[ -n "$_key" ]]; do
    case "$_key" in
      NOTES_HOST|NOTES_PORT|NOTES_DATA_DIR)
        if [[ "$_value" == \"*\" ]]; then
          eval "_resolved=$_value"
        else
          _resolved="$_value"
        fi
        ;;
      *) continue ;;
    esac
    case "$_key" in
      NOTES_HOST) RUNTIME_HOST="$_resolved" ;;
      NOTES_PORT) RUNTIME_PORT="$_resolved" ;;
      NOTES_DATA_DIR) RUNTIME_DATA_DIR="$_resolved" ;;
    esac
  done < "$CONFIG_FILE"
}

install_app() {
  local src_dir
  src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "${src_dir}/server.py" ]] || { red "未找到 server.py。"; exit 1; }
  command -v python3 >/dev/null 2>&1 || { red "未找到 python3，请先安装 Python 3。"; exit 1; }

  printf '请选择进程管理方式：\n'
  printf '  1) pm2（推荐）\n'
  printf '  2) systemd\n\n'
  local mgr_choice manager
  read -r -p "请输入选项 [1]: " mgr_choice
  mgr_choice="${mgr_choice:-1}"
  case "$mgr_choice" in
    1) manager="pm2"; command -v pm2 >/dev/null 2>&1 || { red "未找到 pm2，请先安装：npm install -g pm2"; exit 1; } ;;
    2) manager="systemd"; command -v systemctl >/dev/null 2>&1 || { red "未找到 systemctl。"; exit 1; } ;;
    *) red "无效选项。"; exit 1 ;;
  esac

  local port username password data_dir
  port="$(ask "请输入端口号" "$DEFAULT_PORT")"
  validate_port "$port"
  username="$(ask "请输入登录账号" "admin")"
  validate_username "$username"
  password="$(ask_password "请输入登录密码")"
  [[ -n "$password" ]] || { red "登录密码不能为空。"; exit 1; }
  data_dir="$(ask "请输入数据目录" "$DEFAULT_DATA_DIR")"
  [[ -n "$data_dir" ]] || { red "数据目录不能为空。"; exit 1; }

  mkdir -p "$APP_DIR" "$data_dir"
  chmod 700 "$APP_DIR" "$data_dir"
  copy_program_files "$src_dir"
  write_runtime_files "$port" "$data_dir" "$username" "$password" "0.0.0.0"

  if [[ "$manager" == "systemd" ]]; then
    install_systemd
  else
    install_pm2
  fi

  local ip="服务器IP"
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -n "$ip" ]] || ip="服务器IP"
  fi
  printf '\n'
  green "便签部署完成！（进程管理：${manager}）"
  printf 'Web 页面： http://%s:%s/\n' "$ip" "$port"
  printf '登录账号： %s\n' "$username"
  printf '数据目录： %s\n' "$data_dir"
}

update_app() {
  local src_dir
  src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "${src_dir}/server.py" ]] || { red "未找到 server.py。"; exit 1; }
  [[ -d "$APP_DIR" ]] || { red "未检测到安装目录：$APP_DIR"; exit 1; }
  command -v python3 >/dev/null 2>&1 || { red "未找到 python3。"; exit 1; }

  load_runtime_config
  copy_program_files "$src_dir"
  chmod 700 "$APP_DIR"
  if [[ -n "$RUNTIME_PORT" && -n "$RUNTIME_DATA_DIR" ]]; then
    write_runtime_files "$RUNTIME_PORT" "$RUNTIME_DATA_DIR" "" "" "$RUNTIME_HOST"
  fi
  restart_app
  green "更新完成，已保留账号密码和便签数据。"
}

install_systemd() {
  cat > "$SERVICE_FILE" <<SERVICE_EOF
[Unit]
Description=Simple Notes
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
  systemctl enable --now "$SERVICE_NAME"
}

install_pm2() {
  pm2 delete "$SERVICE_NAME" >/dev/null 2>&1 || true
  pm2 start "$APP_DIR/start.sh" --name "$SERVICE_NAME" --interpreter bash --cwd "$APP_DIR"
  pm2 save --force
  pm2 startup 2>/dev/null | grep -E '^sudo ' | head -n1 | bash >/dev/null 2>&1 || true
}

restart_app() {
  local restarted=0
  if systemd_unit_exists; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart "$SERVICE_NAME" >/dev/null 2>&1 && restarted=1
  fi
  if command -v pm2 >/dev/null 2>&1 && pm2 describe "$SERVICE_NAME" >/dev/null 2>&1; then
    pm2 restart "$SERVICE_NAME" --update-env >/dev/null 2>&1 && restarted=1
    pm2 save --force >/dev/null 2>&1 || true
  fi
  (( restarted == 1 )) || yellow "未检测到托管进程；程序文件已更新，请手动重启。"
}

uninstall_app() {
  load_runtime_config
  yellow "即将卸载私有便签。程序目录会删除，数据目录默认保留。"
  [[ -n "$RUNTIME_DATA_DIR" ]] && printf '当前数据目录：%s\n' "$RUNTIME_DATA_DIR"
  confirm "确认卸载吗？" || { yellow "已取消卸载。"; return 0; }
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if command -v pm2 >/dev/null 2>&1; then
    pm2 delete "$SERVICE_NAME" >/dev/null 2>&1 || true
    pm2 save --force >/dev/null 2>&1 || true
  fi
  if [[ "$APP_DIR" == /opt/* && "$APP_DIR" != "/opt" ]]; then
    rm -rf "$APP_DIR"
  fi
  green "卸载完成。"
  [[ -n "$RUNTIME_DATA_DIR" ]] && printf '已保留数据目录：%s\n' "$RUNTIME_DATA_DIR"
}

show_menu() {
  print_header
  printf '请选择操作：\n'
  printf '  1) 安装 / 重新安装\n'
  printf '  2) 更新程序：保留账号密码和便签数据\n'
  printf '  3) 卸载：删除服务和程序，保留数据目录\n'
  printf '  4) 退出\n\n'
  local choice=""
  read -r -p "请输入选项 [1-4]: " choice
  case "${choice:-}" in
    1) install_app ;;
    2) update_app ;;
    3) uninstall_app ;;
    4) yellow "已退出。" ;;
    *) red "无效选项。"; exit 1 ;;
  esac
}

main() {
  need_root "$@"
  case "${1:-menu}" in
    install) install_app ;;
    update) update_app ;;
    uninstall) uninstall_app ;;
    menu|"") show_menu ;;
    *) red "用法: $0 [install|update|uninstall]"; exit 1 ;;
  esac
}

main "$@"
