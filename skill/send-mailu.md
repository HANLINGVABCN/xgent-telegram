```!
Mailu 发信/读信技能：
1. 【免密发信】：严禁索要密码！执行命令：`python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py --to "<收件人>" --from-addr "<发件人>" --subject "<主题>" --body "<正文>"` (增强参数：--body-file, --html, --cc, --bcc, --attach)。
   发信脚本支持任意前缀和任意自定义子域名邮箱。
   发信流程：获取可用的发信地址再进行发信，禁止自己胡编乱造一级域名，若 ai 不知道可用发件邮箱，可通过 check 命令获取。AI发信后，若返回 exit code 2（配置缺失/损坏）或认证全部失败（exit 1），提示用户运行 `sudo python3 .../send-mailu.py check`。
   check 是配置入口：逐项检查 config.json（含逐域名 SMTP 实连 ✓/✗）；全部通过则输出实测可用账号清单（exit 0），任一失败且在交互终端时自动提示「是否开始配置？」，确认后进入配置流程——此时扫 Mailu SQLite 发现全部域名/账号并逐个问密码，配置完自动复查。
   归档（可选）：config 的 sent_archive.enabled=true 时，每封信发信成功后自动 IMAP APPEND 一份到「已发送」文件夹（复用同一账号登录 IMAP），失败仅告警不影响发信。check 末尾会打印 IMAP 归档探针结果（不计入可用性判定）。
2. 【本地读信】：免密检索本地 EML 文件，严禁修改或删除。
   物理路径（<账号>需完整邮箱，如 admin@example.com）：
   - 收件箱: `/mailu/mail/<账号>/[new|cur]`
   - 其他文件夹: `/mailu/mail/<账号>/.[Junk|Sent|Trash|Drafts]/[new|cur]`
   如需高级检索 Python 代码，请 read 本文档全文。
```

# 邮件发信助手 (send-mailu)

本技能用于在不修改服务器配置、不向用户索要邮箱密码的前提下，提供极速、安全、合规的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 首次部署：生成发信配置

发信脚本依赖 `/etc/send-mailu/config.json`（含 SMTP 登录凭证，支持多域名）。首次部署或配置丢失时用 `check` 命令检查并交互配置：

```bash
sudo python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py check
```

`check` 会先检查配置状态，**若检查失败，自动提示「是否开始配置？」**，用户确认后进入交互配置流程。

配置时的账号发现顺序（两级回退）：

1. **优先扫描 Mailu SQLite 数据库**（`/mailu/data/main.db` 等，只读打开）：直接读到**全部域名**和**全部账号**列表（Mailu `domain` / `user` 表），无需手动罗列。
2. **数据库不可用时回退读 `mailu.env`**：Mailu 的环境变量配置文件（`/mailu/mailu.env`），里面通常只有 `DOMAIN=example.com`（单数主域名），只能推出一个 `admin@域名` 账号，无法枚举多域名。

发现域名/账号后，密码**无法自动获取**——Mailu 数据库里存的是 hash（SHA512-Crypt/bcrypt），不能逆向出明文用于 SMTP 登录。所以配置流程会列出发现到的账号，**逐个交互询问明文密码**（不回显）。

### 验证配置 & 获取可用账号清单

```bash
sudo python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py check
```

`check` 有三重用途：

- **健康检查**：文件存在 → JSON 合法 → 必填字段齐全 → 凭证非空 → 安全模式合法，逐项打印 ✓/✗。
- **账号发现**：**逐个域名实测 SMTP 登录**（默认开启，每个域名单独一行结果，带 `[域名]` 标注）。这相当于一份"当前实际可用账号清单"——AI 发信前可先跑 `check`，确认目标域名账号是 ✓ 再发，避免发到一半认证失败。
- **自动引导配置**：检查失败时自动提示「是否开始配置？」，确认后进入交互配置流程。

全部 ✓ 则 exit 0；任一 ✗ 则打印失败项并提示配置。

- `check --no-connect`：跳过 SMTP 实连测试，仅做格式检查（适合部署时网络未通或快速校验；此时不输出可用账号清单）。

### 非交互批量配置（脚本调用）

```bash
sudo python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py check \
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
1. 运行 python3 send-mailu.py --to ... --body ...
2. 若返回 exit code 2（配置缺失/损坏）→ 提示用户：
     "发信配置不存在，请在服务器运行： sudo python3 .../send-mailu.py check"
3. 用户运行 check → 自动检查 → 失败时提示配置 → 填完密码 → 重新发信
4. 若仍认证失败 → 提示用户运行 sudo python3 .../send-mailu.py check --force 重新配置
```

---

## 核心发信策略 (SMTP Delivery)

