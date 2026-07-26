```!
Mailu 辅助技能：用于部署、检查和维护 Mailu，并通过 Postfix overrides 实现任意子域名的邮件接收。执行前必须确认当前机器就是目标 Mailu 服务器，并确认用户允许修改 Docker Compose、Mailu `.env`、证书、Postfix overrides、systemd 服务和容器状态。
```

# Mailu Helper 使用说明

本文是 XGent for Telegram 项目内置 skill 文档，说明 `skill/script/mailu-helper/mailu-helper.sh` 的用途、菜单、实际修改项和提示项。

## 文件位置

```text
skill/mailu-helper.md
skill/script/mailu-helper/mailu-helper.sh
```

## 示例和隐私约定

本文档和脚本提示中的示例域名统一使用保留示例域名：

```text
example.com
mail.example.com
```

不得把用户真实域名、邮箱地址、服务器 IP、账号或其他个人配置硬编码进脚本或 md 文档。运行脚本时应从 Mailu 配置、helper 配置或用户输入动态读取实际域名。

## 一句话说明

这个脚本不是单一的“泛域名邮件脚本”，而是四类功能的组合：

1. Mailu 安装、启动、环境检查、账号、日志、备份。
2. Mailu 证书复制、证书续期同步和 systemd 证书监控。
3. Cloudflare/DNS 检查和 DNS 配置说明。
4. 通过 Postfix overrides 实现 `任意前缀@任意子域名.主域名` 的接收。

脚本中同时存在“会修改系统的执行功能”和“只打印教程的提示功能”，菜单会用标签区分。

## 主菜单

```text
====== Mailu Helper ======
★ 表示实现“任意子域名任意前缀安全收发”的必要步骤；已完成的项目无需重复执行。

一、安装与维护
  1. ★ [执行] 安装/启动 Mailu
  2.   [检查] Mailu 环境状态
  3.   [执行] 管理管理员和用户
  4.   [查看] 查看日志
  5.   [执行] 备份/恢复

二、邮件收发
  6.   [说明] 根域名 catch-all 配置
  7. ★ [执行] 任意子域名接收
  8. ★ [说明] 任意地址发信配置
  9.   [测试] 测试 Postfix 通配规则

三、DNS 与网络
 10. ★ [说明] 生成 Cloudflare DNS 记录
 11.   [检查] 检查 DNS
 12.   [检查] 检查反向代理
 13. ★ [执行] 配置证书

四、其他
 14.   [说明] 邮件客户端参数
 15.   [执行] 卸载 helper 辅助配置
  0. 退出
```

### 标签含义

| 标签 | 含义 |
| --- | --- |
| `[执行]` | 会写文件、修改 Mailu、修改 Postfix、重启容器或操作 systemd |
| `[检查]` | 只读取状态、端口、DNS 或配置，不主动修改目标配置 |
| `[查看]` | 只查看日志或状态 |
| `[说明]` | 只打印人工操作教程，不自动完成外部平台操作 |
| `[测试]` | 对已经存在的配置执行验证，不生成配置 |
| `★` | 实现本项目“任意子域名任意前缀收发”时需要重点执行的步骤 |

注意：菜单 10 标记为 `★`，但它不会直接写 Cloudflare。它只生成记录说明，仍需要用户在 Cloudflare 面板手动添加。

## 运行方式

在项目根目录执行：

```bash
chmod +x skill/script/mailu-helper/mailu-helper.sh
sudo bash skill/script/mailu-helper/mailu-helper.sh
```

不带参数会进入交互菜单。如果已经是 root，可以去掉 `sudo`。

