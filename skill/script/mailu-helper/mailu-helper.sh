#!/usr/bin/env bash
set -u

VERSION="0.1.0"
CONFIG_FILE="${MAILU_HELPER_CONFIG:-/root/.mailu-helper.conf}"

COMPOSE_DIR="${COMPOSE_DIR:-}"
COMPOSE_FILE="${COMPOSE_FILE:-}"
ENV_FILE="${ENV_FILE:-}"
MAILU_DATA_DIR="${MAILU_DATA_DIR:-}"
CERT_DIR="${CERT_DIR:-}"
POSTFIX_OVERRIDE_DIR="${POSTFIX_OVERRIDE_DIR:-}"
MAIL_DOMAIN="${MAIL_DOMAIN:-}"
MAIL_HOST="${MAIL_HOST:-}"
WEB_HTTP_PORT="${WEB_HTTP_PORT:-}"
CERT_UPDATE_SCRIPT="${CERT_UPDATE_SCRIPT:-}"
CERT_WATCHER_SERVICE="${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"

if [ "$(id -u 2>/dev/null || echo 1)" != "0" ] && [ "$CONFIG_FILE" = "/root/.mailu-helper.conf" ]; then
  CONFIG_FILE="${HOME:-.}/.mailu-helper.conf"
fi

if [ -t 1 ]; then
  RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"
  BLUE="$(printf '\033[34m')"
  BOLD="$(printf '\033[1m')"
  RESET="$(printf '\033[0m')"
else
  RED=""
  GREEN=""
  YELLOW=""
  BLUE=""
  BOLD=""
  RESET=""
fi

info() { printf '%s\n' "${BLUE}==>${RESET} $*"; }
ok() { printf '%s\n' "${GREEN}OK${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}WARN${RESET} $*"; }
fail() { printf '%s\n' "${RED}FAIL${RESET} $*"; }

strip_cr() {
  printf '%s' "${1%$'\r'}"
}

is_valid_domain() {
  local value="$1"
  [ -n "$value" ] || return 1
  case "$value" in
    *.*) ;;
    *) return 1 ;;
  esac
  printf '%s' "$value" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$'
}

pause() {
  printf '\n按 Enter 继续...'
  IFS= read -r _ || true
}

read_menu_choice() {
  local default="${1:-1}"
  local prompt="${2:-请选择}"
  local choice
  local display="$prompt"
  [ -n "$default" ] && display="$prompt（默认：$default）"
  printf '%s：' "$display" >&2
  if ! IFS= read -r choice; then
    printf '0'
    return 0
  fi
  choice="$(strip_cr "$choice")"
  if [ -z "$choice" ] && [ -n "$default" ]; then
    if [ -t 0 ]; then
      printf '\033[A\r\033[K%s：%s\n' "$display" "$default" >&2
    else
      printf '%s\n' "$default" >&2
    fi
  fi
  printf '%s' "${choice:-$default}"
}

read_with_prefill() {
  local prompt="$1"
  local default="${2:-}"
  local answer
  printf '%s' "$prompt" >&2
  IFS= read -r answer || answer=""
  answer="$(strip_cr "$answer")"
  if [ -z "$answer" ]; then
    if [ -n "$default" ]; then
      if [ -t 0 ]; then
        printf '\033[A\r\033[K%s%s\n' "$prompt" "$default" >&2
      else
        printf '%s\n' "$default" >&2
      fi
    fi
    printf '%s' "$default"
  else
    printf '%s' "$answer"
  fi
}

ask() {
  # ask "提示" "默认值"
  local prompt="$1"
  local default="${2:-}"
  if [ -n "$default" ]; then
    read_with_prefill "$prompt（默认：$default）：" "$default"
  else
    read_with_prefill "$prompt：" "$default"
  fi
}

ask_default() {
  local prompt="$1"
  local default="${2:-}"
  if [ -n "$default" ]; then
    read_with_prefill "$prompt（默认：$default）：" "$default"
  else
    read_with_prefill "$prompt：" "$default"
  fi
}

ask_labeled_default() {
  local prompt="$1"
  local default="${2:-}"
  local default_label="${3:-$default}"
  if [ -t 0 ]; then
    read_with_prefill "$prompt（默认：$default）：" "$default"
  else
    read_with_prefill "$prompt（默认：$default_label）：" "$default_label"
  fi
}

choose_option() {
  local title="$1"
  local default_value="$2"
  shift 2
  local items=("$@")
  local i choice label value default_index
  default_index=1
  i=1
  for item in "${items[@]}"; do
    label="${item%%|*}"
    value="${item#*|}"
    if [ "$value" = "$default_value" ]; then
      default_index="$i"
      break
    fi
    i=$((i + 1))
  done
  printf '%s\n' "$title" >&2
  i=1
  for item in "${items[@]}"; do
    label="${item%%|*}"
    printf '  %s. %s\n' "$i" "$label" >&2
    i=$((i + 1))
  done
  choice="$(read_with_prefill "请选择（默认：${default_index}）：" "$default_index")"
  if [ -z "$choice" ]; then
    printf '%s' "$default_value"
    return 0
  fi
  for item in "${items[@]}"; do
    label="${item%%|*}"
    value="${item#*|}"
    if [ "$choice" = "$label" ] || [ "$choice" = "$value" ]; then
      printf '%s' "$value"
      return 0
    fi
  done
  case "$choice" in
    默认|default) printf '%s' "$default_value"; return 0 ;;
  esac
  case "$choice" in
    *[!0-9]*|"") warn "无效选择，使用默认值：$default_value" >&2; printf '%s' "$default_value"; return 0 ;;
  esac
  if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#items[@]}" ]; then
    warn "无效选择，使用默认值：$default_value" >&2
    printf '%s' "$default_value"
    return 0
  fi
  value="${items[$((choice - 1))]#*|}"
  printf '%s' "$value"
}

choose_yes_no() {
  local title="$1"
  local default_value="${2:-no}"
  local default_label="n"
  local answer
  if [ "$default_value" = "yes" ]; then
    default_label="y"
  fi
  answer="$(read_with_prefill "$title（y/n，默认：${default_label}）：" "$default_label")"
  case "$answer" in
    y|Y|yes|YES|Yes|是) printf 'yes' ;;
    *) printf 'no' ;;
  esac
}

list_public_ipv4() {
  if need_cmd ip; then
    ip -o -4 addr show scope global 2>/dev/null |
      awk '{split($4, a, "/"); print a[1]}' |
      grep -Ev '^(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)' || true
  elif need_cmd hostname; then
    hostname -I 2>/dev/null |
      tr ' ' '\n' |
      grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' |
      grep -Ev '^(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)' || true
  fi
}

choose_mail_bind_ip() {
  local ips=()
  local ip items=()
  while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    ips+=("$ip")
  done < <(list_public_ipv4 | awk '!seen[$0]++')

    if [ "${#ips[@]}" -gt 0 ]; then
      for ip in "${ips[@]}"; do
        items+=("$ip|$ip")
      done
      items+=("全部网卡 0.0.0.0|0.0.0.0")
      items+=("手动输入公网 IP|manual")
      ip="$(choose_option "邮件端口绑定 IP" "${ips[0]}" "${items[@]}")"
      if [ "$ip" = "manual" ]; then
        ip="$(ask_default "邮件端口绑定 IP（填公网 IP；不确定可填 0.0.0.0；不要填 127.0.0.1）" "${ips[0]}")"
      fi
    printf '%s' "$ip"
    return 0
  fi

  warn "没有自动识别到公网 IPv4；可能是 NAT 机器、权限不足，或系统没有 ip/hostname 命令。可选全部网卡或手动输入公网 IP。" >&2
  choose_option "邮件端口绑定 IP" "0.0.0.0" \
    "全部网卡 0.0.0.0|0.0.0.0" \
    "手动输入公网 IP|manual" |
  {
    IFS= read -r ip
    if [ "$ip" = "manual" ]; then
      ask_default "邮件端口绑定 IP（填公网 IP；不确定可填 0.0.0.0；不要填 127.0.0.1）" "0.0.0.0"
    else
      printf '%s' "$ip"
    fi
  }
}

confirm() {
  local prompt="$1"
  local default="${2:-N}"
  local default_label="n"
  local answer
  if [ "$default" = "Y" ]; then
    default_label="y"
  fi
  answer="$(read_with_prefill "$prompt（y/n，默认：${default_label}）：" "$default_label")"
  case "$answer" in
    默认|default) answer="$default_label" ;;
  esac
  case "$answer" in
    y|Y|yes|YES|Yes|是) return 0 ;;
    *) return 1 ;;
  esac
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

safe_mkdir() {
  local dir="$1"
  if [ -z "$dir" ]; then
    fail "目录为空"
    return 1
  fi
  mkdir -p "$dir"
}

backup_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  local stamp dest n
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$file.bak.$stamp"
  n=1
  while [ -e "$dest" ]; do
    dest="$file.bak.$stamp.$n"
    n=$((n + 1))
  done
  cp -a "$file" "$dest"
  ok "已备份：$dest"
}

load_config() {
  if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
  fi
  sanitize_loaded_config
}

bad_saved_path() {
  local value="${1:-}"
  case "$value" in
    ""|"0"|[0-9]|[0-9][0-9]|[0-9]/*|[0-9][0-9]/*) return 0 ;;
    *) return 1 ;;
  esac
}

sanitize_loaded_config() {
  bad_saved_path "${COMPOSE_DIR:-}" && COMPOSE_DIR=""
  bad_saved_path "${COMPOSE_FILE:-}" && COMPOSE_FILE=""
  bad_saved_path "${ENV_FILE:-}" && ENV_FILE=""
  bad_saved_path "${MAILU_DATA_DIR:-}" && MAILU_DATA_DIR=""
  bad_saved_path "${CERT_DIR:-}" && CERT_DIR=""
  bad_saved_path "${POSTFIX_OVERRIDE_DIR:-}" && POSTFIX_OVERRIDE_DIR=""
  is_valid_domain "${MAIL_DOMAIN:-}" || MAIL_DOMAIN=""
  is_valid_domain "${MAIL_HOST:-}" || MAIL_HOST=""
}

save_config() {
  local dir
  dir="$(dirname "$CONFIG_FILE")"
  mkdir -p "$dir"
  {
    printf '# Generated by mailu-helper.sh\n'
    printf 'COMPOSE_DIR=%q\n' "${COMPOSE_DIR:-}"
    printf 'COMPOSE_FILE=%q\n' "${COMPOSE_FILE:-}"
    printf 'ENV_FILE=%q\n' "${ENV_FILE:-}"
    printf 'MAILU_DATA_DIR=%q\n' "${MAILU_DATA_DIR:-}"
    printf 'CERT_DIR=%q\n' "${CERT_DIR:-}"
    printf 'POSTFIX_OVERRIDE_DIR=%q\n' "${POSTFIX_OVERRIDE_DIR:-}"
    printf 'MAIL_DOMAIN=%q\n' "${MAIL_DOMAIN:-}"
    printf 'MAIL_HOST=%q\n' "${MAIL_HOST:-}"
    printf 'WEB_HTTP_PORT=%q\n' "${WEB_HTTP_PORT:-}"
    printf 'CERT_UPDATE_SCRIPT=%q\n' "${CERT_UPDATE_SCRIPT:-}"
    printf 'CERT_WATCHER_SERVICE=%q\n' "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"
  } > "$CONFIG_FILE"
  ok "配置已保存：$CONFIG_FILE"
}

compose_base() {
  if [ -n "${COMPOSE_FILE:-}" ]; then
    dirname "$COMPOSE_FILE"
  elif [ -n "${COMPOSE_DIR:-}" ]; then
    printf '%s\n' "$COMPOSE_DIR"
  else
    pwd
  fi
}

dc() {
  if [ -z "${COMPOSE_FILE:-}" ]; then
    fail "未识别 docker-compose.yml / compose.yml"
    return 1
  fi
  if [ -n "${ENV_FILE:-}" ]; then
    (cd "$(dirname "$COMPOSE_FILE")" && docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@")
  else
    (cd "$(dirname "$COMPOSE_FILE")" && docker compose -f "$COMPOSE_FILE" "$@")
  fi
}

file_contains_mailu() {
  local file="$1"
  grep -Eq 'ghcr\.io/mailu|mailu\.env|env_file:[[:space:]]*\.env|DOCKER_ORG.*mailu|MAILU_VERSION' "$file" 2>/dev/null
}

select_from_list() {
  local title="$1"
  shift
  local items=("$@")
  local i choice
  if [ "${#items[@]}" -eq 0 ]; then
    return 1
  fi
  printf '\n%s\n' "$title" >&2
  i=1
  for item in "${items[@]}"; do
    printf '  %s) %s\n' "$i" "$item" >&2
    i=$((i + 1))
  done
  printf '请选择编号，直接回车选 1：' >&2
  IFS= read -r choice || choice=1
  choice="${choice:-1}"
  case "$choice" in
    ''|*[!0-9]*) return 1 ;;
  esac
  if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#items[@]}" ]; then
    return 1
  fi
  printf '%s' "${items[$((choice - 1))]}"
}

detect_compose_dir() {
  if [ -n "${COMPOSE_FILE:-}" ] && [ -f "$COMPOSE_FILE" ]; then
    COMPOSE_DIR="$(dirname "$COMPOSE_FILE")"
    return 0
  fi

  local candidates=()
  local file
  for file in \
    "./docker-compose.yml" "./compose.yml" "./docker-compose.yaml" "./compose.yaml" \
    ./*compose*.yml ./*compose*.yaml; do
    [ -f "$file" ] || continue
    if file_contains_mailu "$file"; then
      candidates+=("$(cd "$(dirname "$file")" && pwd)/$(basename "$file")")
    fi
  done

  if [ "${#candidates[@]}" -eq 0 ]; then
    while IFS= read -r file; do
      [ -f "$file" ] || continue
      if file_contains_mailu "$file"; then
        candidates+=("$file")
      fi
    done < <(find /opt /root /home -maxdepth 5 \( -name 'docker-compose.yml' -o -name 'compose.yml' -o -name 'docker-compose.yaml' -o -name 'compose.yaml' \) 2>/dev/null)
  fi

  if [ "${#candidates[@]}" -gt 0 ]; then
    COMPOSE_FILE="$(select_from_list "发现可能的 Mailu compose 文件：" "${candidates[@]}")" || return 1
    COMPOSE_DIR="$(dirname "$COMPOSE_FILE")"
    ok "Compose 文件：$COMPOSE_FILE"
    return 0
  fi

  COMPOSE_FILE="$(ask "请输入 docker-compose.yml / compose.yml 完整路径" "${COMPOSE_FILE:-}")"
  if [ ! -f "$COMPOSE_FILE" ]; then
    fail "文件不存在：$COMPOSE_FILE"
    return 1
  fi
  COMPOSE_DIR="$(dirname "$COMPOSE_FILE")"
}

detect_env_file() {
  if [ -n "${ENV_FILE:-}" ] && [ -f "$ENV_FILE" ]; then
    return 0
  fi
  local base
  base="$(compose_base)"
  if [ -f "$base/.env" ]; then
    ENV_FILE="$base/.env"
    return 0
  fi
  if [ -f "$base/mailu.env" ]; then
    ENV_FILE="$base/mailu.env"
    return 0
  fi
  local candidates=()
  local file
  for file in "$base"/*.env ./*.env; do
    [ -f "$file" ] || continue
    if grep -Eq '^DOMAIN=|^HOSTNAMES=|^TLS_FLAVOR=' "$file" 2>/dev/null; then
      candidates+=("$(cd "$(dirname "$file")" && pwd)/$(basename "$file")")
    fi
  done
  if [ "${#candidates[@]}" -gt 0 ]; then
    ENV_FILE="$(select_from_list "发现可能的 Mailu env 文件：" "${candidates[@]}")" || return 1
    ok "Env 文件：$ENV_FILE"
    return 0
  fi
  ENV_FILE="$(ask "请输入 .env 完整路径" "${ENV_FILE:-}")"
  [ -f "$ENV_FILE" ] || { fail "文件不存在：$ENV_FILE"; return 1; }
}

service_block() {
  local service="$1"
  awk -v svc="$service" '
    $0 ~ "^[[:space:]]{2}" svc ":" { inside=1; next }
    inside && $0 ~ "^[[:space:]]{2}[A-Za-z0-9_-]+:" { exit }
    inside { print }
  ' "$COMPOSE_FILE"
}

extract_volume_host_from_text() {
  local target="$1"
  awk -v target="$target" '
    /^[[:space:]]*-[[:space:]]*/ {
      line=$0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      gsub(/^["'\''"]|["'\''"]$/, "", line)
      n=split(line, parts, ":")
      if (n >= 2 && parts[2] == target) {
        print parts[1]
        exit
      }
    }
  '
}

