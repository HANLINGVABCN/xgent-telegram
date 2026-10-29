"""Independent state partitions. Archive data never supplies executable SQL."""
from __future__ import annotations

import base64
import io
import json
import math
import re
import sqlite3
import uuid
from contextlib import closing, nullcontext
from pathlib import Path

from .data_contents import PART_CATEGORIES

MAX_RECORD = 64 * 1024 * 1024
SAMPLE_LIMIT = 64 * 1024
TABLES = {
    "conversation_history": ("chat_sessions", "chat_messages", "global_messages", "ui_messages",
        "context_compressions", "context_compression_jobs", "conversation_groups", "artifact_objects",
        "conversation_snapshots", "snapshot_entries", "conversation_turns", "turn_operations",
        "conversation_branch_drafts", "conversation_mutation_requests"),
    "tasks": ("trigger_tasks", "trigger_runs"),
    "usage_statistics": ("token_usage_stats",),
    "security_logs": ("unauthorized_access_logs", "channel_deadletter"),
    "runtime_state": ("cli_relay_ops", "channel_outbox"),
}
AI_KEYS = {"active_provider", "default_model", "default_media_provider", "default_media_model",
           "model_request_limits", "discovered_model_limits", "model_fetch_status", "thinking_level",
           "ai_request_timeout", "smart_match_threshold", "global_depth", "stream_timeout"}
HISTORY_KEYS = {"current_chat_id", "telegram_conversation_id", "attachment_generation"}
INTERNAL_KEYS = {"conversation_schema_version", "message_policy_migration", "message_policy_import_pending"}
SECRET_KEY = re.compile(r"(?:^|_)(?:api_?key|key|secret|password|passwd|token|credential)(?:$|_)", re.I)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def config_part(key):
    if key in INTERNAL_KEYS:
        return None
    if key.startswith(("token_usage_", "usage_stats_")):
        return "usage_statistics"
    if SECRET_KEY.search(key):
        return "secrets"
    if key.endswith(("_prompt", "_prompt_addon")) or key in {"global_prompt_addon", "unauthorized_reply_messages"}:
        return "prompts"
    if key in AI_KEYS or key.startswith(("model_", "discovered_model_", "ai_")):
        return "ai_config"
    if key in {"conversation_running", "conversation_runs"} or key.startswith(("restart_", "last_idle_", "last_startup_", "runtime_")):
        return "runtime_state"
    if key in HISTORY_KEYS or key.startswith("conversation_"):
        return "conversation_history"
    return "app_settings"


def env_part(key):
    if SECRET_KEY.search(key):
        return "secrets"
    if key.startswith(("OPENAI_", "ANTHROPIC_", "GEMINI_", "MODEL_", "AI_")):
        return "ai_config"
    return "app_settings"


def env_values(text):
    # Full-file legacy maintenance must remain stdlib-only. The normal runtime
    # installs python-dotenv; load its parser only for independent env parts.
    from dotenv.parser import parse_stream

    values = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError(".env 包含无法解析的内容，请修正后再分类导出或导入")
        if binding.key is not None:
            if not ENV_KEY.fullmatch(binding.key):
                raise ValueError(".env 变量名称无效")
            values[binding.key] = binding.value
    return values


def replace_env(text, categories, incoming):
    """Preserve comments and all unselected assignments, including multiline ones."""
    from dotenv.parser import parse_stream

    env_values(text)
    kept = [binding.original.string for binding in parse_stream(io.StringIO(text))
            if binding.key is None or env_part(binding.key) not in categories]
    result = "".join(kept)
    if result and not result.endswith("\n"):
        result += "\n"
    for key, value in incoming.items():
        if not ENV_KEY.fullmatch(key) or env_part(key) not in categories:
            raise ValueError("分类环境变量与所选范围不一致")
        result += key + ("=" + json.dumps(value, ensure_ascii=False) if value is not None else "") + "\n"
    return result


def encode(value):
    if isinstance(value, bytes):
        return {"binary": base64.b64encode(value).decode("ascii")}
    return value


