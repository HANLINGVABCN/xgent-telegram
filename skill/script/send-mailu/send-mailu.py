#!/usr/bin/env python3
"""
Send Mailu 发信工具。

子命令：
  send    发信（默认，可省略子命令直接 send-mailu.py --to ... --body ...）
  check   检查配置是否可用（格式 + SMTP 实连测试），失败时提示是否开始配置
"""
import argparse
import base64
import getpass
import json
import mimetypes
import os
import sqlite3
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.utils import formatdate, make_msgid

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

def get_domain_candidates(from_addr, config):
    """
    根据发件人地址，生成所有可能的登录域名候选列表（从具体到宽泛）。
    """
    candidates = []
    default_dom = config.get("default_domain", "example.com")  # 彻底使用通用占位符作为代码默认值

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
    if int(smtp_port) == 465:
        return "ssl"
    return "starttls"


def smtp_connect(host, port, security, username, password):
    """
    按指定安全模式建立 SMTP 连接并登录。返回已登录的 server，失败抛异常。
    """
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=10)
    else:
        server = smtplib.SMTP(host, port, timeout=10)
        if security == "starttls":
            server.starttls()
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
    envelope_from = args.from_addr
    if args.from_addr.lower().endswith("@" + matched_domain) and args.from_addr.lower() != username.lower():
        envelope_from = username

    # 构造邮件（Message-ID 域名兜底用 matched_domain 或 default_dom）
    msg = build_message(args, plain_body, matched_domain or default_dom)

    # 发送邮件（finally 确保连接关闭，修复历史 socket 泄漏）
    try:
        server.sendmail(envelope_from, all_recipients, msg.as_string())
        print("SUCCESS: Email sent successfully!")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    finally:
        close_smtp(server)


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
        _emit_check_results(results, do_connect)
        return False
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

    # 3) 凭证非空
    cred_ok = True
    if isinstance(domains_cfg, dict) and domains_cfg:
        for dom, cred in domains_cfg.items():
            if not isinstance(cred, dict):
                results.append((False, f"domains.{dom} 结构不正确（应为对象）"))
                cred_ok = False
                continue
            if not cred.get("username") or not cred.get("password"):
                results.append((False, f"domains.{dom} 的 username/password 为空"))
                cred_ok = False
        if cred_ok:
            results.append((True, f"凭证完整（{len(domains_cfg)} 个域名）"))

    # 4) 安全模式解析（顺带验证取值合法）
    try:
        security = resolve_security(config, smtp_port)
        results.append((True, f"安全模式: {security}（端口 {smtp_port}）"))
    except Exception as e:
        results.append((False, f"解析安全模式失败: {e}"))
        security = None

    # 5) SMTP 实连测试（逐个域名凭证，每个都测，全部打印）
    if do_connect and field_ok and cred_ok and security:
        for dom, cred in domains_cfg.items():
            uname = cred.get("username")
            upass = cred.get("password")
            try:
                srv = smtp_connect(smtp_host, smtp_port, security, uname, upass)
                close_smtp(srv)
                results.append((True, f"SMTP 实连测试 [{dom}]: 登录成功 ({uname})"))
            except smtplib.SMTPAuthenticationError as e:
                results.append((False, f"SMTP 实连测试 [{dom}]: 认证失败 ({uname}) — {e}"))
            except Exception as e:
                results.append((False, f"SMTP 实连测试 [{dom}]: 连接失败 ({uname}) — {e}"))

    _emit_check_results(results, do_connect)
    return all(ok for ok, _ in results)


