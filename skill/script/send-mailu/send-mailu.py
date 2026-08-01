#!/usr/bin/env python3
"""
Send Mailu 发信工具。

子命令：
  send    发信（默认，可省略子命令直接 send-mailu.py --to ... --body ...）
  get     按绝对路径读取本地 EML，输出邮件信息和正文预览
  check   检查配置是否可用；开始前自动扫描 Mailu 对缺失域名逐个提示添加，再逐账号 SMTP/IMAP 实连测试，失败时提示是否开始全量配置
"""
import argparse
import base64
import getpass
import json
import mimetypes
import os
import sqlite3
import sys
import imaplib
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import formatdate, make_msgid, parseaddr

# 发信配置路径（支持环境变量覆盖，便于测试/迁移）
CONFIG_PATH = os.environ.get("SEND_MAILU_CONFIG", "/etc/send-mailu/config.json")
CONFIG_DIR = os.path.dirname(CONFIG_PATH)

# Mailu .env 探测路径（按优先级）
MAILU_ENV_CANDIDATES = [
    "/mailu/mailu.env",
    "/mailu/.env",
    "/mailu/data/mailu.env",
]

# Mailu SQLite 数据库候选路径（按优先级）
MAILU_DB_CANDIDATES = [
    "/mailu/data/main.db",
    "/mailu/main.db",
    "/mailu/data/mailu.db",
]

# 账号默认前缀（Mailu 默认 POSTMASTER=admin）
DEFAULT_POSTMASTER = "admin"

# 退出码约定
EXIT_OK = 0
EXIT_ERROR = 1          # 一般错误
EXIT_CONFIG_MISSING = 2 # config 缺失/损坏，需运行 init


# ============================================================
# 公共工具函数（send / check / init 复用）
# ============================================================

def extract_email(from_addr):
    """
    从 --from-addr 提取纯邮箱地址。
    支持纯邮箱（'a@b.com'）和带显示名格式（'"名称 <a@b.com>"' / '名称 <a@b.com>'），
    统一返回尖括号内或解析后的邮箱本体，便于域名匹配与信封路由。
    失败时原样返回，交由后续校验处理。
    """
    if not from_addr:
        return from_addr
    _, addr = parseaddr(from_addr)
    return addr if addr else from_addr


def get_domain_candidates(from_addr, config):
    """
    根据发件人地址，生成所有可能的登录域名候选列表（从具体到宽泛）。
    兼容带显示名的格式：先用 extract_email 提取纯邮箱再拆域名。
    """
    candidates = []
    default_dom = config.get("default_domain", "example.com")  # 彻底使用通用占位符作为代码默认值
    from_addr = extract_email(from_addr)

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


def split_addresses(raw):
    """把逗号/分号/空白分隔的收件人字符串拆成地址列表，忽略空项。"""
    if not raw:
        return []
    cleaned = raw.replace(",", " ").replace(";", " ")
    return [item.strip() for item in cleaned.split() if item.strip()]


def resolve_security(config, smtp_port):
    """决定 SMTP 安全模式：显式配置优先，否则按端口智能兜底。"""
    security = str(config.get("smtp_security", "")).strip().lower()
    if security in ("starttls", "ssl", "none"):
        return security
    # 未配置时的兜底：465 通常走隐式 SSL，其余走 STARTTLS（与历史行为一致）
    try:
        if int(smtp_port) == 465:
            return "ssl"
    except (TypeError, ValueError):
        pass
    return "starttls"