def decode(value):
    if isinstance(value, dict) and set(value) == {"binary"} and isinstance(value["binary"], str):
        return base64.b64decode(value["binary"], validate=True)
    if value is None or type(value) in (str, int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("数据库分类记录包含无效数值")
        return value
    raise ValueError("数据库分类记录类型无效")


def quote(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("数据库列名称无效")
    return '"' + value + '"'


def line(record):
    data = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_RECORD:
        raise ValueError("单条分类记录过大，请使用完整数据库备份")
    return data


class DataParts:
    def __init__(self, root, database=None):
        self.root = Path(root)
        self.database = Path(database) if database else self.root / "xgent_memory.db"
        self._env_snapshot = None

    def records(self, category, *, sample=False):
        if category not in PART_CATEGORIES:
            raise ValueError("未知的可独立导入部分")
        if self.database.is_symlink() or (self.root / ".env").is_symlink():
            raise ValueError("分类数据文件不能是符号链接")
        manager = closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) if self.database.is_file() else nullcontext(None)
        with manager as conn:
            if conn is not None:
                conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} if conn else set()
            columns = {name: [row[1] for row in conn.execute("PRAGMA table_info(" + quote(name) + ")")]
                       for name in TABLES.get(category, ()) if name in tables}
            version = conn.execute("SELECT value FROM config WHERE key='conversation_schema_version'").fetchone() if "config" in tables else None
            env_path = self.root / ".env"
            env_exists = self._env_snapshot[0] if self._env_snapshot is not None else env_path.is_file()
            header = {"format": "xgent-part", "version": 1, "category": category, "tables": columns,
                      "config": "config" in tables, "env": env_exists and category in ("secrets", "ai_config", "app_settings"),
                      "providers": "providers" in tables and category in ("secrets", "ai_config"),
                      "schema_version": int(json.loads(version[0])) if version else 0}
            yield header
            if header["env"]:
                env_text = self._env_snapshot[1] if self._env_snapshot is not None else env_path.read_text(encoding="utf-8-sig")
                for key, value in env_values(env_text).items():
                    if env_part(key) == category:
                        yield {"type": "env", "key": key, "value": value}
            if "config" in tables:
                for key, value in conn.execute("SELECT key,value FROM config ORDER BY key"):
                    if config_part(key) == category:
                        yield {"type": "config", "key": key, "value": value}
            if header["providers"]:
                available = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
                wanted = [name for name in ("name", "api_key") if name in available] if category == "secrets" else [
                    name for name in ("name", "base_url", "models", "api_format") if name in available]
                if "name" not in wanted:
                    raise ValueError("提供商表结构无效")
                for row in conn.execute("SELECT " + ",".join(quote(name) for name in wanted) + " FROM providers ORDER BY name"):
                    yield {"type": "provider", "values": dict(zip(wanted, row))}
            for table, names in columns.items():
                query = "SELECT " + ",".join(quote(name) for name in names) + " FROM " + quote(table)
                if sample:
                    query += " LIMIT 4"
                for row in conn.execute(query):
                    yield {"type": "row", "table": table, "values": [encode(value) for value in row]}

    def capture(self, directory, categories):
        categories = [key for key in categories if key in PART_CATEGORIES]
        if not categories:
            return []
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        entries = []
        env_path = self.root / ".env"
        if env_path.is_symlink():
            raise ValueError("分类环境文件不能是符号链接")
        # Use one environment snapshot across credential/AI/application parts.
        previous_snapshot = self._env_snapshot
        self._env_snapshot = (env_path.is_file(), env_path.read_text(encoding="utf-8-sig") if env_path.is_file() else "")
        try:
            for category in categories:
                target = directory / (category + ".jsonl")
                with target.open("xb") as output:
                    # Generated parts contain credentials. Protect before first write,
                    # including on Windows where chmod is not an ACL restriction.
                    from .data_bundles import protect
                    protect(target)
                    for record in self.records(category):
                        output.write(line(record))
                entries.append(("backup_parts/" + category + ".jsonl", target))
        finally:
            self._env_snapshot = previous_snapshot
        return entries

    def measure(self, category):
        count, size, samples, header = 0, 0, bytearray(), None
        for record in self.records(category, sample=True):
            packed = line(record)
            if header is None:
                header = record
            else:
                count += 1
            size += len(packed)
            if len(samples) < SAMPLE_LIMIT:
                samples.extend(packed[:SAMPLE_LIMIT - len(samples)])
        if self.database.is_file() and header["tables"]:
            with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                for table in header["tables"]:
                    rows = conn.execute("SELECT count(*) FROM " + quote(table)).fetchone()[0]
                    count += max(0, rows - 4)
                    try:
                        pages = conn.execute("SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name=?", (table,)).fetchone()[0]
                    except sqlite3.OperationalError:
                        pages = rows * 512
                    size += max(0, pages - min(rows, 4) * 128)
        return {"size": size, "item_count": count, "sample": bytes(samples), "virtual": True,
                "file_count": 1, "size_is_estimate": True,
                "files": [{"name": "backup_parts/" + category + ".jsonl", "size": size, "virtual": True}]}


