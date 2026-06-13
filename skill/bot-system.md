```!
Telegram AI Bot 的提示词结构、拼接逻辑、回复流程和基础功能说明。
用于快速定位系统提示词拼接、Agent 协议、记忆/上下文和更新机制。
```

# Telegram AI Bot 系统说明

本文用于快速理解当前项目的运行结构。若本文与代码不一致，以 `bot_server.py` 的当前实现为准。

## 1. 基本定位

这是一个私有 Telegram AI Bot，主程序是 `bot_server.py`。系统使用单一授权用户模式，只有 `.env` 中 `AUTHORIZED_USER_ID` 对应的 Telegram 用户可以正常使用。

核心能力包括：

- Telegram 文本、文件、图片、贴纸消息处理。
- OpenAI / OpenAI 兼容 / Gemini / Vertex / Claude 提供商接入。
- 默认对话模型和默认媒体模型分开配置。
- 单一全局记忆模式。
- 流式或非流式回复。
- Agent 模式下执行命令、读写文件、发送文件、管理交互式 shell 会话和调用媒体生成。

## 2. 提示词文件结构

提示词文件由 `PromptFileManager` 管理，路径位于 `prompts/`：

- `prompts/main.txt`：主提示词，当前只定义基础身份。
- `prompts/global_addon.txt`：全局事实层，说明运行环境、上下文读取和记忆使用规则。
- `prompts/agent_addon.txt`：Agent 与工具能力说明，定义 `run`、`shell`、`stdin:*`、`shellread:*`、`shellkill:*`、`read`、`file:`、`sendfile`、`media` 等协议。
- `prompts/agent_disabled_addon.txt`：Agent 关闭时的补充说明。
- `prompts/extras/idle_message.txt`：空闲提醒消息的生成提示。
- `prompts/extras/unauthorized_reply_messages.txt`：未授权用户的拒绝回复。
- `prompts/extras/agent_command_blacklist.txt`：Agent 命令黑名单。

`assistant_prompt` 和 `global_prompt_addon` 既会写入文件，也会持久化到 SQLite 的 `config` 表。运行时通常优先从 `UserDataManager` 读取。

多条文本输入规则：

- 修改“未授权用户的拒绝回复”时，可以一次发送一条并多次发送；也可以一次发送多条，条目之间用独立一行三个横杠 `---` 分隔。
- 批量添加 Agent 命令黑名单时，可以一次粘贴多条；每条一行，或用独立一行三个横杠 `---` 分隔。
- 黑名单文件中空行、独立一行的 `---`、以及 `#` 开头的注释会被忽略。

## 3. 系统提示词拼接逻辑

每轮对话在 `_process_conversation_inner()` 中组装系统提示词。

拼接顺序：

1. `assistant_prompt`
2. `global_prompt_addon`
3. `agent_prompt_addon`
4. 自动生成的当前运行目录绝对路径段
5. 自动生成的 `skill/` 文件索引段
6. 如果 Agent 模式关闭，再追加 `agent_disabled_addon`

对应代码入口：

- `get_runtime_prompt()`
- `get_agent_runtime_prompt(agent_mode)`
- `build_absolute_path_prompt_section()`
- `build_skill_prompt_section()`

`skill/` 目录不会被全文自动塞进提示词。系统只拼接文件索引，模型需要时再通过 Agent 的 `read` 能力读取具体文件。

skill 文件简介只读取 `!` 围栏协议块：

````text
```!
这里写会进入索引的简介；可以写多行。
系统会合并同一文件内所有 `!` 块。
```
````

## 4. 回复逻辑

文本消息主流程：

1. 通过 `check_authorized_user_middleware()` 校验授权用户。
2. `handle_text_message()` 记录用户消息到全局记忆。
3. `process_conversation()` 设置处理锁和停止事件。
4. `_process_conversation_inner()` 获取默认 Provider、默认模型、全局历史和系统提示词。
5. 根据 `stream_mode` 调用：
   - `send_streaming_response()`
   - `send_non_streaming_response()`
6. 回复成功后，写入 `global_messages` 和兼容镜像 `chat_messages`。
7. 如果 Agent 模式开启，解析模型回复里的协议块并进入最多 10 轮工具执行循环。
8. 工具结果会作为新的上下文回灌给模型，直到模型不再请求工具或达到轮数上限。shell/stdin/shellread 会先等到命令结束、交互提示、明显长驻或等待窗口到期，再把当前结果回灌给模型继续自动判断。

停止按钮会设置全局停止事件。命令、媒体生成和 Agent 操作会检查该事件并尽量中断后续流程。