def smtp_connect(host, port, security, username, password):
    """
    按指定安全模式建立 SMTP 连接并登录。返回已登录的 server，失败抛异常。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # 允许 IP 地址连接（证书 CN 通常不含 IP）
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=10, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=10)
        if security == "starttls":
            server.starttls(context=ctx)
        # security == "none" 时不加密
    server.login(username, password)
    return server


def close_smtp(server):
    """尽可能优雅地关闭 SMTP 连接，忽略一切错误。"""
    if server is None:
        return
    try:
        server.quit()
    except Exception:
        try:
            server.close()
        except Exception:
            pass


# IMAP 归档默认值（可被 config 的 sent_archive 覆盖）
_DEFAULT_SENT_FOLDER = "Sent"
_DEFAULT_IMAP_PORT = {
    "ssl": 993,
    "starttls": 143,
    "none": 143,
}


def resolve_imap_settings(config, matched_domain, username, password, smtp_security):
    """
    解析发信后 IMAP 归档所需的连接参数。

    config['sent_archive'] 为可选对象：
      enabled (bool, 默认 False)
      host / port / security / folder
    未显式 enable 时不归档（向后兼容现有 config）。

    host 默认沿用 SMTP host；port 默认按 security 推断（ssl→993，其余→143）；
    security 默认沿用 SMTP 的 security；folder 默认 'Sent'。
    缺关键凭证（password）时返回 None 表示不归档。
    """
    arch_cfg = config.get("sent_archive")
    # sent_archive 不是 dict 时一律视为关闭
    if not isinstance(arch_cfg, dict):
        return None

    enabled = bool(arch_cfg.get("enabled", False))
    # 允许 false / 'false' / '0' 等字符串
    enabled_str = str(arch_cfg.get("enabled", "")).strip().lower()
    if enabled_str in ("0", "false", "no", "off"):
        enabled = False
    elif enabled_str in ("1", "true", "yes", "on"):
        enabled = True
    if not enabled:
        return None

    host = str(arch_cfg.get("host") or config.get("smtp_host") or "127.0.0.1").strip()
    security = str(arch_cfg.get("security") or smtp_security or "starttls").strip().lower()
    if security not in ("ssl", "starttls", "none"):
        security = "starttls"
    # 端口：显式 > security 默认
    if arch_cfg.get("port"):
        try:
            port = int(arch_cfg.get("port"))
        except (TypeError, ValueError):
            port = _DEFAULT_IMAP_PORT.get(security, 143)
    else:
        port = _DEFAULT_IMAP_PORT.get(security, 143)
    folder = str(arch_cfg.get("folder") or _DEFAULT_SENT_FOLDER).strip() or _DEFAULT_SENT_FOLDER

    # 归档用的是"已登录成功"的 username/password（即 matched_domain 的凭证）
    if not username or not password:
        return None

    return {
        "host": host,
        "port": port,
        "security": security,
        "folder": folder,
        "username": username,
        "password": password,
    }


def archive_to_sent(imap_settings, raw_message):
    """
    通过 IMAP APPEND 把整封邮件归档到「已发送」文件夹。

    raw_message: 已构造好的邮件字符串（含头）。用 IMAP INTERNALDATE 标为"现在"。
    任何失败都吞掉（仅记到 stderr 告警），不影响发信本身的成功判定。
    """
    if not imap_settings:
        return
    host = imap_settings["host"]
    port = imap_settings["port"]
    security = imap_settings["security"]
    folder = imap_settings["folder"]
    username = imap_settings["username"]
    password = imap_settings["password"]

    # imaplib.append 的 date_time 只接受 datetime/struct_time，不接受字符串
    # （内部 Time2Internaldate 会自动转成带引号的 "20-Jun-2026 12:34:56 +0000"）。
    internaldate = datetime.now(timezone.utc)

    imap = None
    try:
        if security == "ssl":
            imap_ctx = ssl.create_default_context()
            imap_ctx.check_hostname = False  # 允许 IP 地址连接
            imap = imaplib.IMAP4_SSL(host, port, timeout=10, ssl_context=imap_ctx)
        else:
            imap = imaplib.IMAP4(host, port, timeout=10)
            if security == "starttls":
                imap_ctx = ssl.create_default_context()
                imap_ctx.check_hostname = False
                imap.starttls(imap_ctx)
        imap.login(username, password)

        # mailbox 不用手动加引号，imaplib 会自动转义；手加会变成 ""Sent"" 双重转义
        typ, data = imap.append(folder, r'(\Seen)', internaldate, raw_message.encode("utf-8"))
        if typ != "OK":
            print(f"Warning: IMAP 归档到 {folder} 未成功 (server reply: {typ} {data})", file=sys.stderr)
    except Exception as e:
        print(f"Warning: IMAP 归档失败（不影响发信结果）: {e}", file=sys.stderr)
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def load_config(path=CONFIG_PATH):
    """
    读取并解析 config.json。返回 (config_dict, error_str)。
    成功时 error_str 为 None；失败时 config_dict 为 None。
    """
    if not os.path.exists(path):
        return None, f"配置文件不存在: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        return None, f"配置文件不是合法 JSON: {e}"
    except OSError as e:
        return None, f"读取配置文件失败: {e}"
    if not isinstance(config, dict):
        return None, "配置文件根结构必须是 JSON 对象 {}"
    return config, None


def print_init_hint(stream=sys.stderr):
    """打印配置引导提示。"""
    print("  → 请运行检查命令（失败后可交互配置）：", file=stream)
    print(f"    sudo {sys.argv[0]} check", file=stream)


# ============================================================
# send 子命令（发信）
# ============================================================

def read_body(args):
    """
    解析正文来源。--body 与 --body-file 互斥，且至少给一个。
    返回纯文本正文（可能为空字符串，当仅提供 HTML 时）。
    """
    if args.body is not None and args.body_file is not None:
        print("Error: --body 和 --body-file 不能同时使用，请二选一。", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if args.body is None and args.body_file is None and not args.html:
        print("Error: 必须提供 --body 或 --body-file（或 --html）作为正文。", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    if args.body_file is not None:
        if not os.path.isfile(args.body_file):
            print(f"Error: 正文文件不存在: {args.body_file}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            print(f"Error: 读取正文文件失败: {e}", file=sys.stderr)
            sys.exit(EXIT_ERROR)

    return args.body if args.body is not None else ""


def add_attachment_part(msg, path):
    """把单个文件作为附件 part 添加到 msg。失败则打印错误并退出。"""
    if not os.path.isfile(path):
        print(f"Error: 附件不存在或不是文件: {path}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"Error: 读取附件失败 ({path}): {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    maintype, subtype = (mimetypes.guess_type(path)[0] or "application/octet-stream").split("/", 1)
    part = MIMEBase(maintype, subtype)
    try:
        # 显式 base64 编码（编码表里不含不可打印字符，最兼容）
        part.set_payload(base64.b64encode(data).decode("ascii"))
    except Exception as e:  # 极端情况兜底
        print(f"Error: 编码附件失败 ({path}): {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    part.add_header("Content-Transfer-Encoding", "base64")
    filename = os.path.basename(path) or "attachment"
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    msg.attach(part)


def build_message(args, plain_body, matched_domain):
    """
    构造完整邮件对象（含公共头）。
    根据是否有 HTML 正文 / 附件，自动选择最合适的 MIME 结构。
    """
    has_html = bool(args.html)
    has_attachments = bool(args.attach)

    if has_attachments:
        msg = MIMEMultipart("mixed")
        # 正文 part
        if has_html:
            if plain_body:
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(plain_body, "plain", "utf-8"))
                alt.attach(MIMEText(args.html, "html", "utf-8"))
                msg.attach(alt)
            else:
                msg.attach(MIMEText(args.html, "html", "utf-8"))
        else:
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        # 附件
        for path in args.attach:
            add_attachment_part(msg, path)
    elif has_html:
        # 无附件，但需同时给纯文本降级
        msg = MIMEMultipart("alternative")
        if plain_body:
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(args.html, "html", "utf-8"))
    else:
        # 最简情况：纯文本单 part（与历史行为一致）
        msg = MIMEText(plain_body, "plain", "utf-8")

    # 公共头
    msg["Subject"] = args.subject
    msg["From"] = args.from_addr
    msg["To"] = args.to
    cc_list = split_addresses(args.cc)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=matched_domain)
    return msg


def cmd_send(args):
    """发信子命令。"""
    # 读取 config（缺失/损坏时给 init 引导，exit code 2）
    config, err = load_config()
    if err:
        print(f"Error: {err}", file=sys.stderr)
        print_init_hint()
        sys.exit(EXIT_CONFIG_MISSING)

    smtp_host = config.get("smtp_host", "127.0.0.1")
    smtp_port = config.get("smtp_port", 587)
    domains_cfg = config.get("domains", {})
    default_dom = config.get("default_domain", "example.com")  # 彻底使用通用占位符
    default_cfg = domains_cfg.get(default_dom, {})
    security = resolve_security(config, smtp_port)

    # 解析正文（同时做参数互斥校验）
    plain_body = read_body(args)

    # 收件人列表
    to_list = split_addresses(args.to)
    cc_list = split_addresses(args.cc)
    bcc_list = split_addresses(args.bcc)
    if not to_list:
        print("Error: --to 未提供有效收件人地址", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    all_recipients = to_list + cc_list + bcc_list

    # 获取所有候选登录域名
    candidates = get_domain_candidates(args.from_addr, config)

    server = None
    authenticated = False
    matched_domain = None
    username = None
    matched_password = None  # 记录登录成功的明文密码，供 IMAP 归档复用

    # 终极回溯与降级尝试算法
    for domain in candidates:
        # 获取该候选域名的凭证
        if domain in domains_cfg:
            cur_username = domains_cfg[domain].get("username")
            cur_password = domains_cfg[domain].get("password")
        else:
            cur_username = f"admin@{domain}"
            cur_password = default_cfg.get("password")  # 继承默认密码

        if not cur_username or not cur_password:
            continue

        try:
            # 尝试连接并登录
            server = smtp_connect(smtp_host, smtp_port, security, cur_username, cur_password)

            # 登录成功！记录成功的凭证并跳出循环
            authenticated = True
            matched_domain = domain
            username = cur_username
            matched_password = cur_password
            break
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError):
            # 如果是认证失败，自动尝试下一个候选域名（降级回溯）
            close_smtp(server)
            server = None
            continue
        except Exception as e:
            print(f"SMTP Connection Error: {e}", file=sys.stderr)
            close_smtp(server)
            sys.exit(EXIT_ERROR)

    if not authenticated:
        print("Error: 所有 SMTP 认证尝试均失败，请检查凭证。", file=sys.stderr)
        print("  可能原因：config 里的密码错误或已过期。", file=sys.stderr)
        print_init_hint()
        sys.exit(EXIT_ERROR)

    # 智能信封路由判定：
    # 智能信封路由判定：基于纯邮箱判断后缀，避免带显示名时末尾为 `>` 导致 endswith 失效。
    # 信封发件人（envelope_from）用纯邮箱走 SMTP 防拒绝；Header From 仍保留原始 args.from_addr（含显示名）。
    pure_from = extract_email(args.from_addr)
    envelope_from = pure_from
    if pure_from.lower().endswith("@" + matched_domain) and pure_from.lower() != username.lower():
        envelope_from = username

    # 构造邮件（Message-ID 域名兜底用 matched_domain 或 default_dom）
    msg = build_message(args, plain_body, matched_domain or default_dom)
    # 一次序列化，SMTP 投递与 IMAP 归档复用同一份内容（保证两者一致）
    raw_message = msg.as_string()

    # 发送邮件（finally 确保连接关闭，修复历史 socket 泄漏）
    try:
        server.sendmail(envelope_from, all_recipients, raw_message)
        print("SUCCESS: Email sent successfully!")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    finally:
        close_smtp(server)

    # 发信成功后，把同一份邮件归档到「已发送」文件夹（IMAP APPEND）。
    # 归档是尽力而为：未配置/未开启/归档失败都不影响发信结果。
    imap_settings = resolve_imap_settings(config, matched_domain, username, matched_password, security)
    if imap_settings:
        archive_to_sent(imap_settings, raw_message)


# ============================================================
# check 子命令（检查 config 可用性 + 失败时引导配置）
# ============================================================

def _run_check(do_connect):
    """执行所有检查项，打印结果，返回 all_ok:bool。"""
    results = []

    # 1) 文件存在 + JSON 合法
    config, err = load_config()
    if err:
        results.append((False, f"读取配置: {err}"))
        _emit_check_results(results, [], do_connect)
        return False, []
    results.append((True, f"读取配置: {CONFIG_PATH}"))

    # 2) 必填字段
    smtp_host = config.get("smtp_host", "127.0.0.1")
    smtp_port = config.get("smtp_port", 587)
    default_dom = config.get("default_domain", "")
    domains_cfg = config.get("domains", {})

    field_ok = True
    if not default_dom:
        results.append((False, "缺少必填字段: default_domain"))
        field_ok = False
    if not isinstance(domains_cfg, dict) or not domains_cfg:
        results.append((False, "缺少必填字段或为空: domains"))
        field_ok = False
    if field_ok:
        results.append((True, "必填字段齐全"))

    # 3) 凭证：username 必须非空；password 可空（空密码会在 SMTP 实连时失败，
    #    由 cmd_check 的失败菜单逐个补密码），因此 password 空不影响格式判定。
    cred_ok = True
    if isinstance(domains_cfg, dict) and domains_cfg:
        for dom, cred in domains_cfg.items():
            if not isinstance(cred, dict):
                results.append((False, f"domains.{dom} 结构不正确（应为对象）"))
                cred_ok = False
                continue
            if not cred.get("username"):
                results.append((False, f"domains.{dom} 的 username 为空"))
                cred_ok = False
        if cred_ok:
            missing_pw = [d for d, c in domains_cfg.items() if isinstance(c, dict) and not c.get("password")]
            if missing_pw:
                results.append((True, f"凭证格式齐全（{len(domains_cfg)} 个域名，其中 {len(missing_pw)} 个密码待设）"))
            else:
                results.append((True, f"凭证完整（{len(domains_cfg)} 个域名）"))

    # 4) 安全模式解析（顺带验证取值合法）
    try:
        security = resolve_security(config, smtp_port)
        results.append((True, f"安全模式: {security}（端口 {smtp_port}）"))
    except Exception as e:
        results.append((False, f"解析安全模式失败: {e}"))
        security = None

    # 5) SMTP 实连测试：每个配置账号都实际登录一次，不能只测代表账号。
    smtp_results = []
    smtp_failures = []  # [(dom, uname, reason), ...] 供失败菜单使用
    if do_connect and field_ok and cred_ok and security:
        for dom, cred in domains_cfg.items():
            uname = cred.get("username")
            upass = cred.get("password")
            try:
                srv = smtp_connect(smtp_host, smtp_port, security, uname, upass)
                close_smtp(srv)
                smtp_results.append((True, f"{uname}：登录成功"))
            except smtplib.SMTPAuthenticationError as e:
                reason = f"认证失败 — {e}"
                smtp_results.append((False, f"{uname}：{reason}"))
                smtp_failures.append((dom, uname, reason))
            except Exception as e:
                reason = f"连接失败 — {e}"
                smtp_results.append((False, f"{uname}：{reason}"))
                smtp_failures.append((dom, uname, reason))

    _emit_check_results(results, smtp_results, do_connect)

    # 6) IMAP 归档逐账号测试（建议性，不计入 config 可用性判定）。
    #    发信时使用哪个账号成功登录 SMTP，就使用哪个账号登录 IMAP 并归档到它自己的 Sent。
    imap_results = []
    imap_notes = []
    if do_connect and field_ok and cred_ok and security:
        imap_notes, imap_results = _probe_imap_all_accounts(config, domains_cfg, security)
        print("\n--- IMAP Sent 逐邮箱测试（不影响发信判定）---")
        print("  每个邮箱检查自己的 Sent；实际用谁登录发信，就归档到谁的 Sent。")
        for note in imap_notes:
            print(f"  {note}")
        for ok, msg in imap_results:
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {msg}")
        if imap_results:
            imap_ok_count = sum(1 for ok, _ in imap_results if ok)
            print(f"  结果：{imap_ok_count}/{len(imap_results)} 通过")
    elif not do_connect:
        print("\n--- IMAP Sent 逐邮箱测试 ---")
        print("  [·] --no-connect：已跳过")
    else:
        print("\n--- IMAP Sent 逐邮箱测试 ---")
        print("  [·] 基础配置未通过，未测试")

    all_ok = all(ok for ok, _ in (results + smtp_results))
    return all_ok, smtp_failures


def _probe_imap_once(imap_settings, uname, upass):
    """
    用给定 IMAP 设置登录一次，检查目标文件夹是否存在。
    返回 (status, message)：status 为 True/False。
    """
    host = imap_settings["host"]
    port = imap_settings["port"]
    sec = imap_settings["security"]
    folder = imap_settings["folder"]
    imap = None
    try:
        if sec == "ssl":
            imap = imaplib.IMAP4_SSL(host, port, timeout=10)
        else:
            imap = imaplib.IMAP4(host, port, timeout=10)
            if sec == "starttls":
                imap.starttls()
        imap.login(uname, upass)
        # LIST 全部文件夹，模糊匹配目标文件夹名（兼容 Sent / Sent Messages / 已发送 等命名）
        typ, data = imap.list()
        folders = []
        if typ == "OK":
            for item in data:
                try:
                    folders.append(item.decode("utf-8", "ignore"))
                except Exception:
                    folders.append(str(item))
        folder_match = any(folder.lower() in f.lower() for f in folders)
        if folder_match:
            return (True, f"{uname}：IMAP 登录成功；自己的文件夹「{folder}」存在（{host}:{port}/{sec}）")
        sample = ", ".join(folders[:8]) if folders else "(空)"
        return (False, f'{uname}：IMAP 登录成功，但自己的文件夹「{folder}」未找到；请检查 sent_archive.folder。现有文件夹：{sample}')
    except Exception as e:
        return (False, f"{uname}：IMAP 连接/登录失败（{host}:{port}/{sec}）— {e}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def _probe_imap_all_accounts(config, domains_cfg, security):
    """逐个账号检查 IMAP 登录和各自的 Sent 文件夹。

    返回 (notes, results)。IMAP 是发信后的尽力归档能力，因此结果不影响
    SMTP/config 的 exit code；但每个账号都会单独输出，避免把 default_domain
    误解成统一归档邮箱。
    """
    arch_cfg = config.get("sent_archive")
    enabled = isinstance(arch_cfg, dict) and bool(arch_cfg.get("enabled", False))
    enabled_str = str(arch_cfg.get("enabled", "")).strip().lower() if isinstance(arch_cfg, dict) else ""
    if enabled_str in ("0", "false", "no", "off"):
        enabled = False
    if not enabled:
        return (["归档功能未开启（sent_archive.enabled=false/缺省）；发信不会写入 Sent。"], [])

    def run_all(test_config):
        account_results = []
        for dom, cred in domains_cfg.items():
            if not isinstance(cred, dict):
                account_results.append((False, f"{dom}：凭证结构无效"))
                continue
            uname = cred.get("username")
            upass = cred.get("password")
            settings = resolve_imap_settings(test_config, dom, uname, upass, security)
            if not settings:
                account_results.append((False, f"{uname or dom}：没有可用归档凭证或配置"))
                continue
            ok, msg = _probe_imap_once(settings, uname, upass)
            account_results.append((ok, msg))
        return account_results

    results = run_all(config)
    notes = []

    # 仅当所有账号都未通过时，尝试一次端口自动修正；已有账号通过时，
    # 其他失败应保留为各账号自身的密码/文件夹问题，不要误改全局配置。
    if results and not any(ok for ok, _ in results):
        detected = detect_imap_settings(config.get("smtp_host", "127.0.0.1"))
        current = resolve_imap_settings(config, "", "probe", "probe", security)
        if detected and current and (
            detected["host"] != current["host"]
            or detected["port"] != current["port"]
            or detected["security"] != current["security"]
        ):
            patched = patch_sent_archive({
                "host": detected["host"],
                "port": detected["port"],
                "security": detected["security"],
            })
            if patched:
                notes.append(
                    f"🔧 当前 IMAP 参数不可用，已自动改为 {detected['security']} @ "
                    f"{detected['host']}:{detected['port']}，下面是重新测试结果。"
                )
                retry_config = dict(config)
                retry_config["sent_archive"] = dict(arch_cfg)
                retry_config["sent_archive"].update({
                    "host": detected["host"],
                    "port": detected["port"],
                    "security": detected["security"],
                })
                results = run_all(retry_config)
            else:
                notes.append(
                    f"💡 检测到可用 IMAP：{detected['security']} @ {detected['host']}:{detected['port']}；"
                    "自动写入配置失败，请手动修改 sent_archive。"
                )
        elif not detected:
            notes.append("💡 993/143 均无法连通；可能是防火墙、容器端口或 IMAP 服务未开放。")
        else:
            notes.append("💡 当前 IMAP 连接参数可达但所有账号均未通过，请分别检查各账号密码和 Sent 文件夹名称。")
    elif results and not all(ok for ok, _ in results):
        notes.append("💡 部分账号未通过；连接参数已被通过的账号验证可用，请检查失败行对应账号的密码或 Sent 文件夹。")

    return notes, results



def _auto_init_from_mailu():
    """config 不存在时，用 detect_defaults 从 Mailu 自动建基础 config
    （smtp_host/port/security + default_domain + 扫到的账户密码留空）。扫不到
    Mailu（source 为空）或没域名时返回 False，交给 y/n 全量配置。返回是否已建。
    """
    defaults = detect_defaults()
    if not defaults.get("source"):
        return False
    smtp_host = defaults.get("smtp_host") or "127.0.0.1"
    smtp_port = defaults.get("smtp_port") or 587
    security = defaults.get("security") or "starttls"
    domains = defaults.get("domains") or []
    accounts = defaults.get("accounts") or []
    if not domains:
        return False
    domain_to_accounts = {}
    for em in accounts:
        if "@" in em:
            domain_to_accounts.setdefault(em.split("@", 1)[1], []).append(em)
    domains_cfg = {}
    for dom in domains:
        accs = domain_to_accounts.get(dom, [])
        if accs:
            uname = next((a for a in accs if a.split("@", 1)[0] == DEFAULT_POSTMASTER), accs[0])
        else:
            uname = f"{DEFAULT_POSTMASTER}@{dom}"
        domains_cfg[dom] = {"username": uname, "password": ""}
    arch = {"enabled": True, "folder": _DEFAULT_SENT_FOLDER}
    imap_detected = detect_imap_settings(smtp_host)
    if imap_detected:
        arch.update(imap_detected)
    config = {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_security": security,
        "default_domain": domains[0],
        "domains": domains_cfg,
        "sent_archive": arch,
    }
    try:
        _write_config(config, force=True)
    except SystemExit:
        print("Error: 自动写入 config 失败，转全量配置。", file=sys.stderr)
        return False
    print("\n--- 自动从 Mailu 建立配置 ---")
    print(f"✓ 已从 Mailu 自动建立 config: {CONFIG_PATH}")
    print(f"  SMTP {smtp_host}:{smtp_port} ({security})，{len(domains_cfg)} 个域名，账户已加、密码留空待补")
    return True


def _auto_add_missing_accounts():
    """扫描 Mailu，把 config 中缺失的域名自动加进去（只加账户，密码留空），不询问。

    每个域名只存一个登录账号（与现有 config 结构一致）。无 config、探测不到任何
    域名、或全部已添加时静默跳过。密码留空，留给 SMTP 实连测试暴露失败、由失败
    菜单逐个补。返回本次自动添加的域名数。
    """
    config, err = load_config()
    if err:
        return 0  # 无 config，交给 check 失败流程走全量配置

    existing = set((config.get("domains") or {}).keys())
    defaults = detect_defaults()
    accounts = defaults.get("accounts") or []
    known_domains = defaults.get("domains") or []

    # 优先用 user 表里的真实账户推域名；没有则退到 domain 表/.env 的域名
    domain_to_accounts = {}
    if accounts:
        for em in accounts:
            if "@" in em:
                dom = em.split("@", 1)[1]
                domain_to_accounts.setdefault(dom, []).append(em)
        candidate_domains = sorted(domain_to_accounts.keys())
    elif known_domains:
        candidate_domains = list(known_domains)
    else:
        return 0

    print("\n--- 账户自动扫描 ---")
    print(f"Mailu 检测到 {len(candidate_domains)} 个域名账户，config 已有 {len(existing)} 个")
    missing = [d for d in candidate_domains if d not in existing]
    if not missing:
        print("✓ 全部已在 config 中，无需添加")
        return 0

    print(f"检测到 {len(missing)} 个域名尚未配置: {', '.join(missing)}")
    print("自动添加账户（密码留空，稍后由 SMTP 失败菜单逐个补密码）。\n")

    updates = {}
    added_users = []
    for dom in missing:
        accs = domain_to_accounts.get(dom, [])
        if accs:
            default_user = next(
                (a for a in accs if a.split("@", 1)[0] == DEFAULT_POSTMASTER),
                accs[0],
            )
        else:
            default_user = f"{DEFAULT_POSTMASTER}@{dom}"
        updates[dom] = {"username": default_user, "password": ""}
        added_users.append(default_user)

    if merge_domain_credentials(updates):
        print(f"✓ 已自动添加 {len(updates)} 个账户: {', '.join(added_users)}")
        print("  （密码留空；下面 SMTP 测试会逐个标记登录失败，到时按菜单补密码。）")
        return len(updates)
    print(f"Error: 写入配置失败，本次 {len(updates)} 个域名未保存。", file=sys.stderr)
    return 0


def _retest_one(dom, do_connect):
    """单测一个域名的 SMTP 登录，返回 (ok, msg)。"""
    config, err = load_config()
    if err:
        return (False, "读取配置失败")
    cred = (config.get("domains") or {}).get(dom) or {}
    uname = cred.get("username")
    upass = cred.get("password")
    if not uname or not upass:
        return (False, "账号或密码为空")
    if not do_connect:
        return (True, "未实连（--no-connect），格式通过")
    smtp_host = config.get("smtp_host", "127.0.0.1")
    smtp_port = config.get("smtp_port", 587)
    try:
        security = resolve_security(config, smtp_port)
    except Exception as e:
        return (False, f"解析安全模式失败 — {e}")
    try:
        srv = smtp_connect(smtp_host, smtp_port, security, uname, upass)
        close_smtp(srv)
        return (True, "登录成功")
    except smtplib.SMTPAuthenticationError as e:
        return (False, f"认证失败 — {e}")
    except Exception as e:
        return (False, f"连接失败 — {e}")


def _failure_menu(args, do_connect, failures):
    """对 SMTP 登录失败的账号循环提供菜单：1 给失败邮箱输密码（单测） /
    2 手动添加账户+密码 / 0 退出。failures: [(dom, uname, reason), ...]。

    全部失败账号处理并通过后返回；按 0 直接 sys.exit(EXIT_ERROR)。
    """
    if not failures:
        return
    while failures:
        print("\n--- 登录失败处理 ---")
        print("失败账号:")
        for i, (dom, uname, reason) in enumerate(failures, 1):
            print(f"  {i}. {uname}  ({reason})")
        print("  按 1：给失败邮箱输密码（再选哪个）")
        print("  按 2：手动添加账户和密码（扫描没扫到的）")
        print("  按 0：退出")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(EXIT_ERROR)

        if choice == "0":
            sys.exit(EXIT_ERROR)

        if choice == "1":
            if len(failures) == 1:
                pick = 0
            else:
                try:
                    pick = int(input(f"选哪个（1-{len(failures)}）: ").strip()) - 1
                except (EOFError, KeyboardInterrupt):
                    print(); sys.exit(EXIT_ERROR)
                except ValueError:
                    print("  输入无效，跳过。"); continue
                if pick < 0 or pick >= len(failures):
                    print("  编号超出范围，跳过。"); continue
            dom, uname, _reason = failures[pick]
            pw = _ask_password_for(uname)
            if not pw:
                print(f"  密码为空，{uname} 未更新。"); continue
            if not merge_domain_credentials({dom: {"username": uname, "password": pw}}):
                print(f"  Error: 写入失败，{uname} 未保存。", file=sys.stderr); continue
            ok, msg = _retest_one(dom, do_connect)
            if ok:
                print(f"  ✓ {uname}：登录成功，已从失败列表移除")
                failures.pop(pick)
            else:
                print(f"  ✗ {uname}：{msg}（仍在失败列表，可再试或改账号）")
                failures[pick] = (dom, uname, msg)

        elif choice == "2":
            uname = _ask("手动添加的登录账号（完整邮箱）", "")
            if "@" not in uname:
                print("  账号必须是完整邮箱，跳过。"); continue
            dom = uname.split("@", 1)[1]
            pw = _ask_password_for(uname)
            if not pw:
                print("  密码为空，跳过。"); continue
            if not merge_domain_credentials({dom: {"username": uname, "password": pw}}):
                print(f"  Error: 写入失败，{uname} 未保存。", file=sys.stderr); continue
            ok, msg = _retest_one(dom, do_connect)
            if ok:
                print(f"  ✓ {uname}：登录成功")
                failures = [f for f in failures if f[0] != dom]
            else:
                print(f"  ✗ {uname}：{msg}")
                failures.append((dom, uname, msg))

        else:
            print("  输入无效，请输 0/1/2。")

    print("\n✓ 全部失败账号已处理并通过。")


def cmd_check(args):
    """
    检查 config.json：先自动扫描 Mailu、把缺失账户自动加进 config（密码留空），
    再跑基础配置 + 逐账号 SMTP/IMAP 实连测试。SMTP 有登录失败时弹失败菜单
    （1 给失败邮箱输密码 / 2 手动加账户+密码 / 0 退出）；基础配置未通过或
    --no-connect 时，提示是否开始全量配置。
    """
    do_connect = not args.no_connect
    config, _err = load_config()
    if _err:
        # config 不存在：先尝试用 Mailu 自动建基础 config（smtp + 扫到的账户密码空）
        _auto_init_from_mailu()  # 扫不到 Mailu 返回 False，下面 _run_check 显示 err → 走 y/n 全量
    else:
        _auto_add_missing_accounts()
    all_ok, failures = _run_check(do_connect)

    if all_ok:
        return

    if do_connect and failures:
        # SMTP 实连有失败：弹失败菜单（输密码 / 手动加 / 退出）
        _failure_menu(args, do_connect, failures)
        return

    # 基础配置未通过（default_domain 缺、domains 空、security 解析失败等）或 --no-connect：
    # 提示是否开始全量配置
    try:
        answer = input("\n检查未通过。按 y 开始配置（扫 Mailu 找账号 → 逐项确认 SMTP/域名 → 逐个输密码 → 生成 config）；按 n 退出。(y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_ERROR)
    if answer in ('y', 'yes'):
        args.force = True  # 用户确认重新配置，允许覆盖
        cmd_init(args)
        # 配置完成后重新检查
        print("\n--- 重新检查配置 ---")
        recheck_ok, _ = _run_check(do_connect)
        if not recheck_ok:
            sys.exit(EXIT_ERROR)
        return

    sys.exit(EXIT_ERROR)


def _emit_check_results(results, smtp_results, do_connect):
    """简洁打印基础配置和逐邮箱 SMTP 测试结果。"""
    print(f"检查配置: {CONFIG_PATH}")
    print("\n--- 基础配置 ---")
    for ok, msg in results:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {msg}")

    print("\n--- SMTP 逐邮箱测试（只登录，不发信）---")
    if do_connect:
        for ok, msg in smtp_results:
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {msg}")
        if smtp_results:
            smtp_ok_count = sum(1 for ok, _ in smtp_results if ok)
            print(f"  结果：{smtp_ok_count}/{len(smtp_results)} 通过")
        else:
            print("  [·] 基础配置未通过，未测试")
    else:
        print("  [·] --no-connect：已跳过")



# ============================================================
# 配置生成逻辑（由 check 失败时触发）
# ============================================================

def _find_mailu_env():
    """按优先级查找 Mailu .env，返回路径或 None。"""
    for p in MAILU_ENV_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 当前目录及上级
    cur = os.getcwd()
    for _ in range(3):
        for name in ("mailu.env", ".env"):
            cand = os.path.join(cur, name)
            if os.path.isfile(cand):
                # 简单判定是否像 Mailu（含 DOMAIN= 或 HOSTNAMES=）
                if _looks_like_mailu_env(cand):
                    return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _looks_like_mailu_env(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        return "DOMAIN=" in text or "HOSTNAMES=" in text
    except OSError:
        return False


def parse_env_file(path):
    """简易解析 KEY=VALUE，返回 dict（值去引号/去注释）。不 source，避免副作用。"""
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # 去掉行内注释（仅当 # 前有空格，避免误伤 URL 里的 #）
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                # 去引号
                if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                    val = val[1:-1]
                data[key] = val
    except OSError:
        pass
    return data


def _find_mailu_db():
    """按优先级查找 Mailu SQLite 数据库，返回路径或 None。"""
    for p in MAILU_DB_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _scan_mailu_accounts(db_path):
    """
    扫描 Mailu SQLite 数据库，提取所有域名及每个域名下的账号。

    返回 (domains:list[str], accounts:dict[email->None], error:str|None)。
    accounts 键即完整邮箱；同时也会从 user 表补充独立账号。
    """
    domains = []
    accounts = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        # 1) 域名表（Mailu 表名 domain，字段 name）
        try:
            cur.execute("SELECT name FROM domain ORDER BY name")
            domains = [row["name"] for row in cur.fetchall() if row["name"]]
        except sqlite3.Error:
            pass
        # 2) 用户表（Mailu 表名 user，字段 email / domain）
        try:
            cur.execute("SELECT email FROM user ORDER BY email")
            accounts = [row["email"] for row in cur.fetchall() if row["email"]]
        except sqlite3.Error:
            pass
        con.close()
    except sqlite3.Error as e:
        return [], [], str(e)

    # 合并：账号里出现的域名也纳入；账号列表去重保序
    seen = set(domains)
    for em in accounts:
        if "@" in em:
            dom = em.split("@", 1)[1]
            if dom not in seen:
                seen.add(dom)
                domains.append(dom)

    # accounts 去重保序
    seen_acc = set()
    accounts_dedup = []
    for em in accounts:
        if em not in seen_acc:
            seen_acc.add(em)
            accounts_dedup.append(em)

    return domains, accounts_dedup, None


def get_public_ip():
    """尝试获取本机的公网 IP，失败则回退到 127.0.0.1"""
    try:
        import urllib.request
        return urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode("utf-8").strip()
    except Exception:
        return "127.0.0.1"


def detect_defaults():
    """
    探测 Mailu 环境得到 init 默认值。
    优先扫 SQLite 数据库（可发现全部域名/账号），回退到 .env（仅主域名）。
    返回 dict：{domain, username, smtp_host, smtp_port, security, source,
                domains(list), accounts(list)}
    """
    d = {
        "domain": "",
        "username": "",
        "smtp_host": get_public_ip(),
        "smtp_port": 587,
        "security": "starttls",
        "source": None,
        "domains": [],          # 全部已知域名（数据库或 .env 推导）
        "accounts": [],         # 全部已知账号（完整邮箱）
        "default_domain": "",   # 推荐的 default_domain
    }

    # 1) 先试 SQLite 数据库（最权威，能拿到全部域名/账号）
    db_path = _find_mailu_db()
    if db_path:
        doms, accs, db_err = _scan_mailu_accounts(db_path)
        if not db_err and (doms or accs):
            d["domains"] = doms
            d["accounts"] = accs
            d["source"] = db_path
            d["default_domain"] = doms[0] if doms else ""
            if accs:
                d["domain"] = accs[0].split("@", 1)[1] if "@" in accs[0] else ""
                d["username"] = accs[0]
            elif doms:
                d["domain"] = doms[0]
                d["username"] = f"{DEFAULT_POSTMASTER}@{doms[0]}"
            return d

    # 2) 回退到 .env（只有主域名信息）
    env_path = _find_mailu_env()
    if not env_path:
        return d
    env = parse_env_file(env_path)
    domain = env.get("DOMAIN", "").strip()
    postmaster = env.get("POSTMASTER", DEFAULT_POSTMASTER).strip() or DEFAULT_POSTMASTER
    hostnames = [h.strip() for h in env.get("HOSTNAMES", "").split(",") if h.strip()]
    if domain:
        d["domain"] = domain
        d["username"] = f"{postmaster}@{domain}"
        d["domains"] = [domain]
        # HOSTNAMES 如 mail.example.com 不可直接当域名，但可作信息
        d["default_domain"] = domain
    d["source"] = env_path
    return d


def detect_imap_settings(smtp_host):
    """
    探测 IMAP 归档可用连接参数。

    Mailu 常见两种 IMAP 端口：993（IMAPS，隐式 SSL）和 143（IMAP，明文/STARTTLS）。
    不少 VPS 防火墙会拦 143 防爆破，但放行 993；端口也可能绑在公网 IP 而非 127.0.0.1。
    这里用纯 socket 试连，优先 993，回退 143，返回第一个能连上的那套配置。

    返回 dict: {host, port, security, folder}，探测失败返回 None（不写 sent_archive）。
    """
    import socket

    candidates = [
        (smtp_host, 993, "ssl"),
        (smtp_host, 143, "starttls"),
        ("127.0.0.1", 993, "ssl"),
        ("127.0.0.1", 143, "starttls"),
    ]
    for host, port, security in candidates:
        try:
            with socket.create_connection((host, port), timeout=3):
                return {"host": host, "port": port, "security": security, "folder": _DEFAULT_SENT_FOLDER}
        except OSError:
            continue
    return None


def _ask(prompt, default=""):
    """交互式提问，支持默认值（直接回车采用）。"""
    suffix = f" [{default}]" if default != "" else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_ERROR)
    return raw if raw else default


def _ask_password_for(label, default=""):
    """带账号标签的密码询问，不回显。"""
    try:
        pw = getpass.getpass(f"  {label} 的密码: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_ERROR)
    return pw if pw else default


def cmd_init(args):
    """交互式生成 config.json（多域名）。纯交互一条路：逐项询问、逐个输密码。"""
    defaults = detect_defaults()

    # ---------- 交互模式 ----------
    if defaults["source"]:
        kind = "数据库" if defaults["accounts"] or (defaults["domains"] and not defaults["source"].endswith(".env")) else ".env"
        print(f"已检测到 Mailu {kind}: {defaults['source']}")
        if defaults["domains"]:
            print(f"发现域名 ({len(defaults['domains'])}): {', '.join(defaults['domains'])}")
        if defaults["accounts"]:
            print(f"发现账号 ({len(defaults['accounts'])}): {', '.join(defaults['accounts'])}")
        print("密码在数据库里是 hash，需逐个输入明文。直接回车采用方括号内默认值。\n")
    else:
        print("未检测到 Mailu 环境，将逐项询问。直接回车采用方括号内默认值。\n")

    smtp_host = _ask("[1/5] SMTP 服务器地址", args.smtp_host or defaults["smtp_host"])
    smtp_port = _ask("[2/5] SMTP 端口 (587=STARTTLS / 465=SSL / 25=无加密)", str(args.smtp_port or defaults["smtp_port"]))
    security = _ask("[3/5] 安全模式 (starttls/ssl/none)", args.security or defaults["security"])

    # 域名列表：默认用探测到的，可手动改
    default_domains_str = ",".join(defaults["domains"]) if defaults["domains"] else ""
    domains_input = _ask("[4/5] 所有域名 (逗号分隔)", args.domains or default_domains_str)
    domain_list = split_addresses(domains_input)
    if not domain_list:
        print("Error: 至少需要一个域名。", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    username_prefix = _ask("      用户名前缀 (每域一个账号)", args.username_prefix or DEFAULT_POSTMASTER)
    accounts = [f"{username_prefix}@{dom}" for dom in domain_list]

    # 5) 逐个询问密码（不可回显）
    print(f"\n[5/5] 逐个输入密码（{len(domain_list)} 个域名，输入不可见）:")
    passwords = []
    for dom, acc in zip(domain_list, accounts):
        pw = _ask_password_for(acc)
        if not pw:
            print(f"Error: {acc} 的密码不能为空。", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        passwords.append(pw)
    print()

    # ---------- 校验 ----------
    try:
        smtp_port = int(smtp_port)
    except (TypeError, ValueError):
        print(f"Error: SMTP 端口必须是数字，得到: {smtp_port}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if security not in ("starttls", "ssl", "none"):
        print(f"Error: 安全模式必须是 starttls/ssl/none，得到: {security}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    for acc in accounts:
        if "@" not in acc:
            print(f"Error: 账号必须是完整邮箱地址: {acc}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
    if not all(passwords):
        print("Error: 登录密码不能为空。", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    # ---------- 组装多域名 config ----------
    domains_cfg = {}
    for dom, acc, pw in zip(domain_list, accounts, passwords):
        domains_cfg[dom] = {"username": acc, "password": pw}

    # 探测 IMAP 归档可用端口（很多 VPS 拦 143 但放行 993，公网回环 vs 127.0.0.1 也可能不同）
    imap_detected = detect_imap_settings(smtp_host)

    config = {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_security": security,
        "default_domain": domain_list[0],
        "domains": domains_cfg,
        # 发信成功后，把同一封邮件 IMAP APPEND 到「已发送」文件夹。
        # 默认开启；关闭把 enabled 改成 false 即可（向后兼容：未配置则不归档）。
        "sent_archive": {
            "enabled": True,
            **(imap_detected or {"folder": _DEFAULT_SENT_FOLDER}),
        },
    }

    _write_config(config, args.force)
    print(f"\n✓ 已生成配置: {CONFIG_PATH}")
    print(f"  共 {len(domain_list)} 个域名: {', '.join(domain_list)}")
    if imap_detected:
        print(f"  已发送归档: 开启（IMAP {imap_detected['security']} @ {imap_detected['host']}:{imap_detected['port']}，归档到「{imap_detected['folder']}」）")
        print(f"    （已自动探测到可用 IMAP 端口；若探针仍报错，可手动改 sent_archive.port/security）")
    else:
        print(f"  已发送归档: 开启，但未探测到可用 IMAP 端口（993/143 均连不上）。")
        print(f"    发信不受影响；归档需手动填 sent_archive.host/port/security。")




def patch_sent_archive(updates):
    """
    原子地只更新 config 的 sent_archive 字段，其余字段原样保留，权限维持 0600。

    用于 check 探针发现 sent_archive 配错（如端口被防火墙拦）但探测到可用端口时，
    自动把 host/port/security 改对，免去手动编辑 JSON。

    updates: dict，会合并进现有 sent_archive（同名键覆盖）。
    成功返回 True；失败（文件不存在/读写失败）返回 False。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    arch = config.get("sent_archive")
    if not isinstance(arch, dict):
        arch = {}
    arch.update(updates)
    config["sent_archive"] = arch

    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def merge_domain_credentials(updates):
    """原子地把新域名凭证合并进 config['domains']，保留其余字段与已有域名。权限 0600。

    用于 check 自动扫描时把用户确认添加的缺失域名并入现有 config，而不重写整份配置。
    updates: {domain: {"username": str, "password": str}}；同名域名会被覆盖（调用方应只传缺失项）。
    成功返回 True；文件不存在或读写失败返回 False。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    domains = config.get("domains")
    if not isinstance(domains, dict):
        domains = {}
    for dom, cred in updates.items():
        domains[dom] = cred
    config["domains"] = domains

    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _write_config(config, force):
    """原子写入 config.json，权限 0600。权限不足时回退到 stdout 输出。"""
    if os.path.exists(CONFIG_PATH) and not force:
        print(f"Error: 配置文件已存在: {CONFIG_PATH}", file=sys.stderr)
        print("  若要覆盖，请加 --force。", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"

    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        # 无权创建目录（通常非 root 写 /etc）
        print(f"Error: 无权创建目录 {CONFIG_DIR}（请用 sudo 运行）。", file=sys.stderr)
        print("  可手动写入，把以下内容 tee 到目标路径：", file=sys.stderr)
        print(f"    sudo tee {CONFIG_PATH} > /dev/null <<'MAIL_MANAGER_CONFIG'", file=sys.stderr)
        sys.stderr.write(payload)
        print("    MAIL_MANAGER_CONFIG", file=sys.stderr)
        print(f"    sudo chmod 600 {CONFIG_PATH}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # 非 POSIX 或无权改权限，忽略
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        print(f"Error: 写入配置失败: {e}", file=sys.stderr)
        print("  请用 sudo 运行；或手动写入以下内容到", CONFIG_PATH, "：", file=sys.stderr)
        sys.stderr.write(payload)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        sys.exit(EXIT_ERROR)


# ============================================================
# get 子命令（按文件名读取 EML 正文）
# ============================================================

def cmd_get(args):
    root = os.path.realpath(os.environ.get("MAILU_MAIL_ROOT", "/mailu/mail"))
    requested = os.path.expanduser(args.path.strip())
    if not os.path.isabs(requested):
        print("Error: get 必须传 EML 绝对路径，例如 /mailu/mail/admin@example.com/new/文件名", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    path = os.path.realpath(requested)
    try:
        inside_root = os.path.commonpath([root, path]) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        print(f"Error: 邮件路径必须位于 {root} 内", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if not os.path.isfile(path):
        print(f"Error: 找不到邮件文件: {requested}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    try:
        with open(path, "rb") as handle:
            msg = BytesParser(policy=email_policy).parse(handle)
        part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
        body = part.get_content() if part is not None else ""
        if isinstance(body, bytes):
            body = body.decode(part.get_content_charset() or "utf-8", errors="replace")
        body = str(body).strip()
        attachments = [str(item.get_filename() or "[未命名附件]") for item in msg.iter_attachments()]

        print(f"发件人: {msg.get('From', '')}")
        print(f"收件人: {msg.get('To', '')}")
        print(f"主题: {msg.get('Subject', '')}")
        print(f"日期: {msg.get('Date', '')}")
        print("--- 正文（前 1200 字符）---")
        print(body[:1200] if body else "[无正文]")
        if len(body) > 1200:
            print(f"[正文已截断，共 {len(body)} 字符]")
        print("--- 附件 ---")
        print("\n".join(f"- {name}" for name in attachments) if attachments else "[无附件]")
    except (OSError, ValueError) as exc:
        print(f"Error: 读取邮件失败: {exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


# ============================================================
# argparse 子命令分发（保持向后兼容：无子命令=发信）
# ============================================================

def _add_send_args(p):
    p.add_argument("--to", required=True, help="收件人，多个用逗号/分号/空格分隔")
    p.add_argument("--from-addr", dest="from_addr", required=True, help="发件人地址（据此回溯登录凭证）")
    p.add_argument("--subject", required=True, help="邮件主题")
    p.add_argument("--body", help="纯文本正文，与 --body-file 互斥")
    p.add_argument("--body-file", help="从文件读取纯文本正文，与 --body 互斥")
    p.add_argument("--html", help="HTML 正文，提供后以 multipart/alternative 发送")
    p.add_argument("--cc", help="抄送，逗号/分号/空格分隔多个")
    p.add_argument("--bcc", help="密送（只进信封不写头），逗号/分号/空格分隔多个")
    p.add_argument("--attach", action="append", help="附件路径，可重复传多次")


def build_parser():
    """构建 argparse。关键：子命令可选，无子命令时默认 send。"""
    parser = argparse.ArgumentParser(
        description="Mailu 工具（send 发信 / get 读信 / check 检查配置）",
    )
    sub = parser.add_subparsers(dest="command")

    # send 子命令
    p_send = sub.add_parser("send", help="发信（默认，可省略子命令）")
    _add_send_args(p_send)

    # get 子命令：只传文件名，递归查找并输出正文
    p_get = sub.add_parser("get", help="按绝对路径读取 EML 信息和正文预览")
    p_get.add_argument("path", help="邮件 EML 绝对路径，例如 /mailu/mail/admin@example.com/new/文件名")

    # check 子命令：自动扫描补齐账户 + 逐账号 SMTP/IMAP 实连测试；失败时弹菜单或全量配置
    p_check = sub.add_parser("check", help="检查配置；自动扫描补齐账户，SMTP 失败可逐个补密码，或全量配置")
    p_check.add_argument("--no-connect", action="store_true", help="跳过全部 SMTP/IMAP 实连测试，仅做格式检查")
    # 以下参数在交互配置时作为各项默认值（直接回车采用），不再有跳过交互的批量模式
    p_check.add_argument("--domains", help="所有域名，逗号/分号/空格分隔（交互配置时的默认值）")
    p_check.add_argument("--username-prefix", help="每域账号前缀（默认 admin，交互配置时的默认值）")
    p_check.add_argument("--smtp-host", help="SMTP 服务器地址（交互配置时的默认值）")
    p_check.add_argument("--smtp-port", help="SMTP 端口（交互配置时的默认值）")
    p_check.add_argument("--security", choices=["starttls", "ssl", "none"], help="安全模式（交互配置时的默认值）")
    p_check.add_argument("--force", action="store_true", help="覆盖已存在的配置")

    return parser


def main():
    # 关键：无子命令时，把整个 argv 当作发信参数解析（向后兼容）
    if len(sys.argv) >= 2 and sys.argv[1] in ("send", "get", "check", "-h", "--help"):
        parser = build_parser()
        args = parser.parse_args()
        command = args.command or "send"
    else:
        # 向后兼容：无子命令 → 直接当 send 解析
        parser = argparse.ArgumentParser(description="Secure Mail Sender via Local SMTP")
        _add_send_args(parser)
        args = parser.parse_args()
        command = "send"

    if command == "send":
        cmd_send(args)
    elif command == "get":
        cmd_get(args)
    elif command == "check":
        cmd_check(args)
    else:
        parser.print_help()
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