extract_volume_host() {
  local target="$1"
  extract_volume_host_from_text "$target" < "$COMPOSE_FILE"
}

detect_mailu_dirs() {
  [ -n "${COMPOSE_FILE:-}" ] || return 1
  local cert postfix data mail
  cert="$(extract_volume_host "/certs" || true)"
  postfix="$(service_block smtp | extract_volume_host_from_text "/overrides" || true)"
  data="$(extract_volume_host "/data" || true)"
  mail="$(extract_volume_host "/mail" || true)"

  [ -n "$cert" ] && CERT_DIR="$cert"
  [ -n "$postfix" ] && POSTFIX_OVERRIDE_DIR="$postfix"

  if [ -n "$data" ]; then
    MAILU_DATA_DIR="$(dirname "$data")"
  elif [ -n "$mail" ]; then
    MAILU_DATA_DIR="$(dirname "$mail")"
  elif [ -n "$cert" ]; then
    MAILU_DATA_DIR="$(dirname "$cert")"
  fi

  if [ -z "${CERT_DIR:-}" ]; then
    CERT_DIR="$(ask "请输入 Mailu 证书目录（容器 /certs 对应宿主机目录）" "${CERT_DIR:-/mailu/certs}")"
  fi
  if [ -z "${POSTFIX_OVERRIDE_DIR:-}" ]; then
    POSTFIX_OVERRIDE_DIR="$(ask "请输入 Mailu Postfix overrides 目录（smtp 服务 /overrides 对应宿主机目录）" "${POSTFIX_OVERRIDE_DIR:-/mailu/overrides/postfix}")"
  fi
  if [ -z "${MAILU_DATA_DIR:-}" ]; then
    MAILU_DATA_DIR="$(ask "请输入 Mailu 数据根目录" "${MAILU_DATA_DIR:-/mailu}")"
  fi
}

env_get() {
  local key="$1"
  [ -f "${ENV_FILE:-}" ] || return 1
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-
}

env_set() {
  local key="$1"
  local value="$2"
  [ -f "${ENV_FILE:-}" ] || return 1
  backup_file "$ENV_FILE"
  if grep -Eq "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  ok "已设置 $key=$value"
}

load_env_defaults() {
  if [ -f "${ENV_FILE:-}" ]; then
    MAIL_DOMAIN="${MAIL_DOMAIN:-$(env_get DOMAIN || true)}"
    MAIL_HOST="${MAIL_HOST:-$(env_get HOSTNAMES || true)}"
    MAIL_HOST="${MAIL_HOST%%,*}"
  fi
}

front_ports() {
  service_block front | awk '
    /^[[:space:]]*ports:/ { in_ports=1; next }
    in_ports && /^[[:space:]]*[A-Za-z0-9_-]+:/ { exit }
    in_ports && /^[[:space:]]*-[[:space:]]*/ {
      line=$0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      gsub(/^["'\''"]|["'\''"]$/, "", line)
      print line
    }
  '
}

host_port_for_container_port() {
  local container_port="$1"
  local line clean n last prev
  while IFS= read -r line; do
    clean="${line%%/*}"
    IFS=':' read -r -a parts <<< "$clean"
    n="${#parts[@]}"
    [ "$n" -eq 0 ] && continue
    last="${parts[$((n - 1))]}"
    if [ "$last" = "$container_port" ]; then
      if [ "$n" -ge 2 ]; then
        prev="${parts[$((n - 2))]}"
        printf '%s\n' "$prev"
      else
        printf '%s\n' "$container_port"
      fi
      return 0
    fi
  done < <(front_ports)
  return 1
}

host_bind_for_container_port() {
  local container_port="$1"
  local line clean n last
  while IFS= read -r line; do
    clean="${line%%/*}"
    IFS=':' read -r -a parts <<< "$clean"
    n="${#parts[@]}"
    [ "$n" -eq 0 ] && continue
    last="${parts[$((n - 1))]}"
    if [ "$last" = "$container_port" ]; then
      if [ "$n" -ge 3 ]; then
        printf '%s\n' "${parts[0]}"
      else
        printf '%s\n' "0.0.0.0"
      fi
      return 0
    fi
  done < <(front_ports)
  return 1
}

ensure_context() {
  load_config
  detect_compose_dir || return 1
  detect_env_file || return 1
  detect_mailu_dirs || return 1
  load_env_defaults
  WEB_HTTP_PORT="${WEB_HTTP_PORT:-$(host_port_for_container_port 80 || true)}"
  save_config
}

check_env_core() {
  [ -f "${ENV_FILE:-}" ] || { fail ".env 不存在"; return 1; }
  local keys=(DOMAIN HOSTNAMES TLS_FLAVOR WEB_ADMIN WEB_WEBMAIL WEBSITE)
  local key value
  printf '\n%s\n' "${BOLD}.env 关键项${RESET}"
  for key in "${keys[@]}"; do
    value="$(env_get "$key" || true)"
    if [ -n "$value" ]; then
      ok "$key=$value"
    else
      warn "$key 未设置"
    fi
  done

  local tls
  tls="$(env_get TLS_FLAVOR || true)"
  case "$tls" in
    mail) ok "TLS_FLAVOR=mail，适合宿主机反代 Web、Mailu 自管邮件 TLS" ;;
    cert|letsencrypt|notls|mail-letsencrypt|"")
      warn "当前 TLS_FLAVOR=$tls。宿主机反向代理 Web 的场景，通常建议 TLS_FLAVOR=mail。"
      if confirm "是否自动修改为 TLS_FLAVOR=mail？"; then
        env_set TLS_FLAVOR mail
      fi
      ;;
    *) warn "未知 TLS_FLAVOR=$tls，请确认是否符合你的部署方式" ;;
  esac
}

check_compose_ports() {
  [ -f "${COMPOSE_FILE:-}" ] || { fail "compose 文件不存在"; return 1; }
  printf '\n%s\n' "${BOLD}front 服务端口检查${RESET}"
  local web_port web_bind bind port
  web_port="$(host_port_for_container_port 80 || true)"
  web_bind="$(host_bind_for_container_port 80 || true)"
  if [ -n "$web_port" ]; then
    WEB_HTTP_PORT="$web_port"
    if [ "$web_bind" = "127.0.0.1" ] || [ "$web_bind" = "localhost" ]; then
      ok "Web HTTP：$web_bind:$web_port -> 80"
    else
      warn "Web HTTP 当前绑定 $web_bind:$web_port。宿主机反代场景建议绑定 127.0.0.1。"
    fi
  else
    warn "未发现 front 的 80 端口映射，宿主机反代可能无法访问 Mailu Web。"
  fi

  if host_port_for_container_port 443 >/dev/null 2>&1; then
    warn "发现 front 的 443 端口映射。宿主机反代场景通常不需要暴露 Mailu Web HTTPS。"
  else
    ok "未暴露 Mailu Web HTTPS 443"
  fi

  for port in 25 465 587 993; do
    bind="$(host_bind_for_container_port "$port" || true)"
    if [ -z "$bind" ]; then
      warn "未发现邮件端口 $port 映射"
    elif [ "$bind" = "127.0.0.1" ] || [ "$bind" = "localhost" ]; then
      fail "邮件端口 $port 绑定到了 $bind，外部服务器/客户端无法连接。"
    else
      ok "邮件端口 $port 绑定：$bind"
    fi
  done
}

random_secret() {
  if need_cmd openssl; then
    openssl rand -hex 16
  elif need_cmd sha256sum; then
    printf '%s-%s\n' "$(date +%s)" "$RANDOM" | sha256sum | cut -c1-32
  else
    printf '%s%s%s%s\n' "$(date +%s)" "$RANDOM" "$RANDOM" "$RANDOM" | cut -c1-32
  fi
}

write_generated_env() {
  local env_path="$1"
  cat > "$env_path" <<EOF
# Generated by mailu-helper.sh

###################################
# Common configuration variables
###################################

SECRET_KEY=$GEN_SECRET_KEY
SUBNET=$GEN_SUBNET
DOMAIN=$GEN_DOMAIN
HOSTNAMES=$GEN_HOSTNAMES
POSTMASTER=$GEN_POSTMASTER
TLS_FLAVOR=$GEN_TLS_FLAVOR
AUTH_RATELIMIT_IP=$GEN_AUTH_RATELIMIT_IP
AUTH_RATELIMIT_USER=$GEN_AUTH_RATELIMIT_USER
DISABLE_STATISTICS=True

###################################
# Optional features
###################################

ADMIN=$GEN_ADMIN
WEBMAIL=$GEN_WEBMAIL
API=$GEN_API
WEBDAV=$GEN_WEBDAV
ANTIVIRUS=none
SCAN_MACROS=true

###################################
# Mail settings
###################################

MESSAGE_SIZE_LIMIT=$GEN_MESSAGE_SIZE_LIMIT
MESSAGE_RATELIMIT=$GEN_MESSAGE_RATELIMIT
RELAYNETS=
RELAYHOST=
FETCHMAIL_ENABLED=False
FETCHMAIL_DELAY=600
RECIPIENT_DELIMITER=+
DMARC_RUA=$GEN_DMARC_RUA
DMARC_RUF=$GEN_DMARC_RUF
WELCOME=false
WELCOME_SUBJECT=Welcome to your new email account
WELCOME_BODY=Welcome to your new email account, if you can read this, then it is configured properly!
COMPRESSION=
COMPRESSION_LEVEL=
FULL_TEXT_SEARCH=$GEN_FULL_TEXT_SEARCH

###################################
# Web settings
###################################

WEBROOT_REDIRECT=$GEN_WEBROOT_REDIRECT
WEB_ADMIN=$GEN_WEB_ADMIN
WEB_WEBMAIL=$GEN_WEB_WEBMAIL
WEB_API=$GEN_WEB_API
SITENAME=$GEN_SITENAME
WEBSITE=$GEN_WEBSITE

###################################
# Advanced settings
###################################

COMPOSE_PROJECT_NAME=$GEN_COMPOSE_PROJECT_NAME
CREDENTIAL_ROUNDS=12
REAL_IP_HEADER=
REAL_IP_FROM=
REJECT_UNLISTED_RECIPIENT=
LOG_LEVEL=INFO
TZ=$GEN_TZ
DEFAULT_SPAM_THRESHOLD=80
API_TOKEN=
FULL_TEXT_SEARCH_ATTACHMENTS=
EOF
}