def read_part(filename, category):
    with Path(filename).open("rb") as source:
        header = None
        for raw in iter(lambda: source.readline(MAX_RECORD + 1), b""):
            if len(raw) > MAX_RECORD:
                raise ValueError("分类记录过大")
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("分类记录格式无效")
            if header is None:
                header = record
                if (record.get("format") != "xgent-part" or record.get("version") != 1
                        or record.get("category") != category or not isinstance(record.get("tables"), dict)):
                    raise ValueError("分类备份头信息无效")
                from .conversations import SCHEMA_VERSION
                if type(record.get("schema_version")) is not int or not 0 <= record["schema_version"] <= SCHEMA_VERSION:
                    raise ValueError("分类数据库版本较新，请先升级 XGent")
                for name, columns in record["tables"].items():
                    if name not in TABLES.get(category, ()) or not isinstance(columns, list) or not columns:
                        raise ValueError("分类备份包含范围外数据表")
                    if len(columns) > 128 or len(columns) != len(set(columns)):
                        raise ValueError("分类备份列信息无效")
                    for column in columns:
                        quote(column)
                for flag in ("env", "config", "providers"):
                    if type(record.get(flag)) is not bool:
                        raise ValueError("分类备份标记无效")
                if record["env"] and category not in {"secrets", "ai_config", "app_settings"}:
                    raise ValueError("分类环境配置范围无效")
                if record["providers"] and category not in {"secrets", "ai_config"}:
                    raise ValueError("分类提供商范围无效")
            else:
                kind = record.get("type")
                if kind in {"config", "env"}:
                    key, value = record.get("key"), record.get("value")
                    if not isinstance(key, str) or len(key) > 256:
                        raise ValueError("配置项名称无效")
                    if kind == "env":
                        if not header["env"] or not ENV_KEY.fullmatch(key) or env_part(key) != category or (value is not None and not isinstance(value, str)):
                            raise ValueError("环境配置超出所选分类")
                    elif not header["config"] or config_part(key) != category or not isinstance(value, str):
                        raise ValueError("数据库配置超出所选分类")
                elif kind == "provider":
                    values = record.get("values")
                    allowed = {"name", "api_key"} if category == "secrets" else {"name", "base_url", "models", "api_format"}
                    if (not header["providers"] or not isinstance(values, dict) or not isinstance(values.get("name"), str) or not values["name"]
                            or len(values["name"]) > 1024
                            or set(values) - allowed or any(not isinstance(value, str) for value in values.values())):
                        raise ValueError("提供商记录超出所选分类")
                    if category == "secrets" and "api_key" not in values:
                        raise ValueError("提供商密钥记录不完整")
                    if category == "ai_config" and "base_url" not in values:
                        raise ValueError("提供商配置记录不完整")
                elif kind == "row":
                    table, values = record.get("table"), record.get("values")
                    if table not in header["tables"] or not isinstance(values, list) or len(values) != len(header["tables"][table]):
                        raise ValueError("数据表记录与分类清单不一致")
                    for value in values:
                        decode(value)
                else:
                    raise ValueError("分类备份包含未知记录")
            yield record
        if header is None:
            raise ValueError("分类备份为空")


