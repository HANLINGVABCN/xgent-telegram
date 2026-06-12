```!
邮件管理助手：当用户需要免密码、无痕地在服务器上进行发信测试（支持任意前缀、SPF对齐绕过DMARC），或者需要直接读取和检索本地Maildir格式的收件箱邮件（包括查看未读、已读、垃圾箱邮件）时使用本技能。发信提供Mailu本地通道发信（自动带DKIM签名）和绕过Mailu直接SMTP投递（发野生子域名）两种方式；读信通过Python脚本直接只读解析本地EML文件，绝不修改、删除任何邮件或系统配置。
```

# 邮件管理助手 (mail-manager)

本技能用于在不修改服务器配置、不索要邮箱密码的前提下，提供安全的无痕发信测试与本地收件箱直接读取、检索功能。

---

## 核心发信策略 (SMTP Delivery)

### 1. Mailu 本地通道投递（推荐，带 DKIM 签名）

**适用场景**：使用在 Mailu 中已注册的常规域名发信，支持任意前缀。

**特点**：Mailu 会自动打上合法的 DKIM 签名，SPF 完美对齐，信誉极高，可直接进收件箱。

**执行命令**（使用自定义 EOF 标识符防止内容截断）：

```bash
docker exec -i mailu-smtp-1 sendmail -f "<发件人邮箱>" -t << 'EOF_MAIL_BOUNDARY'
To: <收件人邮箱>
From: <发件人邮箱>
Subject: <邮件主题>
Content-Type: text/plain; charset="utf-8"

<邮件正文>
EOF_MAIL_BOUNDARY
```

**注意事项**：
- 发件人邮箱必须是在 Mailu 中已注册的域名（支持任意前缀）
- 使用自定义 EOF 标识符 `EOF_MAIL_BOUNDARY` 防止邮件内容中出现 `EOF` 导致截断
- 邮件头必须包含 `To:`、`From:`、`Subject:` 和 `Content-Type:`

### 2. 绕过 Mailu 直接 SMTP 投递（用于测试野生子域名）

**适用场景**：强行发送未在 Mailu 注册的子域名，防止 Mailu 的 SRS 机制强制重写发件人导致退信。

**前置操作**：执行发信前，必须先使用 `dig +short MX <收件人域名>` 获取真实的接收方 MX 服务器地址。

**执行命令**（必须使用环境变量传参，杜绝引号引起的系统命令注入）：

```bash
export MX_SERVER="<上一步获取的MX地址>"
export SENDER="<发件人邮箱>"
export RECEIVER="<收件人邮箱>"
export SUBJECT="<邮件主题>"
export BODY="<邮件正文>"

python3 -c '
import smtplib, os
from email.mime.text import MIMEText
from email.header import Header

mx_server = os.environ.get("MX_SERVER")
sender = os.environ.get("SENDER")
receiver = os.environ.get("RECEIVER")

msg = MIMEText(os.environ.get("BODY"), "plain", "utf-8")
msg["Subject"] = Header(os.environ.get("SUBJECT"), "utf-8")
msg["From"] = sender
msg["To"] = receiver

# 动态提取发件人域名用于 EHLO 问候
sender_domain = sender.split("@")[-1] if "@" in sender else "localhost"
ehlo_host = f"mail.{sender_domain}"

try:
    server = smtplib.SMTP(mx_server, 25, timeout=15)
    server.ehlo(ehlo_host)
    if server.has_extn("starttls"):
        server.starttls()
        server.ehlo(ehlo_host)
    server.sendmail(sender, [receiver], msg.as_string())
    server.quit()
    print("【成功】直投成功")
except Exception as e:
    print("【失败】", e)
'
```

**注意事项**：
- 必须先用 `dig +short MX <收件人域名>` 查询接收方 MX 服务器
- 所有参数必须通过环境变量传递，严禁字符串拼接
- 发件人域名未在 Mailu 注册时，不会有 DKIM 签名，可能被识别为垃圾邮件
- 用于测试任意子域名发信功能，不适合日常发信

---

## 核心读信策略 (Maildir Reader)

Mailu 采用标准的 Maildir 格式存储邮件。AI 直接读取本地 EML 文件，实现免密、安全的检索和阅读。

**目录映射**：
- 未读新邮件: `/mailu/mail/<邮箱账号>/new/`
- 已读旧邮件: `/mailu/mail/<邮箱账号>/cur/`
- 垃圾邮件箱: `/mailu/mail/<邮箱账号>/.Junk/`
- 已删除邮件: `/mailu/mail/<邮箱账号>/.Trash/`