write_generated_compose() {
  local compose_path="$1"
  local webmail_override="snappymail"
  [ "$GEN_WEBMAIL" = "roundcube" ] && webmail_override="roundcube"

  cat > "$compose_path" <<EOF
# Generated by mailu-helper.sh
services:
  redis:
    image: redis:alpine
    restart: always
    volumes:
      - "$GEN_DATA_ROOT/redis:/data"
    depends_on:
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  front:
    image: ghcr.io/mailu/nginx:$GEN_MAILU_VERSION
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-front
    ports:
      - "$GEN_WEB_BIND_IP:$GEN_WEB_HOST_HTTP_PORT:$GEN_WEB_CONTAINER_HTTP_PORT"
EOF

  if [ "$GEN_EXPOSE_WEB_HTTPS" = "yes" ]; then
    cat >> "$compose_path" <<EOF
      - "$GEN_WEB_HTTPS_BIND_IP:$GEN_WEB_HOST_HTTPS_PORT:$GEN_WEB_CONTAINER_HTTPS_PORT"
EOF
  fi

  cat >> "$compose_path" <<EOF
      - "$GEN_MAIL_BIND_IP:$GEN_SMTP_HOST_PORT:$GEN_SMTP_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_SMTPS_HOST_PORT:$GEN_SMTPS_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_SUBMISSION_HOST_PORT:$GEN_SUBMISSION_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_IMAPS_HOST_PORT:$GEN_IMAPS_CONTAINER_PORT"
EOF

  if [ "$GEN_EXPOSE_LEGACY_MAIL_PORTS" = "yes" ]; then
    cat >> "$compose_path" <<EOF
      - "$GEN_MAIL_BIND_IP:$GEN_POP3_HOST_PORT:$GEN_POP3_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_POP3S_HOST_PORT:$GEN_POP3S_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_IMAP_HOST_PORT:$GEN_IMAP_CONTAINER_PORT"
      - "$GEN_MAIL_BIND_IP:$GEN_SIEVE_HOST_PORT:$GEN_SIEVE_CONTAINER_PORT"
EOF
  fi

  cat >> "$compose_path" <<EOF
    networks:
      - default
      - webmail
      - radicale
    volumes:
      - "$GEN_DATA_ROOT/certs:/certs"
      - "$GEN_DATA_ROOT/overrides/nginx:/overrides:ro"
    depends_on:
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  resolver:
    image: ghcr.io/mailu/unbound:$GEN_MAILU_VERSION
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-resolver
    restart: always
    networks:
      default:
        ipv4_address: $GEN_RESOLVER_IP

  admin:
    image: ghcr.io/mailu/admin:$GEN_MAILU_VERSION
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-admin
    volumes:
      - "$GEN_DATA_ROOT/data:/data"
      - "$GEN_DATA_ROOT/dkim:/dkim"
    depends_on:
      - redis
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  imap:
    image: ghcr.io/mailu/dovecot:$GEN_MAILU_VERSION
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-imap
    volumes:
      - "$GEN_DATA_ROOT/mail:/mail"
      - "$GEN_DATA_ROOT/overrides/dovecot:/overrides:ro"
    networks:
      - default
    depends_on:
      - front
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  smtp:
    image: ghcr.io/mailu/postfix:$GEN_MAILU_VERSION
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-smtp
    volumes:
      - "$GEN_DATA_ROOT/mailqueue:/queue"
      - "$GEN_DATA_ROOT/overrides/postfix:/overrides:ro"
    depends_on:
      - front
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  oletools:
    image: ghcr.io/mailu/oletools:$GEN_MAILU_VERSION
    hostname: oletools
    logging:
      driver: journald
      options:
        tag: mailu-oletools
    restart: always
    networks:
      - oletools
    depends_on:
      - resolver
    dns:
      - $GEN_RESOLVER_IP

  antispam:
    image: ghcr.io/mailu/rspamd:$GEN_MAILU_VERSION
    hostname: antispam
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-antispam
    networks:
      - default
      - oletools
    volumes:
      - "$GEN_DATA_ROOT/filter:/var/lib/rspamd"
      - "$GEN_DATA_ROOT/overrides/rspamd:/overrides:ro"
    depends_on:
      - front
      - redis
      - oletools
      - resolver
    dns:
      - $GEN_RESOLVER_IP
EOF

  if [ "$GEN_WEBDAV" = "radicale" ]; then
    cat >> "$compose_path" <<EOF

  webdav:
    image: ghcr.io/mailu/radicale:$GEN_MAILU_VERSION
    restart: always
    logging:
      driver: journald
      options:
        tag: mailu-webdav
    volumes:
      - "$GEN_DATA_ROOT/dav:/data"
    networks:
      - radicale
EOF
  fi

  if [ "$GEN_WEBMAIL" != "none" ]; then
    cat >> "$compose_path" <<EOF

  webmail:
    image: ghcr.io/mailu/webmail:$GEN_MAILU_VERSION
    restart: always
    env_file: .env
    logging:
      driver: journald
      options:
        tag: mailu-webmail
    volumes:
      - "$GEN_DATA_ROOT/webmail:/data"
      - "$GEN_DATA_ROOT/overrides/$webmail_override:/overrides:ro"
    networks:
      - webmail
    depends_on:
      - front
EOF
  fi

  cat >> "$compose_path" <<EOF

networks:
  default:
    driver: bridge
    ipam:
      driver: default
      config:
        - subnet: $GEN_SUBNET
  radicale:
    driver: bridge
  webmail:
    driver: bridge
  oletools:
    driver: bridge
    internal: true
EOF
}

set_generated_config_defaults() {
  local default_dir="$1"
  local default_domain="$2"
  local default_host="$3"
  local mail_ip

  mail_ip="$(list_public_ipv4 | awk '!seen[$0]++ {print; exit}')"
  [ -n "$mail_ip" ] || mail_ip="0.0.0.0"

  COMPOSE_DIR="$default_dir"
  COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
  ENV_FILE="$COMPOSE_DIR/.env"

  GEN_DOMAIN="$default_domain"
  GEN_HOSTNAMES="$default_host"
  GEN_POSTMASTER="admin"
  GEN_MAILU_VERSION="2024.06"
  GEN_COMPOSE_PROJECT_NAME="mailu"
  GEN_DATA_ROOT="${MAILU_DATA_DIR:-/mailu}"
  GEN_SUBNET="192.168.203.0/24"
  GEN_RESOLVER_IP="192.168.203.254"
  GEN_TZ="Etc/UTC"

  GEN_WEB_BIND_IP="127.0.0.1"
  GEN_WEB_HOST_HTTP_PORT="${WEB_HTTP_PORT:-13613}"
  GEN_WEB_CONTAINER_HTTP_PORT="80"
  GEN_EXPOSE_WEB_HTTPS="no"
  GEN_WEB_HTTPS_BIND_IP="127.0.0.1"
  GEN_WEB_HOST_HTTPS_PORT="443"
  GEN_WEB_CONTAINER_HTTPS_PORT="443"

  GEN_MAIL_BIND_IP="$mail_ip"
  GEN_SMTP_HOST_PORT="25"
  GEN_SMTP_CONTAINER_PORT="25"
  GEN_SMTPS_HOST_PORT="465"
  GEN_SMTPS_CONTAINER_PORT="465"
  GEN_SUBMISSION_HOST_PORT="587"
  GEN_SUBMISSION_CONTAINER_PORT="587"
  GEN_IMAPS_HOST_PORT="993"
  GEN_IMAPS_CONTAINER_PORT="993"
  GEN_EXPOSE_LEGACY_MAIL_PORTS="no"
  GEN_POP3_HOST_PORT="110"
  GEN_POP3_CONTAINER_PORT="110"
  GEN_POP3S_HOST_PORT="995"
  GEN_POP3S_CONTAINER_PORT="995"
  GEN_IMAP_HOST_PORT="143"
  GEN_IMAP_CONTAINER_PORT="143"
  GEN_SIEVE_HOST_PORT="4190"
  GEN_SIEVE_CONTAINER_PORT="4190"

  GEN_TLS_FLAVOR="mail"
  GEN_ADMIN="true"
  GEN_WEBMAIL="snappymail"
  GEN_WEBDAV="radicale"
  GEN_API="false"
  GEN_WEBROOT_REDIRECT="/webmail"
  GEN_WEB_ADMIN="/admin"
  GEN_WEB_WEBMAIL="/webmail"
  GEN_WEB_API="/api"
  GEN_WEBSITE="https://${GEN_HOSTNAMES%%,*}"
  GEN_SITENAME="Mailu of $GEN_DOMAIN"
  GEN_AUTH_RATELIMIT_IP="5/hour"
  GEN_AUTH_RATELIMIT_USER="50/day"
  GEN_MESSAGE_SIZE_LIMIT="50000000"
  GEN_MESSAGE_RATELIMIT="200/day"
  GEN_DMARC_RUA="$GEN_POSTMASTER"
  GEN_DMARC_RUF="$GEN_POSTMASTER"
  GEN_FULL_TEXT_SEARCH="en"
  GEN_SECRET_KEY="$(random_secret)"
}

print_generated_defaults_summary() {
  local default_dir="$1"
  local default_domain="$2"
  local default_host="$3"
  local mail_ip
  mail_ip="$(list_public_ipv4 | awk '!seen[$0]++ {print; exit}')"
  [ -n "$mail_ip" ] || mail_ip="0.0.0.0"
  printf '\n默认配置：目录=%s，域名=%s，主机=%s，Web=127.0.0.1:%s，邮件=%s，TLS=mail，后台开，API关。\n' \
    "$default_dir" "$default_domain" "$default_host" "${WEB_HTTP_PORT:-13613}" "$mail_ip"
}