## 子命令

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh detect
sudo bash skill/script/mailu-helper/mailu-helper.sh check
sudo bash skill/script/mailu-helper/mailu-helper.sh dns
sudo bash skill/script/mailu-helper/mailu-helper.sh proxy
sudo bash skill/script/mailu-helper/mailu-helper.sh client
sudo bash skill/script/mailu-helper/mailu-helper.sh catchall
sudo bash skill/script/mailu-helper/mailu-helper.sh postfix-test
sudo bash skill/script/mailu-helper/mailu-helper.sh cloudflare-dns
sudo bash skill/script/mailu-helper/mailu-helper.sh cert-service
```

| 子命令 | 用途 | 是否修改 |
| --- | --- | --- |
| `detect` | 自动识别 compose、env、数据目录并保存配置 | 会保存 helper 配置 |
| `check` | 检查 env、端口、监听、Web、证书 | 通常只检查 |
| `dns` | 检查 DNS 并显示 Cloudflare/DMARC/catch-all 说明 | 只检查和提示 |
| `proxy` | 检查宿主机反向代理到 Mailu Web 的本地端口 | 只检查 |
| `client` | 输出 IMAP/SMTP 客户端参数 | 只提示 |
| `catchall` | 进入任意子域名 catch-all 管理 | 可能修改 Postfix |
| `postfix-test` | 测试已有 Postfix 通配规则 | 只测试 |
| `cloudflare-dns` | 输出 Cloudflare DNS 记录说明 | 不调用 Cloudflare API |
| `cert-service` | 管理证书监控 systemd 服务 | 可能修改 systemd |

配置文件默认是：

```text
/root/.mailu-helper.conf
```

非 root 且没有指定 `MAILU_HELPER_CONFIG` 时，回退到：

```text
~/.mailu-helper.conf
```

## 针对本项目需求的推荐顺序

目标是实现：

```text
任意前缀@任意子域名.example.com
```

例如：

```text
anything@abc.example.com
hello@random.example.com
test@a.b.example.com
```

推荐顺序：

```text
1. ★ 菜单 1：安装/启动 Mailu
2. ★ 菜单 13：复制并挂载邮件 TLS 证书
3. ★ 菜单 7：长效开启任意子域名接收
4. ★ 菜单 10：查看 Cloudflare 记录说明并手动添加 DNS
5. 菜单 11：检查 DNS
6. 菜单 9：测试 Postfix 通配规则
7. ★ 菜单 8：给专用账号开启任意身份发信权限
```

如果 Mailu、证书或 DNS 已经配置完成，不需要重复执行对应步骤。

## 各菜单实际做什么

### 1. 安装/启动 Mailu

实际执行：

- 识别或生成 `docker-compose.yml` 和 `.env`。
- 设置 `DOMAIN`、`HOSTNAMES`、`TLS_FLAVOR`、Web 管理、Webmail、邮件端口等。
- 创建 Mailu 数据目录和 overrides 目录。
- 启动或重启 Docker Compose 服务。

邮件端口 `25/465/587/993` 不能只绑定 `127.0.0.1`，否则外部邮件服务器或邮件客户端无法连接。Web 反向代理可以使用 `127.0.0.1` 的本地 HTTP 端口，但邮件端口通常需要直接对公网开放。

### 2. Mailu 环境状态

只做检查，主要包括：

- `.env` 关键项。
- Docker Compose 服务和端口映射。
- 本机监听端口。
- Web 入口。
- `/certs` 中的 `cert.pem` 和 `key.pem`。

### 3. 管理管理员和用户

实际调用 Mailu admin 容器中的命令，可用于：

- 创建管理员。
- 重置管理员密码。
- 创建普通用户。

密码不会回显。不要把密码、`.env`、私钥或 API token 回复到聊天中。

### 4. 查看日志

只读取日志，可查看：

```text
全部日志
front
smtp
imap
admin
antispam
```

### 5. 备份/恢复

备份和恢复会实际读写文件，恢复前需要确认目标路径。通常包括：

```text
compose 文件
.env 文件
MAILU_DATA_DIR/data
MAILU_DATA_DIR/dkim
MAILU_DATA_DIR/mail
MAILU_DATA_DIR/certs
MAILU_DATA_DIR/overrides
MAILU_DATA_DIR/filter
```

### 6. 根域名 catch-all 说明

这项目前是提示功能，不会自动调用 Mailu API 创建别名。

Mailu 后台操作路径：

```text
Mail domains
  -> 选择主域名
  -> Aliases
  -> 添加新别名
```

新版 Mailu 使用 SQL LIKE 通配符，因此应填写：

```text
Alias: %
Use SQL LIKE Syntax: 勾选
Destination: admin@example.com
```

不要只填写：

```text
*
```

`*` 不是当前 Mailu SQL LIKE catch-all 的正确写法，可能只会被当作字面量别名。

根域名 catch-all 处理的是：

```text
anything@example.com
```

它不负责让 Mailu 自动认识：

```text
anything@random.example.com
```

### 7. 任意子域名接收

这是脚本最核心的实际配置功能之一。

Mailu UI 原生可以配置当前域名的 SQL LIKE catch-all，但不能在 UI 中声明无限随机子域名。要实现：

```text
anything@abc.example.com
hello@random.example.com
test@a.b.example.com
```

脚本会在 Postfix overrides 中生成：

```text
wildcard_domains
wildcard_aliases
subdomain-catchall.list
postfix.cf 中的 MAILU_HELPER_WILDCARD 管理块
```

长效配置路径：

```text
主菜单 7
  -> 2. 长效开启某个域名
