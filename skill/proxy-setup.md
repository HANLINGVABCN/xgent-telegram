```!
当用户要在 Linux VPS 部署/重装/卸载代理节点、sing-box、Reality、Hysteria2、TUIC、Shadowsocks、Cloudflare WARP 或 Clash/Mihomo 配置时使用本技能。脚本一定随项目位于 skill/script/proxy-setup/proxy_setup.sh；本地执行：chmod +x skill/script/proxy-setup/proxy_setup.sh && sudo bash skill/script/proxy-setup/proxy_setup.sh。默认推荐路线：主菜单 1 全新安装 -> 机器类型 3 标准 VPS -> 协议 1 VLESS + Reality -> 出站 3 双节点 -> WARP 方式 1 WireGuard 直连。WireGuard 模式无需 warp-cli；WARP 异常先用主菜单 6 状态检查看 endpoint/wireguard 握手与出站日志。脚本会尝试开放本机防火墙端口，但云厂商安全组必须让用户手动放行。卸载用主菜单 4 深度卸载；不会删除脚本自身或云安全组规则；通用依赖 ca-certificates/wget/tar/curl/openssl/jq/qrencode 强烈不建议删除，默认保留。
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

## 防火墙与安全组

脚本会尝试自动开放本机防火墙端口：

- 普通模式：开放主节点监听端口。
- 双节点模式：开放直连节点端口和 WARP 节点端口。
- 支持 `iptables`、`ufw`、`firewalld`。

脚本无法修改云厂商控制台里的安全组/防火墙规则。若 VPS 位于 AWS、GCP、Azure、Oracle、阿里云、腾讯云、Vultr、Hetzner 等平台，仍需在控制台手动放行对应 TCP 端口。

检查本机监听：

```bash
ss -lntup | grep sing-box
```

如果本机已监听但外部无法连接，优先检查云厂商安全组。

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

## WARP WireGuard 排查

WireGuard 模式不依赖 `warp-cli`，配置保存在：

```text
/etc/sing-box/warp_wg.json
```

若 WARP 节点无法访问，先重新运行脚本选择：

```text
6) WARP 状态检查
```

状态检查会临时启动一个本地测试代理，经 `out-warp` 访问 Cloudflare trace，并打印关键日志。重点看日志中是否出现：

```text
endpoint/wireguard[out-warp]: received handshake response
endpoint/wireguard[out-warp]: outbound connection to ...
```

若已收到 `handshake response`，说明 WireGuard 隧道本身已握手，问题通常在 DNS、目标连接或 VPS 网络策略。若没有握手响应，优先检查 WARP endpoint、VPS 出站 UDP、机房是否限制 Cloudflare WARP。

重新注册 WireGuard 配置：

```bash
rm -f /etc/sing-box/warp_wg.json
sudo bash skill/script/proxy-setup/proxy_setup.sh
```

然后选择：

```text
5) WARP 安装向导 -> 7) WireGuard 模式注册
```

再重新部署或重启 `sing-box`。

注意：新版 sing-box 使用顶层 `endpoints` 的 WireGuard 写法，路由可以指向 endpoint 的 `tag`。不要把 `gen_wg_endpoints_block` 生成的对象直接塞进 `outbounds`，否则新版 sing-box 可能配置校验失败。

## 深度卸载

主菜单第 4 项为深度卸载。卸载会清理：

- `sing-box` 服务、systemd/openrc 单元、残留软链和失败状态。
- `/usr/local/bin/sing-box`。
- `/etc/sing-box`，包括 `config.json`、`warp_wg.json`、证书和临时配置。
- `/root/proxy_info` 节点信息。
- `/tmp/proxy_setup_logs`、`/var/log/sing-box`。
- `/tmp/sing-box-warp-test-*`、`/tmp/singbox_install_*`。
- 当前配置中记录的入站端口对应的本机防火墙放行规则。
- Cloudflare WARP 客户端、`warp-svc` 服务、Cloudflare 源、keyring、缓存和 WARP 状态目录。

卸载不会清理：

- 当前脚本文件自身。
- 云厂商控制台里的安全组规则。

卸载时会询问是否强制清理通用依赖：

```text
ca-certificates wget tar curl openssl jq qrencode
```

强烈不建议删除这些通用依赖。它们可能被系统、SSH 运维脚本、证书更新、其他代理或自动化任务使用。默认直接回车保留；只有确认这台机器专门用于本脚本且不再使用时才输入 `y`。

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
