# Architecture

## Current refactor phase

`bot_server.py` is now a small entrypoint. It loads ordered domain sections from
`telegram_ai_bot/sections/` into one shared application namespace. This keeps the
existing Telegram handler and runtime contracts unchanged while separating the
large source file into reviewable units:

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

## Why shared namespace first?

The original application has a large number of cross-domain references and
module-level initialization hooks. Executing ordered sections in one namespace
reduces behavior changes during the first split. The next phase can move each
section to normal imports after tests cover the protocol parser, path validation,
rendering, triggers and provider adapters.