def cmd_check(args):
    """
    检查 config.json：格式 + 字段 +（可选）SMTP 实连测试。
    失败时提示用户是否开始交互配置。
    """
    do_connect = not args.no_connect
    all_ok = _run_check(do_connect)

    if all_ok:
        return

    # 检查失败：提示是否开始配置
    if sys.stdin.isatty():
        try:
            answer = input("\n检查未通过，是否开始配置？(y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(EXIT_ERROR)
        if answer in ('y', 'yes'):
            args.force = True  # 用户确认重新配置，允许覆盖
            cmd_init(args)
            # 配置完成后重新检查
            print("\n--- 重新检查配置 ---")
            recheck_ok = _run_check(do_connect)
            if not recheck_ok:
                sys.exit(EXIT_ERROR)
            return

    sys.exit(EXIT_ERROR)


def _emit_check_results(results, do_connect):
    """格式化打印 check 结果。"""
    print(f"检查配置: {CONFIG_PATH}")
    for ok, msg in results:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {msg}")
    all_ok = all(ok for ok, _ in results)
    if all_ok:
        print("✓ config 可用")
    else:
        print("✗ config 不可用")
        if not do_connect:
            print("  （已跳过 SMTP 实连测试，加 --connect 或省略 --no-connect 可深度验证）", file=sys.stderr)


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


def _ask(prompt, default=""):
    """交互式提问，支持默认值（直接回车采用）。非 TTY 时返回 default。"""
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default != "" else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_ERROR)
    return raw if raw else default


def _ask_password_for(label, default=""):
    """带账号标签的密码询问，不回显。非 TTY 时用 default。"""
    if not sys.stdin.isatty():
        return default
    try:
        pw = getpass.getpass(f"  {label} 的密码: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_ERROR)
    return pw if pw else default


def cmd_init(args):
    """交互式或批量生成 config.json（多域名）。"""
    defaults = detect_defaults()

    # ---------- 批量模式：--domains + --passwords 齐全则跳过交互 ----------
    batch_ready = bool(args.domains and args.passwords)
    if batch_ready:
        domain_list = split_addresses(args.domains)
        passwords = split_addresses(args.passwords)
        if len(passwords) == 1:
            passwords = passwords * len(domain_list)  # 单密码广播
        if len(passwords) != len(domain_list):
            print(f"Error: --domains 有 {len(domain_list)} 个域名，--passwords 有 {len(passwords)} 个密码，数量不匹配。", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        username_prefix = args.username_prefix or DEFAULT_POSTMASTER
        smtp_host = args.smtp_host or "127.0.0.1"
        smtp_port = args.smtp_port or 587
        security = args.security or "starttls"
        accounts = [f"{username_prefix}@{dom}" for dom in domain_list]
        if defaults["source"]:
            print(f"（检测到 Mailu 环境: {defaults['source']}，但使用了命令行批量参数）")
    else:
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

    config = {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_security": security,
        "default_domain": domain_list[0],
        "domains": domains_cfg,
    }

    _write_config(config, args.force)
    print(f"\n✓ 已生成配置: {CONFIG_PATH}")
    print(f"  共 {len(domain_list)} 个域名: {', '.join(domain_list)}")




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
        description="Send Mailu 发信工具（发信 / 检查配置）",
    )
    sub = parser.add_subparsers(dest="command")

    # send 子命令
    p_send = sub.add_parser("send", help="发信（默认，可省略子命令）")
    _add_send_args(p_send)

    # check 子命令（含配置生成功能，失败时可交互配置）
    p_check = sub.add_parser("check", help="检查配置是否可用，失败时可交互配置")
    p_check.add_argument("--no-connect", action="store_true", help="跳过 SMTP 实连测试，仅做格式检查")
    # 以下参数用于批量配置模式（合并原 init 功能）
    p_check.add_argument("--domains", help="所有域名，逗号/分号/空格分隔（批量配置模式）")
    p_check.add_argument("--username-prefix", help="每域账号前缀（默认 admin，批量配置模式可用）")
    p_check.add_argument("--passwords", help="各域密码，逗号/分号分隔；或单个密码广播到所有域（批量配置模式）")
    p_check.add_argument("--smtp-host", help="SMTP 服务器地址")
    p_check.add_argument("--smtp-port", help="SMTP 端口")
    p_check.add_argument("--security", choices=["starttls", "ssl", "none"], help="安全模式")
    p_check.add_argument("--force", action="store_true", help="覆盖已存在的配置")

    return parser


def main():
    # 关键：无子命令时，把整个 argv 当作发信参数解析（向后兼容）
    if len(sys.argv) >= 2 and sys.argv[1] in ("send", "check", "-h", "--help"):
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
    elif command == "check":
        cmd_check(args)
    else:
        parser.print_help()
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
