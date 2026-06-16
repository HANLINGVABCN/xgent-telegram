```!
Mailu邮件服务器管理助手：
1. 【免密发信】：AI发信时，严禁索要密码！直接调用本地安全脚本发信：
   `/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py --to "<收件人>" --from-addr "<发件人>" --subject "<主题>" --body "<正文>"`
   该脚本会自动读取本地凭证，支持任意前缀 and 任意自定义子域名，且自动补全 Message-ID、Date 头，智能信封路由防进垃圾箱。
   可选增强参数：--html "<HTML正文>"、--cc/--bcc "<抄送/密送>"、--body-file "<正文文件>"、--attach "<附件路径>"（可重复）。
   【发信流程】AI 直接发信；若返回 exit code 2（配置缺失/损坏）或认证失败，则提示用户运行 `sudo .../send_mail.py init` 生成配置、填密码，再重试。
   【配置维护命令】`send_mail.py check` 验证配置可用性；`send_mail.py init` 从 Mailu 自动探测并交互生成 /etc/mail-manager/config.json。
2. 【本地读信】：免密检索和解析 Maildir。物理路径规范（账号必须是完整邮箱，如 admin@example.com）：
   - 收件箱: /mailu/mail/<账号>/[new|cur]
   - 其他(垃圾箱/已发送/已删除/草稿): /mailu/mail/<账号>/.[Junk|Sent|Trash|Drafts]/[new|cur]
   直接读取本地 EML 文件，严禁修改或删除。
```

# 邮件管理助手 (mail-manager)

本技能用于在不修改服务器配置、不向用户索要邮箱密码的前提下，提供极速、安全、合规的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 首次部署：生成发信配置

发信脚本依赖 `/etc/mail-manager/config.json`（含 SMTP 登录凭证）。首次部署或配置丢失时用 `init` 命令生成：

```bash
sudo /opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py init
```

`init` 会自动探测 Mailu 的 `mailu.env`（DOMAIN / POSTMASTER 等），自动填充域名和默认用户名，**只需交互填入登录密码**（密码不回显）。探测不到 Mailu 环境时降级为逐项询问。

### 验证配置是否可用

```bash
sudo /opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py check
```

`check` 逐项检查：文件存在 → JSON 合法 → 必填字段齐全 → 凭证非空 → **SMTP 实连登录测试**（默认开启）。全部 ✓ 则 exit 0；任一 ✗ 则打印失败项并给 init 引导。

- `check --no-connect`：跳过 SMTP 实连测试，仅做格式检查（适合部署时网络未通或快速校验）。

### 非交互批量生成（脚本调用）

```bash
sudo /opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py init \
  --domain example.com --username admin@example.com --password "密码" \
  --smtp-host 127.0.0.1 --smtp-port 587 --security starttls --force
```

### AI 发信排查流程

```
当用户要发信时，AI 直接运行发信命令：
1. 运行 send_mail.py --to ... --body ...
2. 若返回 exit code 2（配置缺失/损坏）→ 提示用户：
     "发信配置不存在，请在服务器运行： sudo .../send_mail.py init"
3. 用户按提示填完密码后 → 重新发信
4. 若仍认证失败 → 提示用户运行 sudo .../send_mail.py init --force 改密码，
   或运行 sudo .../send_mail.py check 排查具体原因
```

---

## 核心发信策略 (SMTP Delivery)

AI 不需要在对话中索要用户的明文密码。直接在服务器上调用本地已部署的安全发信脚本即可。

### 执行命令（最小调用）

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
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

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "月度报告" --body "附件请查收" \
  --attach "/tmp/report.pdf" --attach "/tmp/data.xlsx"

**HTML 正文（自动带纯文本降级）：**

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "通知" --body "纯文本降级内容" \
  --html "<h1>通知</h1><p>这是 <b>HTML</b> 正文。</p>"

**抄送 + 密送：**

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "会议纪要" --body "见正文" \
  --cc "a@example.com,b@example.com" --bcc "boss@example.com"

**正文从文件读取（避免大段正文走命令行）：**

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
  --to "user@example.com" --from-addr "admin@example.com" \
  --subject "日志" --body-file "/tmp/long-body.txt"

### SMTP 安全模式

脚本读取 `/etc/mail-manager/config.json` 中的 `smtp_security` 字段决定连接方式：
- `starttls`（默认）：先明文连接再升级 TLS，适配 587 端口
- `ssl`：直接 SSL/TLS 连接，适配 465 端口
- `none`：不加密，适配本地 25 无加密中继

若未配置该字段，脚本按端口智能判定：**465 → ssl，其余 → starttls**。
配置样例见 `skill/script/mail-manager/config.example.json`。

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