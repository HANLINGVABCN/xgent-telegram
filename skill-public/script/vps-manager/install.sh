#!/usr/bin/env bash
# VPS Manager - 依赖检查与初始化脚本
# 用法: bash skill-public/script/vps-manager/install.sh

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; }

echo "=============================="
echo " VPS Manager 环境检查与初始化"
echo "=============================="
echo

errors=0

# 1. 检查 ssh
if command -v ssh &>/dev/null; then
    ok "ssh 客户端已安装: $(ssh -V 2>&1 | head -1)"
else
    fail "ssh 客户端未安装"
    echo "    安装: apt install openssh-client -y"
    ((errors++))
fi

# 2. 检查 scp
if command -v scp &>/dev/null; then
    ok "scp 已安装"
else
    fail "scp 未安装"
    echo "    安装: apt install openssh-client -y"
    ((errors++))
fi

# 3. 检查 sshpass (可选，密码认证需要)
if command -v sshpass &>/dev/null; then
    ok "sshpass 已安装 (支持密码认证)"
else
    warn "sshpass 未安装 (密码认证不可用，密钥认证不受影响)"
    echo "    安装: apt install sshpass -y"
fi

# 4. 检查 python3
if command -v python3 &>/dev/null; then
    ok "Python3 已安装: $(python3 --version 2>&1)"
else
    fail "Python3 未安装"
    echo "    安装: apt install python3 -y"
    ((errors++))
fi

# 5. 确定项目根目录 (脚本位于 skill-public/script/vps-manager/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORKSPACE_DIR="$PROJECT_ROOT/workspace"
CONFIG_FILE="$WORKSPACE_DIR/vps_servers.json"
TOOL_SCRIPT="$SCRIPT_DIR/vps_tool.py"

# 6. 创建 workspace 目录
if [ ! -d "$WORKSPACE_DIR" ]; then
    mkdir -p "$WORKSPACE_DIR"
    ok "创建 workspace 目录: $WORKSPACE_DIR"
else
    ok "workspace 目录已存在: $WORKSPACE_DIR"
fi

# 7. 创建空配置文件
if [ ! -f "$CONFIG_FILE" ]; then
    echo '{"servers": []}' > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    ok "创建配置文件: $CONFIG_FILE (权限 600)"
else
    ok "配置文件已存在: $CONFIG_FILE"
fi

# 8. 检查核心工具脚本
if [ -f "$TOOL_SCRIPT" ]; then
    ok "核心工具脚本存在: $TOOL_SCRIPT"
else
    fail "核心工具脚本缺失: $TOOL_SCRIPT"
    ((errors++))
fi

echo
if [ "$errors" -gt 0 ]; then
    fail "发现 $errors 个问题，请先修复后再使用"
    exit 1
else
    ok "环境检查通过，VPS Manager 已就绪！"
    echo
    echo "使用方法:"
    echo "  python3 skill-public/script/vps-manager/vps_tool.py --help"
    echo "  python3 skill-public/script/vps-manager/vps_tool.py list"
    echo "  python3 skill-public/script/vps-manager/vps_tool.py add --name <别名> --host <IP> --port 22 --user root --key ~/.ssh/id_rsa"
fi
