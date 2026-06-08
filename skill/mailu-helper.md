```!
Mailu 邮件服务器部署与运维辅助：当用户要安装/启动 Mailu、检查 Mailu 环境、配置证书挂载、检查宿主机反向代理、排查 DNS/MX/SPF/DKIM/DMARC/PTR、创建或重置管理员、配置根域名或任意子域名 catch-all、查看邮件客户端参数、查看 Mailu 日志、备份/恢复、卸载辅助配置、管理证书监控服务时使用本技能。脚本位置固定为 skill/script/mailu-helper/mailu-helper.sh。常用命令：chmod +x skill/script/mailu-helper/mailu-helper.sh && sudo bash skill/script/mailu-helper/mailu-helper.sh；也可使用子命令 detect/check/dns/proxy/client/catchall/cert-service。脚本会写配置文件、修改 Mailu .env/compose、复制证书、写 Postfix overrides、创建 systemd 证书监控服务或重启 Docker Compose 服务，执行前必须确认当前机器就是目标 Mailu 服务器且用户允许改动这些配置。DNS 面板、PTR 反向解析和云厂商安全组通常不能由脚本自动完成，需要提醒用户到对应控制台手动处理。
```

# Mailu Helper 使用说明

本文是 Telegram AI Bot 项目内置 skill 文档，用于指导 Agent 模式在服务器上使用 `skill/script/mailu-helper/mailu-helper.sh` 部署、检查和维护 Mailu 邮件服务。

## 文件位置

```text
skill/mailu-helper.md
skill/script/mailu-helper/mailu-helper.sh
```

脚本用途：

- 安装或启动 Mailu。
- 自动识别 Mailu `docker-compose.yml` / `compose.yml`、`.env`、数据目录、证书目录和 Postfix overrides 目录。
- 检查 `.env` 核心项、Docker Compose 端口映射、监听端口、Web 入口和证书状态。
- 配置宿主机证书到 Mailu `/certs`，并可安装证书变更监控 systemd 服务。
- 检查宿主机反向代理场景下的 Mailu Web 端口配置。
- 检查 DNS：A、MX、SPF/TXT、DMARC、autoconfig、autodiscover 和任意子域名 catch-all 相关记录。
- 管理 Mailu 管理员、普通用户和密码。
- 引导根域名 catch-all，以及通过 Postfix overrides 配置任意子域名 catch-all。
- 输出 IMAP/SMTP 客户端配置。
- 查看 Mailu 容器日志。
- 备份或恢复 compose、env 和常见 Mailu 数据目录。
- 删除脚本创建的辅助配置。

## 运行方式

在项目根目录执行：

```bash
chmod +x skill/script/mailu-helper/mailu-helper.sh
sudo bash skill/script/mailu-helper/mailu-helper.sh
```

如果当前已经是 `root` 用户，可以去掉 `sudo`：

```bash
bash skill/script/mailu-helper/mailu-helper.sh
```

不带参数会进入交互菜单。菜单包含：

```text
1. 安装 / 启动 Mailu
2. 环境检查
3. 配置证书挂载
4. 检查反向代理
5. DNS 检查
6. 管理员账号
7. 根域名 catch-all 引导
8. 任意子域名 catch-all 管理
9. 任意身份发信检查
10. 邮件客户端配置说明
11. 日志查看
12. 备份 / 恢复
13. 卸载辅助配置
14. 常驻服务管理
```

## 子命令