def part_summary(filename, category):
    count, names, header = 0, [], None
    for record in read_part(filename, category):
        if header is None:
            header = record
        else:
            count += 1
            if len(names) < 40 and record.get("type") in {"env", "config", "provider"}:
                names.append(record.get("key") or record["values"]["name"])
    return {"item_count": count, "entry_names": names, "db_tables": list(header["tables"]),
            "has_database_data": bool(header["config"] or header["providers"] or header["tables"]),
            "has_environment_data": header["env"]}


def apply_database(conn, files):
    """Apply only selected record families inside the caller's snapshot transaction."""
    headers = {category: next(read_part(filename, category)) for category, filename in files.items()}
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for category, header in headers.items():
        for table, columns in header["tables"].items():
            if table not in existing:
                raise ValueError("当前实例缺少数据表 " + table + "，请先升级或初始化实例")
            actual = {row[1] for row in conn.execute("PRAGMA table_info(" + quote(table) + ")")}
            if set(columns) - actual:
                raise ValueError("当前数据库列较旧，请先升级 XGent")
            conn.execute("DELETE FROM " + quote(table))
        if header["config"]:
            if "config" not in existing:
                raise ValueError("当前实例尚未初始化配置数据库")
            for (key,) in list(conn.execute("SELECT key FROM config")):
                if config_part(key) == category:
                    conn.execute("DELETE FROM config WHERE key=?", (key,))
        if header["providers"] and "providers" not in existing:
            raise ValueError("当前实例尚未初始化提供商数据库")
        if header["providers"] and category == "secrets":
            conn.execute("UPDATE providers SET api_key='' ")
        if header["providers"] and category == "ai_config":
            names = {record["values"]["name"] for record in read_part(files[category], category)
                     if record.get("type") == "provider"}
            for name, api_key in list(conn.execute("SELECT name,api_key FROM providers")):
                if name not in names:
                    if api_key and "secrets" not in files:
                        # Credentials belong to an unselected category. Keep only
                        # their identity, but remove obsolete AI connection data.
                        conn.execute("UPDATE providers SET base_url='',models='[]',api_format='openai' WHERE name=?", (name,))
                    else:
                        conn.execute("DELETE FROM providers WHERE name=?", (name,))
    # Configuration precedes credentials so matching providers exist when both are selected.
    order = sorted(files, key=lambda category: category == "secrets")
    for category in order:
        for record in read_part(files[category], category):
            kind = record.get("type")
            if kind == "config":
                conn.execute("DELETE FROM config WHERE key=?", (record["key"],))
                conn.execute("INSERT INTO config(key,value) VALUES(?,?)", (record["key"], record["value"]))
            elif kind == "provider":
                values = record["values"]
                name = values["name"]
                current = conn.execute("SELECT api_key FROM providers WHERE name=?", (name,)).fetchone()
                if category == "secrets":
                    if current is None:
                        raise ValueError("密钥对应的提供商不存在：" + name + "；请同时勾选 AI 与模型配置")
                    conn.execute("UPDATE providers SET api_key=? WHERE name=?", (values.get("api_key", ""), name))
                else:
                    api_key = current[0] if current else ""
                    conn.execute("INSERT OR REPLACE INTO providers(name,base_url,api_key,models,api_format) VALUES(?,?,?,?,?)",
                        (name, values.get("base_url", ""), api_key, values.get("models", "[]"), values.get("api_format", "openai")))
            elif kind == "row":
                table = record["table"]
                columns = headers[category]["tables"][table]
                sql = "INSERT INTO " + quote(table) + "(" + ",".join(quote(column) for column in columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
                conn.execute(sql, [decode(value) for value in record["values"]])
    if "config" in existing and any(table in {"global_messages", "ui_messages", "cli_relay_ops", "channel_outbox", "channel_deadletter"}
                                    for header in headers.values() for table in header["tables"]):
        # Imported partitions may predate the message contract while the target
        # schema is already current. Migrate once at the next startup, before any
        # history reader or delivery worker starts.
        conn.execute("DELETE FROM config WHERE key='message_policy_import_pending'")
        conn.execute("INSERT INTO config(key,value) VALUES(?,?)",
                     ('message_policy_import_pending', json.dumps(uuid.uuid4().hex)))
