```!
Mailu邮件服务器管理助手：
1. 【免密发信】：AI发信时，严禁索要密码！直接调用本地安全脚本发信：
   `/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py --to "<收件人>" --from-addr "<发件人>" --subject "<主题>" --body "<正文>"`
   该脚本会自动读取本地凭证，支持任意前缀 and 任意自定义子域名，且自动补全 Message-ID、Date 头，智能信封路由防进垃圾箱。
2. 【本地读信】：免密检索和解析 Maildir。物理路径规范（账号必须是完整邮箱，如 admin@example.com）：
   - 收件箱: /mailu/mail/<账号>/[new|cur]
   - 其他(垃圾箱/已发送/已删除/草稿): /mailu/mail/<账号>/.[Junk|Sent|Trash|Drafts]/[new|cur]
   直接读取本地 EML 文件，严禁修改或删除。
```

# 邮件管理助手 (mail-manager)

本技能用于在不修改服务器配置、不向用户索要邮箱密码的前提下，提供极速、安全、合规的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 核心发信策略 (SMTP Delivery)

AI 不需要在对话中索要用户的明文密码。直接在服务器上调用本地已部署的安全发信脚本即可。

### 执行命令

/opt/telegram-ai-bot/skill/script/mail-manager/send_mail.py \
  --to "<收件人>" \
  --from-addr "<发件人>" \
  --subject "<主题>" \
  --body "<正文>"

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