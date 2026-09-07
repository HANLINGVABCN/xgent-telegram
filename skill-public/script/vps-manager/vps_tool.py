#!/usr/bin/env python3
"""
VPS Manager — XGent Skill 多服务器管理 CLI 工具

通过系统原生 SSH/SCP 实现远程服务器管理，零额外 Python 依赖。
配置存储于 workspace/vps_servers.json。

用法:
    python3 skill-public/script/vps-manager/vps_tool.py <子命令> [参数]

子命令:
    list        列出所有已配置服务器
    add         添加服务器
    remove      删除服务器
    test        SSH 连通性测试
    exec        远程执行命令
    batch       批量执行命令
    status      系统状态巡检
    push        上传文件到远程
    pull        从远程下载文件
    info        查看服务器配置详情
    ssh-cmd     输出 SSH 连接命令
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows GBK 编码问题
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

# 自动推断项目根目录: 脚本位于 skill-public/script/vps-manager/
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
CONFIG_FILE = WORKSPACE_DIR / "vps_servers.json"

# SSH 通用选项
SSH_COMMON_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",  # 非交互模式，密码认证时由 sshpass 注入
]


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------

class VPSConfig:
    """VPS 服务器配置管理器"""

    def __init__(self, config_path: Path = CONFIG_FILE):
        self.config_path = config_path
        self._ensure_config()

    def _ensure_config(self):
        """确保配置文件和目录存在"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self._write({"servers": []})
            # 设置文件权限为 600 (仅 owner 读写)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass  # Windows 不支持 chmod

    def _read(self) -> dict:
        """读取配置文件"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "servers" not in data:
                data["servers"] = []
            return data
        except (json.JSONDecodeError, FileNotFoundError):
            return {"servers": []}

    def _write(self, data: dict):
        """写入配置文件"""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def list_servers(self) -> list:
        """获取所有服务器列表"""
        return self._read()["servers"]

    def get_server(self, name: str) -> dict | None:
        """按别名获取服务器配置"""
        for s in self.list_servers():
            if s["name"] == name:
                return s
        return None

    def add_server(self, server: dict) -> bool:
        """添加服务器，别名不可重复"""
        data = self._read()
        for s in data["servers"]:
            if s["name"] == server["name"]:
                return False
        data["servers"].append(server)
        self._write(data)
        return True

    def remove_server(self, name: str) -> bool:
        """按别名删除服务器"""
        data = self._read()
        original_len = len(data["servers"])
        data["servers"] = [s for s in data["servers"] if s["name"] != name]
        if len(data["servers"]) == original_len:
            return False
        self._write(data)
        return True

    def update_server(self, name: str, updates: dict) -> bool:
        """更新服务器配置字段"""
        data = self._read()
        for s in data["servers"]:
            if s["name"] == name:
                s.update(updates)
                self._write(data)
                return True
        return False


# ---------------------------------------------------------------------------
# SSH 执行器
# ---------------------------------------------------------------------------

class SSHExecutor:
    """基于系统原生 SSH/SCP 的远程执行器"""

    @staticmethod
    def _build_ssh_cmd(server: dict, for_interactive: bool = False) -> list[str]:
        """构建 SSH 命令参数列表"""
        cmd = []
        auth_type = server.get("auth_type", "key")

        # 密码认证：通过 sshpass 注入
        if auth_type == "password" and server.get("password"):
            sshpass_path = shutil.which("sshpass")
            if not sshpass_path:
                raise RuntimeError(
                    "密码认证需要 sshpass，请先安装: apt install sshpass -y"
                )
            cmd.extend(["sshpass", "-p", server["password"]])

        cmd.append("ssh")

        # 交互模式不使用 BatchMode
        if for_interactive:
            opts = [o for o in SSH_COMMON_OPTS if o != "BatchMode=yes"]
            # 也移除 BatchMode=yes 前面的 -o
            cleaned = []
            skip_next = False
            for item in opts:
                if skip_next:
                    skip_next = False
                    continue
                if item == "-o":
                    # 检查下一个是否是 BatchMode=yes
                    idx = opts.index(item)
                    if idx + 1 < len(opts) and opts[idx + 1] == "BatchMode=yes":
                        skip_next = True
                        continue
                cleaned.append(item)
            cmd.extend(cleaned)
        else:
            cmd.extend(SSH_COMMON_OPTS)

        # 端口
        port = server.get("port", 22)
        if port != 22:
            cmd.extend(["-p", str(port)])

        # 密钥
        if auth_type == "key" and server.get("key_path"):
            key_path = os.path.expanduser(server["key_path"])
            cmd.extend(["-i", key_path])

        # 用户@主机
        cmd.append(f"{server['user']}@{server['host']}")

        return cmd

    @staticmethod
    def _build_ssh_cmd_string(server: dict, for_interactive: bool = True) -> str:
        """构建 SSH 命令字符串（用于 shell-x 直连）"""
        parts = []
        auth_type = server.get("auth_type", "key")

        if auth_type == "password" and server.get("password"):
            parts.extend(["sshpass", "-p", f"'{server['password']}'"])

        parts.append("ssh")
        parts.extend(["-o", "StrictHostKeyChecking=accept-new"])
        parts.extend(["-o", "ServerAliveInterval=30"])

        port = server.get("port", 22)
        if port != 22:
            parts.extend(["-p", str(port)])

        if auth_type == "key" and server.get("key_path"):
            key_path = os.path.expanduser(server["key_path"])
            parts.extend(["-i", key_path])

        parts.append(f"{server['user']}@{server['host']}")
        return " ".join(parts)

    @staticmethod
    def _build_scp_cmd(server: dict, local_path: str, remote_path: str,
                       upload: bool = True) -> list[str]:
        """构建 SCP 命令"""
        cmd = []
        auth_type = server.get("auth_type", "key")

        if auth_type == "password" and server.get("password"):
            sshpass_path = shutil.which("sshpass")
            if not sshpass_path:
                raise RuntimeError(
                    "密码认证需要 sshpass，请先安装: apt install sshpass -y"
                )
            cmd.extend(["sshpass", "-p", server["password"]])

        cmd.append("scp")
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"])

        port = server.get("port", 22)
        if port != 22:
            cmd.extend(["-P", str(port)])

        if auth_type == "key" and server.get("key_path"):
            key_path = os.path.expanduser(server["key_path"])
            cmd.extend(["-i", key_path])

        remote_spec = f"{server['user']}@{server['host']}:{remote_path}"

        if upload:
            cmd.extend([local_path, remote_spec])
        else:
            cmd.extend([remote_spec, local_path])

        return cmd

    @staticmethod
    def execute(server: dict, command: str, timeout: int = 300) -> dict:
        """在远程服务器执行命令"""
        try:
            ssh_cmd = SSHExecutor._build_ssh_cmd(server)
            ssh_cmd.append(command)

            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"命令执行超时 ({timeout}s)",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }

    @staticmethod
    def test_connection(server: dict) -> dict:
        """测试 SSH 连通性"""
        start = time.monotonic()
        result = SSHExecutor.execute(server, "echo pong && uname -r", timeout=15)
        elapsed = round((time.monotonic() - start) * 1000)

        if result["success"]:
            lines = result["stdout"].strip().split("\n")
            kernel = lines[1] if len(lines) > 1 else "unknown"
            return {
                "online": True,
                "latency_ms": elapsed,
                "kernel": kernel,
            }
        else:
            return {
                "online": False,
                "latency_ms": elapsed,
                "error": result["stderr"].strip() or "连接失败",
            }

    @staticmethod
    def transfer(server: dict, local_path: str, remote_path: str,
                 upload: bool = True, timeout: int = 600) -> dict:
        """SCP 文件传输"""
        try:
            scp_cmd = SSHExecutor._build_scp_cmd(server, local_path, remote_path, upload)
            result = subprocess.run(
                scp_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            direction = "上传" if upload else "下载"
            if result.returncode == 0:
                return {"success": True, "message": f"{direction}成功"}
            else:
                return {
                    "success": False,
                    "message": f"{direction}失败: {result.stderr.strip()}",
                }
        except subprocess.TimeoutExpired:
            return {"success": False, "message": f"传输超时 ({timeout}s)"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @staticmethod
    def get_system_status(server: dict) -> dict:
        """获取服务器系统状态"""
        status_cmd = (
            "echo '===UPTIME===' && uptime && "
            "echo '===CPU===' && nproc && "
            "echo '===MEMORY===' && free -h | head -3 && "
            "echo '===DISK===' && df -h / && "
            "echo '===LOAD===' && cat /proc/loadavg && "
            "echo '===KERNEL===' && uname -r && "
            "echo '===OS===' && (cat /etc/os-release 2>/dev/null | head -4 || echo 'unknown')"
        )
        result = SSHExecutor.execute(server, status_cmd, timeout=30)
        if not result["success"]:
            return {"success": False, "error": result["stderr"]}

        output = result["stdout"]
        sections = {}
        current_section = None
        current_lines = []

        for line in output.split("\n"):
            if line.startswith("===") and line.endswith("==="):
                if current_section:
                    sections[current_section] = "\n".join(current_lines).strip()
                current_section = line.strip("=")
                current_lines = []
            else:
                current_lines.append(line)
        if current_section:
            sections[current_section] = "\n".join(current_lines).strip()

        return {"success": True, "sections": sections}


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def print_table(headers: list[str], rows: list[list[str]]):
    """打印对齐的文本表格"""
    if not rows:
        print("(空)")
        return

    # 计算每列最大宽度 (考虑中文字符宽度)
    def display_width(s: str) -> int:
        w = 0
        for ch in s:
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
                w += 2
            else:
                w += 1
        return w

    def pad(s: str, width: int) -> str:
        return s + " " * (width - display_width(s))

    col_widths = []
    for i, h in enumerate(headers):
        max_w = display_width(h)
        for row in rows:
            if i < len(row):
                max_w = max(max_w, display_width(row[i]))
        col_widths.append(max_w)

    # 打印表头
    header_line = "  ".join(pad(h, col_widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("  ".join("-" * w for w in col_widths))

    # 打印数据行
    for row in rows:
        line = "  ".join(
            pad(row[i] if i < len(row) else "", col_widths[i])
            for i in range(len(headers))
        )
        print(line)


def mask_password(password: str | None) -> str:
    """密码脱敏"""
    if not password:
        return "-"
    if len(password) <= 3:
        return "***"
    return password[0] + "*" * (len(password) - 2) + password[-1]


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_list(args, config: VPSConfig):
    """列出所有服务器"""
    servers = config.list_servers()
    if not servers:
        print("暂无已配置的服务器。")
        print("添加服务器: python3 skill-public/script/vps-manager/vps_tool.py add --name <别名> --host <IP> --port 22 --user root --key ~/.ssh/id_rsa")
        return

    headers = ["别名", "主机", "端口", "用户", "认证", "描述", "标签"]
    rows = []
    for s in servers:
        rows.append([
            s["name"],
            s["host"],
            str(s.get("port", 22)),
            s.get("user", "root"),
            s.get("auth_type", "key"),
            s.get("description", ""),
            ",".join(s.get("tags", [])),
        ])

    print(f"已配置 {len(servers)} 台服务器:\n")
    print_table(headers, rows)


def cmd_add(args, config: VPSConfig):
    """添加服务器"""
    if not args.name or not args.host:
        print("错误: --name 和 --host 为必填参数", file=sys.stderr)
        sys.exit(1)

    # 确定认证方式
    if args.password:
        auth_type = "password"
    elif args.key:
        auth_type = "key"
    else:
        # 默认尝试密钥
        auth_type = "key"
        if not args.key:
            default_key = os.path.expanduser("~/.ssh/id_rsa")
            if os.path.exists(default_key):
                args.key = "~/.ssh/id_rsa"
            else:
                default_key_ed = os.path.expanduser("~/.ssh/id_ed25519")
                if os.path.exists(default_key_ed):
                    args.key = "~/.ssh/id_ed25519"
                else:
                    print("错误: 未指定 --password 或 --key，且未找到默认密钥 (~/.ssh/id_rsa 或 ~/.ssh/id_ed25519)", file=sys.stderr)
                    sys.exit(1)

    server = {
        "name": args.name,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "auth_type": auth_type,
        "key_path": args.key,
        "password": args.password,
        "description": args.desc or "",
        "tags": [t.strip() for t in args.tags.split(",")] if args.tags else [],
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    if config.add_server(server):
        print(f"✓ 服务器 '{args.name}' 已添加")
        print(f"  主机: {args.host}:{args.port}")
        print(f"  用户: {args.user}")
        print(f"  认证: {auth_type}" + (f" (密钥: {args.key})" if auth_type == "key" else ""))
        if args.desc:
            print(f"  描述: {args.desc}")
    else:
        print(f"错误: 别名 '{args.name}' 已存在，请使用不同别名", file=sys.stderr)
        sys.exit(1)


def cmd_remove(args, config: VPSConfig):
    """删除服务器"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        print(f"确认删除服务器 '{args.name}' ({server['user']}@{server['host']}:{server.get('port', 22)})？")
        print("使用 --yes 跳过确认")
        sys.exit(0)

    if config.remove_server(args.name):
        print(f"✓ 服务器 '{args.name}' 已删除")
    else:
        print(f"错误: 删除失败", file=sys.stderr)
        sys.exit(1)


