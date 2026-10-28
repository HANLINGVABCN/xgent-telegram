"""Selectable state categories shared by export, scheduling and restore previews."""
from __future__ import annotations

CATEGORIES = (
    {"id": "secrets", "label": "密钥与登录凭据", "paths": (), "part": True, "sensitive": True,
     "description": "Bot / 搜索 / Provider API 密钥、Web 密码；不包含模型配置或对话。"},
    {"id": "ai_config", "label": "AI 与模型配置", "paths": (), "part": True,
     "description": "提供商地址、模型列表、默认模型及推理参数；不包含 API 密钥。"},
    {"id": "app_settings", "label": "应用与环境配置", "paths": (), "part": True,
     "description": "非密钥环境变量、Web、Agent、外观等设置；不包含对话数据。"},
    {"id": "conversation_history", "label": "对话历史与上下文记忆", "paths": (), "part": True,
     "description": "对话、分组、消息、上下文摘要及分支记录；不覆盖密钥和 AI 配置。"},
    {"id": "memory", "label": "记忆文件", "paths": ("memory",)},
    {"id": "prompts", "label": "提示词与命令黑名单", "paths": ("prompts",), "part": True},
    {"id": "tasks", "label": "定时任务与执行记录", "paths": (), "part": True},
    {"id": "usage_statistics", "label": "Token 用量统计", "paths": (), "part": True},
    {"id": "security_logs", "label": "访问记录与通道死信", "paths": (), "part": True, "sensitive": True},
    {"id": "private_skills", "label": "私有技能", "paths": ("skill-private",)},
    {"id": "workspace", "label": "工作区文件", "paths": ("workspace",)},
    {"id": "conversation_state", "label": "对话对象与独立工作区", "paths": ("xgent_storage/conversation_objects", "xgent_storage/conversation_workspaces")},
    {"id": "uploads", "label": "上传附件", "paths": ("xgent_storage/uploads",)},
    {"id": "generated_media", "label": "生成的媒体", "paths": ("xgent_storage/generated_media",)},
    {"id": "exports", "label": "历史导出文件", "paths": ("xgent_storage/exports",)},
    {"id": "attachment_versions", "label": "附件历史版本", "paths": ("xgent_storage/attachment_versions",)},
    {"id": "server_assets", "label": "服务器资产配置", "paths": ("server_assets",), "sensitive": True},
    {"id": "command_outputs", "label": "命令完整输出", "paths": ("xgent_storage/command_outputs",)},
    {"id": "settings", "label": "备份规则与路径映射", "paths": ("backup-schedule.json", "path-map.json")},
    {"id": "runtime_state", "label": "待发送队列与运行状态", "paths": (), "part": True,
     "advanced": True, "sensitive": True, "description": "高级恢复：旧队列可能再次发送消息，默认不选。"},
    {"id": "env", "label": "完整环境文件 .env（高级）", "paths": (".env",), "sensitive": True,
     "advanced": True, "conflicts": ("secrets", "ai_config", "app_settings"),
     "description": "整份替换 .env；与独立密钥、环境配置选项互斥。"},
    {"id": "database", "label": "完整数据库（高级）", "paths": ("xgent_memory.db",), "sensitive": True,
     "advanced": True, "conflicts": ("secrets", "ai_config", "app_settings", "conversation_history", "prompts", "tasks", "usage_statistics", "security_logs", "runtime_state"),
     "description": "整库替换，包含密钥、AI 配置、对话、任务和统计；与独立数据库分类互斥。"},
)
CATEGORY_MAP = {item["id"]: item for item in CATEGORIES}
# Keep old CLI/full-archive semantics stable. Web defaults use independent parts.
DEFAULT_CATEGORIES = ("env", "database", "memory", "private_skills", "workspace",
                      "conversation_state", "uploads", "generated_media", "exports",
                      "attachment_versions", "server_assets", "settings")
RECOMMENDED_CATEGORIES = tuple(item["id"] for item in CATEGORIES
                               if not item.get("advanced") and item["id"] != "command_outputs")
PART_CATEGORIES = tuple(item["id"] for item in CATEGORIES if item.get("part"))
PREVIEW_FILES = 200


def normalize_categories(value=None, outputs=False):
    if value is None:
        return list(DEFAULT_CATEGORIES) + (["command_outputs"] if outputs else [])
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("请至少选择一种需要压缩的数据类型")
    if any(not isinstance(item, str) or item not in CATEGORY_MAP for item in value):
        raise ValueError("包含未知的数据类型")
    if len(value) != len(set(value)):
        raise ValueError("数据类型不能重复")
    return [item["id"] for item in CATEGORIES if item["id"] in value]


def category_for(name):
    if name.startswith("backup_parts/") and name.endswith(".jsonl"):
        category = name[len("backup_parts/"):-len(".jsonl")]
        if category in PART_CATEGORIES:
            return category
    for item in CATEGORIES:
        if any(name == prefix or name.startswith(prefix + "/") for prefix in item["paths"]):
            return item["id"]
    raise ValueError("不支持的数据路径：" + name)


def inventory(entries, selected):
    rows = {item["id"]: {**item, "selected": item["id"] in selected,
            "size": 0, "file_count": 0, "files": []} for item in CATEGORIES}
    for name, source in entries:
        row = rows[category_for(name)]
        size = source.stat().st_size
        row["size"] += size
        row["file_count"] += 1
        if len(row["files"]) < PREVIEW_FILES:
            row["files"].append({"name": name, "size": size})
    for row in rows.values():
        row["files_truncated"] = row["file_count"] > len(row["files"])
    return list(rows.values())


def manifest_categories(manifest):
    if manifest.get("version") == 2:
        return normalize_categories(manifest.get("categories"))
    selected = normalize_categories(outputs=manifest.get("include_command_outputs", False))
    if manifest.get("include_prompts") is True:
        selected.append("prompts")
    return selected


def replacement_paths(categories):
    names = [name for category in categories for name in CATEGORY_MAP[category]["paths"]]
    if "database" in categories:
        names += ["bot_memory.db"]
        names += [name + suffix for name in ("xgent_memory.db", "bot_memory.db")
                  for suffix in ("-wal", "-shm", "-journal")]
    return names


def validate_independent_selection(categories):
    selected = normalize_categories(categories)
    for key in selected:
        conflicts = set(CATEGORY_MAP[key].get("conflicts", ())) & set(selected)
        if conflicts:
            raise ValueError(CATEGORY_MAP[key]["label"] + "不能与独立分类同时选择，请选择整份恢复或独立分类。")
    return selected