### A. 邮件列表检索（支持按时间倒序和关键词过滤）

**执行命令**：

```bash
export MAIL_DIR="/mailu/mail/<邮箱账号>/<目录名>"
export SEARCH_KEY="<可选搜索词，留空则全量输出>"

python3 -c '
import os, email, sys
from email.parser import BytesParser
from email.policy import default

mail_dir = os.environ.get("MAIL_DIR")
search_key = os.environ.get("SEARCH_KEY", "").lower()

if not os.path.exists(mail_dir):
    print("目录不存在或为空")
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
      
        # 关键词过滤
        if search_key and search_key not in subject.lower() and search_key not in sender.lower():
            continue
          
        print(f"[{count+1}] 文件名: {os.path.basename(filepath)}")
        print(f"    发件人: {sender}")
        print(f"    主题: {subject}")
        print(f"    日期: {msg[\"Date\"]}")
        print("-" * 40)
      
        count += 1
        if count >= 15: # 最多显示 15 条
            break
'
```

**使用示例**：

```bash
# 查看未读邮件列表
export MAIL_DIR="/mailu/mail/admin@example.com/new/"
export SEARCH_KEY=""
python3 -c '...'

# 搜索包含 "invoice" 的已读邮件
export MAIL_DIR="/mailu/mail/admin@example.com/cur/"
export SEARCH_KEY="invoice"
python3 -c '...'
```

### B. 读取并解析单封邮件（支持多部件与附件探测）

**执行命令**：

```bash
export MAIL_FILE="/mailu/mail/<邮箱账号>/<目录名>/<文件名>"

python3 -c '
import email, sys, os
from email.parser import BytesParser
from email.policy import default

filepath = os.environ.get("MAIL_FILE")
if not os.path.exists(filepath):
    print("邮件文件不存在")
    sys.exit()

with open(filepath, "rb") as f:
    msg = BytesParser(policy=default).parse(f)
    print("=" * 50)
    print(f"发件人: {msg[\"From\"]}")
    print(f"收件人: {msg[\"To\"]}")
    print(f"主题: {msg[\"Subject\"]}")
    print(f"日期: {msg[\"Date\"]}")
    print("=" * 50)
  
    body = ""
    attachments = []
  
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition"))
          
            # 记录附件
            if "attachment" in cdisp or part.get_filename():
                attachments.append(part.get_filename() or "Unknown_File")
                continue
              
            # 优先提取纯文本，若无则提取HTML
            if ctype == "text/plain" and not body:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
            elif ctype == "text/html" and not body:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                body = "[HTML内容] " + body.replace("<br>", "\n").replace("</p>", "\n")
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
      
    print(body.strip() if body else "[无法解析的正文内容]")
    if attachments:
        print("\n--- 附件列表 ---")
        for att in attachments:
            print(f"- {att}")
'
```

**使用示例**：

```bash
# 读取指定邮件
export MAIL_FILE="/mailu/mail/admin@example.com/new/1234567890.M123456P12345.hostname"
python3 -c '...'
```

---

## 约束与纪律

### 1. 纯只读原则
所有 Python 解析邮件的代码仅执行 `open(..., "rb")`，严禁写入、修改或执行 `rm` 删除邮件。

### 2. 防注入保护
无论执行哪种 Bash 脚本，带有用户输入的变量（如邮箱、正文、搜索词）必须通过 `export` 环境变量传递给 Python，严禁直接使用 `'` 或 `"` 字符串拼接，防止被利用进行系统命令注入。

### 3. 动态查询原则
执行"绕过 Mailu 直接 SMTP 投递"前，必须先利用 `dig` 查询接收方的 MX 记录，不得凭空猜测或硬编码域名。

### 4. 隐私无痕
- 严禁向用户索要系统的邮箱密码
- 严禁在服务器物理机上生成任何 `.py` 或 `.sh` 持久化脚本文件
- 所有代码必须用 `python3 -c` 在内存中单次运行

---

## 常见使用场景

### 场景 1：测试已注册域名的任意前缀发信

用户想测试 `randomprefix@example.com` 能否正常发信（`example.com` 已在 Mailu 注册）：

```bash
docker exec -i mailu-smtp-1 sendmail -f "randomprefix@example.com" -t << 'EOF_MAIL_BOUNDARY'
To: test@gmail.com
From: randomprefix@example.com
Subject: Test Email
Content-Type: text/plain; charset="utf-8"

This is a test email.
EOF_MAIL_BOUNDARY
```

