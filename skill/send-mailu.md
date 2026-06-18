```!
Mailu邮件服务器管理助手：
1. 【免密发信】：AI发信时，严禁索要密码！直接调用本地安全脚本发信：
   `/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py --to "<收件人>" --from-addr "<发件人>" --subject "<主题>" --body "<正文>"`
   该脚本会自动读取本地凭证，支持任意前缀 and 任意自定义子域名，且自动补全 Message-ID、Date 头，智能信封路由防进垃圾箱。
   可选增强参数：--html "<HTML正文>"、--cc/--bcc "<抄送/密送>"、--body-file "<正文文件>"、--attach "<附件路径>"（可重复）。
   【发信流程】AI 直接发信；若返回 exit code 2（配置缺失/损坏）或认证失败，则提示用户运行 `sudo .../send-mailu.py check` 检查并交互配置，再重试。
   【配置维护命令】`send-mailu.py check` 既是配置健康检查，也是**自动获取当前可用邮箱账号清单**的途径——它逐个域名实测 SMTP 登录，结果清单可直接用于发信决策；检查失败时自动提示是否开始交互配置，生成 /etc/send-mailu/config.json。
2. 【本地读信】：免密检索和解析 Maildir。物理路径规范（账号必须是完整邮箱，如 admin@example.com）：
   - 收件箱: /mailu/mail/<账号>/[new|cur]
   - 其他(垃圾箱/已发送/已删除/草稿): /mailu/mail/<账号>/.[Junk|Sent|Trash|Drafts]/[new|cur]
   直接读取本地 EML 文件，严禁修改或删除。
```

# 邮件发信助手 (send-mailu)

本技能用于在不修改服务器配置、不向用户索要邮箱密码的前提下，提供极速、安全、合规的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 首次部署：生成发信配置

发信脚本依赖 `/etc/send-mailu/config.json`（含 SMTP 登录凭证，支持多域名）。首次部署或配置丢失时用 `check` 命令检查并交互配置：

```bash
sudo /opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py check
```

`check` 会先检查配置状态，**若检查失败，自动提示「是否开始配置？」**，用户确认后进入交互配置流程。

配置时的账号发现顺序（两级回退）：

1. **优先扫描 Mailu SQLite 数据库**（`/mailu/data/main.db` 等，只读打开）：直接读到**全部域名**和**全部账号**列表（Mailu `domain` / `user` 表），无需手动罗列。
2. **数据库不可用时回退读 `mailu.env`**：Mailu 的环境变量配置文件（`/mailu/mailu.env`），里面通常只有 `DOMAIN=example.com`（单数主域名），只能推出一个 `admin@域名` 账号，无法枚举多域名。

发现域名/账号后，密码**无法自动获取**——Mailu 数据库里存的是 hash（SHA512-Crypt/bcrypt），不能逆向出明文用于 SMTP 登录。所以配置流程会列出发现到的账号，**逐个交互询问明文密码**（不回显）。

### 验证配置 & 获取可用账号清单

```bash
sudo /opt/telegram-ai-bot/skill/script/send_mail/send_mail.py check
```

`check` 有三重用途：

- **健康检查**：文件存在 → JSON 合法 → 必填字段齐全 → 凭证非空 → 安全模式合法，逐项打印 ✓/✗。
- **账号发现**：**逐个域名实测 SMTP 登录**（默认开启，每个域名单独一行结果，带 `[域名]` 标注）。这相当于一份"当前实际可用账号清单"——AI 发信前可先跑 `check`，确认目标域名账号是 ✓ 再发，避免发到一半认证失败。
- **自动引导配置**：检查失败时自动提示「是否开始配置？」，确认后进入交互配置流程。

全部 ✓ 则 exit 0；任一 ✗ 则打印失败项并提示配置。

- `check --no-connect`：跳过 SMTP 实连测试，仅做格式检查（适合部署时网络未通或快速校验；此时不输出可用账号清单）。

### 非交互批量配置（脚本调用）

```bash
sudo /opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py check \
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
1. 运行 send-mailu.py --to ... --body ...
2. 若返回 exit code 2（配置缺失/损坏）→ 提示用户：
     "发信配置不存在，请在服务器运行： sudo .../send-mailu.py check"
3. 用户运行 check → 自动检查 → 失败时提示配置 → 填完密码 → 重新发信
4. 若仍认证失败 → 提示用户运行 sudo .../send-mailu.py check --force 重新配置
```

---

## 核心发信策略 (SMTP Delivery)

AI 不需要在对话中索要用户的明文密码。直接在服务器上调用本地已部署的安全发信脚本即可。

### 执行命令（最小调用）

/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "<收件人>" \
  --from-addr "<发件人>" \
  --subject "<主题>" \
  --body "<正文>"

### 完整参数说明

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--to` | 是 | 收件人。多个地址用逗号/分号/空格分隔 |
| `--from-addr` | 是 | 发件人地址（脚本据此回溯匹配登录凭证） |
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

/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "月度报告" --body "附件请查收" \
  --attach "/tmp/report.pdf" --attach "/tmp/data.xlsx"

**HTML 正文（自动带纯文本降级）：**

/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "通知" --body "纯文本降级内容" \
  --html "<h1>通知</h1><p>这是 <b>HTML</b> 正文。</p>"

**抄送 + 密送：**

/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "会议纪要" --body "见正文" \
  --cc "a@example.com,b@example.com" --bcc "boss@example.com"

**正文从文件读取（避免大段正文走命令行）：**

/opt/telegram-ai-bot/skill/script/send-mailu/send-mailu.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "日志" --body-file "/tmp/long-body.txt"

### SMTP 安全模式

脚本读取 `/etc/send-mailu/config.json` 中的 `smtp_security` 字段决定连接方式：
- `starttls`（默认）：先明文连接再升级 TLS，适配 587 端口
- `ssl`：直接 SSL/TLS 连接，适配 465 端口
- `none`：不加密，适配本地 25 无加密中继

若未配置该字段，脚本按端口智能判定：**465 → ssl，其余 → starttls**。
配置样例见 `skill/script/send-mailu/config.example.json`。

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