AI 不需要在对话中索要用户的明文密码。直接在服务器上调用本地已部署的安全发信脚本即可。

### 执行命令（最小调用）

python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
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

python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "月度报告" --body "附件请查收" \
  --attach "/tmp/report.pdf" --attach "/tmp/data.xlsx"

**HTML 正文（自动带纯文本降级）：**

python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "通知" --body "纯文本降级内容" \
  --html "<h1>通知</h1><p>这是 <b>HTML</b> 正文。</p>"

**抄送 + 密送：**

python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "会议纪要" --body "见正文" \
  --cc "a@example.com,b@example.com" --bcc "boss@example.com"

**正文从文件读取（避免大段正文走命令行）：**

python3 telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
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

`check` 末尾会打印一段 IMAP 归档探针（不计入可用性判定），告诉你归档能否真正跑通、目标文件夹是否存在。**失败时会自动探测可用端口并给出可直接照抄的 `sent_archive` 配置建议**：

```
  --- 已发送归档（IMAP）探针（不计入可用性判定）---
  [✓] 已发送归档: IMAP 登录成功，文件夹「Sent」存在 (admin@example.com@1.2.3.4:993/ssl)
```

失败示例（自动给出修复建议）：

```
  [✗] 已发送归档: IMAP 连接/登录失败 (admin@example.com@1.2.3.4:143/starttls) — [Errno 111] Connection refused
      💡 探测到可用 IMAP: ssl @ 1.2.3.4:993
         建议把 sent_archive 改为: host='1.2.3.4', port=993, security='ssl'
```

> 旧 config 升级：直接重跑 `sudo python3 .../send-mailu.py check`（检查失败时确认配置 → 自动重填含探测结果的 sent_archive），或手动按上面字段补 `host`/`port`/`security`。

---

## 核心读信策略 (Maildir Reader)

### 1. 物理路径规范
- <账号> 必须包含域名（例如 admin@example.com）。
- 收件箱: /mailu/mail/<账号>/[new|cur]
- 其他目录: /mailu/mail/<账号>/.[Junk|Sent|Trash|Drafts]/[new|cur]

### 2. 邮件列表检索

export MAIL_DIR="/mailu/mail/admin@example.com/new"
export SEARCH_KEY="" # 可选关键词

python3 -c '
import os, email, sys
from email.parser import BytesParser
from email.policy import default

mail_dir = os.environ.get("MAIL_DIR")
search_key = os.environ.get("SEARCH_KEY", "").lower()

if not os.path.exists(mail_dir):
    print(f"目录不存在: {mail_dir}")
    sys.exit()

files = [os.path.join(mail_dir, f) for f in os.listdir(mail_dir) if os.path.isfile(os.path.join(mail_dir, f))]
files.sort(key=os.path.getmtime, reverse=True)

print(f"--- 邮件列表 (共 {len(files)} 封) ---")
count = 0
for filepath in files:
    with open(filepath, "rb") as f:
        msg = BytesParser(policy=default).parse(f)
        subject = str(msg["Subject"])
        sender = str(msg["From"])
        if search_key and search_key not in subject.lower() and search_key not in sender.lower():
            continue
        print(f"[{count+1}] 文件名: {os.path.basename(filepath)}\n    发件人: {sender}\n    主题: {subject}\n    日期: {msg[\"Date\"]}\n" + "-"*40)
        count += 1
        if count >= 15:
            break
'

### 3. 读取单封邮件

export MAIL_FILE="/mailu/mail/admin@example.com/new/filename"

python3 -c '
import email, sys, os
from email.parser import BytesParser
from email.policy import default

filepath = os.environ.get("MAIL_FILE")
if not os.path.exists(filepath):
    print(f"文件不存在: {filepath}")
    sys.exit()

with open(filepath, "rb") as f:
    msg = BytesParser(policy=default).parse(f)
    print("=" * 50)
    print(f"发件人: {msg[\"From\"]}\n收件人: {msg[\"To\"]}\n主题: {msg[\"Subject\"]}\n日期: {msg[\"Date\"]}")
    print("=" * 50)
  
    body = ""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition"))
            if "attachment" in cdisp or part.get_filename():
                attachments.append(part.get_filename() or "Unknown_File")
                continue
            if ctype == "text/plain" and not body:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
            elif ctype == "text/html" and not body:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                body = "[HTML] " + body.replace("<br>", "\n").replace("</p>", "\n")
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
      
    print(body.strip() if body else "[无内容]")
    if attachments:
        print("\n--- 附件 ---")
        for att in attachments:
            print(f"- {att}")
'

---

## 约束与纪律

1. 纯只读原则：严禁修改或删除邮件。
2. 防注入：变量通过 export 传递。