```

或者如果列表已经存在：

```text
主菜单 7
  -> 3. 长效开启列表内全部域名
```

核心 Postfix 配置为：

```postfix
virtual_mailbox_domains = regexp:/overrides/wildcard_domains, socketmap:unix:/tmp/podop.socket:domain
virtual_alias_maps = socketmap:unix:/tmp/podop.socket:alias, regexp:/overrides/wildcard_aliases
```

这里必须把 Mailu 内置 `socketmap` 放在 wildcard regexp 前面，否则可能抢先匹配正常用户和正常别名。

脚本支持两种模式：

- 临时开启：写入正在运行的 smtp 容器，容器重建或重启后可能失效。
- 长效开启：写入宿主机 overrides，重启 smtp 后持续生效。

目标接收邮箱必须是已经存在的 Mailu 本地用户或别名，例如：

```text
admin@example.com
```

### 8. 任意地址发信说明

这项目前主要是提示功能，不会自动修改 Mailu 用户权限。

要让真实登录账号：

```text
admin@example.com
```

发送具体地址：

```text
From: random@abc.example.com
```

需要在 Mailu 用户设置中开启：

```text
Allow the user to spoof the sender
```

重要安全边界：Mailu 的 sender spoofing 通常是“允许该账号作为任意地址发信”，不是只限制在 `example.com` 内。因此只应该给专用账号开启，并设置强密码、合理限速。

Webmail 通常不能保存 `*@*.example.com` 这种通配身份，需要添加具体身份；SMTP/API 客户端可以逐封指定具体 From。

### 9. 测试 Postfix 通配规则

该菜单只测试已经存在的配置，不写入新规则。

它会：

1. 读取 `wildcard_domains` 和 `wildcard_aliases`。
2. 显示 smtp 容器当前的 `virtual_mailbox_domains` 和 `virtual_alias_maps`。
3. 对 `test.<domain>` 和 `anything@test.<domain>` 执行 `postmap -q`。
4. 判断是否命中并显示最终目标邮箱。

也可以直接执行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh postfix-test
```

### 10. Cloudflare DNS 记录说明

脚本不会自动登录 Cloudflare 或调用 Cloudflare API。它只输出记录格式。

以 `example.com` 为例，建议记录为：

```text
A    mail       服务器公网 IP                         DNS only
MX   @          mail.example.com                      优先级 10
MX   *          mail.example.com                      优先级 10
TXT  @          "v=spf1 mx a:mail.example.com ~all"
TXT  *          "v=spf1 mx a:mail.example.com ~all"
TXT  _dmarc     "v=DMARC1; p=reject; sp=reject; adkim=r; aspf=r"
TXT  DKIM 名称   Mailu 后台提供的 DKIM 内容
```

其中：

- Cloudflare 中 MX 的 Name 填 `*`，目标是 `mail.example.com`。
- `mail.example.com` 的 A/AAAA 必须为 DNS only 灰云。
- Cloudflare 普通 HTTP 代理不能代理 SMTP/IMAP。
- 通配 `*` 不包含根域名 `@`，根域名和通配记录都要配置。
- 如果某个具体子域已有 A、CNAME、MX 等记录，可能遮蔽通配记录，需要单独为该子域配置 MX/SPF。

### 11. DNS 检查

