"""Real database partition exports and selective restore, not UI-only choices."""
from contextlib import closing
import json
import sqlite3

import pytest

from xgent_app.data_bundles import BundleError, DataBundles
from xgent_app.data_contents import BACKUP_GROUPS, CATEGORIES, RECOMMENDED_CATEGORIES
from xgent_app.data_parts import DataParts, env_values, read_part
from xgent_app.data_paths import DataPaths


def database(root, marker):
    root.mkdir()
    with closing(sqlite3.connect(root / "xgent_memory.db")) as conn:
        conn.executescript('''
            CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE providers(name TEXT PRIMARY KEY,base_url TEXT NOT NULL,api_key TEXT NOT NULL,models TEXT,api_format TEXT);
            CREATE TABLE chat_sessions(id TEXT PRIMARY KEY);
            CREATE TABLE global_messages(id INTEGER PRIMARY KEY,content TEXT);
            CREATE TABLE chat_messages(id INTEGER PRIMARY KEY,content TEXT);
            CREATE TABLE trigger_tasks(id TEXT PRIMARY KEY,command TEXT);
            CREATE TABLE trigger_runs(run_id TEXT PRIMARY KEY,output TEXT);
            CREATE TABLE token_usage_stats(id INTEGER PRIMARY KEY,total_tokens INTEGER);
            CREATE TABLE unauthorized_access_logs(id INTEGER PRIMARY KEY,content TEXT);
            CREATE TABLE channel_deadletter(id INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE channel_outbox(id INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE cli_relay_ops(id INTEGER PRIMARY KEY,payload TEXT);
        ''')
        for key, value in {
            "conversation_schema_version": 6, "default_model": marker + "-model", "active_provider": "shared",
            "thinking_level": marker + "-thinking", "web_password_hash": marker + "-hash",
            "web_port": 7000 if marker == "source" else 8000, "agent_mode": marker,
            "assistant_prompt": marker + "-prompt", "current_chat_id": marker + "-chat",
            "token_usage_last_reset": marker + "-usage", "conversation_running": marker + "-running",
        }.items():
            conn.execute("INSERT INTO config VALUES(?,?)", (key, json.dumps(value)))
        conn.execute("INSERT INTO providers VALUES(?,?,?,?,?)",
                     ("shared", marker + "-url", marker + "-api-key", json.dumps([marker + "-model"]), "openai"))
        if marker == "target":
            conn.execute("INSERT INTO providers VALUES('old','old-url','keep-old-key','[\"old-model\"]','openai')")
        for table, row in {
            "chat_sessions": (marker + "-chat",), "global_messages": (1, marker + "-message"),
            "chat_messages": (1, marker + "-session-message"), "trigger_tasks": ("task", marker + "-task"),
            "trigger_runs": ("run", marker + "-run"), "token_usage_stats": (1, 10 if marker == "source" else 20),
            "unauthorized_access_logs": (1, marker + "-access"), "channel_deadletter": (1, marker + "-deadletter"),
            "channel_outbox": (1, marker + "-outbox"), "cli_relay_ops": (1, marker + "-relay"),
        }.items():
            conn.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", row)
        conn.commit()
    (root / ".env").write_text(
        '# keep comments\nBOT_TOKEN="' + marker + '-bot"\nTAVILY_API_KEY="' + marker + '-search"\n'
        'WEB_PORT=' + ('7000' if marker == 'source' else '8000') + '\nOPENAI_MODEL="' + marker + '-env-model"\n'
        'NOTE="two\nlines"\n', encoding="utf-8")
    for folder in ("memory", "prompts", "workspace"):
        (root / folder).mkdir()
        (root / folder / "note.txt").write_text(marker + "-" + folder, encoding="utf-8")
    return DataBundles(DataPaths(root, root))


@pytest.fixture
def instances(tmp_path):
    return database(tmp_path / "source", "source"), database(tmp_path / "target", "target")