### 场景 2：测试野生子域名发信

用户想测试 `test@wild.example.com` 发信（`wild.example.com` 未在 Mailu 注册）：

```bash
# 第一步：查询接收方 MX
dig +short MX gmail.com

# 第二步：绕过 Mailu 直投
export MX_SERVER="gmail-smtp-in.l.google.com"
export SENDER="test@wild.example.com"
export RECEIVER="test@gmail.com"
export SUBJECT="Wild Subdomain Test"
export BODY="Testing wild subdomain delivery"

python3 -c '...'  # 使用上面的直投脚本
```

### 场景 3：查看未读邮件

用户想查看 `admin@example.com` 的未读邮件：

```bash
export MAIL_DIR="/mailu/mail/admin@example.com/new/"
export SEARCH_KEY=""

python3 -c '...'  # 使用邮件列表检索脚本
```

### 场景 4：搜索包含特定关键词的邮件

用户想在已读邮件中搜索包含 "invoice" 的邮件：

```bash
export MAIL_DIR="/mailu/mail/admin@example.com/cur/"
export SEARCH_KEY="invoice"

python3 -c '...'  # 使用邮件列表检索脚本
```

### 场景 5：读取单封邮件内容

用户想读取特定邮件的完整内容：

```bash
export MAIL_FILE="/mailu/mail/admin@example.com/new/1234567890.M123456P12345.hostname"

python3 -c '...'  # 使用邮件读取脚本
```

---

## 安全注意事项

1. **只读访问**：所有读信操作都是只读的，不会修改、移动或删除任何邮件
2. **环境变量传参**：所有用户输入必须通过环境变量传递，防止命令注入
3. **不索要密码**：技能不需要用户提供邮箱密码
4. **无痕操作**：不在服务器上留下持久化脚本文件
5. **MX 动态查询**：直投发信前必须动态查询 MX 记录，不硬编码
6. **权限确认**：执行发信操作前，确认用户有权限使用该邮箱域名

---

## 与其他 skill 的配合

- **mailu-helper.md**：用于 Mailu 的部署、配置、DNS 检查、catch-all 配置等运维操作
- **mail-manager.md**（本技能）：用于发信测试和读信检索，不涉及配置修改

配合使用示例：
1. 使用 `mailu-helper` 部署 Mailu 并配置 DNS
2. 使用 `mail-manager` 测试任意前缀发信功能
3. 使用 `mail-manager` 检查收件箱是否收到测试邮件

---

## 故障排查

### 发信失败

**问题**：Mailu 本地通道发信失败

**排查步骤**：
1. 确认容器名称：`docker ps | grep smtp`
2. 查看 SMTP 容器日志：`docker logs mailu-smtp-1`
3. 确认发件人域名已在 Mailu 注册
4. 检查邮件格式是否正确（必须包含 `To:`、`From:`、`Subject:` 等头）

**问题**：直投发信失败

**排查步骤**：
1. 确认 MX 记录查询正确：`dig +short MX <接收方域名>`
2. 检查服务器 25 端口出站是否被封禁
3. 查看 Python 脚本的错误输出
4. 测试与 MX 服务器的连接：`telnet <MX_SERVER> 25`

### 读信失败

**问题**：目录不存在或为空

**排查步骤**：
1. 确认邮箱账号存在：检查 `/mailu/mail/` 下的目录
2. 确认使用正确的目录名：`new/`（未读）、`cur/`（已读）、`.Junk/`（垃圾箱）
3. 检查目录权限：`ls -la /mailu/mail/<邮箱账号>/`

**问题**：邮件文件不存在

**排查步骤**：
1. 先执行邮件列表检索，获取正确的文件名
2. 确认文件名完整（包括时间戳和主机名部分）
3. 检查文件是否被移动到其他目录（如已读邮件移到 `cur/`）

---

## 推荐给用户的简短结果说明

测试完成后可以这样回复：

```text
邮件测试已完成。

发信测试：
✓ 使用 randomprefix@example.com 发送到 test@gmail.com
✓ Mailu 已自动添加 DKIM 签名
✓ SPF 对齐成功

收件箱检查：
✓ 共 3 封未读邮件
✓ 最新邮件来自 noreply@example.com
✓ 主题：Welcome to Example.com

所有测试均未修改服务器配置或邮箱密码。
```