def cmd_test(args, config: VPSConfig):
    """连通性测试"""
    if args.all:
        servers = config.list_servers()
        if not servers:
            print("暂无已配置的服务器")
            return
    elif args.name:
        server = config.get_server(args.name)
        if not server:
            print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
            sys.exit(1)
        servers = [server]
    else:
        print("错误: 请指定 --name <别名> 或 --all", file=sys.stderr)
        sys.exit(1)

    print(f"测试 {len(servers)} 台服务器的 SSH 连通性...\n")

    results = []
    with ThreadPoolExecutor(max_workers=min(len(servers), 10)) as pool:
        futures = {pool.submit(SSHExecutor.test_connection, s): s for s in servers}
        for future in as_completed(futures):
            server = futures[future]
            result = future.result()
            results.append((server, result))

    # 按别名排序输出
    results.sort(key=lambda x: x[0]["name"])

    headers = ["别名", "主机", "状态", "延迟", "内核/错误"]
    rows = []
    for server, result in results:
        if result["online"]:
            rows.append([
                server["name"],
                f"{server['host']}:{server.get('port', 22)}",
                "✓ 在线",
                f"{result['latency_ms']}ms",
                result.get("kernel", ""),
            ])
        else:
            rows.append([
                server["name"],
                f"{server['host']}:{server.get('port', 22)}",
                "✗ 离线",
                f"{result['latency_ms']}ms",
                result.get("error", "连接失败"),
            ])

    print_table(headers, rows)

    online_count = sum(1 for _, r in results if r["online"])
    print(f"\n在线: {online_count}/{len(results)}")