脚本只检查 DNS，不替用户写 DNS：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh dns
```

会检查：

- `mail.example.com` 的 A 记录。
- 根域名 MX。
- 根域名 TXT/SPF。
- `_dmarc` TXT。
- `autoconfig` 和 `autodiscover`。
- 随机子域名的通配 MX/SPF。

PTR 反向解析需要在 VPS/服务器商后台设置，通常不能在普通 DNS 面板中修改。

### 12. 反向代理检查

只检查 Mailu Web 的本地 HTTP 端口和宿主机反向代理情况。

邮件端口不能通过普通 HTTP 反向代理代替，SMTP/IMAP 仍需要直接连接 Mailu 邮件服务。

### 13. 配置证书

证书菜单位于：

```text
主菜单 13
```

这个菜单的核心不是“申请证书”，而是把已有证书正确放到 Mailu 需要的位置，并在证书更新后让 Mailu front 重新加载。

## 证书更新使用三个目录

自动续期时必须把三个目录分清：

### 目录 1：原证书目录

这是证书管理工具申请或续期证书后保存原始文件的位置，通常由证书管理工具内部维护。常见文件为：

```text
目录1/fullchain.pem
目录1/privkey.pem
```

目录 1 只保存原证书，不建议直接改名、覆盖，也不建议让 Mailu 直接依赖证书管理工具的内部目录。

### 目录 2：本地部署/中转目录

在证书管理工具中配置“复制目录”“部署目录”或“推送目录”时，填写目录 2。证书续期后，证书管理工具负责：

```text
目录1/fullchain.pem -> 目录2/fullchain.pem
目录1/privkey.pem   -> 目录2/privkey.pem
```

目录 2 是稳定的本地交接位置。helper 生成的后置脚本从目录 2 读取证书，而不是直接修改目录 1。默认建议放在 Mailu Compose 目录下，例如：

```text
Mailu Compose 目录/cert-deploy
```

### 目录 3：Mailu 证书目标目录

目录 3 是 Mailu Compose 挂载到容器 `/certs` 的宿主机目录。它由 helper 从 Compose 自动识别。Mailu 最终需要：

```text
目录3/cert.pem
目录3/key.pem
```

容器内对应：

```text
/certs/cert.pem
/certs/key.pem
```

helper 生成的后置脚本负责：

```text
目录2/fullchain.pem -> 目录3/cert.pem
目录2/privkey.pem   -> 目录3/key.pem
重启 Mailu front
```

### 完整文件流向

```text
目录1：原证书目录
  fullchain.pem / privkey.pem
          ↓ 证书管理工具复制、部署或推送
目录2：本地部署/中转目录
  fullchain.pem / privkey.pem
          ↓ helper 后置脚本复制并改名
目录3：Mailu 宿主机目标目录
  cert.pem / key.pem
          ↓ Compose 挂载
容器：/certs/cert.pem / /certs/key.pem
```

选项 4 适合证书管理工具能够执行“目录 1 -> 目录 2”和续期后置脚本的情况。选项 5 适合没有这种后置功能的情况：监控脚本会自己完成“目录 1 -> 目录 2 -> 目录 3”。

### 忘记时只看这里：三目录速查卡

```text
目录1 = 原证书目录
作用：保存证书管理工具申请、续期后的原始证书
文件：fullchain.pem、privkey.pem
谁负责：证书管理工具
注意：不要改名，不要覆盖，不要直接当作 Mailu 目标目录

目录2 = 本地部署/中转目录
作用：接收证书管理工具从目录1复制出来的证书
文件：fullchain.pem、privkey.pem
谁负责写入：证书管理工具
谁负责读取：helper 生成的后置脚本
建议：使用 Compose 目录下独立的 cert-deploy 目录