def state(service):
    with closing(sqlite3.connect(service.paths.database)) as conn:
        result = {"config": dict(conn.execute("SELECT key,value FROM config")),
                  "providers": dict((row[0], row[1:]) for row in conn.execute("SELECT * FROM providers"))}
        for table in ("chat_sessions", "global_messages", "chat_messages", "trigger_tasks", "trigger_runs",
                      "token_usage_stats", "unauthorized_access_logs", "channel_deadletter", "channel_outbox", "cli_relay_ops"):
            result[table] = list(conn.execute("SELECT * FROM " + table))
    result["env"] = env_values(service.paths.env.read_text(encoding="utf-8"))
    return result


def restore(source, target, exported, categories):
    staging, manifest = target.prepare_import(exported["path"])
    preview = target.import_preview(staging, manifest)
    assert set(categories) <= {row["id"] for row in preview["categories"]}
    before = state(target)
    result = target.activate(staging, categories=categories)
    return before, state(target), result, preview


@pytest.mark.parametrize("format", ["zip", "tar.gz", "tar.xz"])
def test_secret_only_restore_keeps_ai_settings_history_and_environment(instances, format):
    source, target = instances
    exported = source.export(categories=["secrets", "ai_config", "app_settings", "conversation_history"], archive_format=format)
    before, after, result, preview = restore(source, target, exported, ["secrets"])
    assert after["env"]["BOT_TOKEN"] == "source-bot"
    assert after["env"]["TAVILY_API_KEY"] == "source-search"
    assert after["env"]["WEB_PORT"] == "8000"
    assert after["env"]["OPENAI_MODEL"] == "target-env-model"
    assert after["providers"]["shared"][1] == "source-api-key"
    assert after["providers"]["shared"][0] == "target-url"
    assert after["config"]["web_password_hash"] == '"source-hash"'
    assert after["config"]["default_model"] == before["config"]["default_model"]
    for table in ("global_messages", "trigger_tasks", "token_usage_stats", "channel_outbox"):
        assert after[table] == before[table]
    assert "source-api-key" not in json.dumps(preview)
    assert "source-bot" not in json.dumps(preview)
    target.rollback_activation(result)
    assert state(target) == before


def test_ai_only_restore_preserves_all_credentials_and_other_settings(instances):
    source, target = instances
    exported = source.export(categories=["secrets", "ai_config", "app_settings"])
    before, after, _, _ = restore(source, target, exported, ["ai_config"])
    assert after["providers"]["shared"][0] == "source-url"
    assert after["providers"]["shared"][1] == "target-api-key"
    assert after["providers"]["old"] == ("", "keep-old-key", "[]", "openai")
    assert after["env"]["OPENAI_MODEL"] == "source-env-model"
    assert after["env"]["BOT_TOKEN"] == before["env"]["BOT_TOKEN"]
    assert after["config"]["default_model"] == '"source-model"'
    assert after["config"]["web_password_hash"] == before["config"]["web_password_hash"]
    assert after["config"]["web_port"] == before["config"]["web_port"]
    assert after["global_messages"] == before["global_messages"]


@pytest.mark.parametrize("category,tables", [
    ("conversation_history", ["chat_sessions", "global_messages", "chat_messages"]),
    ("tasks", ["trigger_tasks", "trigger_runs"]),
    ("usage_statistics", ["token_usage_stats"]),
    ("security_logs", ["unauthorized_access_logs", "channel_deadletter"]),
    ("runtime_state", ["channel_outbox", "cli_relay_ops"]),
])
def test_database_parts_do_not_overwrite_unselected_tables(instances, category, tables):
    source, target = instances
    exported = source.export(categories=[category])
    before, after, result, _ = restore(source, target, exported, [category])
    incoming = state(source)
    for key in before:
        if key in {"config", "providers", "env"}:
            continue
        assert after[key] == (incoming[key] if key in tables else before[key])
    assert after["providers"] == before["providers"]
    assert after["env"] == before["env"]
    assert after["config"]["default_model"] == before["config"]["default_model"]
    assert after["config"]["conversation_running"] == (incoming["config"]["conversation_running"]
                                                       if category == "runtime_state" else before["config"]["conversation_running"])
    target.rollback_activation(result)
    assert state(target) == before