generate_mailu_config() {
  load_config
  printf '\n%s\n' "${BOLD}生成 Mailu docker-compose.yml 和 .env${RESET}"
  printf '提示：不确定就用默认值，之后也可以再改。\n\n'

  local default_dir default_domain default_host
  local default_all
  default_dir="${COMPOSE_DIR:-$(pwd)}"
  if is_valid_domain "${MAIL_DOMAIN:-}"; then
    default_domain="$MAIL_DOMAIN"
  else
    default_domain="example.com"
  fi
  if is_valid_domain "${MAIL_HOST:-}"; then
    default_host="$MAIL_HOST"
  else
    default_host="mail.$default_domain"
  fi

  print_generated_defaults_summary "$default_dir" "$default_domain" "$default_host"
  default_all="Y"

  if confirm "是否按上面的默认值自动填写全部配置？" "$default_all"; then
    set_generated_config_defaults "$default_dir" "$default_domain" "$default_host"
  else
    COMPOSE_DIR="$(ask "Compose 保存目录" "$default_dir")"
    COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
    ENV_FILE="$COMPOSE_DIR/.env"

    GEN_DOMAIN="$(ask "主域名 DOMAIN" "$default_domain")"
    GEN_HOSTNAMES="$(ask "邮件主机名 HOSTNAMES" "${default_host}")"
    GEN_POSTMASTER="$(ask "管理员用户名" "admin")"
    GEN_MAILU_VERSION="$(ask "Mailu 版本" "2024.06")"
    GEN_COMPOSE_PROJECT_NAME="$(ask "Compose 项目名" "mailu")"
    GEN_DATA_ROOT="$(ask "Mailu 数据目录" "${MAILU_DATA_DIR:-/mailu}")"
    GEN_SUBNET="$(ask "Docker 子网 CIDR" "192.168.203.0/24")"
    GEN_RESOLVER_IP="$(ask "Resolver IP" "192.168.203.254")"
    GEN_TZ="$(ask "时区 TZ" "Etc/UTC")"

    GEN_WEB_BIND_IP="$(ask "Web 绑定 IP" "127.0.0.1")"
    GEN_WEB_HOST_HTTP_PORT="$(ask "Web 宿主机端口" "${WEB_HTTP_PORT:-13613}")"
    GEN_WEB_CONTAINER_HTTP_PORT="$(ask "Web 容器端口" "80")"
    if [ "$(choose_yes_no "映射 Web HTTPS 端口" "no")" = "yes" ]; then
      GEN_EXPOSE_WEB_HTTPS="yes"
      GEN_WEB_HTTPS_BIND_IP="$(ask "Web HTTPS 绑定 IP" "127.0.0.1")"
      GEN_WEB_HOST_HTTPS_PORT="$(ask "Web HTTPS 宿主机端口" "443")"
      GEN_WEB_CONTAINER_HTTPS_PORT="$(ask "Web HTTPS 容器端口" "443")"
    else
      GEN_EXPOSE_WEB_HTTPS="no"
      GEN_WEB_HTTPS_BIND_IP="127.0.0.1"
      GEN_WEB_HOST_HTTPS_PORT="443"
      GEN_WEB_CONTAINER_HTTPS_PORT="443"
    fi

    GEN_MAIL_BIND_IP="$(choose_mail_bind_ip)"
    GEN_SMTP_HOST_PORT="$(ask "SMTP 宿主机端口" "25")"
    GEN_SMTP_CONTAINER_PORT="$(ask "SMTP 容器端口" "25")"
    GEN_SMTPS_HOST_PORT="$(ask "SMTPS 宿主机端口" "465")"
    GEN_SMTPS_CONTAINER_PORT="$(ask "SMTPS 容器端口" "465")"
    GEN_SUBMISSION_HOST_PORT="$(ask "Submission 宿主机端口" "587")"
    GEN_SUBMISSION_CONTAINER_PORT="$(ask "Submission 容器端口" "587")"
    GEN_IMAPS_HOST_PORT="$(ask "IMAPS 宿主机端口" "993")"
    GEN_IMAPS_CONTAINER_PORT="$(ask "IMAPS 容器端口" "993")"

    if [ "$(choose_yes_no "额外映射 POP3/IMAP/Sieve" "no")" = "yes" ]; then
      GEN_EXPOSE_LEGACY_MAIL_PORTS="yes"
      GEN_POP3_HOST_PORT="$(ask "POP3 宿主机端口" "110")"
      GEN_POP3_CONTAINER_PORT="$(ask "POP3 容器端口" "110")"
      GEN_POP3S_HOST_PORT="$(ask "POP3S 宿主机端口" "995")"
      GEN_POP3S_CONTAINER_PORT="$(ask "POP3S 容器端口" "995")"
      GEN_IMAP_HOST_PORT="$(ask "IMAP 宿主机端口" "143")"
      GEN_IMAP_CONTAINER_PORT="$(ask "IMAP 容器端口" "143")"
      GEN_SIEVE_HOST_PORT="$(ask "Sieve 宿主机端口" "4190")"
      GEN_SIEVE_CONTAINER_PORT="$(ask "Sieve 容器端口" "4190")"
    else
      GEN_EXPOSE_LEGACY_MAIL_PORTS="no"
      GEN_POP3_HOST_PORT="110"
      GEN_POP3_CONTAINER_PORT="110"
      GEN_POP3S_HOST_PORT="995"
      GEN_POP3S_CONTAINER_PORT="995"
      GEN_IMAP_HOST_PORT="143"
      GEN_IMAP_CONTAINER_PORT="143"
      GEN_SIEVE_HOST_PORT="4190"
      GEN_SIEVE_CONTAINER_PORT="4190"
    fi

    printf 'TLS：反代 Web 时通常选 mail。\n'
    GEN_TLS_FLAVOR="$(choose_option "TLS_FLAVOR" "mail" \
      "mail：反代 Web|mail" \
      "letsencrypt：自动证书|letsencrypt" \
      "cert：手动证书|cert" \
      "mail-letsencrypt|mail-letsencrypt" \
      "notls：无 TLS|notls")"
    GEN_ADMIN="$(choose_option "管理后台 ADMIN" "true" "启用|true" "关闭|false")"
    GEN_WEBMAIL="$(choose_option "网页邮箱 WEBMAIL" "snappymail" "snappymail|snappymail" "roundcube|roundcube" "关闭网页邮箱|none")"
    GEN_WEBDAV="$(choose_option "WebDAV 日历/联系人" "radicale" "启用 radicale|radicale" "关闭|none")"
    GEN_API="$(choose_option "API 接口" "false" "关闭|false" "启用|true")"
    GEN_WEBROOT_REDIRECT="$(ask "根路径跳转" "/webmail")"
    GEN_WEB_ADMIN="$(ask "后台路径 WEB_ADMIN" "/admin")"
    GEN_WEB_WEBMAIL="$(ask "Webmail 路径" "/webmail")"
    GEN_WEB_API="$(ask "API 路径" "/api")"
    GEN_WEBSITE="$(ask "外部访问地址 WEBSITE" "https://${GEN_HOSTNAMES%%,*}")"
    GEN_SITENAME="$(ask "站点名 SITENAME" "Mailu of $GEN_DOMAIN")"
    GEN_AUTH_RATELIMIT_IP="$(choose_option "同一 IP 登录限速 AUTH_RATELIMIT_IP" "5/hour" \
      "5/hour|5/hour" \
      "20/hour|20/hour" \
      "50/day|50/day" \
      "手动输入|manual")"
    [ "$GEN_AUTH_RATELIMIT_IP" = "manual" ] && GEN_AUTH_RATELIMIT_IP="$(ask_default "同一 IP 登录限速（格式：数字/hour 或 数字/day）" "5/hour")"
    GEN_AUTH_RATELIMIT_USER="$(choose_option "同一用户登录限速 AUTH_RATELIMIT_USER" "50/day" \
      "50/day|50/day" \
      "100/day|100/day" \
      "20/hour|20/hour" \
      "手动输入|manual")"
    [ "$GEN_AUTH_RATELIMIT_USER" = "manual" ] && GEN_AUTH_RATELIMIT_USER="$(ask_default "同一用户登录限速（格式：数字/hour 或 数字/day）" "50/day")"
    GEN_MESSAGE_SIZE_LIMIT="$(choose_option "单封邮件最大体积 MESSAGE_SIZE_LIMIT" "50000000" \
      "50MB|50000000" \
      "25MB|25000000" \
      "100MB|100000000" \
      "手动输入|manual")"
    [ "$GEN_MESSAGE_SIZE_LIMIT" = "manual" ] && GEN_MESSAGE_SIZE_LIMIT="$(ask_default "单封邮件最大体积（单位：字节）" "50000000")"
    GEN_MESSAGE_RATELIMIT="$(choose_option "单用户发信频率 MESSAGE_RATELIMIT" "200/day" \
      "200/day|200/day" \
      "100/day|100/day" \
      "500/day|500/day" \
      "手动输入|manual")"
    [ "$GEN_MESSAGE_RATELIMIT" = "manual" ] && GEN_MESSAGE_RATELIMIT="$(ask_default "单用户发信频率（格式：数字/day 或 数字/hour）" "200/day")"
    printf 'DMARC：默认发给管理员。\n'
    GEN_DMARC_RUA="$(ask_default "DMARC_RUA 收件人" "$GEN_POSTMASTER")"
    GEN_DMARC_RUF="$(ask_default "DMARC_RUF 收件人" "$GEN_POSTMASTER")"
    GEN_FULL_TEXT_SEARCH="$(choose_option "全文搜索 FULL_TEXT_SEARCH" "en" "英文 en|en" "英文+中文 en,zh|en,zh" "关闭|off" "手动输入|manual")"
    [ "$GEN_FULL_TEXT_SEARCH" = "manual" ] && GEN_FULL_TEXT_SEARCH="$(ask_default "全文搜索语言（例如 en 或 en,zh；关闭填 off）" "en")"
    GEN_SECRET_KEY="$(ask "SECRET_KEY（直接回车自动生成；也可手动填随机字符串）" "")"
    [ -n "$GEN_SECRET_KEY" ] || GEN_SECRET_KEY="$(random_secret)"
  fi

  printf '\n将生成：\n'
  printf '  %s\n' "$COMPOSE_FILE"
  printf '  %s\n' "$ENV_FILE"
  printf 'Web 反代目标： http://%s:%s -> 容器 %s\n' "$GEN_WEB_BIND_IP" "$GEN_WEB_HOST_HTTP_PORT" "$GEN_WEB_CONTAINER_HTTP_PORT"
  printf '邮件端口绑定：%s:%s,%s,%s,%s\n' "$GEN_MAIL_BIND_IP" "$GEN_SMTP_HOST_PORT" "$GEN_SMTPS_HOST_PORT" "$GEN_SUBMISSION_HOST_PORT" "$GEN_IMAPS_HOST_PORT"
  confirm "确认生成文件？" "Y" || return 0

  safe_mkdir "$COMPOSE_DIR" || return 1
  safe_mkdir "$GEN_DATA_ROOT/certs" || return 1
  safe_mkdir "$GEN_DATA_ROOT/overrides/nginx" || return 1
  safe_mkdir "$GEN_DATA_ROOT/overrides/postfix" || return 1
  safe_mkdir "$GEN_DATA_ROOT/overrides/dovecot" || return 1
  safe_mkdir "$GEN_DATA_ROOT/overrides/rspamd" || return 1
  [ "$GEN_WEBMAIL" != "none" ] && safe_mkdir "$GEN_DATA_ROOT/overrides/$GEN_WEBMAIL"

  backup_file "$COMPOSE_FILE"
  backup_file "$ENV_FILE"
  write_generated_compose "$COMPOSE_FILE"
  write_generated_env "$ENV_FILE"

  MAIL_DOMAIN="$GEN_DOMAIN"
  MAIL_HOST="${GEN_HOSTNAMES%%,*}"
  MAILU_DATA_DIR="$GEN_DATA_ROOT"
  CERT_DIR="$GEN_DATA_ROOT/certs"
  POSTFIX_OVERRIDE_DIR="$GEN_DATA_ROOT/overrides/postfix"
  WEB_HTTP_PORT="$GEN_WEB_HOST_HTTP_PORT"
  save_config

  ok "已生成 docker-compose.yml 和 .env"
  printf 'DNS 仍请按 Mailu 后台提示配置；证书可以继续用菜单 3 挂载到 %s。\n' "$CERT_DIR"

  if confirm "是否现在执行 docker compose up -d 安装/启动？"; then
    if ! need_cmd docker; then
      fail "未找到 docker 命令"
      return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
      fail "docker compose 不可用"
      return 1
    fi
    dc up -d && dc ps
  else
    warn "已生成配置，未启动。"
  fi
}

install_existing_mailu() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}使用已有配置启动 Mailu${RESET}"

  local base compose_name env_name
  base="$(compose_base)"
  compose_name="$(basename "$COMPOSE_FILE")"
  env_name="$(basename "$ENV_FILE")"

  if [ "$compose_name" != "docker-compose.yml" ] && [ "$compose_name" != "compose.yml" ]; then
    warn "当前 compose 文件名为 $compose_name。docker compose 默认只识别 docker-compose.yml / compose.yml。"
    if confirm "是否复制为 $base/docker-compose.yml？"; then
      backup_file "$base/docker-compose.yml"
      cp -a "$COMPOSE_FILE" "$base/docker-compose.yml"
      COMPOSE_FILE="$base/docker-compose.yml"
      ok "已复制 compose 文件"
    fi
  fi

  if [ "$env_name" != ".env" ]; then
    warn "当前 env 文件名为 $env_name。你要求统一使用 .env。"
    if confirm "是否复制为 $base/.env，并把 compose 里的 env_file 改为 .env？"; then
      backup_file "$base/.env"
      cp -a "$ENV_FILE" "$base/.env"
      ENV_FILE="$base/.env"
      backup_file "$COMPOSE_FILE"
      sed -i 's/env_file:[[:space:]]*mailu\.env/env_file: .env/g; s/-[[:space:]]*mailu\.env/- .env/g' "$COMPOSE_FILE"
      ok "已复制 env 文件"
    fi
  fi

  check_env_core
  check_compose_ports
  save_config

  if ! need_cmd docker; then
    fail "未找到 docker 命令"
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "docker compose 不可用"
    return 1
  fi

  if confirm "执行 docker compose up -d 启动 Mailu？"; then
    dc up -d && dc ps
  else
    warn "已跳过启动"
  fi
}

install_mailu() {
  printf '\n%s\n' "${BOLD}安装 / 启动 Mailu${RESET}"
  printf '1. 生成新的 docker-compose.yml 和 .env，然后可选启动\n'
  printf '2. 使用已有配置检查/启动\n'
  printf '0. 返回\n'
  local choice
  choice="$(read_menu_choice)"
  case "$choice" in
    1) generate_mailu_config ;;
    2) install_existing_mailu ;;
    0) return 0 ;;
    *) warn "无效选择" ;;
  esac
}

check_listening_ports() {
  printf '\n%s\n' "${BOLD}端口监听${RESET}"
  if need_cmd ss; then
    ss -lntup 2>/dev/null | grep -E '(:25|:465|:587|:993|:80|:443|:110|:995|:143)([[:space:]]|$)' || true
  elif need_cmd netstat; then
    netstat -lntup 2>/dev/null | grep -E '(:25|:465|:587|:993|:80|:443|:110|:995|:143)([[:space:]]|$)' || true
  else
    warn "未找到 ss/netstat，跳过端口监听检查"
  fi
}

check_local_web() {
  local port="${WEB_HTTP_PORT:-}"
  [ -n "$port" ] || port="$(host_port_for_container_port 80 || true)"
  printf '\n%s\n' "${BOLD}本地 Web 连通性${RESET}"
  if [ -z "$port" ]; then
    warn "未识别 Mailu HTTP 端口"
    return 0
  fi
  if need_cmd curl; then
    if curl -I --max-time 8 "http://127.0.0.1:$port" 2>/dev/null; then
      ok "http://127.0.0.1:$port 可访问"
    else
      fail "http://127.0.0.1:$port 不通；反向代理很可能会 502。"
    fi
  else
    warn "未找到 curl，跳过 Web 连通性检查"
  fi
}

check_certs() {
  printf '\n%s\n' "${BOLD}证书文件${RESET}"
  local cert="${CERT_DIR:-}/cert.pem"
  local key="${CERT_DIR:-}/key.pem"
  if [ -f "$cert" ]; then
    ok "cert.pem 存在：$cert"
    ls -l "$cert"
  else
    fail "cert.pem 不存在：$cert"
  fi
  if [ -f "$key" ]; then
    ok "key.pem 存在：$key"
    ls -l "$key"
  else
    fail "key.pem 不存在：$key"
  fi
}

check_environment() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}环境检查${RESET}"
  if need_cmd docker; then
    docker --version || true
    docker compose version || warn "docker compose 不可用"
  else
    fail "未找到 docker"
  fi

  [ -f "$COMPOSE_FILE" ] && ok "Compose 文件存在：$COMPOSE_FILE" || fail "Compose 文件不存在：$COMPOSE_FILE"
  [ -f "$ENV_FILE" ] && ok "Env 文件存在：$ENV_FILE" || fail "Env 文件不存在：$ENV_FILE"
  printf '\n%s\n' "${BOLD}目录识别${RESET}"
  printf 'Compose 目录：%s\n' "${COMPOSE_DIR:-未识别}"
  printf 'Mailu 数据目录：%s\n' "${MAILU_DATA_DIR:-未识别}"
  printf '证书目录：%s\n' "${CERT_DIR:-未识别}"
  printf 'Postfix overrides：%s\n' "${POSTFIX_OVERRIDE_DIR:-未识别}"

  check_env_core
  check_compose_ports

  printf '\n%s\n' "${BOLD}容器状态${RESET}"
  if need_cmd docker && docker compose version >/dev/null 2>&1; then
    dc ps || warn "docker compose ps 执行失败"
  fi

  check_listening_ports
  check_local_web
  check_certs

  if confirm "是否继续执行 DNS 检查？"; then
    check_dns
  fi
}