目录3 = Mailu 目标目录
作用：保存 Mailu 真正使用的证书
文件：cert.pem、key.pem
谁负责写入：helper 后置脚本
如何识别：从 Compose 的 /certs 挂载自动识别
容器内：/certs/cert.pem、/certs/key.pem
```

最短记忆方式：

```text
证书工具：目录1 -> 目录2
helper 脚本：目录2 -> 目录3并改名 -> 重启 front
```

不要配置成：

```text
目录2 = 目录3
```

目录 2 和目录 3 应保持独立，这样可以清楚判断证书管理工具是否成功部署、helper 是否成功复制，以及 Mailu 最终用了哪一份证书。

### 忘记时只看这里：证书管理工具填写对照

| 输入框可能显示的名称 | 应该填写什么 |
| --- | --- |
| 原证书目录 | 通常由证书管理工具内部维护，即目录 1，不需要修改 |
| 证书复制目录 | 填目录 2 |
| 证书部署目录 | 填目录 2 |
| 证书推送目录 | 填目录 2 |
| 续期后执行脚本 | 填 `update-mailu-cert.sh` 完整路径 |
| 后置脚本路径 | 填 `update-mailu-cert.sh` 完整路径 |
| 执行命令 | `bash /后置脚本完整路径 /目录2完整路径` |
| 脚本内容 | 粘贴 helper 最后打印的完整脚本内容 |
| Mailu 证书目录 | 不要猜；由 helper 从 Compose 的 `/certs` 挂载自动识别为目录 3 |

证书管理工具续期成功后的正确顺序是：

```text
1. 更新目录1中的原证书
2. 把 fullchain.pem、privkey.pem 复制到目录2
3. 调用 update-mailu-cert.sh
4. 后置脚本检查目录2中的域名、有效期和公私钥
5. fullchain.pem -> 目录3/cert.pem
6. privkey.pem   -> 目录3/key.pem
7. 重启 Mailu front
```

### 忘记时只看这里：首次配置步骤

```text
1. 运行 Mailu Helper
2. 进入主菜单 13“配置证书”
3. 选择 4“自动续期/有管理工具”
4. 查看脚本自动找到的目录1
5. 确认脚本自动识别的目录3
6. 直接回车使用建议的目录2，或填写一个独立的本地中转目录
7. 允许脚本把当前证书从目录1初始化复制到目录2
8. 直接回车使用建议的 update-mailu-cert.sh 保存路径
9. 把目录2填到证书管理工具的复制/部署/推送目录
10. 把 update-mailu-cert.sh 填到续期后执行脚本
11. 手动运行一次测试命令
12. 使用证书菜单 7检查 Mailu 最终证书
```

测试命令格式：

```bash
bash /后置脚本完整路径 /目录2完整路径
```

测试后应该存在：

```text
目录2/fullchain.pem
目录2/privkey.pem
目录3/cert.pem
目录3/key.pem
```

并且脚本应显示 Mailu `front` 已重启。

### 三目录配置保存在哪里

helper 会把识别和确认后的路径保存到：

```text
/root/.mailu-helper.conf
```

相关配置项为：

```text
CERT_SOURCE_DIR=目录1
CERT_STAGE_DIR=目录2
CERT_DIR=目录3
CERT_UPDATE_SCRIPT=后置脚本完整路径
CERT_WATCHER_SCRIPT=监控脚本完整路径
```

其中：

- `CERT_UPDATE_SCRIPT` 是选项 4 的一次性续期后置脚本。
- `CERT_WATCHER_SCRIPT` 是选项 5 的常驻监控脚本。
- 两者分开保存，不能混用。

### 三目录常见错误排查

#### 目录 2 没有证书

说明证书管理工具没有完成“目录 1 -> 目录 2”。检查复制、部署或推送目录是否填写成脚本显示的目录 2。

#### 目录 2 有证书，目录 3 没有

说明后置脚本没有运行或运行失败。手动执行：

```bash
bash /后置脚本完整路径 /目录2完整路径
```

然后查看脚本输出的域名、公私钥和文件路径错误。

#### 目录 3 有文件，但客户端仍显示旧证书

检查：

1. 目录 3 中是否确实是新的 `cert.pem`、`key.pem`。
2. 证书和私钥是否匹配。
3. 证书是否覆盖客户端连接的邮件主机名。
4. Mailu `front` 是否成功重启。
5. 客户端实际连接的主机名是否正确。

#### 提示证书不匹配邮件主机

不要强行复制。说明选择的证书 SAN 没有覆盖当前邮件主机，应返回选项 1重新搜索或重新申请正确证书。

#### 提示证书与私钥不匹配

不要把不同目录、不同签发批次的证书和私钥拼在一起。应重新从同一个目录、同一次签发结果中取得 `fullchain.pem` 和 `privkey.pem`。

#### 多个目录看起来都有同一张泛域名证书

不要根据目录名判断。查看 helper 显示的：

```text
SHA256 指纹
SAN 域名
到期时间
公私钥匹配结果
```

指纹相同表示证书内容相同；指纹不同则不是完全相同的一份证书。

### 推荐操作：不确定时直接选 1

进入：

```text
主菜单 13
  -> 1. 自动诊断/推荐
