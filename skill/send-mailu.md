```!
Mailu 发信/读信（使用既有配置，严禁向用户索要密码）。
- 发信：`python3 skill/script/send-mailu/send-mailu.py send --to "<收件人>" --from-addr "<发件人>" --subject "<主题>" --body "<正文>"`；`--from-addr` 必填，支持已配置域名及其子域的任意前缀；地址未知先运行 `sudo python3 skill/script/send-mailu/send-mailu.py check`，不得编造。
- `check`：逐账号检测 SMTP 发信配置和 IMAP/Sent 已发送归档功能，并输出可用发件邮箱。
- 读本地 EML：`python3 skill/script/send-mailu/send-mailu.py get "<EML绝对路径>"`。增强参数、配置和故障处理按需读取本文档。
```

# 邮件发信助手 (send-mailu)

本技能用于在不修改服务器配置、不向用户索要邮箱密码的前提下，提供极速、安全、合规的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 首次部署：生成发信配置

发信脚本依赖 `/etc/send-mailu/config.json`（含 SMTP 登录凭证，支持多域名）。首次部署或配置丢失时用 `check` 命令检查并交互配置：

```bash
sudo python3 skill/script/send-mailu/send-mailu.py check
```

`check` 会先检查配置状态，**若检查失败，自动提示「是否开始配置？」**，用户确认后进入交互配置流程。

配置时的账号发现顺序（两级回退）：

1. **优先扫描 Mailu SQLite 数据库**（`/mailu/data/main.db` 等，只读打开）：直接读到**全部域名**和**全部账号**列表（Mailu `domain` / `user` 表），无需手动罗列。
2. **数据库不可用时回退读 `mailu.env`**：Mailu 的环境变量配置文件（`/mailu/mailu.env`），里面通常只有 `DOMAIN=example.com`（单数主域名），只能推出一个 `admin@域名` 账号，无法枚举多域名。

发现域名/账号后，密码**无法自动获取**——Mailu 数据库里存的是 hash（SHA512-Crypt/bcrypt），不能逆向出明文用于 SMTP 登录。所以配置流程会列出发现到的账号，**逐个交互询问明文密码**（不回显）。

### 验证配置 & 获取可用账号清单

```bash
sudo python3 skill/script/send-mailu/send-mailu.py check
```

`check` 有三重用途：

- **健康检查**：文件存在 → JSON 合法 → 必填字段齐全 → 凭证非空 → 安全模式合法，逐项打印 ✓/✗。
- **账号发现**：对配置中的**每个邮箱逐个实测**：先逐账号登录 SMTP（只登录、不发测试邮件），再逐账号登录 IMAP 并检查该账号自己的 Sent 文件夹。输出会明确分成“基础配置 / SMTP 逐账号实测 / IMAP 逐账号实测 / 汇总”，不能把某一个账号的成功推断成全部账号成功。
- **自动引导配置**：检查失败时自动提示「是否开始配置？」，确认后进入交互配置流程。

SMTP 与基础配置全部 ✓ 则 exit 0；任一 SMTP/基础配置 ✗ 则打印失败项并提示配置。IMAP 归档仍属于“尽力而为”，每个账号都会实测和展示，但不改变发信配置的 exit code。

输出语义必须按以下规则理解：

- `default_domain`：仅在发件域名凭证匹配失败时作兜底；**不是自动发件地址，也不是归档仓库**。
- `--from-addr`：发信时必填。
- SMTP 行：只代表该行邮箱自己的 SMTP 登录结果。
- IMAP 行：只代表该行邮箱能否登录自己的 IMAP、找到自己的 Sent。
- 实际归档：哪个账号最终成功登录 SMTP 发出邮件，就用同一账号把副本写入它自己的 Sent。

- `check --no-connect`：跳过全部 SMTP/IMAP 实连测试，仅做格式检查（适合部署时网络未通或快速校验；此时不输出实测可用账号清单）。

### 非交互批量配置（脚本调用）

```bash
sudo python3 skill/script/send-mailu/send-mailu.py check \
  --domains "example.com,example.net,example.org,example.io" \
  --username-prefix admin --passwords "pw1,pw2,pw3,pw4" \
  --smtp-host 127.0.0.1 --smtp-port 587 --security starttls --force
```