resolve_cert_pair_from_dir() {
  local dir="$1"
  local cert=""
  local key=""

  if [ -f "$dir/fullchain.pem" ] && [ -f "$dir/privkey.pem" ]; then
    cert="$dir/fullchain.pem"
    key="$dir/privkey.pem"
  elif [ -f "$dir/cert.pem" ] && [ -f "$dir/key.pem" ]; then
    cert="$dir/cert.pem"
    key="$dir/key.pem"
  elif [ -f "$dir/fullchain.cer" ] && [ -f "$dir/$MAIL_HOST.key" ]; then
    cert="$dir/fullchain.cer"
    key="$dir/$MAIL_HOST.key"
  elif [ -n "${MAIL_HOST:-}" ] && [ -f "$dir/$MAIL_HOST.cer" ] && [ -f "$dir/$MAIL_HOST.key" ]; then
    cert="$dir/$MAIL_HOST.cer"
    key="$dir/$MAIL_HOST.key"
  else
    cert="$(find "$dir" -maxdepth 1 -type f \( -name 'fullchain*.pem' -o -name '*fullchain*.cer' -o -name 'cert.pem' -o -name '*.crt' \) 2>/dev/null | head -n 1)"
    key="$(find "$dir" -maxdepth 1 -type f \( -name 'privkey*.pem' -o -name '*.key' -o -name 'key.pem' \) 2>/dev/null | head -n 1)"
  fi

  if [ -n "$cert" ] && [ -n "$key" ] && [ -f "$cert" ] && [ -f "$key" ]; then
    printf '%s|%s\n' "$cert" "$key"
    return 0
  fi
  return 1
}

find_cert_pairs() {
  local domain="${1:-}"
  local roots=()
  local root dir pair

  [ -n "$domain" ] && roots+=("/etc/letsencrypt/live/$domain" "/root/.acme.sh/$domain" "/root/.acme.sh/${domain}_ecc")
  roots+=(
    "/etc/letsencrypt/live"
    "/root/.acme.sh"
    "/www/server/panel/vhost/cert"
    "/www/server/panel/ssl"
    "/opt/1panel"
    "/opt/1panel/resource/cert"
    "/opt/1panel/docker/compose"
    "/etc/ssl"
    "/root"
  )

  for root in "${roots[@]}"; do
    [ -e "$root" ] || continue
    if [ -d "$root" ]; then
      pair="$(resolve_cert_pair_from_dir "$root" 2>/dev/null || true)"
      [ -n "$pair" ] && printf '%s|%s\n' "$pair" "$root"
      while IFS= read -r dir; do
        pair="$(resolve_cert_pair_from_dir "$dir" 2>/dev/null || true)"
        [ -n "$pair" ] && printf '%s|%s\n' "$pair" "$dir"
      done < <(find "$root" -maxdepth 4 -type d 2>/dev/null)
    fi
  done | awk -F'|' '!seen[$1 "|" $2]++'
}

choose_cert_pair() {
  local domain="${1:-}"
  local pairs=()
  local labels=()
  local line cert key dir choice

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pairs+=("$line")
    IFS='|' read -r cert key dir <<< "$line"
    labels+=("$dir  ->  $(basename "$cert") / $(basename "$key")")
  done < <(find_cert_pairs "$domain")

  if [ "${#pairs[@]}" -eq 0 ]; then
    warn "没有自动找到证书对。"
    return 1
  fi

  choice="$(select_from_list "发现以下证书，请选择：" "${labels[@]}")" || return 1
  local i
  for i in "${!labels[@]}"; do
    if [ "${labels[$i]}" = "$choice" ]; then
      printf '%s\n' "${pairs[$i]}"
      return 0
    fi
  done
  return 1
}

install_mailu_cert_pair() {
  local source_cert="$1"
  local source_key="$2"
  local restart_front="${3:-ask}"

  CERT_DIR="$(ask "Mailu 证书目录（容器 /certs 对应宿主机目录）" "${CERT_DIR:-/mailu/certs}")"
  [ -f "$source_cert" ] || { fail "证书文件不存在：$source_cert"; return 1; }
  [ -f "$source_key" ] || { fail "私钥文件不存在：$source_key"; return 1; }

  safe_mkdir "$CERT_DIR" || return 1
  backup_file "$CERT_DIR/cert.pem"
  backup_file "$CERT_DIR/key.pem"
  install -m 0644 "$source_cert" "$CERT_DIR/cert.pem"
  install -m 0600 "$source_key" "$CERT_DIR/key.pem"
  ok "已复制并改名："
  printf '  %s -> %s/cert.pem\n' "$source_cert" "$CERT_DIR"
  printf '  %s -> %s/key.pem\n' "$source_key" "$CERT_DIR"
  save_config

  if [ "$restart_front" = "yes" ] || { [ "$restart_front" = "ask" ] && confirm "是否现在重启 Mailu front？"; }; then
    dc restart front
  fi
}

manual_cert_copy() {
  ensure_context || return 1
  local source input pair fullchain privkey
  input="$(ask "请输入证书目录，或 fullchain/cert 文件路径" "")"
  [ -n "$input" ] || { fail "路径不能为空"; return 1; }

  if [ -d "$input" ]; then
    pair="$(resolve_cert_pair_from_dir "$input" || true)"
    if [ -z "$pair" ]; then
      fail "目录里没有识别到 fullchain.pem+privkey.pem 或 cert.pem+key.pem"
      return 1
    fi
    IFS='|' read -r fullchain privkey <<< "$pair"
  else
    fullchain="$input"
    privkey="$(ask "请输入私钥 privkey/key 文件路径" "")"
  fi
  install_mailu_cert_pair "$fullchain" "$privkey" "ask"
}

auto_find_and_copy_cert() {
  ensure_context || return 1
  load_env_defaults
  local domain pair fullchain privkey source_dir
  domain="$(ask "要查找证书的域名" "${MAIL_HOST:-}")"
  pair="$(choose_cert_pair "$domain" || true)"
  if [ -z "$pair" ]; then
    printf '你可以把证书目录直接输入到“指定目录/文件复制改名”。常见文件名是 fullchain.pem 和 privkey.pem。\n'
    return 1
  fi
  IFS='|' read -r fullchain privkey source_dir <<< "$pair"
  install_mailu_cert_pair "$fullchain" "$privkey" "ask"
}

write_panel_cert_hook() {
  ensure_context || return 1
  local source_dir hook_path
  source_dir="$(ask "面板证书目录（后置脚本运行时所在目录；不知道可填证书实际目录）" "")"
  [ -n "$source_dir" ] || source_dir="$COMPOSE_DIR"
  hook_path="$(ask "后置脚本保存路径" "$source_dir/update-mailu-cert.sh")"
  safe_mkdir "$(dirname "$hook_path")" || return 1
  CERT_DIR="$(ask "Mailu 证书目录" "${CERT_DIR:-/mailu/certs}")"

  cat > "$hook_path" <<EOF
#!/usr/bin/env bash
# Generated by mailu-helper.sh
set -e

SOURCE_DIR="\${1:-\$(pwd)}"
[ -d "\$SOURCE_DIR" ] || SOURCE_DIR="$source_dir"

find_pair() {
  local dir="\$1"
  if [ -f "\$dir/fullchain.pem" ] && [ -f "\$dir/privkey.pem" ]; then
    printf '%s|%s\n' "\$dir/fullchain.pem" "\$dir/privkey.pem"
  elif [ -f "\$dir/cert.pem" ] && [ -f "\$dir/key.pem" ]; then
    printf '%s|%s\n' "\$dir/cert.pem" "\$dir/key.pem"
  else
    return 1
  fi
}

PAIR="\$(find_pair "\$SOURCE_DIR" || true)"
if [ -z "\$PAIR" ]; then
  echo "No certificate pair found in \$SOURCE_DIR"
  echo "Expected fullchain.pem + privkey.pem, or cert.pem + key.pem"
  exit 1
fi

CERT="\${PAIR%%|*}"
KEY="\${PAIR#*|}"
mkdir -p "$CERT_DIR"
install -m 0644 "\$CERT" "$CERT_DIR/cert.pem"
install -m 0600 "\$KEY" "$CERT_DIR/key.pem"

cd "$COMPOSE_DIR"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" restart front

echo "Mailu certificate updated: \$CERT -> $CERT_DIR/cert.pem"
EOF
  chmod +x "$hook_path"
  CERT_UPDATE_SCRIPT="$hook_path"
  save_config
  ok "已生成面板后置脚本：$hook_path"
  printf '\n有面板时这样用：\n'
  printf '  1. 在 1Panel / 宝塔 / acme.sh / certbot 里申请或续签证书。\n'
  printf '  2. 把这个脚本路径填到“证书更新后执行脚本/后置脚本”：\n'
  printf '     %s\n' "$hook_path"
  printf '  3. 如果面板支持传入证书目录，就传证书目录；否则脚本会使用你刚才填的默认目录。\n'
}

write_cert_watcher() {
  ensure_context || return 1
  load_env_defaults
  local watch_dir watcher interval
  watch_dir="$(ask "要监控的证书目录；留空则自动寻找" "")"
  interval="$(ask "检查间隔秒数" "300")"
  watcher="$(ask "监控脚本保存路径" "$COMPOSE_DIR/watch-mailu-cert.sh")"
  CERT_DIR="$(ask "Mailu 证书目录" "${CERT_DIR:-/mailu/certs}")"
  safe_mkdir "$(dirname "$watcher")" || return 1

  cat > "$watcher" <<EOF
#!/usr/bin/env bash
# Generated by mailu-helper.sh
set -e

WATCH_DIR="$watch_dir"
MAIL_HOST="$MAIL_HOST"
TARGET_CERT="$CERT_DIR/cert.pem"
TARGET_KEY="$CERT_DIR/key.pem"
COMPOSE_DIR="$COMPOSE_DIR"
COMPOSE_FILE="$COMPOSE_FILE"
ENV_FILE="$ENV_FILE"
INTERVAL="$interval"
STATE_FILE="$COMPOSE_DIR/.mailu-cert-watch.sha256"

find_pair_in_dir() {
  local dir="\$1"
  if [ -f "\$dir/fullchain.pem" ] && [ -f "\$dir/privkey.pem" ]; then
    printf '%s|%s\n' "\$dir/fullchain.pem" "\$dir/privkey.pem"
  elif [ -f "\$dir/cert.pem" ] && [ -f "\$dir/key.pem" ]; then
    printf '%s|%s\n' "\$dir/cert.pem" "\$dir/key.pem"
  elif [ -n "\$MAIL_HOST" ] && [ -f "\$dir/\$MAIL_HOST.cer" ] && [ -f "\$dir/\$MAIL_HOST.key" ]; then
    printf '%s|%s\n' "\$dir/\$MAIL_HOST.cer" "\$dir/\$MAIL_HOST.key"
  else
    return 1
  fi
}

auto_find_pair() {
  local root dir pair
  for root in "\$WATCH_DIR" "/etc/letsencrypt/live/\$MAIL_HOST" "/root/.acme.sh/\$MAIL_HOST" "/root/.acme.sh/\${MAIL_HOST}_ecc" "/www/server/panel/vhost/cert" "/www/server/panel/ssl" "/opt/1panel" "/etc/letsencrypt/live"; do
    [ -n "\$root" ] && [ -d "\$root" ] || continue
    pair="\$(find_pair_in_dir "\$root" 2>/dev/null || true)"
    [ -n "\$pair" ] && { printf '%s\n' "\$pair"; return 0; }
    while IFS= read -r dir; do
      pair="\$(find_pair_in_dir "\$dir" 2>/dev/null || true)"
      [ -n "\$pair" ] && { printf '%s\n' "\$pair"; return 0; }
    done < <(find "\$root" -maxdepth 4 -type d 2>/dev/null)
  done
  return 1
}

copy_if_changed() {
  local pair cert key current last
  pair="\$(auto_find_pair || true)"
  if [ -z "\$pair" ]; then
    echo "\$(date '+%F %T') no certificate pair found"
    return 0
  fi
  cert="\${pair%%|*}"
  key="\${pair#*|}"
  current="\$(sha256sum "\$cert" "\$key" | sha256sum | awk '{print \$1}')"
  last="\$(cat "\$STATE_FILE" 2>/dev/null || true)"
  if [ "\$current" != "\$last" ]; then
    mkdir -p "$(dirname "$CERT_DIR/cert.pem")"
    install -m 0644 "\$cert" "\$TARGET_CERT"
    install -m 0600 "\$key" "\$TARGET_KEY"
    printf '%s\n' "\$current" > "\$STATE_FILE"
    cd "\$COMPOSE_DIR"
    docker compose --env-file "\$ENV_FILE" -f "\$COMPOSE_FILE" restart front
    echo "\$(date '+%F %T') updated Mailu certificate from \$cert"
  fi
}

while true; do
  copy_if_changed
  sleep "\$INTERVAL"
done
EOF
  chmod +x "$watcher"
  CERT_UPDATE_SCRIPT="$watcher"
  save_config
  ok "已生成无面板证书监控脚本：$watcher"
  printf '\n无面板时这样用：\n'
  printf '  临时运行：nohup %s >/var/log/mailu-cert-watch.log 2>&1 &\n' "$watcher"
  printf '  它会循环查找证书，一旦发现 fullchain/privkey 变化，就复制为 %s/cert.pem 和 %s/key.pem，并重启 front。\n' "$CERT_DIR" "$CERT_DIR"
  if need_cmd systemctl; then
    if confirm "是否安装为 systemd 常驻服务？"; then
      install_cert_watcher_service "$watcher"
    fi
  else
    warn "当前系统没有 systemctl，不能自动安装常驻服务；可使用上面的 nohup 命令临时后台运行。"
  fi
}

cert_watcher_service_file() {
  printf '/etc/systemd/system/%s\n' "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"
}