def test_prompts_and_memory_are_separately_selectable(instances):
    source, target = instances
    exported = source.export(categories=["prompts", "memory"])
    staging, _ = target.prepare_import(exported["path"])
    target.activate(staging, categories=["memory"])
    assert (target.paths.memory / "note.txt").read_text() == "source-memory"
    assert (target.paths.root / "prompts/note.txt").read_text() == "target-prompts"
    assert state(target)["config"]["assistant_prompt"] == '"target-prompt"'


def test_old_full_archive_exposes_independent_import_parts(instances):
    source, target = instances
    exported = source.export(outputs=True)
    before, after, _, preview = restore(source, target, exported, ["ai_config"])
    assert {"secrets", "ai_config", "conversation_history", "database", "env"} <= {row["id"] for row in preview["categories"]}
    assert preview["selectable"]
    assert after["providers"]["shared"][0] == "source-url"
    assert after["providers"]["shared"][1] == "target-api-key"
    assert after["global_messages"] == before["global_messages"]
    assert (target.paths.root / "prompts/note.txt").read_text() == "target-prompts"


def test_unavailable_and_conflicting_import_selection_is_rejected(instances):
    source, target = instances
    exported = source.export(categories=["memory"])
    staging, _ = target.prepare_import(exported["path"])
    for categories in ([], ["secrets"], ["database", "ai_config"]):
        with pytest.raises(BundleError):
            target.select_import(staging, categories)
    target.discard_staging(staging)


def test_export_groups_include_empty_content_without_overlapping_choices(instances):
    source, _ = instances
    estimate = source.estimate(categories=list(RECOMMENDED_CATEGORIES), compare_formats=True)
    assert len(estimate["categories"]) == len(CATEGORIES)
    assert {row["id"] for row in estimate["categories"]} == {spec["id"] for spec in CATEGORIES}
    assert {"secrets", "ai_config", "conversation_history", "prompts", "tasks"} <= set(estimate["recommended_categories"])
    assert all(item["sample_bytes"] <= 1024 * 1024 for item in estimate["formats"])
    assert "source-api-key" not in json.dumps(estimate)
    groups = estimate['groups']
    assert len(groups) == 7
    members = [key for group in groups for key in group['categories']]
    assert len(members) == len(set(members))
    assert not {'env', 'database', 'server_assets'} & set(members)
    assert sum(group['size'] for group in groups if group['selected']) == estimate['size']
    assert not next(group for group in groups if group['id'] == 'runtime_state')['selected']


def test_merged_configuration_restore_restores_all_config_and_keeps_conversations(instances):
    source, target = instances
    selected = [key for group in BACKUP_GROUPS if group['id'] in {'configuration', 'conversations'}
                for key in group['categories']]
    exported = source.export(categories=selected)
    staging, manifest = target.prepare_import(exported['path'])
    preview = target.import_preview(staging, manifest)
    assert {group['id'] for group in preview['groups']} == {'configuration', 'conversations'}
    group = next(group for group in preview['groups'] if group['id'] == 'configuration')
    before = state(target)
    result = target.activate(staging, categories=group['categories'])
    after, incoming = state(target), state(source)
    assert after['env'] == incoming['env']
    assert after['providers'] == incoming['providers']
    assert after['config']['default_model'] == incoming['config']['default_model']
    assert after['config']['web_password_hash'] == incoming['config']['web_password_hash']
    for table in ('global_messages', 'chat_sessions', 'trigger_tasks', 'channel_outbox'):
        assert after[table] == before[table]
    target.rollback_activation(result)
    assert state(target) == before


