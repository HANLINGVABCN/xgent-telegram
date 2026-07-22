# Architecture

## Current refactor phase

`bot_server.py` is now a minimal executable entrypoint. Manifest parsing,
validation, and section loading live in `bot_app/bootstrap.py`. It loads
ordered domain sections from `bot_app/sections/` into one shared
application namespace.

This compatibility layer keeps the existing Telegram handlers and runtime
contracts unchanged while separating the original large source file into
reviewable units:

| Section | Responsibility |
| --- | --- |
| `core.py` | imports, configuration, logging, prompt and blacklist setup |
| `database.py` | SQLite memory and user data |
| `agent.py` | one-shot Agent execution and filesystem tools |
| `shell_triggers.py` | interactive shell and persistent triggers |
| `models.py` | provider clients and model calls |
| `services.py` | records, artifacts, memory files, utility services |
| `ui.py` | callback data, menus and authorization |
| `rendering.py` | Telegram HTML/Rich Message and streaming output |
| `commands.py` | command handlers |
| `callbacks.py` | inline-button callback routing |
| `messages.py` | text, document, photo, sticker and conversation handling |
| `command_handlers.py` | remaining command functions |
| `idle.py` | idle reminder scheduling |
| `other_messages.py` | fallback message handling |
| `lifecycle.py` | errors, startup and shutdown hooks |
| `main.py` | application construction and polling entrypoint |

## Bootstrap contract

`bot_app.bootstrap.read_section_manifest()` validates that the manifest:

- exists and is not empty;
- has no duplicate entries;
- contains only direct `.py` filenames;
- references files that exist in the sections directory.

`load_sections()` compiles every section using its real path before executing it
in the shared namespace. Tracebacks therefore point to the responsible section.
The loader returns the ordered filename tuple for diagnostics and tests.

`SOURCE_BASELINE.sha256` protects the compatibility split from accidental code
loss. The integrity test joins the section bodies in manifest order and compares
the resulting digest with the checked-in baseline. Runtime smoke tests redirect
the full trace log to a temporary directory so tests do not pollute the project
root.

## Why shared namespace first?

The original application has many cross-domain references and module-level
initialization hooks. Executing ordered sections in one namespace reduces
behavior changes during the first split. Normal Python imports should be
introduced subsystem by subsystem after tests cover protocol parsing, path
validation, rendering, triggers, provider adapters, and Telegram handlers.

## Intended next phases

1. Move configuration and pure utility functions into importable modules.
2. Introduce explicit service objects for database, model, and Agent state.
3. Move handlers into modules that receive dependencies instead of reading
   mutable globals.
4. Replace the ordered compatibility loader once no section relies on a shared
   global namespace.
