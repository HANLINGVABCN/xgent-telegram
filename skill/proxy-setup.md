```!
综合代理部署脚本 proxy_setup.sh 的使用说明和推荐执行路线。脚本位置：skill/script/proxy-setup/proxy_setup.sh。
在 VPS 上部署 sing-box 节点、安装 Cloudflare WARP、生成 Reality / Shadowsocks / Hysteria2 / TUIC / WS TLS 等节点配置，并输出分享链接与 Clash Meta / Mihomo 配置。推荐先运行该脚本，主菜单先选 5 安装 WARP，再选 1 免费 WARP 注册；安装完成返回主菜单后，选 1 全新安装 / 重置，机器类型选 3 标准 VPS，协议选 1 VLESS + Reality，最后按需要选择 1 直连出站或 3 双节点模式。
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
- 支持安装 / 登录 Cloudflare WARP，并配置本地 SOCKS5 出口。
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

## 推荐执行路线

最推荐的流程是先把免费 Cloudflare WARP 装好，再部署标准 VPS 的 Reality 节点。

### 第一步：先安装免费 Cloudflare WARP

进入主菜单后选择：

```text
5) WARP 安装向导
```

然后在 WARP 菜单选择：

```text
1) 免费 WARP 注册
```

安装完成后，检查 WARP 状态。如果 WARP SOCKS5 端口正常运行，再返回主菜单。

### 第二步：开始全新安装

回到主菜单后选择：

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

### 第三步：选择出站模式

根据使用场景选择两种模式之一：

```text
1) 直连出站
```

适合日常使用，速度快，`ping0.cc` 显示 VPS 自己的 IP。

或者选择：

```text
3) 双节点模式
```

推荐给需要同时保留直连和 WARP 出口的场景。脚本会生成两个节点：

- 直连节点：日常使用，速度快。
- WARP 节点：需要 Cloudflare 出口、解锁或切换出口 IP 时使用。

## 推荐一句话流程

```text
主菜单 5 -> WARP 菜单 1 -> 返回主菜单 -> 主菜单 1 -> 机器类型 3 -> 协议 1 -> 出站模式选择 1 或 3
```

## Agent 使用注意

当用户要求部署代理、安装 WARP、生成 Reality 节点、配置 sing-box、查看 Clash/Mihomo 配置时，应优先读取本文，再运行 `skill/script/proxy-setup/proxy_setup.sh`。

执行前先确认：

- 当前环境是否是 VPS。
- 是否有 root 权限。
- 是否允许改动系统网络、服务和防火墙。
- 是否会影响当前 SSH 连接。

如果用户选择 WARP 出站或双节点模式，应先确认 WARP 已经安装并且本地 SOCKS5 端口可用，默认端口通常是：

```text
127.0.0.1:40000
```

## 常用检查命令

```bash
systemctl status sing-box --no-pager
systemctl restart sing-box
cat /etc/sing-box/config.json
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