## 5. 记忆和上下文

系统使用单一全局记忆：

- 固定会话 ID：`global_memory`
- 核心表：`global_messages`
- 兼容镜像表：`chat_messages`

`get_conversation_messages(global_depth)` 会取最近若干条全局记录，并把系统操作、按钮点击、Agent 命令、Agent 结果转换成模型可读文本。

文件、图片、run 和 shell 结果采用路径/输出索引式记忆：

- 当前工具循环可以把真实内容交给模型。
- `read` 会把文件本体直接回灌给当前工具循环：文本/代码/JSON/Markdown 等作为完整文本，图片作为图片本体。
- `read` 的文件本体通常不会完整写入长期记忆；长期记忆通常只保存读取提示、路径和简短说明。
- `run` 会等待一次性命令结束，把完整输出保存到 `bot_storage/command_outputs/`，并把返回码、输出路径和截断输出写入全局记忆。
- `shell` 用于交互式或长驻会话；长驻/日志类/等待输入的命令仍在运行时会记录当前输出并回灌给 AI，AI 可继续自动决定 `stdin`、`shellread`、`shellkill` 或回复用户。
- `file:` 与 `sendfile` 执行后会把执行结果回灌给 AI 并写入上下文，但只包含状态、路径、大小和错误信息，不包含文件本体；需要内容时应使用 `read`。
- 后续需要完整内容时，应按路径重新读取。

## 6. Agent 基本功能

Agent 模式开启后，模型可以通过协议块调用真实工具：

- `run`：执行会自然结束的一次性命令，适合测试、构建、诊断、Git、依赖、服务状态和只读检查；完整输出会保存到路径。
- `shell`：启动可持续交互 shell 会话并返回会话 ID。适合交互式、阻塞式、长驻、持续输出或需要多次输入的任务。
- `stdin`：向已有 shell 会话输入终端宏；普通文本直接写，只有明确控制前缀才有特殊含义；`key:` 发送按键，`line:` 输入文本并回车，`paste:` 或 `paste: <<EOF` 显式粘贴文本，`raw:`/`hex:`/`base64:`/`bytes:` 可表达任意字节；完整语法见 `skill/stdin-syntax.md`。
- `shellread`：快速读取已有 shell 会话的新输出，只短暂捕获当前输出，不按完整命令等待窗口长等，适合用户明确要求继续观察持续日志、安装进度或服务状态。
- `shellkill`：关闭不再需要的 shell 会话。
- `read`：按路径读取文件本体并直接回灌给 AI。文本/代码/JSON/Markdown 等作为完整文本上下文返回，图片作为图片本体返回，其他文件视模型通道能力返回。
- `sendfile`：把服务器上的文件发送给用户；只把发送结果回灌给 AI，不回灌文件本体。
- `file:`：创建或覆盖服务器文件；只把写入结果回灌给 AI，不回灌文件本体。支持三种写法：普通三反引号（内容不含 ``` 时）、heredoc 语法 `file:/path <<EOF ... EOF`（内容含 ``` 或特殊字符，推荐用于 Markdown/代码文件）、base64 语法 `file:base64:/path`（二进制安全，解码后按字节写入）。
- `media`：调用默认媒体模型生成图片或其他媒体。

Agent 命令会受 `prompts/extras/agent_command_blacklist.txt` 管理。一次性命令优先走 `run`；交互、长驻或持续输出命令走 `shell`，仍在运行时会记录当前输出并回灌给 AI 继续判断。

## 7. 更新流程

机器人提供 `/update` 和“更多设置”里的更新按钮。

更新会从配置的更新源下载最新代码并覆盖当前项目文件，完成后自动重启。

默认更新源为 GitHub zipball API：`https://api.github.com/repos/HANLINGVABCN/telegram-ai-bot/zipball/main`。如果仓库是私有仓库，需要在 `.env` 配置 `UPDATE_GITHUB_TOKEN`，token 至少要有该仓库 Contents 只读权限。

更新前会先询问本地提示词与 skill 文件处理方式：

- 保留当前提示词和 skill：跳过 `prompts/` 与 `skill/` 下的文件，适合服务器上已经手动调过提示词或技能说明的场景。
- 覆盖并备份提示词和 skill：先把当前 `prompts/` 与 `skill/` 一起备份到 `bot_storage/update_backups/custom_时间戳/`，再覆盖为 GitHub 最新版本。

运行数据、数据库、日志、存储目录、虚拟环境和 Git 目录不会被更新流程覆盖。