require_root_for_systemd() {
  if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
    fail "安装/管理 systemd 系统服务需要 root 权限。"
    return 1
  fi
}

install_cert_watcher_service() {
  local watcher="${1:-${CERT_UPDATE_SCRIPT:-}}"
  local service_file bash_path
  require_root_for_systemd || return 1
  need_cmd systemctl || { fail "未找到 systemctl"; return 1; }
  [ -n "$watcher" ] && [ -f "$watcher" ] || { fail "监控脚本不存在，请先生成无面板证书监控脚本。"; return 1; }

  CERT_WATCHER_SERVICE="$(ask "systemd 服务名" "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}")"
  case "$CERT_WATCHER_SERVICE" in
    *.service) ;;
    *) CERT_WATCHER_SERVICE="$CERT_WATCHER_SERVICE.service" ;;
  esac
  service_file="$(cert_watcher_service_file)"
  bash_path="$(command -v bash || printf '/bin/bash')"

  cat > "$service_file" <<EOF
# Generated by mailu-helper.sh
[Unit]
Description=Mailu certificate watcher
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$bash_path "$watcher"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$service_file"
  systemctl daemon-reload
  systemctl enable "$CERT_WATCHER_SERVICE"
  save_config
  ok "已安装 systemd 服务：$CERT_WATCHER_SERVICE"
  if confirm "是否现在启动证书监控服务？" "Y"; then
    systemctl restart "$CERT_WATCHER_SERVICE"
    systemctl --no-pager --full status "$CERT_WATCHER_SERVICE" || true
  fi
}

cert_watcher_service_status() {
  need_cmd systemctl || { warn "当前系统没有 systemctl"; return 0; }
  systemctl --no-pager --full status "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}" || true
}

start_cert_watcher_service() {
  require_root_for_systemd || return 1
  systemctl restart "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"
  cert_watcher_service_status
}

stop_cert_watcher_service() {
  require_root_for_systemd || return 1
  systemctl stop "${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"
  cert_watcher_service_status
}

uninstall_cert_watcher_service() {
  require_root_for_systemd || return 1
  local service="${CERT_WATCHER_SERVICE:-mailu-cert-watch.service}"
  local service_file
  service_file="$(cert_watcher_service_file)"
  confirm "确认停止并卸载 $service？" || return 0
  systemctl stop "$service" 2>/dev/null || true
  systemctl disable "$service" 2>/dev/null || true
  if [ -f "$service_file" ]; then
    if grep -q 'Generated by mailu-helper.sh' "$service_file"; then
      rm -f "$service_file"
      ok "已删除服务文件：$service_file"
    else
      warn "服务文件不是 mailu-helper 生成的，未删除：$service_file"
    fi
  fi
  systemctl daemon-reload
}

manage_cert_watcher_service() {
  load_config
  while true; do
    printf '\n%s\n' "${BOLD}证书监控常驻服务${RESET}"
    printf '说明：主脚本不常驻；只有这里安装的证书监控服务会持续运行。\n'
    printf '1. 安装/覆盖 systemd 服务\n'
    printf '2. 启动/重启服务\n'
    printf '3. 停止服务\n'
    printf '4. 查看状态\n'
    printf '5. 卸载服务\n'
    printf '0. 返回\n'
    local choice watcher
    choice="$(read_menu_choice)"
    case "$choice" in
      1)
        watcher="$(ask "监控脚本路径" "${CERT_UPDATE_SCRIPT:-$COMPOSE_DIR/watch-mailu-cert.sh}")"
        install_cert_watcher_service "$watcher"
        ;;
      2) start_cert_watcher_service ;;
      3) stop_cert_watcher_service ;;
      4) cert_watcher_service_status ;;
      5) uninstall_cert_watcher_service ;;
      0) return 0 ;;
      *) warn "无效选择" ;;
    esac
    pause
  done
}

show_cert_guidance() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}证书使用提醒${RESET}"
  printf 'Mailu 邮件 TLS 需要容器 /certs 里有：\n'
  printf '  cert.pem  权限 0644\n'
  printf '  key.pem   权限 0600\n'
  printf '当前宿主机目录：%s\n\n' "${CERT_DIR:-未识别}"
  printf '有面板：先在面板申请/续签证书，然后把“生成面板后置脚本”的路径填到证书更新后执行脚本。\n'
  printf '无面板：用“生成无面板自动监控脚本”，脚本会定时找证书，发现更新就复制改名并重启 Mailu front。\n'
  printf '手动：用“指定目录/文件复制改名”，把 fullchain.pem/privkey.pem 或 cert.pem/key.pem 复制到 Mailu 证书目录。\n'
}

setup_cert() {
  while true; do
    printf '\n%s\n' "${BOLD}配置证书挂载${RESET}"
    printf '1. 自动寻找证书并复制改名到 Mailu\n'
    printf '2. 指定证书目录/文件复制改名到 Mailu\n'
    printf '3. 有面板：生成证书更新后置脚本\n'
    printf '4. 无面板：生成自动监控证书脚本\n'
    printf '5. 管理证书监控常驻服务\n'
    printf '6. 查看当前 Mailu 证书文件\n'
    printf '7. 证书使用提醒\n'
    printf '0. 返回\n'
    local choice
    choice="$(read_menu_choice)"
    case "$choice" in
      1) auto_find_and_copy_cert ;;
      2) manual_cert_copy ;;
      3) write_panel_cert_hook ;;
      4) write_cert_watcher ;;
      5) manage_cert_watcher_service ;;
      6) ensure_context && check_certs ;;
      7) show_cert_guidance ;;
      0) return 0 ;;
      *) warn "无效选择" ;;
    esac
    pause
  done
}

check_proxy() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}检查反向代理${RESET}"
  local domain port
  domain="$(ask "请输入外部访问域名" "${MAIL_HOST:-}")"
  port="$(ask "请输入本地 Mailu HTTP 端口" "${WEB_HTTP_PORT:-}")"

  printf '\n正常面板反代只需要：\n'
  printf '  %s -> http://127.0.0.1:%s\n' "$domain" "$port"
  printf '  开启 HTTPS，并传递 Host / X-Forwarded-Proto。\n\n'

  if ! need_cmd curl; then
    warn "未找到 curl，无法自动检查"
    return 0
  fi

  info "检查本地 Mailu HTTP"
  curl -I --max-time 8 "http://127.0.0.1:$port" || warn "本地 Mailu HTTP 不通，反代可能 502。"

  info "检查外部 /admin"
  curl -Ik --max-time 12 "https://$domain/admin" || warn "外部 /admin 检查失败。"

  info "检查外部 /webmail"
  curl -Ik --max-time 12 "https://$domain/webmail" || warn "外部 /webmail 检查失败。"

  printf '\n排查提示：\n'
  printf '  502：本地端口不通，或反代目标端口写错。\n'
  printf '  重定向太多：检查 TLS_FLAVOR=mail 和 X-Forwarded-Proto。\n'
  printf '  404：检查 HOSTNAMES、WEB_ADMIN、WEB_WEBMAIL。\n'
  printf '  证书错误：检查宿主机反代证书。\n'
}

dig_or_nslookup() {
  local type="$1"
  local name="$2"
  printf '\n%s %s\n' "$type" "$name"
  if need_cmd dig; then
    dig +short "$type" "$name" || true
  elif need_cmd nslookup; then
    nslookup -type="$type" "$name" || true
  else
    warn "未找到 dig/nslookup"
  fi
}

show_catchall_dns_records() {
  ensure_context || return 1
  load_env_defaults
  printf '\n%s\n' "${BOLD}catch-all / 任意子域名 DNS 记录说明${RESET}"
  select_catchall_domains "选择要查看 DNS 记录的域名" || return 1
  local domain host random do_check
  domain="${SELECTED_DOMAINS[0]}"
  host="$(ask "邮件主机名" "${MAIL_HOST:-mail.$domain}")"
  MAIL_DOMAIN="$domain"
  MAIL_HOST="$host"
  save_config

  printf '\n说明：根域名 catch-all（例如 *@example.com）不需要额外 DNS；任意子域名 catch-all（例如 *@*.example.com）需要通配 MX/SPF。\n'
  for domain in "${SELECTED_DOMAINS[@]}"; do
    printf '\n%s\n' "${BOLD}${domain}${RESET}"
    printf '  根域名：\n'
    printf '    %s.        MX   10 %s.\n' "$domain" "$host"
    printf '    %s.        TXT  Mailu 后台给你的 SPF\n' "$domain"
    printf '    selector._domainkey.%s. TXT  Mailu 后台 DKIM\n' "$domain"
    printf '    _dmarc.%s. TXT  \"v=DMARC1; p=none; sp=none; rua=mailto:admin@%s\"\n' "$domain" "$domain"
    printf '  任意子域名 catch-all 额外添加：\n'
    printf '    *.%s.      MX   10 %s.\n' "$domain" "$host"
    printf '    *.%s.      TXT  \"v=spf1 mx a:%s ~all\"\n' "$domain" "$host"
  done
  printf '\n注意：通配记录 *.example.com 不包含 example.com 本身；本体记录和通配记录都要有。\n'
  printf 'PTR 反向解析仍然要在服务器/VPS 商后台设置到 %s。\n' "$host"

  if confirm "是否现在检查所选域名的通配 MX/SPF 是否生效？"; then
    do_check="yes"
  else
    do_check="no"
  fi
  if [ "$do_check" = "yes" ]; then
    for domain in "${SELECTED_DOMAINS[@]}"; do
      random="mh-$(date +%s).$domain"
      printf '\n检查 %s\n' "$random"
      dig_or_nslookup MX "$random"
      dig_or_nslookup TXT "$random"
    done
  fi
}

check_dns() {
  load_env_defaults
  printf '\n%s\n' "${BOLD}DNS 检查${RESET}"
  local domain host random
  domain="$(ask "请输入根域名" "${MAIL_DOMAIN:-}")"
  host="$(ask "请输入邮件主机名" "${MAIL_HOST:-mail.$domain}")"
  random="mh-$(date +%s).$domain"
  MAIL_DOMAIN="$domain"
  MAIL_HOST="$host"
  save_config

  printf '\nDNS 主要按 Mailu 后台自动生成的记录配置；脚本只检查是否生效，不替你写 DNS。\n'
  dig_or_nslookup A "$host"
  dig_or_nslookup MX "$domain"
  dig_or_nslookup TXT "$domain"
  dig_or_nslookup TXT "_dmarc.$domain"
  dig_or_nslookup CNAME "autoconfig.$domain"
  dig_or_nslookup CNAME "autodiscover.$domain"
  dig_or_nslookup MX "$random"
  dig_or_nslookup TXT "$random"

  printf '\nDKIM：请在 Mailu 后台复制 DKIM 记录后检查对应 selector，例如：dig TXT selector._domainkey.%s\n' "$domain"
  printf 'PTR：需要在 VPS/服务器提供商后台设置反向解析到 %s，普通 DNS 面板通常改不了。\n' "$host"
  printf 'DMARC 报告建议发回本域名，例如 rua=mailto:admin@%s；发到其他域名需要额外 _report._dmarc 授权。\n' "$domain"
  printf '跨域报告授权示例：%s._report._dmarc.接收报告域名 TXT \"v=DMARC1;\"\n' "$domain"
  printf '\n如果你开启任意子域名 catch-all，还需要额外添加：*.%s MX 10 %s 和 *.%s TXT \"v=spf1 mx a:%s ~all\"。\n' "$domain" "$host" "$domain" "$host"
  if confirm "是否查看 catch-all / 任意子域名需要补充的 DNS 记录？"; then
    show_catchall_dns_records
  fi

  if need_cmd openssl && confirm "是否检查邮件端口 TLS 证书？"; then
    printf '\nIMAP 993 证书：\n'
    timeout 12 openssl s_client -connect "$host:993" -servername "$host" </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates || true
    printf '\nSMTP 587 STARTTLS 证书：\n'
    timeout 12 openssl s_client -connect "$host:587" -starttls smtp -servername "$host" </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates || true
  fi
}

admin_accounts() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}管理员账号 / 用户管理${RESET}"
  printf 'Mailu 没有默认账号密码。第一次登录要求改密码是正常现象。\n\n'
  printf '1. 创建管理员\n'
  printf '2. 重置管理员密码\n'
  printf '3. 创建普通用户\n'
  printf '0. 返回\n'
  local choice user domain pass cmd
  choice="$(read_menu_choice)"
  case "$choice" in
    1) cmd=admin ;;
    2) cmd=password ;;
    3) cmd=user ;;
    0) return 0 ;;
    *) warn "无效选择"; return 1 ;;
  esac
  user="$(ask "用户名（不含 @域名）" "admin")"
  domain="$(ask "域名" "${MAIL_DOMAIN:-$(env_get DOMAIN || true)}")"
  printf '密码：'
  IFS= read -r -s pass || pass=""
  printf '\n'
  [ -n "$pass" ] || { fail "密码不能为空"; return 1; }
  dc exec admin flask mailu "$cmd" "$user" "$domain" "$pass"
}

root_catchall() {
  printf '\n%s\n' "${BOLD}根域名 catch-all 引导${RESET}"
  printf '根域名 catch-all 不需要额外 DNS。\n'
  printf '请在 Mailu 后台添加别名：名称填 *，域名选择对应根域名，目标填本地接收邮箱（例如 admin@example.com）。\n'
  printf '如果要转发到 QQ/Gmail/Outlook，请到这个本地接收邮箱的用户设置里配置转发。\n'
  printf '如果要收任意子域名邮箱（例如 anything@sub.example.com），请使用菜单 8。\n'
}

escape_regex() {
  printf '%s' "$1" | sed 's/[.[\*^$()+?{}|\\]/\\&/g'
}

