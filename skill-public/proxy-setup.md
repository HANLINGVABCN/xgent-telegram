```!
在用户指定的 Linux VPS 上部署或排查 sing-box 代理节点（Reality、SS2022、Hysteria2、TUIC、WS+TLS、SOCKS5、WARP）。脚本：skill-public/script/proxy-setup/proxy_setup.sh。执行前 read 本文，确认目标服务器及系统修改范围；只审查技能或查看配置时不要启动安装。不要自动重装、深度卸载或把 WARP 改成直连。
```

# Proxy Setup

这是 XGent 项目技能，保留项目的 ` ```! ` 简介格式，不是 Codex 的 YAML frontmatter 技能。

## 先确定任务与边界

- **审查技能/修改脚本**：只修改仓库文件、做隔离测试；不在当前开发机执行部署。
- **查看或排障**：先检查版本、配置校验、服务状态、监听及网络；不要把重新安装当成诊断步骤。
- **部署或重置**：确认目标 Linux VPS、root/sudo 权限、已有 sing-box/WARP 是否需要保留，以及安装软件、修改服务/网络/防火墙的授权。不要因为当前终端可用就假定它是目标服务器。
- **卸载**：先说明下文的实际清理范围，由用户确认；不能由启动失败推导出卸载授权。

部署会修改 `/etc/sing-box`、安装 sing-box 和系统依赖、写 systemd/OpenRC 服务、调整系统网络/文件描述符参数，并尝试开放本机端口。重置不是无损更新：脚本会备份旧 `config.json`，但二进制、证书、服务和系统参数**没有完整事务回滚**；保留现有部署时先做独立备份。

## 执行与交互

从项目真实根目录执行；在 Agent 工具中先解析实际项目路径，不要写死用户目录。

```bash
# 无副作用：无需 root，不创建日志，也不部署
bash skill-public/script/proxy-setup/proxy_setup.sh --help

