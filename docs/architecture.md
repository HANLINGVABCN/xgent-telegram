# Architecture

## Current refactor phase

`xgent_server.py` is now a minimal executable entrypoint. Manifest parsing,
validation, and section loading live in `xgent_app/bootstrap.py`. It loads
ordered domain sections from `xgent_app/sections/` into one shared
application namespace.

The compatibility loader is now surrounded by importable, dependency-light
components. The first extracted components are:

| Module | Responsibility |
| --- | --- |
| `xgent_app/protocols.py` | Parse and strip Agent protocol blocks only; no command execution or messaging |
| `xgent_app/text_utils.py` | Generic text clipping and normalization helpers |
| `xgent_app/shell_output.py` | Format shell display output, model context, and command-result notices |
| `xgent_app/agent_context.py` | Build next-turn Agent context for file, read, edit, grep, run, shell, trigger, and media results; no execution, persistence, or Telegram sending |
| `xgent_app/agent_results.py` | Normalize executor dictionaries into a common Agent result contract while preserving legacy fields; no execution, persistence, or Telegram sending |
| `xgent_app/agent_dispatch.py` | Dispatch and normalize read, edit, grep, run, search, and fetch without Telegram or persistence concerns |
| `xgent_app/agent_files.py` | Execute text and base64 file writes; base64 decoding/writing is moved off the event loop |
| `xgent_app/agent_file_delivery.py` | Send files produced by `file`/`file:base64` and preserve their Telegram captions and size-limit notices |
| `xgent_app/agent_sendfile.py` | Execute server-file delivery, including local Bot API hard-link/copy fallback, upload indicator, timeout, and cleanup |
| `xgent_app/agent_shell.py` | Execute shell/stdin/session protocols and preserve stop-session behavior |
| `xgent_app/agent_trigger.py` | Execute trigger protocols and normalize failure notices |
| `xgent_app/agent_presenter.py` | Build pure Telegram presentation text for Agent results |
| `xgent_app/agent_history.py` | Keep Agent recorder/database write ordering and special media history format |
| `xgent_app/agent_loop_state.py` | Hold one Agent operation round's continuation context and pause state |
| `xgent_app/agent_coordinator.py` | Plan stop/end/continue transitions and build the next in-memory transcript without I/O |
| `xgent_app/agent_media.py` | Manage media generation waiting, stop cancellation, typing state, and progress-message cleanup |
| `xgent_app/agent_media_delivery.py` | Deliver generated media or the existing failure warning without persistence/context concerns |
| `xgent_app/web_auth.py` | Web Chat password hashing, signed session cookies, login rate limiting, and Telegram `initData` verification; no HTTP or Telegram concerns |
| `xgent_app/web_bridge.py` | Duck-typed `Update`/`Context`/`Bot` shims that let the Web UI drive `process_conversation` unchanged, plus the thread-safe outbound frame queue |
| `xgent_app/web_server.py` | Zero-dependency threaded HTTP/SSE server for the Web Chat UI; receives all application behavior through injected callbacks |
| `xgent_app/fanout.py` | Per-channel outbound isolation: bounded queue + single worker + circuit breaker + durable pending store, so one unreachable surface cannot slow another; stdlib only, no `telegram` import |

Legacy names remain available through thin imports/wrappers in the sections so
existing handlers keep working while the internal boundaries become explicit.

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
| `lifecycle.py` | errors, `boot_core()`, `telegram_ready()`, and the single shutdown path |
| `runtime.py` | component registry, per-component health, the supervised Telegram component, and the single process entrypoint `run_app()` |
| `main.py` | `sys.exit(asyncio.run(run_app()))` and nothing else |

## Surface isolation

Telegram, Web and CLI are independently deployable and cannot drag each other
down. Two mechanisms carry that guarantee:

**Startup** — `runtime.run_app()` owns the event loop. `boot_core()` (config +
database) runs first, then every non-Telegram component, and only then is
Telegram started as a supervised task that retries forever. Telegram being
unreachable leaves that one component in `degraded`; nothing else notices.
This replaces `app.run_polling()`, whose `__run` performs `Bot.initialize()`
(a live `get_me()` call, `bootstrap_retries=0` by default) *before* `post_init`
— so a network failure killed the process before the Web listener ever bound.

**Output** — every outbound Telegram call goes through a `fanout.ChannelWorker`:
the conversation core emits the Web frame first and hands Telegram an `Op`
(synchronous, non-blocking, never raises). Web latency is therefore independent
of Telegram latency. Message identity belongs to the core (logical ids from
1,000,000 up); each channel maps logical → native id itself. Undeliverable ops
land in `channel_outbox` and replay in order when the channel recovers, with
superseded intermediate edits collapsed.

`GET /healthz` (unauthenticated, `{"ok": true}` only) separates "process dead"
from "process alive, one surface degraded"; `GET /api/health` (authenticated)
reports per-component and per-channel detail.

## Bootstrap contract

`xgent_app.bootstrap.read_section_manifest()` validates that the manifest:

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

1. Continue moving configuration and pure utility functions into importable modules.
2. Keep shrinking the Agent loop through compatibility-preserving orchestration
   helpers. Context construction, normalization, dispatch, Shell/file/trigger/media
   execution, Telegram delivery, persistence, and round transitions now have explicit
   boundaries.
3. Introduce explicit service objects for database, model, and Agent state.
4. Move handlers into modules that receive dependencies instead of reading
   mutable globals.
5. Replace the ordered compatibility loader once no section relies on a
   shared global namespace.