ensure_postfix_override_dir() {
  if [ -z "${POSTFIX_OVERRIDE_DIR:-}" ]; then
    POSTFIX_OVERRIDE_DIR="$(ask "Postfix overrides 目录" "/mailu/overrides/postfix")"
  else
    ok "Postfix overrides 目录：$POSTFIX_OVERRIDE_DIR"
  fi
  safe_mkdir "$POSTFIX_OVERRIDE_DIR"
}

collect_mailu_domain_names() {
  local domain
  while IFS= read -r domain; do
    domain="${domain%%#*}"
    domain="$(printf '%s' "$domain" | sed "s/^[[:space:]]*//;s/[[:space:]]*$//;s/^['\"]//;s/['\"]$//")"
    is_valid_domain "$domain" || continue
    printf '%s\n' "$domain"
  done < <(
    {
      dc exec -T admin flask mailu config-export domain.name 2>/dev/null || true
      dc exec -T admin flask mailu config-export domain 2>/dev/null || true
      dc exec -T admin flask mailu domain 2>/dev/null || true
    } \
      | sed -nE \
          -e 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
          -e 's/^[[:space:]]*-[[:space:]]*name:[[:space:]]*"?([^"#[:space:]]+)"?.*/\1/p' \
          -e 's/^[[:space:]]*name:[[:space:]]*"?([^"#[:space:]]+)"?.*/\1/p' \
          -e 's/^[[:space:]]*([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})[[:space:]]*$/\1/p'
  ) | sort -u
}

collect_known_domain_names() {
  local list="${POSTFIX_OVERRIDE_DIR:-}/subdomain-catchall.list"
  {
    collect_mailu_domain_names
    if [ -f "$list" ]; then
      awk -F'|' '{print $1}' "$list"
    fi
  } | while IFS= read -r domain; do
    is_valid_domain "$domain" || continue
    printf '%s\n' "$domain"
  done | sort -u
}

AVAILABLE_DOMAINS=()
SELECTED_DOMAINS=()
SELECTED_ALL="no"

select_catchall_domains() {
  local prompt="${1:-选择域名}"
  local domain selection token idx found
  AVAILABLE_DOMAINS=()
  SELECTED_DOMAINS=()
  SELECTED_ALL="no"

  while IFS= read -r domain; do
    AVAILABLE_DOMAINS+=("$domain")
  done < <(collect_known_domain_names)

  if [ "${#AVAILABLE_DOMAINS[@]}" -eq 0 ]; then
    fail "没有读取到 Mailu 域名。请确认 admin 容器正常，或先在 Mailu 后台添加域名。"
    return 1
  fi

  printf '\nMailu 域名列表：\n'
  idx=1
  for domain in "${AVAILABLE_DOMAINS[@]}"; do
    printf '  %s) %s\n' "$idx" "$domain"
    idx=$((idx + 1))
  done

  selection="$(ask "$prompt（输入 all 或编号，例如 1 2 4）" "all")"
  selection="${selection//,/ }"
  selection="$(printf '%s' "$selection" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [ -z "$selection" ] || [ "$selection" = "all" ] || [ "$selection" = "ALL" ] || [ "$selection" = "全部" ]; then
    SELECTED_DOMAINS=("${AVAILABLE_DOMAINS[@]}")
    SELECTED_ALL="yes"
    return 0
  fi

  for token in $selection; do
    case "$token" in
      ''|*[!0-9]*)
        if is_valid_domain "$token"; then
          SELECTED_DOMAINS+=("$token")
        else
          warn "忽略无效选择：$token"
        fi
        ;;
      *)
        if [ "$token" -ge 1 ] && [ "$token" -le "${#AVAILABLE_DOMAINS[@]}" ]; then
          SELECTED_DOMAINS+=("${AVAILABLE_DOMAINS[$((token - 1))]}")
        else
          warn "忽略超出范围的编号：$token"
        fi
        ;;
    esac
  done

  if [ "${#SELECTED_DOMAINS[@]}" -eq 0 ]; then
    fail "没有选中任何域名。"
    return 1
  fi

  # 去重但保持原顺序。
  local unique=()
  for domain in "${SELECTED_DOMAINS[@]}"; do
    found=0
    for token in "${unique[@]}"; do
      [ "$token" = "$domain" ] && found=1 && break
    done
    [ "$found" -eq 0 ] && unique+=("$domain")
  done
  SELECTED_DOMAINS=("${unique[@]}")
}

append_wildcard_rule_line() {
  local base="$1"
  local target="$2"
  local include_root="$3"
  local domains_file="$4"
  local aliases_file="$5"
  local escaped domain_regex alias_regex
  escaped="$(escape_regex "$base")"
  if [ "$include_root" = "yes" ]; then
    domain_regex="/^(.+\\.)?${escaped}\$/"
    alias_regex="/^.+@(.+\\.)?${escaped}\$/"
  else
    domain_regex="/^.+\\.${escaped}\$/"
    alias_regex="/^.+@.+\\.${escaped}\$/"
  fi
  printf '%s %s\n' "$domain_regex" "$base" >> "$domains_file"
  printf '%s %s\n' "$alias_regex" "$target" >> "$aliases_file"
}

ask_target_for_domain() {
  local domain="$1"
  ask "本地接收邮箱：$domain" "admin@$domain"
}

ensure_subdomain_catchall_list() {
  local list="$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
  if [ -f "$list" ] && grep -Eq '^[^#|]+[|][^|]+' "$list"; then
    return 0
  fi

  warn "catch-all 列表不存在或为空：$list"
  warn "将尝试从 Mailu 后台现有域名生成列表。"

  rebuild_subdomain_catchall_list_from_selection
}

write_wildcard_rule_files_from_list() {
  local list="$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
  local domains="$POSTFIX_OVERRIDE_DIR/wildcard_domains"
  local aliases="$POSTFIX_OVERRIDE_DIR/wildcard_aliases"
  local tmp_domains="$domains.tmp.$$"
  local tmp_aliases="$aliases.tmp.$$"
  local base target include_root escaped domain_regex alias_regex

  : > "$tmp_domains"
  : > "$tmp_aliases"

  [ -f "$list" ] || { rm -f "$tmp_domains" "$tmp_aliases"; fail "列表不存在：$list"; return 1; }
  while IFS='|' read -r base target include_root; do
    [ -n "${base:-}" ] || continue
    case "$base" in \#*) continue ;; esac
    append_wildcard_rule_line "$base" "$target" "${include_root:-no}" "$tmp_domains" "$tmp_aliases"
  done < "$list"

  if [ ! -s "$tmp_domains" ] || [ ! -s "$tmp_aliases" ]; then
    rm -f "$tmp_domains" "$tmp_aliases"
    fail "列表为空，没有生成任何规则。"
    return 1
  fi

  mv "$tmp_domains" "$domains"
  mv "$tmp_aliases" "$aliases"
  chmod 0644 "$domains" "$aliases"
  ok "已生成 wildcard_domains / wildcard_aliases"
}

ensure_postfix_cf_managed() {
  local postfix_cf="$POSTFIX_OVERRIDE_DIR/postfix.cf"
  local tmp_postfix_cf
  if [ -f "$postfix_cf" ] && grep -q 'BEGIN MAILU_HELPER_WILDCARD' "$postfix_cf"; then
    if grep -Fq '${podop}' "$postfix_cf" || grep -Fq 'virtual_alias_maps = regexp:/overrides/wildcard_aliases, socketmap:unix:/tmp/podop.socket:alias' "$postfix_cf"; then
      warn "检测到旧版 postfix wildcard 配置，将自动修复。"
      backup_file "$postfix_cf"
      tmp_postfix_cf="$postfix_cf.tmp.$$"
      awk '
        /^virtual_mailbox_domains = regexp:\/overrides\/wildcard_domains, .*podop.*domain$/ {
          print "virtual_mailbox_domains = regexp:/overrides/wildcard_domains, socketmap:unix:/tmp/podop.socket:domain"
          next
        }
        /^virtual_alias_maps = regexp:\/overrides\/wildcard_aliases, .*podop.*alias$/ {
          print "virtual_alias_maps = socketmap:unix:/tmp/podop.socket:alias, regexp:/overrides/wildcard_aliases"
          next
        }
        /^virtual_alias_maps = regexp:\/overrides\/wildcard_aliases, socketmap:unix:\/tmp\/podop\.socket:alias$/ {
          print "virtual_alias_maps = socketmap:unix:/tmp/podop.socket:alias, regexp:/overrides/wildcard_aliases"
          next
        }
        { print }
      ' "$postfix_cf" > "$tmp_postfix_cf" || { rm -f "$tmp_postfix_cf"; return 1; }
      mv "$tmp_postfix_cf" "$postfix_cf"
      chmod 0644 "$postfix_cf"
      ok "已修复 postfix.cf 管理块"
    fi
    return 0
  fi
  if [ -f "$postfix_cf" ]; then
    warn "postfix.cf 已存在，将追加 mailu-helper 管理块。"
    backup_file "$postfix_cf"
  else
    printf '# Generated by mailu-helper.sh\n' > "$postfix_cf"
  fi
  cat >> "$postfix_cf" <<'EOF'

# BEGIN MAILU_HELPER_WILDCARD
virtual_mailbox_domains = regexp:/overrides/wildcard_domains, socketmap:unix:/tmp/podop.socket:domain
virtual_alias_maps = socketmap:unix:/tmp/podop.socket:alias, regexp:/overrides/wildcard_aliases
# END MAILU_HELPER_WILDCARD
EOF
  ok "已写入 postfix.cf 管理块"
}

rebuild_subdomain_catchall_list_from_selection() {
  select_catchall_domains "选择要写入长效 catch-all 列表的域名" || return 1
  local list="$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
  local tmp_list="$list.tmp.$$"
  local domain target include
  if confirm "是否同时包含根域名本身（例如 *@example.com）？"; then
    include="yes"
  else
    include="no"
  fi
  confirm "确认重建 catch-all 列表？未选中的旧条目会被移除。" "Y" || return 1
  [ -f "$list" ] && backup_file "$list"
  : > "$tmp_list"
  for domain in "${SELECTED_DOMAINS[@]}"; do
    target="$(ask_target_for_domain "$domain")"
    printf '%s|%s|%s\n' "$domain" "$target" "$include" >> "$tmp_list"
  done
  mv "$tmp_list" "$list"
  chmod 0644 "$list"
  ok "已重建 subdomain-catchall.list（${#SELECTED_DOMAINS[@]} 个域名）"
}

add_long_lived_subdomain_catchall() {
  ensure_context || return 1
  ensure_postfix_override_dir || return 1
  select_catchall_domains "选择要长效开启 catch-all 的域名" || return 1
  local domain target include list tmp_list next_tmp
  if confirm "是否同时包含根域名本身（例如 *@example.com）？"; then
    include="yes"
  else
    include="no"
  fi
  list="$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
  touch "$list"
  backup_file "$list"
  tmp_list="$list.tmp.$$"
  cp "$list" "$tmp_list"
  for domain in "${SELECTED_DOMAINS[@]}"; do
    next_tmp="$list.tmp.$$.next"
    grep -Fv "$domain|" "$tmp_list" > "$next_tmp" || true
    mv "$next_tmp" "$tmp_list"
  done
  for domain in "${SELECTED_DOMAINS[@]}"; do
    target="$(ask_target_for_domain "$domain")"
    printf '%s|%s|%s\n' "$domain" "$target" "$include" >> "$tmp_list"
  done
  mv "$tmp_list" "$list"
  chmod 0644 "$list"
  ensure_postfix_cf_managed
  write_wildcard_rule_files_from_list
  save_config
  if confirm "是否重启 smtp 让长效配置生效？" "Y"; then
    dc restart smtp
  fi
  if confirm "是否用 postmap 测试规则？"; then
    for domain in "${SELECTED_DOMAINS[@]}"; do
      test_wildcard_postmap_rule "$domain" "test.$domain" "anything@test.$domain"
    done
  fi
}

test_wildcard_postmap_rule() {
  local domain="$1"
  local domain_query="$2"
  local alias_query="$3"
  local domain_result alias_result

  printf '\n测试域名：%s\n' "$domain"
  printf '  wildcard_domains:\n'
  printf '    输入：%s\n' "$domain_query"
  domain_result="$(dc exec -T smtp postmap -q "$domain_query" regexp:/overrides/wildcard_domains 2>/dev/null || true)"
  if [ -n "$domain_result" ]; then
    printf '    命中：%s\n' "$domain_result"
  else
    printf '    未命中\n'
  fi

  printf '  wildcard_aliases:\n'
  printf '    输入：%s\n' "$alias_query"
  alias_result="$(dc exec -T smtp postmap -q "$alias_query" regexp:/overrides/wildcard_aliases 2>/dev/null || true)"
  if [ -n "$alias_result" ]; then
    printf '    命中：%s\n' "$alias_result"
  else
    printf '    未命中\n'
  fi

  if [ -n "$domain_result" ] && [ -n "$alias_result" ]; then
    ok "测试通过：$domain 的任意子域名 catch-all 会转发到 $alias_result"
  else
    warn "测试未完全通过：请检查 $domain 的 wildcard_domains / wildcard_aliases 规则。"
  fi
}