def test_partial_and_old_full_archives_use_the_same_nonoverlapping_import_groups(instances):
    source, target = instances
    output = source.paths.storage / 'command_outputs/result.txt'
    output.parent.mkdir(parents=True)
    output.write_text('completed command', encoding='utf-8')
    exported = source.export(outputs=True)
    staging, manifest = target.prepare_import(exported['path'])
    preview = target.import_preview(staging, manifest)
    assert len(preview['groups']) == len(BACKUP_GROUPS)
    members = [key for group in preview['groups'] for key in group['categories']]
    assert len(members) == len(set(members))
    assert not {'env', 'database', 'server_assets'} & set(members)
    assert {'secrets','ai_config','app_settings','conversation_history','runtime_state'} <= set(members)
    target.select_import(staging, members)
    target.discard_staging(staging)


def test_new_full_independent_backup_round_trips_all_parts(instances):
    source, target = instances
    selected = list(RECOMMENDED_CATEGORIES)
    exported = source.export(categories=selected, archive_format="tar.xz")
    before, after, result, _ = restore(source, target, exported, selected)
    incoming = state(source)
    assert after["providers"] == incoming["providers"]
    assert after["global_messages"] == incoming["global_messages"]
    assert after["config"]["default_model"] == incoming["config"]["default_model"]
    assert after["config"]["assistant_prompt"] == incoming["config"]["assistant_prompt"]
    assert after["config"]["web_password_hash"] == incoming["config"]["web_password_hash"]
    assert after["channel_outbox"] == before["channel_outbox"]
    target.rollback_activation(result)
    assert state(target) == before


def test_missing_provider_secret_import_fails_before_active_files_change(instances):
    source, target = instances
    with closing(sqlite3.connect(source.paths.database)) as conn:
        conn.execute("INSERT INTO providers VALUES('new','new-url','new-key','[]','openai')")
        conn.commit()
    exported = source.export(categories=["secrets"])
    staging, _ = target.prepare_import(exported["path"])
    before = state(target)
    with pytest.raises(BundleError, match="AI"):
        target.activate(staging, categories=["secrets"])
    assert state(target) == before
    target.discard_staging(staging)


def test_malicious_part_cannot_import_ai_values_as_secrets(instances, tmp_path):
    source, _ = instances
    entries = DataParts(source.paths.root).capture(tmp_path / "parts", ["secrets"])
    filename = entries[0][1]
    with filename.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"type": "config", "key": "default_model", "value": '"bad"'}) + "\n")
    with pytest.raises(ValueError, match="分类"):
        list(read_part(filename, "secrets"))


def test_env_secret_import_without_database_keeps_other_variables(tmp_path):
    source_root = tmp_path / "source";source_root.mkdir()
    target_root = tmp_path / "target";target_root.mkdir()
    (source_root / ".env").write_text("BOT_TOKEN=source\nWEB_PORT=7000\n", encoding="utf-8")
    (target_root / ".env").write_text("# target comment\nBOT_TOKEN=target\nWEB_PORT=8000\n", encoding="utf-8")
    source = DataBundles(DataPaths(source_root, source_root));target = DataBundles(DataPaths(target_root, target_root))
    exported = source.export(categories=["secrets"])
    staging, _ = target.prepare_import(exported["path"])
    target.activate(staging, categories=["secrets"])
    assert env_values(target.paths.env.read_text()) == {"BOT_TOKEN": "source", "WEB_PORT": "8000"}
    assert "# target comment" in target.paths.env.read_text()
    assert not target.paths.database.exists()


def test_pre_import_full_backup_contains_recoverable_prompt_files(instances):
    source, target = instances
    exported = source.export(outputs=True, include_prompts=True, name_prefix="pre-import")
    staging, manifest = target.prepare_import(exported["path"])
    assert "prompts/note.txt" in manifest["files"]
    assert manifest["include_prompts"] is True
    target.activate(staging)
    assert (target.paths.root / "prompts/note.txt").read_text() == "source-prompts"