- `--domains`：所有域名，逗号/分号/空格分隔。
- `--username-prefix`：每个域的账号前缀（默认 `admin`，生成 `admin@example.com` 等）。
- `--passwords`：各域密码，逗号/分号分隔，数量须与域名一致；**或只传一个密码广播到所有域**。

### AI 发信排查流程

```
当用户要发信时，AI 直接运行发信命令：
1. 运行 python3 send-mailu.py send --to ... --body ...
2. 若返回 exit code 2（配置缺失/损坏）→ 提示用户：
     "发信配置不存在，请在服务器运行： sudo python3 .../send-mailu.py check"
3. 用户运行 check → 自动检查 → 失败时提示配置 → 填完密码 → 重新发信
4. 若仍认证失败 → 提示用户运行 sudo python3 .../send-mailu.py check --force 重新配置
```

---

## 核心发信策略 (SMTP Delivery)

AI 不需要在对话中索要用户的明文密码。直接在服务器上调用本地已部署的安全发信脚本即可。

### 执行命令（最小调用）

python3 skill/script/send-mailu/send-mailu.py send \
  --to "<收件人>" \
  --from-addr "<发件人>" \
  --subject "<主题>" \
  --body "<正文>"

### 完整参数说明

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--to` | 是 | 收件人。多个地址用逗号/分号/空格分隔 |
| `--from-addr` | 是 | 发件人地址（脚本据此回溯匹配登录凭证）。可传纯邮箱，也可带显示名（如 `名称 <prefix@domain>`），脚本用 parseaddr 自动提取纯邮箱做信封路由，Header From 保留显示名 |
| `--subject` | 是 | 邮件主题 |
| `--body` | 否* | 纯文本正文。与 `--body-file` 互斥 |
| `--body-file` | 否* | 从文件读取纯文本正文（适合长正文，避免命令行转义/长度限制）。与 `--body` 互斥 |
| `--html` | 否 | HTML 正文。提供后会以 multipart/alternative 发送，老客户端自动降级到纯文本 |
| `--cc` | 否 | 抄送地址，逗号/分号/空格分隔多个 |
| `--bcc` | 否 | 密送地址（只进信封，不写邮件头），逗号/分号/空格分隔多个 |
| `--attach` | 否 | 附件路径，**可重复传多次**：`--attach a.pdf --attach b.zip` |

> *正文要求：`--body` 与 `--body-file` 必须二选一（仅发 HTML 时可都不给纯文本，但仍建议至少给 `--body` 做降级）。

### 发信示例

**带附件（可重复传多个）：**

python3 skill/script/send-mailu/send-mailu.py send \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "月度报告" --body "附件请查收" \
  --attach "/tmp/report.pdf" --attach "/tmp/data.xlsx"

**HTML 正文（自动带纯文本降级）：**

python3 skill/script/send-mailu/send-mailu.py send \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "通知" --body "纯文本降级内容" \
  --html "<h1>通知</h1><p>这是 <b>HTML</b> 正文。</p>"

**抄送 + 密送：**

python3 skill/script/send-mailu/send-mailu.py send \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "会议纪要" --body "见正文" \
  --cc "a@example.com,b@example.com" --bcc "boss@example.com"

**正文从文件读取（避免大段正文走命令行）：**

python3 skill/script/send-mailu/send-mailu.py send \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "日志" --body-file "/tmp/long-body.txt"

### SMTP 安全模式

脚本读取 `/etc/send-mailu/config.json` 中的 `smtp_security` 字段决定连接方式：
- `starttls`（默认）：先明文连接再升级 TLS，适配 587 端口
- `ssl`：直接 SSL/TLS 连接，适配 465 端口
- `none`：不加密，适配本地 25 无加密中继

若未配置该字段，脚本按端口智能判定：**465 → ssl，其余 → starttls**。
配置样例见 `skill/script/send-mailu/config.example.json`。

### 已发送归档（IMAP APPEND）

SMTP 只负责"把邮件投递出去"，本身不会在发件箱留下副本——所以脚本发的信默认在 Mailu 网页端"已发送"里看不到。config 里的 `sent_archive` 段开启后，脚本发信成功后会**用同一套登录凭证再连一次 IMAP**，把同一封邮件 `APPEND` 到指定文件夹，网页端即可见。

### 自动探测可用 IMAP 端口

`check`（含自动配置）在生成 config 时会**自动探测** IMAP 可用端口：依次试 `smtp_host:993(ssl)` → `smtp_host:143(starttls)` → `127.0.0.1:993` → `127.0.0.1:143`，第一个能连上的就写进 `sent_archive`。

这能解决两类常见坑：
- **VPS 防火墙拦 143 放行 993**（很多服务商默认防 IMAP 明文爆破）→ 自动选 993/ssl
- **Docker 端口只绑公网 IP 不绑 127.0.0.1** → 自动回退到能连的那个 host

如果四个候选全连不上，仍会生成 `enabled:true`（保留 folder），发信不受影响，归档留待手动填。

```json
"sent_archive": {
  "enabled": true,
  "host": "1.2.3.4",
  "port": 993,
  "security": "ssl",
  "folder": "Sent"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `enabled` | 是 | `true` 开启归档。缺省/非 dict/`false` 都视为关闭（向后兼容旧 config） |
| `host` | 否 | IMAP 主机。省略时自动沿用 `smtp_host`。`check` 配置时已自动探测填好 |
| `port` | 否 | IMAP 端口。省略时按 `security` 推断：ssl→993，其余→143 |
| `security` | 否 | `ssl`/`starttls`/`none`。省略时沿用 `smtp_security` |
| `folder` | 否 | 归档目标文件夹名，默认 `Sent`。不同 Webmail 命名可能是 `Sent Messages` / `已发送`，以 `check` 探针列出的文件夹清单为准 |

归档是**尽力而为**：未配置、连接失败、文件夹不存在等情况都只在 stderr 打印 `Warning: ...`，**不影响发信本身的成功结果**。归档用的账号/密码就是 SMTP 登录成功的那套凭证（即 `domains.匹配域名.username/password`），因此**只有"真实登录发信"的那个账号**的"已发送"里会出现副本，`--from-addr` 用到的别名账号不会单独归档。

`check` 末尾会对配置中的**每个邮箱分别执行 IMAP 登录和 Sent 文件夹检查**（不计入发信可用性判定）。每一行只代表该邮箱自己，绝不使用 `default_domain` 的单次结果代替其他邮箱。**全部失败且检测到连接参数错误时，会自动探测并修正 IMAP 端口后，再逐个重测**：

```
--- IMAP Sent 逐邮箱测试（不影响发信判定）---
  每个邮箱检查自己的 Sent；实际用谁登录发信，就归档到谁的 Sent。
  [✓] admin@example.com：IMAP 登录成功；自己的文件夹「Sent」存在（1.2.3.4:993/ssl）
  [✓] admin@example.net：IMAP 登录成功；自己的文件夹「Sent」存在（1.2.3.4:993/ssl）
  结果：2/2 通过
```

失败示例（自动给出修复建议）：

```
  💡 部分账号未通过；请检查失败账号的密码或 Sent 文件夹。
  [✓] admin@example.com：IMAP 登录成功；自己的文件夹「Sent」存在（1.2.3.4:993/ssl）
  [✗] admin@example.net：IMAP 连接/登录失败（1.2.3.4:993/ssl）— authentication failed
  结果：1/2 通过
```

> 旧 config 升级：直接重跑 `sudo python3 .../send-mailu.py check`（检查失败时确认配置 → 自动重填含探测结果的 sent_archive），或手动按上面字段补 `host`/`port`/`security`。

---

## 核心读信策略

按 EML 绝对路径读取邮件信息和正文预览：

```bash
python3 skill/script/send-mailu/send-mailu.py get "/mailu/mail/admin@example.com/new/邮件文件名"
```

邮箱文件路径固定为：

- 收件箱：`/mailu/mail/<完整邮箱>/new/<文件名>` 或 `/mailu/mail/<完整邮箱>/cur/<文件名>`
- 其他目录：`/mailu/mail/<完整邮箱>/.[Junk|Sent|Trash|Drafts]/[new|cur]/<文件名>`

`get` 只接受 `/mailu/mail` 内的 EML 绝对路径，输出发件人、收件人、主题、日期、正文前 1200 字符和附件名称；不搜索文件名，不读取账号配置或本技能全文。

---

## 约束与纪律

1. 纯只读原则：严禁修改或删除邮件。
2. 防注入：变量通过 export 传递。