脚本也支持直接执行常用动作：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh detect
sudo bash skill/script/mailu-helper/mailu-helper.sh check
sudo bash skill/script/mailu-helper/mailu-helper.sh dns
sudo bash skill/script/mailu-helper/mailu-helper.sh proxy
sudo bash skill/script/mailu-helper/mailu-helper.sh client
sudo bash skill/script/mailu-helper/mailu-helper.sh catchall
sudo bash skill/script/mailu-helper/mailu-helper.sh cert-service
```

子命令说明：

| 子命令 | 用途 |
| --- | --- |
| `detect` | 自动识别 Mailu compose/env/数据目录并保存配置 |
| `check` | 环境检查，包含 env、端口、监听、Web、证书等 |
| `dns` | DNS 检查，并提示 DKIM/PTR/DMARC/catch-all 记录 |
| `proxy` | 检查宿主机反向代理到 Mailu Web 的本地端口 |
| `client` | 输出 IMAP/SMTP 客户端配置 |
| `catchall` | 进入任意子域名 catch-all 管理 |
| `cert-service` | 管理证书监控常驻服务 |

配置文件默认保存在：

```text
/root/.mailu-helper.conf
```

如果不是 root 且没有指定 `MAILU_HELPER_CONFIG`，脚本会回退到当前用户家目录：

```text
~/.mailu-helper.conf
```

可通过环境变量指定配置文件：

```bash
MAILU_HELPER_CONFIG=/path/to/mailu-helper.conf sudo bash skill/script/mailu-helper/mailu-helper.sh
```

## Agent 使用前检查

执行脚本前先确认：

- 当前 shell 所在机器是目标 Mailu 服务器。
- 用户允许改动 Docker Compose、Mailu `.env`、证书目录、Postfix overrides、systemd 服务和容器状态。
- 当前用户有 root 或 sudo 权限。
- 服务器已安装或允许安装 Docker / Docker Compose。
- 改动端口、防火墙、反向代理、证书或 DNS 不会影响现有业务。

优先先运行：

```bash
ls -lah skill/script/mailu-helper
sudo bash skill/script/mailu-helper/mailu-helper.sh detect
sudo bash skill/script/mailu-helper/mailu-helper.sh check
```

如果脚本自动识别不到 compose/env 路径，按提示输入完整路径。

## 安装 / 启动 Mailu

当用户要求安装 Mailu、启动 Mailu、生成 Mailu 配置、配置邮件域名时，使用菜单第 1 项。

脚本会根据实际情况引导：

- 使用现有 Mailu 配置启动或检查。
- 生成 Mailu `.env` 和 compose。
- 设置 `DOMAIN`、`HOSTNAMES`、`TLS_FLAVOR`、Web 管理、Webmail、API、WebDAV、邮件端口绑定 IP 等。
- 启动 Docker Compose 服务。

注意：

- 邮件端口 `25/465/587/993` 不应只绑定 `127.0.0.1`，否则外部邮件服务器或客户端无法连接。
- 宿主机反向代理 Web 的场景通常让 Mailu Web HTTP 绑定到 `127.0.0.1:某端口`。
- 脚本不能替用户修改云厂商安全组；外部访问失败时提醒用户放行对应 TCP 端口。

## 环境检查

用户要求排查 Mailu、检查邮件服务、检查端口、检查证书或“看看有没有问题”时，运行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh check
```

检查重点：

- `.env` 关键项：`DOMAIN`、`HOSTNAMES`、`TLS_FLAVOR`、`WEB_ADMIN`、`WEB_WEBMAIL`、`WEBSITE`。
- `front` 服务端口映射，尤其是 Web 80、邮件 25/465/587/993。
- 本机监听端口。
- 本地 Web 入口是否可访问。
- `/certs` 中证书是否存在且有效。

宿主机反向代理场景下，`TLS_FLAVOR=mail` 通常更合适；脚本可能会询问是否自动修改。

## 证书挂载

用户要求让 Mailu 使用已有 HTTPS 证书、面板证书、Caddy/Nginx/宝塔/1Panel 证书，或邮件客户端 TLS 证书异常时，使用菜单第 3 项或：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh cert-service
```

脚本支持：

- 手动复制证书和私钥到 Mailu 证书目录。
- 自动搜索并复制证书对。
- 写入面板证书更新 hook。
- 创建证书监控 systemd 服务，证书变化时同步到 Mailu 并重启相关容器。

证书注意事项：

- 私钥内容不要回复给用户，不要暴露到聊天记录。
- 证书文件通常会写到 Mailu `/certs` 对应宿主机目录。
- 证书更新后需要让 Mailu 相关服务重新加载或重启。
- 如果用户使用面板或 Caddy 自动续签，优先用证书监控服务或 hook 同步。

## 反向代理

用户要求配置或排查 Mailu Web 反代时，运行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh proxy
```

脚本会检查 Mailu Web 的本地 HTTP 端口。常见建议：

- 宿主机 Caddy/Nginx 反代到 `127.0.0.1:WEB_HTTP_PORT`。
- Mailu 的邮件端口仍然直接对公网开放，不通过普通 Web 反代。
- 不要把邮件端口误绑到 `127.0.0.1`。
- 如使用 Caddy，可结合 `skill/web-deploy.md` 处理站点和 TLS 反代。

## DNS 检查

