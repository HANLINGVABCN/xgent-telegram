```!
综合代理部署脚本 proxy_setup.sh 的使用说明和推荐执行路线。脚本位置：skill/script/proxy-setup/proxy_setup.sh。
在 VPS 上部署 sing-box 节点、安装 Cloudflare WARP、生成 Reality / Shadowsocks / Hysteria2 / TUIC / WS TLS 等节点配置，并输出分享链接与 Clash Meta / Mihomo 配置。支持两种 WARP 出站模式：WireGuard 直连（推荐，无需 warp-cli）和传统 SOCKS5（需安装 warp-cli）。推荐流程：直接选 1 全新安装，机器类型选 3 标准 VPS，协议选 1 VLESS + Reality，出站选 3 双节点模式，WARP 出站方式选 1 WireGuard 直连，脚本会自动注册并配置。
```

# proxy_setup.sh 使用说明

本文是 Telegram AI Bot 项目内置 skill 文档，用于指导 Agent 模式在服务器上使用 `skill/script/proxy-setup/proxy_setup.sh` 部署代理节点。

## 脚本位置

```text
skill/script/proxy-setup/proxy_setup.sh
```

脚本用途：

- 安装并配置 `sing-box`。
- 支持 NAT 小鸡、低配 VPS、标准 VPS 三种机器类型。
- 支持 VLESS + Reality、Shadowsocks 2022、Hysteria2、TUIC v5、VLESS + WS + TLS、VMess + WS + TLS。
- 支持直连出站、WARP 出站、双节点模式。
- WARP 出站支持两种方式：
  - **WireGuard 直连（推荐）**：sing-box 原生 WireGuard 出站，无需安装 warp-cli，自动通过 WARP API 注册并获取密钥。
  - **传统 SOCKS5**：需安装 Cloudflare WARP 客户端，通过本地 SOCKS5 中转。
- 安装完成后输出分享链接、二维码和 Clash Meta / Mihomo 配置。

## 运行方法

在项目根目录执行：

```bash
chmod +x skill/script/proxy-setup/proxy_setup.sh
sudo bash skill/script/proxy-setup/proxy_setup.sh
```

也可以直接在脚本目录执行：

```bash
cd skill/script/proxy-setup
chmod +x proxy_setup.sh
sudo bash proxy_setup.sh
```

必须使用 root 权限或 sudo 运行，因为脚本会安装系统依赖、写入 `/etc/sing-box`、创建 systemd/openrc 服务并开放端口。

## 推荐执行路线（WireGuard 模式）

使用 WireGuard 模式时不需要提前安装 WARP 客户端，脚本会在安装 sing-box 后自动注册。

### 一步到位流程

进入主菜单后选择：

```text
1) 全新安装 / 重置
```

机器类型选择：

```text
3) 标准 VPS
```

协议选择：

```text
1) VLESS + Reality
```

出站模式选择（根据需要）：

```text
1) 直连出站        ← 不需要 WARP
3) 双节点模式      ← 推荐，同时生成直连和 WARP 节点
```

如果选了 WARP 或双节点，会出现 WARP 出站方式选择：

```text
1) WireGuard 直连 (推荐)
2) 传统 SOCKS5 代理
```

选 1 后脚本会自动完成 WireGuard 注册，无需额外操作。

### 推荐一句话流程

```text
主菜单 1 -> 机器类型 3 -> 协议 1 -> 出站模式 3 -> WARP 方式 1 (WireGuard)
```

### 传统 SOCKS5 流程（备选）

如果选择传统 SOCKS5 方式，需要先安装 WARP 客户端：

```text
主菜单 5 -> WARP 菜单 1 (免费注册) -> 返回主菜单 -> 主菜单 1 -> ... -> WARP 方式 2 (SOCKS5)
```

## WARP 安装向导

主菜单第 5 项进入 WARP 安装向导，包含以下选项：

- **1-5)** 传统 warp-cli 登录方式（免费注册、Zero Trust、Service Token 等）
- **6)** 检查 WARP 状态（同时显示 WireGuard 和 warp-cli 状态）
- **7)** WireGuard 模式注册（推荐，可提前注册或重新注册）

## Agent 使用注意

当用户要求部署代理、安装 WARP、生成 Reality 节点、配置 sing-box、查看 Clash/Mihomo 配置时，应优先读取本文，再运行 `skill/script/proxy-setup/proxy_setup.sh`。

执行前先确认：

- 当前环境是否是 VPS。
- 是否有 root 权限。
- 是否允许改动系统网络、服务和防火墙。
- 是否会影响当前 SSH 连接。

优先推荐 WireGuard 出站模式，因为不需要安装额外软件，更轻量稳定。

## 常用检查命令

```bash
systemctl status sing-box --no-pager
systemctl restart sing-box
cat /etc/sing-box/config.json
cat /etc/sing-box/warp_wg.json   # WireGuard 配置
ls -lah /root/proxy_info
```

查看 WARP 状态可以重新运行脚本并选择：

```text
6) WARP 状态检查
```

## 输出文件

脚本通常会把节点信息保存到：

```text
/root/proxy_info
```

其中 `_clash.yaml` 文件可以复制导入 FlClash、Clash Meta 或 Mihomo。

WireGuard 配置保存在：

```text
/etc/sing-box/warp_wg.json
```