```

选项 1 不要求用户先知道证书在哪里。脚本会自动执行：

1. 从 Mailu compose 挂载中识别宿主机证书目标目录。
2. 显示 compose 文件、邮件主机、宿主机目标目录、容器 `/certs` 目录。
3. 检查现有 `cert.pem` 和 `key.pem`。
4. 搜索常见证书目录。
5. 对每个候选显示完整源目录、证书文件、私钥文件、SHA256 指纹和到期时间。
6. 验证证书是否匹配当前邮件主机。
7. 验证证书和私钥是否是一对。
8. 比较多个候选是否其实是同一张泛域名证书。
9. 自动选择匹配、有效、到期时间更合适的来源。
10. 如果当前 Mailu 证书已经正确且与最佳来源相同，不重复复制。
11. 如果缺少或需要更新，显示源路径和目标路径，确认后复制并重启 `front`。

自动搜索位置包括：

```text
Mailu 当前证书目标目录
Mailu compose 目录
/etc/letsencrypt/live
/root/.acme.sh
/www/server
/opt
/www
/data
/srv
/home
/etc/ssl
/root
```

搜索结果使用实际检测到的完整路径，不会只显示模糊名称。

### 多个目录中的泛域名证书是否一样

不能根据目录名判断证书是否相同。多个网站目录可能：

- 都部署了同一张泛域名证书。
- 使用不同时间签发的同名证书。
- 证书相同但私钥不同。
- 一个目录中的证书已经续期，另一个仍是旧证书。

脚本通过以下项目判断：

```text
SHA256 证书指纹
证书 SAN 域名
证书到期时间
证书公钥与私钥是否匹配
```

如果多个目录的 SHA256 指纹相同，并且对应私钥检查也通过，则这些目录中的证书内容相同，一次性复制时任选其中一份效果相同。新版菜单 1 会自动识别这种情况，不需要用户猜测。

### 证书菜单详细说明

```text
1. ★ [自动诊断/推荐] 自动寻找所有目录、检查配置，必要时复制并重启 front
2.   [搜索后手选] 自动搜索候选目录，显示详情后由用户选择
3.   [手动复制] 只填写源证书位置，目标目录自动识别
4.   [自动续期/有管理工具] 规划目录1/2/3，生成“目录2 -> 目录3”后置脚本
5.   [自动续期/无后置功能] 自动完成“目录1 -> 目录2 -> 目录3”监控同步
6.   [管理] 管理选项 5 生成的证书监控 systemd 服务
7.   [检查] 检查 Mailu 最终 cert.pem/key.pem、域名和有效期
8.   [说明] 显示所有目录、文件流向和填写教程
```

所有证书选项都会先自动识别并显示：

```text
Compose 文件
Env 文件
邮件主机
目录1：原证书目录
目录2：本地部署/中转目录
目录3：Mailu 宿主机目标目录
容器内 /certs 目录
原证书、中转证书和最终 cert.pem/key.pem 文件
```

如果目标目录能够从 Compose 的 `/certs` 挂载中识别，就不会再让用户重复填写；只有无法识别时才会要求输入。

#### 选项 1：自动诊断并修复

适用于所有情况，尤其适合不知道证书源目录、目标目录或当前配置是否正确的用户。它会：

1. 自动识别 Compose、env 和 `/certs` 的宿主机挂载。
2. 显示源目录、目标目录、容器目录和实际文件名。
3. 检查 Mailu 当前的 `cert.pem`、`key.pem`。
4. 搜索常见证书目录，并显示每个候选的完整路径。
5. 检查证书 SAN 是否覆盖当前邮件主机。
6. 检查证书和私钥是否是一对。
7. 显示 SHA256 指纹和到期时间。
8. 比较多个目录中的证书是否实际上相同。
9. 自动选择匹配、有效且更合适的证书；只有需要修复时才询问是否复制。
10. 复制后可重启 Mailu `front`。

不确定时直接使用选项 1，不需要先猜路径。

#### 选项 2：搜索后手动选择

脚本会自动搜索候选目录，但不会默认选择第一个。每个候选都会显示：

```text
完整源目录
源证书文件
源私钥文件
是否匹配邮件主机
证书与私钥是否匹配
SHA256 指纹
到期时间
```

用户必须输入编号后才会复制，输入 `0` 取消。适合存在多张证书、希望自己决定来源的情况。

#### 选项 3：手动指定源文件

只需要填写源证书位置，目标位置由脚本从 Mailu Compose 自动识别。源位置有两种填法：

```text
源目录：/path/to/certificate-directory
```

目录中通常应有：

```text
fullchain.pem
privkey.pem
```

或者直接填写：

```text
源证书文件完整路径
源私钥文件完整路径
```

复制前脚本会明确显示：

```text
源证书：.../fullchain.pem
源私钥：.../privkey.pem
目标证书：目标目录/cert.pem
目标私钥：目标目录/key.pem
容器文件：/certs/cert.pem 和 /certs/key.pem
```

#### 选项 4：有证书管理工具，生成证书更新后置脚本

这个选项采用明确的三个目录：

```text
目录1：证书管理工具保存原证书
目录2：证书管理工具复制/部署到本地的中转目录
目录3：Mailu Compose 挂载到 /certs 的宿主机目标目录
```

职责分工：

```text
证书管理工具：目录1 -> 目录2
helper 后置脚本：目录2 -> 目录3，并把文件改名后重启 front
```

执行选项 4 时，helper 会：

1. 自动寻找目录 1 中匹配邮件主机的原证书。
2. 显示目录 1 的证书文件、私钥、域名、SHA256 指纹和有效期。
3. 建议一个独立的目录 2，默认是 Compose 目录下的 `cert-deploy`。
4. 从 Compose 的 `/certs` 挂载自动识别目录 3。
5. 可选把当前证书从目录 1 初始化复制到目录 2，方便立即测试。
6. 生成 `update-mailu-cert.sh`，该脚本只读取目录 2，不修改目录 1。
7. 后置脚本检查目录 2 中证书的域名、公私钥和有效期。
8. 检查通过后执行：

```text
目录2/fullchain.pem -> 目录3/cert.pem
目录2/privkey.pem   -> 目录3/key.pem
```

9. 自动重启 Mailu `front`。

在证书管理工具中填写：

```text
原证书目录：目录1通常由工具内部维护，不需要手动修改
证书复制目录/部署目录/推送目录：填写 helper 显示的目录2
续期后执行脚本/后置脚本路径：填写 update-mailu-cert.sh 的完整路径
```

如果只有“执行命令”输入框：

```bash
bash /完整路径/update-mailu-cert.sh /目录2完整路径
```

如果只有“脚本内容”输入框，粘贴 helper 最后打印的完整脚本内容。

目录 2 和目录 3 必须是两个不同目录：

- 目录 2 保留 `fullchain.pem`、`privkey.pem`，用于接收证书管理工具的部署结果。
- 目录 3 保留 Mailu 需要的 `cert.pem`、`key.pem`。
- 后置脚本负责从目录 2 复制到目录 3 并改名。

#### 选项 5：没有续期后置功能，生成证书监控脚本

脚本会自动寻找目录 1、设置目录 2、识别目录 3 并生成 `watch-mailu-cert.sh`。它会按设定间隔：

1. 在目录 1 和常见证书位置搜索可用的 `fullchain.pem`/`privkey.pem`。
2. 检查邮件主机、有效期和公私钥。
3. 发现证书内容改变时先复制到目录 2，保留 `fullchain.pem`/`privkey.pem`。
4. 再从目录 2 复制到目录 3 并改名为 `cert.pem`/`key.pem`。
5. 重启 Mailu `front`。

选项 5 结束时可以直接安装并启动 systemd 服务，也可以稍后进入选项 6。

#### 选项 6：管理证书监控服务

只管理选项 5 生成的监控脚本：

```text
安装/覆盖服务
启动/重启服务
停止服务
查看状态和最近日志
卸载服务（不会删除证书和监控脚本）
```

脚本会把“选项 4 后置脚本”和“选项 5 监控脚本”分开记录，避免误把一次性后置脚本安装成常驻服务。

#### 选项 7：检查最终证书

只检查 Mailu 真实使用的：

```text
目标目录/cert.pem
目标目录/key.pem
```

并验证文件是否存在、当前邮件主机是否在 SAN 中、证书是否即将过期、公私钥是否匹配。它不会替换文件。

#### 选项 8：证书小白说明

显示当前自动识别的所有路径、文件流向、每个选项的用途，以及证书管理工具中应该填写什么。它不会复制证书。

### 多个邮件域名是否需要多个证书

如果所有邮件域名的 MX 都指向同一个邮件主机，例如：

```text
example.com  MX  mail.example.com
example.net  MX  mail.example.com
example.org  MX  mail.example.com
```

并且所有客户端都连接：

```text
mail.example.com
```

那么通常只需要一套匹配 `mail.example.com` 的证书。

邮箱地址后面的域名很多，不等于需要很多 TLS 证书。证书验证的是客户端连接的主机名，不是邮箱地址后缀。

每个邮件域名仍然需要分别配置自己的：

```text
MX
SPF
DKIM
DMARC
```

如果用户会连接多个不同的主机名，例如：

```text
mail.example.com
mail.example.net
smtp.example.org
```

那么证书必须通过 SAN 或通配符覆盖这些连接主机名。

任意随机子域名邮箱也不需要单独证书。只要邮件通过 MX 指向统一邮件主机，客户端连接的仍然是统一邮件主机。

### 14. 邮件客户端参数

只输出客户端配置：

```text
IMAP: mail.example.com，993，SSL/TLS，用户名使用完整邮箱地址
SMTP: mail.example.com，587，STARTTLS，用户名使用完整邮箱地址
备用 SMTP: 465，SSL/TLS
```

不要让客户端使用 25 登录。25 主要用于服务器之间投递邮件。

### 15. 卸载 helper 辅助配置

只删除脚本创建的辅助配置，例如：

```text
wildcard_domains
wildcard_aliases
subdomain-catchall.list
postfix.cf 中的 MAILU_HELPER_WILDCARD 管理块
证书监控 service
脚本生成的证书更新辅助脚本
```

不会主动删除：

```text
邮件数据
用户数据
DKIM
Mailu compose
Mailu .env
```

如果要删除 Mailu 本体或邮件数据，必须单独确认具体路径和后果。

## 两个容易混淆的操作

### 为什么要修改 Postfix

修改 Postfix 是为了实现：

```text
任意前缀@任意子域名.example.com
```

Cloudflare 的通配 MX 只负责把邮件送到服务器；Postfix 仍然需要知道随机子域名属于本机并把邮件转发到本地目标邮箱，否则可能出现：

```text
Relay access denied
```

如果只需要：

```text
任意前缀@example.com
```

则可以只在 Mailu UI 配置 `%` catch-all，不需要修改 Postfix。

### 为什么要复制证书

复制证书是为了让 Mailu 的 front/邮件入口使用可信 TLS，避免客户端在 465、587、993 上出现证书错误。

它与 Postfix 泛域名规则没有直接关系：

```text
Postfix overrides：解决随机子域名接收
证书复制：解决 SMTP/IMAP/HTTPS 的 TLS 加密
```

## DMARC、report 和 `r,r`

本 helper 默认不生成或推荐 `DMARC_RUA`、`DMARC_RUF` 报告地址：

```env
DMARC_RUA=
DMARC_RUF=
```

脚本不再输出跨域 `_report._dmarc` 授权建议，而是建议在 Cloudflare 手动维护 DMARC。

脚本中“两个改为 `r,r`”按以下含义处理：

```text
adkim=r
aspf=r
```

建议的 Cloudflare DMARC 内容为：

```text
v=DMARC1; p=reject; sp=reject; adkim=r; aspf=r
```

说明：helper 只能改变自己生成的 `.env` 和提示。Mailu admin 镜像自身的 Recommended DNS Records 由 Mailu 内部代码生成，因此后台可能仍显示 `_report._dmarc` 或 `adkim=s; aspf=s`。如果要从 Mailu UI 源代码中彻底删除这些推荐，需要构建自定义 Mailu admin 镜像，不建议直接修改运行中的容器。

## Agent 使用前检查

执行脚本前确认：

- 当前 shell 位于目标 Mailu 服务器。
- 用户允许修改 Docker Compose、`.env`、证书、Postfix overrides、systemd 和容器状态。
- 当前用户拥有 root 或 sudo 权限。
- 已安装或允许安装 Docker Compose。
- 修改端口、反向代理、证书或 DNS 不会影响现有业务。

优先执行：

```bash
sudo bash skill/script/mailu-helper/mailu-helper.sh detect
sudo bash skill/script/mailu-helper/mailu-helper.sh check
```

## 常见排查命令

```bash
docker compose ps
docker compose logs -f front
docker compose logs -f smtp
docker compose logs -f imap
ss -lntp
dig A mail.example.com
dig MX example.com
dig MX random.example.com
dig TXT example.com
dig TXT random.example.com
dig TXT _dmarc.example.com
openssl s_client -connect mail.example.com:993 -servername mail.example.com
openssl s_client -connect mail.example.com:587 -starttls smtp -servername mail.example.com
```

## 安全注意事项

- 不要公开 `.env`、私钥、管理员密码、API token 或完整配置文件敏感内容。
- 修改 `.env`、`postfix.cf`、证书文件前脚本通常会备份，但仍要确认用户接受服务重启。
- DNS、PTR、安全组和 Cloudflare 记录通常需要用户手动操作，脚本只能检查和提示。
- sender spoofing 可能允许账号使用任意发件地址，只给专用账号开启。
- 邮件服务依赖公网端口、MX、SPF、DKIM、DMARC、PTR 和 DNS 声誉；本机检查正常不代表外部投递一定成功。
- 删除和恢复操作有覆盖风险，执行前确认目标路径。

## 推荐给用户的简短结果说明

```text
Mailu 这边已经处理完。

Web 管理入口：
https://mail.example.com/admin/

邮件客户端：
IMAP 993 SSL/TLS
SMTP 587 STARTTLS
用户名使用完整邮箱地址

如果启用了任意子域名收件：
请确认 Cloudflare 已添加 MX @、MX *、SPF、DKIM、DMARC，并确认 mail 主机是 DNS only。

如果启用了任意地址发信：
请确认专用 Mailu 用户已开启 sender spoofing，并注意该权限范围较宽。
```