def cmd_exec(args, config: VPSConfig):
    """远程执行命令"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    command = " ".join(args.command)
    if not command:
        print("错误: 请在 -- 之后指定要执行的命令", file=sys.stderr)
        sys.exit(1)

    print(f"[{args.name}] 执行: {command}")
    print("-" * 60)

    result = SSHExecutor.execute(server, command, timeout=args.timeout)

    if result["stdout"]:
        print(result["stdout"], end="")
        if not result["stdout"].endswith("\n"):
            print()

    if result["stderr"]:
        print(f"[stderr] {result['stderr']}", end="")
        if not result["stderr"].endswith("\n"):
            print()

    print("-" * 60)
    print(f"退出码: {result['returncode']}")

    sys.exit(0 if result["success"] else result["returncode"])


def cmd_batch(args, config: VPSConfig):
    """批量执行命令"""
    names = [n.strip() for n in args.names.split(",")]
    servers = []
    for name in names:
        server = config.get_server(name)
        if not server:
            print(f"错误: 服务器 '{name}' 不存在", file=sys.stderr)
            sys.exit(1)
        servers.append(server)

    command = " ".join(args.command)
    if not command:
        print("错误: 请在 -- 之后指定要执行的命令", file=sys.stderr)
        sys.exit(1)

    print(f"在 {len(servers)} 台服务器上并发执行: {command}\n")

    results = []
    with ThreadPoolExecutor(max_workers=min(len(servers), 10)) as pool:
        futures = {
            pool.submit(SSHExecutor.execute, s, command, args.timeout): s
            for s in servers
        }
        for future in as_completed(futures):
            server = futures[future]
            result = future.result()
            results.append((server, result))

    # 按原始顺序输出
    results.sort(key=lambda x: names.index(x[0]["name"]))

    for server, result in results:
        status = "✓" if result["success"] else "✗"
        print(f"{'=' * 60}")
        print(f"{status} [{server['name']}] {server['host']}:{server.get('port', 22)} (rc={result['returncode']})")
        print(f"{'=' * 60}")
        if result["stdout"]:
            print(result["stdout"], end="")
            if not result["stdout"].endswith("\n"):
                print()
        if result["stderr"]:
            print(f"[stderr] {result['stderr']}", end="")
            if not result["stderr"].endswith("\n"):
                print()
        print()

    success_count = sum(1 for _, r in results if r["success"])
    print(f"结果: {success_count}/{len(results)} 成功")

    if success_count < len(results):
        sys.exit(1)


def cmd_status(args, config: VPSConfig):
    """系统状态巡检"""
    if args.all:
        servers = config.list_servers()
        if not servers:
            print("暂无已配置的服务器")
            return
    elif args.name:
        server = config.get_server(args.name)
        if not server:
            print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
            sys.exit(1)
        servers = [server]
    else:
        print("错误: 请指定 --name <别名> 或 --all", file=sys.stderr)
        sys.exit(1)

    results = []
    with ThreadPoolExecutor(max_workers=min(len(servers), 10)) as pool:
        futures = {pool.submit(SSHExecutor.get_system_status, s): s for s in servers}
        for future in as_completed(futures):
            server = futures[future]
            result = future.result()
            results.append((server, result))

    results.sort(key=lambda x: x[0]["name"])

    for server, result in results:
        print(f"{'=' * 60}")
        print(f"[{server['name']}] {server['user']}@{server['host']}:{server.get('port', 22)}")
        if server.get("description"):
            print(f"  描述: {server['description']}")
        print(f"{'=' * 60}")

        if not result["success"]:
            print(f"  ✗ 状态获取失败: {result.get('error', '未知错误')}")
            print()
            continue

        sections = result["sections"]

        if "UPTIME" in sections:
            print(f"  运行时间: {sections['UPTIME'].strip()}")
        if "CPU" in sections:
            print(f"  CPU 核心:  {sections['CPU'].strip()}")
        if "KERNEL" in sections:
            print(f"  内核版本: {sections['KERNEL'].strip()}")
        if "OS" in sections:
            # 提取 PRETTY_NAME
            for line in sections["OS"].split("\n"):
                if "PRETTY_NAME" in line:
                    os_name = line.split("=", 1)[1].strip().strip('"')
                    print(f"  操作系统: {os_name}")
                    break
        if "LOAD" in sections:
            load_parts = sections["LOAD"].strip().split()
            if len(load_parts) >= 3:
                print(f"  系统负载: {load_parts[0]} {load_parts[1]} {load_parts[2]} (1/5/15min)")
        if "MEMORY" in sections:
            print(f"  内存使用:")
            for line in sections["MEMORY"].split("\n"):
                if line.strip():
                    print(f"    {line.strip()}")
        if "DISK" in sections:
            print(f"  磁盘使用 (/):")
            for line in sections["DISK"].split("\n"):
                if line.strip():
                    print(f"    {line.strip()}")

        print()


def cmd_push(args, config: VPSConfig):
    """上传文件"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.local):
        print(f"错误: 本地文件不存在: {args.local}", file=sys.stderr)
        sys.exit(1)

    print(f"上传 {args.local} -> {args.name}:{args.remote}")
    result = SSHExecutor.transfer(server, args.local, args.remote, upload=True)

    if result["success"]:
        print(f"✓ {result['message']}")
    else:
        print(f"✗ {result['message']}", file=sys.stderr)
        sys.exit(1)