用户要求配置邮件 DNS、检查收发信、MX/SPF/DKIM/DMARC/PTR、任意子域名邮箱或 catch-all 时，运行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh dns
```

脚本会检查：

- 邮件主机名 A 记录。
- 根域名 MX。
- 根域名 SPF/TXT。
- `_dmarc` TXT。
- `autoconfig` 和 `autodiscover` CNAME。
- 任意子域名 catch-all 的通配 MX/SPF 是否生效。

需要提醒用户：

- DKIM 通常要从 Mailu 后台对应域名页面复制。
- PTR 反向解析要在 VPS/服务器商后台设置到邮件主机名，普通 DNS 面板通常改不了。
- DMARC 报告发到其他域名时，需要接收报告域名配置 `_report._dmarc` 授权。
- 通配记录 `*.example.com` 不包含根域名 `example.com`，根域名和通配记录要分别配置。

## 管理员和用户

用户要求创建管理员、重置管理员密码、创建普通用户时，使用菜单第 6 项。

脚本会执行类似：

```bash
docker compose exec admin flask mailu admin 用户名 域名 密码
docker compose exec admin flask mailu password 用户名 域名 密码
docker compose exec admin flask mailu user 用户名 域名 密码
```

注意：

- Mailu 没有通用默认账号密码。
- 密码输入不要回显，不要把用户密码原样回复到聊天里。
- 创建管理员后告诉用户登录地址和账号即可。

## Catch-all

根域名 catch-all 使用菜单第 7 项。

脚本会提示 Mailu 后台方式：

```text
Aliases -> 添加 *@example.com -> admin@example.com
```

任意子域名 catch-all 使用菜单第 8 项或：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh catchall
```

脚本会通过 Postfix overrides 管理：

```text
wildcard_domains
wildcard_aliases
subdomain-catchall.list
postfix.cf 中的 MAILU_HELPER_WILDCARD 管理块
```

可选择临时开启或长效开启。长效方式会写 overrides 文件并重启 `smtp` 服务。

重要边界：

- 根域名 catch-all 不需要额外 DNS；邮箱前缀不参与 DNS 查询。
- 收 `*@任意子域.example.com` 才需要额外通配 MX/SPF。
- 删除脚本辅助配置只删除脚本创建的 wildcard 文件和管理块，不删除邮件数据。

## 客户端配置

用户询问手机、电脑、Outlook、Thunderbird、Apple Mail 等客户端如何填写时，运行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh client
```

默认说明：

```text
IMAP: 服务器 mail.example.com，端口 993，SSL/TLS，用户名完整邮箱地址
SMTP: 服务器 mail.example.com，端口 587，STARTTLS，用户名完整邮箱地址
备用 SMTP: 端口 465，SSL/TLS
```

不要建议客户端使用 25 登录；25 是服务器之间投递邮件用。

## 日志、备份和卸载

查看日志使用菜单第 11 项。可查看全部日志或 `front`、`smtp`、`imap`、`admin`、`antispam`。

备份/恢复使用菜单第 12 项。备份会打包：

```text
compose 文件
env 文件
MAILU_DATA_DIR/data
MAILU_DATA_DIR/dkim
MAILU_DATA_DIR/mail
MAILU_DATA_DIR/certs
MAILU_DATA_DIR/overrides
MAILU_DATA_DIR/filter
```

卸载辅助配置使用菜单第 13 项。它只删除脚本做过的辅助配置，不删除：

- 邮件数据。
- 用户数据。
- DKIM。
- compose 或 env。

如用户要求删除 Mailu 本体或邮件数据，必须单独确认具体路径和后果。

## 常见排查命令

```bash
docker compose ps
docker compose logs -f front
docker compose logs -f smtp
docker compose logs -f imap
ss -lntp
curl -I http://127.0.0.1:端口/
dig A mail.example.com
dig MX example.com
dig TXT example.com
dig TXT _dmarc.example.com
openssl s_client -connect mail.example.com:993 -servername mail.example.com
openssl s_client -connect mail.example.com:587 -starttls smtp -servername mail.example.com
```

如果脚本已识别 compose/env，优先通过脚本菜单操作，减少路径填错和重复判断。

## 安全注意事项

- 不要公开 `.env`、私钥、管理员密码、API token 或完整配置文件敏感内容。
- 修改 `.env`、`postfix.cf`、证书文件前脚本通常会自动备份；仍要注意用户是否接受服务重启。
- DNS/PTR/安全组通常需要用户在控制台手动操作，脚本只能检查和提示。
- 邮件服务对公网端口、DNS 声誉和 PTR 依赖很强；本机检查正常不代表外部投递一定正常。
- 删除和恢复操作有覆盖风险；执行前确认目标路径。

## 推荐给用户的简短结果说明

部署或修复完成后可以这样回复：

```text
Mailu 这边已经处理完。

Web 管理入口：
https://mail.example.com/admin/

客户端参数：
IMAP 993 SSL/TLS
SMTP 587 STARTTLS
用户名用完整邮箱地址

接下来需要在 DNS/服务器商后台确认：MX、SPF、DKIM、DMARC、PTR 和云安全组端口。
```
