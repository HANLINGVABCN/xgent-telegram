#!/usr/bin/env python3
import argparse
import json
import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

def get_domain_candidates(from_addr, config):
    """
    根据发件人地址，生成所有可能的登录域名候选列表（从具体到宽泛）。
    """
    candidates = []
    default_dom = config.get("default_domain", "example.com") # 彻底使用通用占位符作为代码默认值
    
    if "@" not in from_addr:
        return [default_dom]
        
    email_domain = from_addr.split("@")[1].lower()
    parts = email_domain.split(".")
    
    # 依次生成上溯域名
    for i in range(len(parts) - 1):
        candidates.append(".".join(parts[i:]))
        
    # 确保默认域名在最后作为兜底
    if default_dom not in candidates:
        candidates.append(default_dom)
        
    return candidates

def main():
    parser = argparse.ArgumentParser(description="Secure Mail Sender via Local SMTP (Industrial Fallback Support)")
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--from-addr", dest="from_addr", required=True, help="Sender email address")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body content")
    args = parser.parse_args()

    # 从外部系统安全目录读取配置
    config_path = "/etc/mail-manager/config.json"
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print(f"Error reading config: {e}", file=sys.stderr)
        sys.exit(1)

    smtp_host = config.get("smtp_host", "127.0.0.1")
    smtp_port = config.get("smtp_port", 587)
    domains_cfg = config.get("domains", {})
    default_dom = config.get("default_domain", "example.com") # 彻底使用通用占位符
    default_cfg = domains_cfg.get(default_dom, {})

    # 获取所有候选登录域名
    candidates = get_domain_candidates(args.from_addr, config)
    
    server = None
    authenticated = False
    matched_domain = None
    username = None

    # 终极回溯与降级尝试算法
    for domain in candidates:
        # 获取该候选域名的凭证
        if domain in domains_cfg:
            cur_username = domains_cfg[domain].get("username")
            cur_password = domains_cfg[domain].get("password")
        else:
            cur_username = f"admin@{domain}"
            cur_password = default_cfg.get("password") # 继承默认密码

        if not cur_username or not cur_password:
            continue

        try:
            # 尝试连接并登录
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()
            server.login(cur_username, cur_password)
            
            # 登录成功！记录成功的凭证并跳出循环
            authenticated = True
            matched_domain = domain
            username = cur_username
            break
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError):
            # 如果是认证失败，自动尝试下一个候选域名（降级回溯）
            if server:
                try:
                    server.quit()
                except:
                    pass
            continue
        except Exception as e:
            print(f"SMTP Connection Error: {e}", file=sys.stderr)
            sys.exit(1)

    if not authenticated:
        print("Error: All SMTP authentication attempts failed. Please check your credentials.", file=sys.stderr)
        sys.exit(1)

    # 构造邮件内容
    msg = MIMEText(args.body, "plain", "utf-8")
    msg["Subject"] = args.subject
    msg["From"] = args.from_addr
    msg["To"] = args.to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=matched_domain)

    # 智能信封路由判定：
    envelope_from = args.from_addr
    if args.from_addr.lower().endswith("@" + matched_domain) and args.from_addr.lower() != username.lower():
        envelope_from = username

    # 发送邮件
    try:
        server.sendmail(envelope_from, [args.to], msg.as_string())
        server.quit()
        print("SUCCESS: Email sent successfully!")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()