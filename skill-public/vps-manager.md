```!
VPS 多服务器管理技能：通过 SSH 管理多台远程服务器资产。
核心工具：python3 skill-public/script/vps-manager/vps_tool.py <子命令>
配置文件：~/.config/vps-manager/servers.json（首次使用自动创建）

子命令速查：
- init：环境检查与初始化（检查 ssh/scp/sshpass 依赖，创建配置文件）
- list：列出所有已配置服务器（别名、IP、端口、用户、认证方式、描述）
- add --name <别名> --host <IP> --port <端口> --user <用户> [--password <密码>] [--key <密钥路径>] [--desc <描述>] [--tags <标签,逗号分隔>]：添加服务器
- remove --name <别名> [--yes]：删除服务器（--yes 跳过确认）
- test [--name <别名>] [--all]：SSH 连通性测试与延迟测量
- exec --name <别名> [--timeout <秒>] -- <命令>：在远程服务器执行非交互命令，返回完整 stdout/stderr/rc
- batch --names <别名1,别名2,...> [--timeout <秒>] -- <命令>：对多台服务器并发执行同一命令
- status [--name <别名>] [--all]：获取服务器系统状态（CPU/内存/磁盘/负载/运行时间/内核版本）
- push --name <别名> --local <本地路径> --remote <远程路径>：SCP 上传文件到远程
- pull --name <别名> --remote <远程路径> --local <本地路径>：SCP 从远程下载文件
- info --name <别名>：查看服务器完整配置详情（密码脱敏显示）
- ssh-cmd --name <别名>：输出可直接用于 shell-x 的 SSH 连接命令

交互式 SSH 排查：先 exec ssh-cmd 获取连接命令，再用 shell-x 启动该命令建立交互会话，然后通过 stdin-x 操作。
密码认证方式需要宿主机安装 sshpass：apt install sshpass -y。
首次使用前运行初始化检查：python3 skill-public/script/vps-manager/vps_tool.py init。
```

# VPS 多服务器管理技能 — 详细使用指南

## 概述

本技能为 XGent 提供多台远程 VPS 服务器的统一管理能力，支持服务器资产管理、连通性检测、远程命令执行、系统状态巡检、文件传输等功能。

基于系统原生 SSH/SCP 实现，零额外 Python 依赖。

## 安装与初始化

首次使用前执行初始化检查：

```bash
python3 skill-public/script/vps-manager/vps_tool.py init
```

该命令检查：
- `ssh` 客户端是否可用
- `scp` 是否可用
- `sshpass` 是否已安装（密码认证所需）
- 自动创建 `workspace/` 目录和空配置文件

## 配置文件格式

配置存储在 `~/.config/vps-manager/servers.json`，结构如下：

```json
{
  "servers": [
    {
      "name": "hk-1",
      "host": "103.x.x.x",
      "port": 22,
      "user": "root",
      "auth_type": "key",
      "key_path": "~/.ssh/id_rsa",
      "password": null,
      "description": "香港 CN2 GIA",
      "tags": ["production", "proxy"],
      "added_at": "2026-09-07T15:00:00+08:00"
    }
  ]
}
```

认证方式：
- `key`：SSH 密钥认证（推荐），需指定 `--key <密钥路径>`
- `password`：密码认证，需指定 `--password <密码>`，宿主机需安装 `sshpass`

## 子命令详解

### list — 列出所有服务器

```bash
python3 skill-public/script/vps-manager/vps_tool.py list
```

输出表格包含：别名、主机、端口、用户、认证方式、描述、标签。

### add — 添加服务器

```bash
# 密钥认证
python3 skill-public/script/vps-manager/vps_tool.py add \
  --name hk-1 --host 103.1.2.3 --port 22 --user root \
  --key ~/.ssh/id_rsa --desc "香港 CN2 GIA" --tags "production,proxy"

# 密码认证
python3 skill-public/script/vps-manager/vps_tool.py add \
  --name us-1 --host 45.1.2.3 --port 2222 --user ubuntu \
  --password "mypassword" --desc "美国 洛杉矶"
```

### remove — 删除服务器

```bash
python3 skill-public/script/vps-manager/vps_tool.py remove --name hk-1 --yes
```

### test — 连通性测试

```bash
# 测试单台
python3 skill-public/script/vps-manager/vps_tool.py test --name hk-1

# 测试所有
python3 skill-public/script/vps-manager/vps_tool.py test --all
```

返回：连接状态、延迟（毫秒）、远程内核版本。

### exec — 远程执行命令

```bash
python3 skill-public/script/vps-manager/vps_tool.py exec --name hk-1 -- "uptime && df -h /"
```

`--` 之后的所有内容作为远程命令执行。返回完整 stdout、stderr 和退出码。

可选 `--timeout <秒>` 设置执行超时（默认 300 秒）。

### batch — 批量执行

```bash
python3 skill-public/script/vps-manager/vps_tool.py batch \
  --names hk-1,us-1,jp-1 -- "uptime && free -h | head -3"
```

对指定的多台服务器并发执行同一命令，汇总输出，每台服务器的结果以分隔线隔开。

### status — 系统状态巡检

```bash
# 单台
python3 skill-public/script/vps-manager/vps_tool.py status --name hk-1

# 全部
python3 skill-public/script/vps-manager/vps_tool.py status --all
```

自动收集：系统运行时间、CPU 核心数、内存使用、磁盘使用（根分区）、系统负载、内核版本、OS 发行版。

### push / pull — 文件传输

```bash
# 上传
python3 skill-public/script/vps-manager/vps_tool.py push \
  --name hk-1 --local /app/deploy.sh --remote /root/deploy.sh

# 下载
python3 skill-public/script/vps-manager/vps_tool.py pull \
  --name hk-1 --remote /var/log/nginx/error.log --local /tmp/error.log
```

使用 SCP 传输，支持密钥和密码两种认证。传输目录请在路径后加 `-r` 选项或先打包。

### info — 服务器详情

```bash
python3 skill-public/script/vps-manager/vps_tool.py info --name hk-1
```

输出服务器的完整配置信息，密码以 `****` 脱敏显示。

### ssh-cmd — 获取 SSH 连接命令

```bash
python3 skill-public/script/vps-manager/vps_tool.py ssh-cmd --name hk-1
```

输出可直接用于 `shell-x` 的完整 SSH 连接命令。交互式排查流程：

1. 先通过 `run-x` 执行 `ssh-cmd` 获取连接命令
2. 用 `shell-x` 启动该 SSH 命令建立交互会话
3. 通过 `stdin-x` 向会话发送操作命令
4. 用 `shellread-x` 观察输出进度
5. 排查完毕后用 `shellkill-x` 关闭会话

## 安全注意事项

- 密码以明文存储在 `~/.config/vps-manager/servers.json`，确保该文件权限为 `600`
- 推荐使用 SSH 密钥认证替代密码认证
- `exec` 和 `batch` 命令在远程执行前应确认用户已授权（遵循 Agent 执行原则第 2 条）
- 破坏性操作（重启、删除、配置变更）必须获得用户明确授权后才能执行