def cmd_pull(args, config: VPSConfig):
    """下载文件"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    print(f"下载 {args.name}:{args.remote} -> {args.local}")
    result = SSHExecutor.transfer(server, args.local, args.remote, upload=False)

    if result["success"]:
        print(f"✓ {result['message']}")
    else:
        print(f"✗ {result['message']}", file=sys.stderr)
        sys.exit(1)


def cmd_info(args, config: VPSConfig):
    """查看服务器详情"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    print(f"服务器配置详情: {args.name}")
    print("=" * 40)
    print(f"  别名:     {server['name']}")
    print(f"  主机:     {server['host']}")
    print(f"  端口:     {server.get('port', 22)}")
    print(f"  用户:     {server.get('user', 'root')}")
    print(f"  认证方式: {server.get('auth_type', 'key')}")

    if server.get("auth_type") == "key":
        print(f"  密钥路径: {server.get('key_path', '-')}")
    elif server.get("auth_type") == "password":
        print(f"  密码:     {mask_password(server.get('password'))}")

    print(f"  描述:     {server.get('description', '-')}")
    print(f"  标签:     {', '.join(server.get('tags', [])) or '-'}")
    print(f"  添加时间: {server.get('added_at', '-')}")


def cmd_ssh_cmd(args, config: VPSConfig):
    """输出 SSH 连接命令"""
    server = config.get_server(args.name)
    if not server:
        print(f"错误: 服务器 '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)

    ssh_cmd = SSHExecutor._build_ssh_cmd_string(server, for_interactive=True)
    print(ssh_cmd)


def cmd_init(args, config: VPSConfig):
    """环境检查与初始化"""
    print("VPS Manager 环境检查与初始化")
    print("=" * 40)
    print()

    errors = 0

    # ssh
    if shutil.which("ssh"):
        try:
            ver = subprocess.run(["ssh", "-V"], capture_output=True, text=True, timeout=5)
            ver_str = (ver.stderr or ver.stdout).strip()
            print(f"✓ ssh 已安装: {ver_str}")
        except Exception:
            print("✓ ssh 已安装")
    else:
        print("✗ ssh 未安装 → apt install openssh-client -y")
        errors += 1

    # scp
    if shutil.which("scp"):
        print("✓ scp 已安装")
    else:
        print("✗ scp 未安装 → apt install openssh-client -y")
        errors += 1

    # sshpass (可选)
    if shutil.which("sshpass"):
        print("✓ sshpass 已安装 (支持密码认证)")
    else:
        print("! sshpass 未安装 (密码认证不可用，密钥认证不受影响) → apt install sshpass -y")

    # 配置文件
    if config.config_path.exists():
        servers = config.list_servers()
        print(f"✓ 配置文件已存在: {config.config_path} ({len(servers)} 台服务器)")
    else:
        config._ensure_config()
        print(f"✓ 已创建配置文件: {config.config_path}")

    print()
    if errors > 0:
        print(f"✗ 发现 {errors} 个问题，请先修复")
        sys.exit(1)
    else:
        print("✓ 环境就绪")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="vps_tool",
        description="VPS 多服务器管理工具 — XGent Skill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s list\n"
            "  %(prog)s add --name hk-1 --host 103.1.2.3 --port 22 --user root --key ~/.ssh/id_rsa\n"
            "  %(prog)s test --all\n"
            "  %(prog)s exec --name hk-1 -- uptime\n"
            "  %(prog)s batch --names hk-1,us-1 -- 'df -h /'\n"
            "  %(prog)s status --all\n"
            "  %(prog)s ssh-cmd --name hk-1\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcmd", help="子命令")

    # list
    subparsers.add_parser("list", help="列出所有已配置服务器")

    # init
    subparsers.add_parser("init", help="环境检查与初始化 (检查 ssh/scp/sshpass 依赖)")

    # add
    p_add = subparsers.add_parser("add", help="添加服务器")
    p_add.add_argument("--name", required=True, help="服务器别名 (唯一标识)")
    p_add.add_argument("--host", required=True, help="服务器 IP 或域名")
    p_add.add_argument("--port", type=int, default=22, help="SSH 端口 (默认 22)")
    p_add.add_argument("--user", default="root", help="SSH 用户名 (默认 root)")
    p_add.add_argument("--password", help="SSH 密码 (密码认证)")
    p_add.add_argument("--key", help="SSH 私钥路径 (密钥认证)")
    p_add.add_argument("--desc", help="服务器描述")
    p_add.add_argument("--tags", help="标签 (逗号分隔)")

    # remove
    p_remove = subparsers.add_parser("remove", help="删除服务器")
    p_remove.add_argument("--name", required=True, help="服务器别名")
    p_remove.add_argument("--yes", action="store_true", help="跳过确认")

    # test
    p_test = subparsers.add_parser("test", help="SSH 连通性测试")
    p_test.add_argument("--name", help="服务器别名")
    p_test.add_argument("--all", action="store_true", help="测试所有服务器")

    # exec
    p_exec = subparsers.add_parser("exec", help="远程执行命令")
    p_exec.add_argument("--name", required=True, help="服务器别名")
    p_exec.add_argument("--timeout", type=int, default=300, help="超时秒数 (默认 300)")
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="远程命令 (-- 之后)")

    # batch
    p_batch = subparsers.add_parser("batch", help="批量执行命令")
    p_batch.add_argument("--names", required=True, help="服务器别名列表 (逗号分隔)")
    p_batch.add_argument("--timeout", type=int, default=300, help="超时秒数 (默认 300)")
    p_batch.add_argument("command", nargs=argparse.REMAINDER, help="远程命令 (-- 之后)")

    # status
    p_status = subparsers.add_parser("status", help="系统状态巡检")
    p_status.add_argument("--name", help="服务器别名")
    p_status.add_argument("--all", action="store_true", help="所有服务器")

    # push
    p_push = subparsers.add_parser("push", help="上传文件到远程")
    p_push.add_argument("--name", required=True, help="服务器别名")
    p_push.add_argument("--local", required=True, help="本地文件路径")
    p_push.add_argument("--remote", required=True, help="远程目标路径")

    # pull
    p_pull = subparsers.add_parser("pull", help="从远程下载文件")
    p_pull.add_argument("--name", required=True, help="服务器别名")
    p_pull.add_argument("--remote", required=True, help="远程文件路径")
    p_pull.add_argument("--local", required=True, help="本地保存路径")

    # info
    p_info = subparsers.add_parser("info", help="查看服务器配置详情")
    p_info.add_argument("--name", required=True, help="服务器别名")

    # ssh-cmd
    p_ssh = subparsers.add_parser("ssh-cmd", help="输出 SSH 连接命令 (用于 shell-x)")
    p_ssh.add_argument("--name", required=True, help="服务器别名")

    args = parser.parse_args()

    if not args.subcmd:
        parser.print_help()
        sys.exit(0)

    # 处理 exec/batch 的 command 参数：去掉 -- 分隔符
    if args.subcmd in ("exec", "batch") and args.command:
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]

    config = VPSConfig()

    cmd_map = {
        "list": cmd_list,
        "init": cmd_init,
        "add": cmd_add,
        "remove": cmd_remove,
        "test": cmd_test,
        "exec": cmd_exec,
        "batch": cmd_batch,
        "status": cmd_status,
        "push": cmd_push,
        "pull": cmd_pull,
        "info": cmd_info,
        "ssh-cmd": cmd_ssh_cmd,
    }

    handler = cmd_map.get(args.subcmd)
    if handler:
        handler(args, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