add_temporary_subdomain_catchall() {
  ensure_context || return 1
  ensure_postfix_override_dir || return 1
  select_catchall_domains "选择要临时开启 catch-all 的域名" || return 1
  local domain target include domains_tmp aliases_tmp
  if confirm "是否同时包含根域名本身（例如 *@example.com）？"; then
    include="yes"
  else
    include="no"
  fi
  domains_tmp="$POSTFIX_OVERRIDE_DIR/wildcard_domains.tmp"
  aliases_tmp="$POSTFIX_OVERRIDE_DIR/wildcard_aliases.tmp"
  : > "$domains_tmp"
  : > "$aliases_tmp"
  for domain in "${SELECTED_DOMAINS[@]}"; do
    target="$(ask_target_for_domain "$domain")"
    append_wildcard_rule_line "$domain" "$target" "$include" "$domains_tmp" "$aliases_tmp"
  done
  chmod 0644 "$POSTFIX_OVERRIDE_DIR/wildcard_domains.tmp" "$POSTFIX_OVERRIDE_DIR/wildcard_aliases.tmp"

  warn "临时配置通过 postconf 写入正在运行的 smtp 容器；容器重建/重启后会失效。"
  dc exec smtp postconf -e 'virtual_mailbox_domains=regexp:/overrides/wildcard_domains.tmp, socketmap:unix:/tmp/podop.socket:domain'
  dc exec smtp postconf -e 'virtual_alias_maps=socketmap:unix:/tmp/podop.socket:alias, regexp:/overrides/wildcard_aliases.tmp'
  dc exec smtp postfix reload
  ok "临时 catch-all 已尝试启用"
}

show_subdomain_catchall_list() {
  ensure_context || return 1
  ensure_postfix_override_dir || return 1
  local list="${POSTFIX_OVERRIDE_DIR:-}/subdomain-catchall.list"
  printf '\n%s\n' "${BOLD}任意子域名 catch-all 列表${RESET}"
  printf '\nMailu 后台现有域名：\n'
  if ! collect_mailu_domain_names | awk '{printf "  %d) %s\n", NR, $0; found=1} END {exit found ? 0 : 1}'; then
    warn "未能从 Mailu 后台读取到域名。"
  fi
  printf '\nhelper 已配置 catch-all：\n'
  if [ -f "$list" ]; then
    awk -F'|' '{printf "域名: %-30s 目标: %-30s 包含根域名: %s\n", $1, $2, $3}' "$list"
  else
    warn "列表不存在：$list"
    printf '\n如果要为这些域名生成任意子域名 catch-all，请选择菜单 3。\n'
  fi
}

remove_helper_wildcard_config() {
  ensure_context || return 1
  ensure_postfix_override_dir || return 1
  select_catchall_domains "选择要删除 catch-all 配置的域名；输入 all 删除全部 helper 配置" || return 1
  local postfix_cf="$POSTFIX_OVERRIDE_DIR/postfix.cf"
  local list="$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
  local domain tmp_list next_tmp

  if [ "$SELECTED_ALL" = "yes" ]; then
    printf '\n将删除 mailu-helper 创建的 wildcard_domains、wildcard_aliases、subdomain-catchall.list，并清理 postfix.cf 管理块。\n'
    confirm "确认删除这些辅助配置？" || return 0
    rm -f "$POSTFIX_OVERRIDE_DIR/wildcard_domains" \
          "$POSTFIX_OVERRIDE_DIR/wildcard_aliases" \
          "$POSTFIX_OVERRIDE_DIR/wildcard_domains.tmp" \
          "$POSTFIX_OVERRIDE_DIR/wildcard_aliases.tmp" \
          "$POSTFIX_OVERRIDE_DIR/subdomain-catchall.list"
    if [ -f "$postfix_cf" ]; then
      if grep -q '^# Generated by mailu-helper.sh$' "$postfix_cf"; then
        rm -f "$postfix_cf"
        ok "已删除 mailu-helper 生成的 postfix.cf"
      elif grep -q 'BEGIN MAILU_HELPER_WILDCARD' "$postfix_cf"; then
        backup_file "$postfix_cf"
        sed -i '/# BEGIN MAILU_HELPER_WILDCARD/,/# END MAILU_HELPER_WILDCARD/d' "$postfix_cf"
        ok "已清理 postfix.cf 管理块"
      fi
    fi
  else
    [ -f "$list" ] || { warn "helper 列表不存在，没有可按域名删除的配置：$list"; return 0; }
    backup_file "$list"
    tmp_list="$list.tmp.$$"
    cp "$list" "$tmp_list"
    for domain in "${SELECTED_DOMAINS[@]}"; do
      next_tmp="$list.tmp.$$.next"
      grep -Fv "$domain|" "$tmp_list" > "$next_tmp" || true
      mv "$next_tmp" "$tmp_list"
      ok "已从 helper 列表移除：$domain"
    done
    mv "$tmp_list" "$list"
    chmod 0644 "$list"
    if [ -s "$list" ]; then
      ensure_postfix_cf_managed
      write_wildcard_rule_files_from_list
    else
      rm -f "$POSTFIX_OVERRIDE_DIR/wildcard_domains" "$POSTFIX_OVERRIDE_DIR/wildcard_aliases"
      warn "helper 列表已为空，已删除 wildcard_domains / wildcard_aliases。"
    fi
  fi
  if confirm "是否重启 smtp？"; then
    dc restart smtp
  fi
}

manage_subdomain_catchall() {
  while true; do
    printf '\n%s\n' "${BOLD}任意子域名 catch-all 管理${RESET}"
    printf '1. 临时开启某个域名\n'
    printf '2. 长效开启某个域名\n'
    printf '3. 长效开启列表内全部域名（重建规则并重启 smtp）\n'
    printf '4. 删除脚本做过的所有配置\n'
    printf '5. 查看列表\n'
    printf '6. 查看任意子域名需要补充的 DNS 记录\n'
    printf '0. 返回\n'
    local choice
    choice="$(read_menu_choice)"
    case "$choice" in
      1) add_temporary_subdomain_catchall ;;
      2) add_long_lived_subdomain_catchall ;;
      3)
        ensure_context && ensure_postfix_override_dir && ensure_subdomain_catchall_list && ensure_postfix_cf_managed && write_wildcard_rule_files_from_list
        confirm "是否重启 smtp？" "Y" && dc restart smtp
        ;;
      4) remove_helper_wildcard_config ;;
      5) show_subdomain_catchall_list ;;
      6) show_catchall_dns_records ;;
      0) return 0 ;;
      *) warn "无效选择" ;;
    esac
    pause
  done
}

check_sender_spoofing() {
  printf '\n%s\n' "${BOLD}任意身份发信检查${RESET}"
  printf '需要在 Mailu 后台给对应用户开启：允许用户仿冒发件人。\n'
  printf 'SMTP 登录账号仍然是真实账号；From 发件人可以是你控制域名下的具体地址。\n'
  printf 'Webmail 不能填 *@*.domain.com 这种通配身份，必须添加具体身份。\n'
  printf '\n建议检查：\n'
  printf '  1. 目标 From 地址所在域名有正确 MX/SPF/DKIM/DMARC。\n'
  printf '  2. Mailu 用户权限已开启 sender spoofing。\n'
  printf '  3. 客户端使用 587 STARTTLS 或 465 SSL/TLS 登录发信。\n'
}

show_client_config() {
  load_config
  load_env_defaults
  local host
  host="$(ask "邮件主机名" "${MAIL_HOST:-mail.${MAIL_DOMAIN:-example.com}}")"
  printf '\n%s\n' "${BOLD}邮件客户端配置说明${RESET}"
  printf 'IMAP:\n'
  printf '  服务器：%s\n' "$host"
  printf '  端口：993\n'
  printf '  加密：SSL/TLS\n'
  printf '  用户名：完整邮箱地址\n\n'
  printf 'SMTP:\n'
  printf '  服务器：%s\n' "$host"
  printf '  端口：587\n'
  printf '  加密：STARTTLS\n'
  printf '  用户名：完整邮箱地址\n\n'
  printf '备用 SMTP:\n'
  printf '  端口：465\n'
  printf '  加密：SSL/TLS\n\n'
  printf '不要用 25 给客户端登录；25 是服务器之间投递邮件用的。\n'
}

show_logs() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}日志查看${RESET}"
  printf '1. 查看全部日志\n'
  printf '2. 查看 front\n'
  printf '3. 查看 smtp\n'
  printf '4. 查看 imap\n'
  printf '5. 查看 admin\n'
  printf '6. 查看 antispam\n'
  printf '0. 返回\n'
  local choice service
  choice="$(read_menu_choice)"
  case "$choice" in
    1) dc logs -f ;;
    2) service=front ;;
    3) service=smtp ;;
    4) service=imap ;;
    5) service=admin ;;
    6) service=antispam ;;
    0) return 0 ;;
    *) warn "无效选择"; return 1 ;;
  esac
  [ -n "${service:-}" ] && dc logs -f "$service"
}

backup_mailu() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}备份 / 恢复${RESET}"
  printf '1. 备份\n'
  printf '2. 恢复\n'
  printf '0. 返回\n'
  local choice
  choice="$(read_menu_choice)"
  case "$choice" in
    1)
      local backup dest items=() path
      dest="$(ask "备份保存目录" "$COMPOSE_DIR")"
      safe_mkdir "$dest" || return 1
      backup="$dest/mailu-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
      [ -f "$COMPOSE_FILE" ] && items+=("$COMPOSE_FILE")
      [ -f "$ENV_FILE" ] && items+=("$ENV_FILE")
      for path in "$MAILU_DATA_DIR/data" "$MAILU_DATA_DIR/dkim" "$MAILU_DATA_DIR/mail" "$MAILU_DATA_DIR/certs" "$MAILU_DATA_DIR/overrides" "$MAILU_DATA_DIR/filter"; do
        [ -e "$path" ] && items+=("$path")
      done
      if [ "${#items[@]}" -eq 0 ]; then
        fail "没有可备份项目"
        return 1
      fi
      tar -czf "$backup" "${items[@]}"
      ok "备份完成：$backup"
      ;;
    2)
      local archive
      archive="$(ask "请输入备份 tar.gz 路径" "")"
      [ -f "$archive" ] || { fail "备份文件不存在：$archive"; return 1; }
      warn "恢复前建议停止容器。恢复会覆盖备份包内对应路径。"
      if confirm "是否先执行 docker compose down？"; then
        dc down
      fi
      confirm "确认开始恢复？" || return 0
      tar -xzf "$archive" -C /
      ok "恢复完成"
      ;;
    0) return 0 ;;
    *) warn "无效选择" ;;
  esac
}

cleanup_helper() {
  ensure_context || return 1
  printf '\n%s\n' "${BOLD}卸载辅助配置${RESET}"
  printf '只删除脚本做过的辅助配置，不删除邮件数据、用户数据、DKIM、compose 或 env。\n'
  remove_helper_wildcard_config
  if need_cmd systemctl && confirm "是否同时卸载 mailu-helper 的证书监控 systemd 服务？"; then
    uninstall_cert_watcher_service
  fi
  if [ -n "${CERT_UPDATE_SCRIPT:-}" ] && [ -f "$CERT_UPDATE_SCRIPT" ]; then
    if grep -q 'Generated by mailu-helper' "$CERT_UPDATE_SCRIPT" 2>/dev/null || confirm "证书更新脚本不是明确的生成文件，仍要删除？"; then
      rm -f "$CERT_UPDATE_SCRIPT"
      ok "已删除证书更新脚本：$CERT_UPDATE_SCRIPT"
      CERT_UPDATE_SCRIPT=""
      save_config
    fi
  fi
}

main_menu() {
  while true; do
    clear 2>/dev/null || true
    printf '%s\n' "${BOLD}====== Mailu Helper v$VERSION ======${RESET}"
    printf '\n'
    printf '1. 安装 / 启动 Mailu\n'
    printf '2. 环境检查\n'
    printf '3. 配置证书挂载\n'
    printf '4. 检查反向代理\n'
    printf '5. DNS 检查\n'
    printf '6. 管理员账号\n'
    printf '7. 根域名 catch-all 引导\n'
    printf '8. 任意子域名 catch-all 管理\n'
    printf '9. 任意身份发信检查\n'
    printf '10. 邮件客户端配置说明\n'
    printf '11. 日志查看\n'
    printf '12. 备份 / 恢复\n'
    printf '13. 卸载辅助配置\n'
    printf '14. 常驻服务管理\n'
    printf '0. 退出\n'
    printf '\n'
    local choice
    choice="$(read_menu_choice)"
    case "$choice" in
      1) install_mailu ;;
      2) check_environment ;;
      3) setup_cert ;;
      4) check_proxy ;;
      5) ensure_context && check_dns ;;
      6) admin_accounts ;;
      7) root_catchall ;;
      8) manage_subdomain_catchall ;;
      9) check_sender_spoofing ;;
      10) show_client_config ;;
      11) show_logs ;;
      12) backup_mailu ;;
      13) cleanup_helper ;;
      14) manage_cert_watcher_service ;;
      0) exit 0 ;;
      *) warn "无效选择" ;;
    esac
    pause
  done
}

usage() {
  cat <<EOF
Mailu Helper v$VERSION

用法：
  ./mailu-helper.sh                 打开交互菜单
  ./mailu-helper.sh detect          自动识别 compose/env/数据目录并保存配置
  ./mailu-helper.sh check           环境检查
  ./mailu-helper.sh dns             DNS 检查
  ./mailu-helper.sh proxy           反向代理检查
  ./mailu-helper.sh client          输出客户端配置
  ./mailu-helper.sh catchall        任意子域名 catch-all 管理
  ./mailu-helper.sh cert-service    管理证书监控常驻服务

环境变量：
  MAILU_HELPER_CONFIG=/path/conf    指定配置文件，默认 /root/.mailu-helper.conf
EOF
}

main() {
  case "${1:-menu}" in
    menu) main_menu ;;
    detect) ensure_context ;;
    check) check_environment ;;
    dns) ensure_context && check_dns ;;
    proxy) check_proxy ;;
    client) show_client_config ;;
    catchall) manage_subdomain_catchall ;;
    cert-service) manage_cert_watcher_service ;;
    -h|--help|help) usage ;;
    *) usage; return 1 ;;
  esac
}

main "$@"