# 仅在已确认的 Linux 目标服务器执行；已经是 root 时无需 sudo
sudo bash skill-public/script/proxy-setup/proxy_setup.sh
```

使用可持续交互的 PTY，逐次读取菜单后输入。不要拼接固定答案串：不同机器类型、协议、已有 WARP 状态会改变提示顺序。输入 EOF 会退出，而不是无限循环。脚本须使用 Bash，文件保持 LF 换行；不要用 `sh`。

主菜单：`1` 安装/重置，`2` 显示已保存节点，`3` 重启，`4` 深度卸载，`5` WARP 向导，`6` WARP 状态，`7` 概念说明，`0` 退出。

## 选择协议与出站

遵循用户指定方案；没有要求时优先选择满足需要的单协议，而不是默认部署全部协议或双节点。

| 场景 | 要点 |
| --- | --- |
| NAT 小鸡 | Reality / SS2022 / SOCKS5；公网映射端口和内部监听端口分别填写，不能混用 |
| 低配或标准 VPS | 支持下表的全部协议；菜单 `8` 可选最多 7 个直连节点 + 1 个 Reality-WARP 节点 |
| 只需服务器原生出口 | 选择直连，无需注册或安装 WARP |
| 指定 WARP 出口 | 优先考虑原生 WireGuard；无需 warp-cli，但依赖 UDP 和注册 API 可用 |
| 传统 WARP SOCKS5 | 先通过主菜单 `5` 安装/登录 warp-cli，确认本地代理端口（默认 40000）可用 |
| 双节点 | 同时生成直连与 WARP；两个监听端口必须不同 |

标准/低配协议菜单：`1` Reality、`2` SS2022、`3` Hysteria2、`4` TUIC、`5` VLESS-WS-TLS、`6` VMess-WS-TLS、`7` SOCKS5、`8` 自由组合。**NAT 菜单的 `3` 是 SOCKS5**，不要照抄标准 VPS 的序号。

自由组合模式按提示选择协议，WS 域名可填 `skip` 跳过；选中节点从起始端口递增。脚本会拒绝无效端口、双节点端口重复以及递增超过 65535。WireGuard 注册失败会停止安装，不会自动更换出站。

| 协议 | 网络和证书条件 |
| --- | --- |
| Reality | TCP；通常不需要自己的域名，SNI/握手目标必须可达，不能承诺不会被封锁 |
| SS2022 | 按客户端需求放行 TCP/UDP；客户端须支持对应加密方法 |
| Hysteria2 / TUIC | **UDP/QUIC**；仅放行 TCP 无法连接。脚本使用自签名证书，客户端跳过校验有安全代价 |
| VLESS/VMess + WS + TLS | 域名、DNS 和 ACME 验证可达性；使用 Cloudflare 代理时还须核对支持的 HTTPS 端口及回源 TLS 设置 |
| SOCKS5 | 用户名/密码认证**不等于传输加密**；不要把裸 SOCKS5 当作适合公网明文传输凭据的安全方案 |

WS 多协议自动分配到的端口不一定是 CDN 支持端口；不要宣称填写一个域名就一定能用。ACME 的验证端口也不一定等于节点监听端口，证书失败时先核对验证方式、DNS 和防火墙。

## 验收与排障

先做只读检查（依目标系统选择 systemd 或 OpenRC）：

```bash
/usr/local/bin/sing-box version
/usr/local/bin/sing-box check -c /etc/sing-box/config.json
systemctl status sing-box --no-pager
# OpenRC: rc-service sing-box status
ss -lntup | grep sing-box
# 需要错误详情时读取并脱敏，避免直接转发整段日志
journalctl -u sing-box --no-pager -n 50
```

脚本尝试通过 iptables、ufw、firewalld 放行**实际配置中的监听端口**，当前会同时尝试 TCP 和 UDP；这不证明防火墙规则成功生效或重启后持久存在。云安全组、NAT 端口映射要另行配置，脚本不会修改云控制台。

判定部署完成需要分开验证：

1. `sing-box check` 成功、服务持续运行且监听端口正确。
2. 客户端从服务器外部连接成功（含相应 TCP/UDP、安全组和 NAT 映射）。
3. 实际出口 IP 符合直连/WARP 选择；双节点分别验证。

只验证了服务启动时，明确报告外部连接或 WARP 出口尚未验证。启动失败会保留配置和日志，不会自动深度清理；配置校验失败也不会打印含密钥的整份配置。

### WARP 专项

- WireGuard 配置：`/etc/sing-box/warp_wg.json`；不依赖 warp-cli。
- 主菜单 `6` 会检查 WireGuard/warp-cli，并可能临时启动测试代理、访问 Cloudflare trace；这不是纯离线检查。
- 已收到 `received handshake response` 只说明握手成功；继续检查 DNS、目标连接及出口。没有响应时检查 endpoint、出站 UDP 和机房限制。
- 不要先删除注册文件。确需重新注册时，备份私密配置并确认后，通过 `5 -> 7` 注册；运行中的 sing-box 不会自动读取新注册信息，需要重新生成相匹配的配置并校验。
- 新版原生 WireGuard 使用顶层 `endpoints`，不要误放进 `outbounds`。下载器只使用官方最新稳定版；获取失败会停止，不自动走第三方镜像或回退旧版。脚本模板仍需与实际安装版本通过 `sing-box check` 验证，失败时检查配置迁移而非盲目降级。

## 凭据与产物

- 服务配置/证书/注册信息：`/etc/sing-box/`。
- 分享链接及 Clash Meta/Mihomo/FlClash 配置：`/root/proxy_info/`。主菜单 `2` 会显示完整凭据，仅在用户需要取回时使用。
- 每次运行的私有日志目录：`${TMPDIR:-/tmp}/proxy_setup_logs.XXXXXXXX`（启动时显示实际路径）。默认关闭 xtrace；显式 `TRACE_ENABLED=1` 才开启调试跟踪。
- 默认新建文件限制为所有者访问，但屏幕输出日志、节点链接、二维码和 YAML **仍含凭据**。不公开发送、不贴整份配置到排障记录；交付前确认接收渠道。

## 深度卸载范围

主菜单 `4` 再确认 `y` 后，会删除 sing-box 服务/二进制、`/etc/sing-box`（含备份、WireGuard 注册及证书）、`/root/proxy_info`、本次脚本日志和相关临时文件，并清理入站端口规则、**Cloudflare WARP 客户端及其源/keyring/状态**。既有、非本脚本创建的 WARP 也可能受到影响。

不要在共享服务器上将其当作“仅删除一个节点”。卸载不删除脚本文件，也不会清理云安全组；历史日志目录可能仍需单独检查。通用依赖（curl、openssl、ca-certificates 等）默认保留，不能为了“清理干净”删除其他服务可能依赖的软件。
