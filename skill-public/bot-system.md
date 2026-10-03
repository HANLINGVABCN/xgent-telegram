```!
XGent 的提示词结构、Telegram/Web/CLI 共用回复流程、工具结果留存、附件与压缩机制说明。
用于定位提示词拼接、当前 -x 协议、记忆与更新规则；实际执行以系统给出的协议说明和代码为准。
```

# XGent 系统说明

本文说明当前仓库实现。`xgent_server.py` 是入口，业务按清单加载 `xgent_app/sections/`；核对行为时查对应模块，不要只在入口文件中寻找实现。

## 1. 基本定位

XGent 是 Telegram、Web 和 CLI/TUI 共用对话核心、模型配置与数据库的私人助手。各端按自身入口完成授权，不能把所有请求都当成 Telegram 消息。Telegram 使用 `.env` 中的 `AUTHORIZED_USER_ID` 校验用户；项目也支持纯 Web 模式。

核心能力包括：

- 文本、文件与图片等消息处理，以及跨端历史展示。
- OpenAI / OpenAI 兼容 / Gemini / Vertex / Claude 接入；默认对话模型和默认媒体模型分开配置。
- 单一全局对话历史、独立用户记忆层、工具结果留存和全量关联附件恢复。
- 前台流式、后台流式、非流式回复，以及成功后才提交的上下文压缩。
- Agent 开启时可使用协议执行命令、读写和发送文件、管理 shell 会话、联网检索、调用媒体工具或向用户提问。
- Agent 关闭不妨碍分析已经提供的附件，也不禁止对话模型自身已有的原生图片输出能力；不得因此调用 Agent 工具或虚构模型能力。

## 2. 提示词文件结构与加载

`PromptFileManager` 管理项目 `prompts/` 下的文件：

- `main.txt`：基础身份、回答与事实要求。
- `global_addon.txt`：跨端格式、可见上下文与工具留存规则。
- `agent_addon.txt`：执行授权边界、当前 `*-x` 协议格式及工具说明。
- `agent_disabled_addon.txt`：关闭 Agent 时的工具权限限制。
- `compression.txt`：压缩专用指令，不自动拼入普通聊天的 system prompt。
- `extras/idle_message.txt`：空闲提醒指令。
- `extras/unauthorized_reply_messages.txt`：未授权请求的拒绝回复候选文本。
- `extras/agent_command_blacklist.txt`：命令黑名单，属于执行配置而非对话提示词正文。

运行时遵循**文件优先**：`get_runtime_prompt()` 优先取提示词文件的非空内容，只有可用文件内容为空时才回退到 `UserDataManager` 配置。读盘失败时管理器可保留最近可用缓存。菜单编辑通过 `save_runtime_prompt()` 写文件并同步配置；直接编辑磁盘文件后会按文件状态检测更新，不必重启或手动重载。

压缩指令使用必需文件读取：缺失或空白时明确失败，不用其他提示词替代。进行中的压缩使用开始时冻结的指令，下一次压缩或重试重新读取当前文件。

多条配置文本规则：

- 未授权回复可多次输入，也可用独立一行 `---` 分隔多条。
- 黑名单可每行一条或用独立一行 `---` 分隔；空行、分隔线和 `#` 开头的注释忽略。

## 3. 普通聊天提示词拼接

`build_conversation_system_prompt(agent_mode)` 由正常聊天和空闲提醒共用，依次组合：

1. `assistant_prompt`。
2. `global_prompt_addon`。
3. `【用户记忆】`：`memory/` 下全部非空记忆文件，详见第 8 节。
4. `agent_prompt_addon`。
5. 当前运行目录、技能目录和上传目录的绝对路径说明。
6. 技能索引：名字、绝对路径，以及启用技能的简介。
7. 技能按需查阅规则；Agent 关闭时不得借查阅技能执行协议。
8. Agent 关闭时追加 `agent_disabled_addon`。
9. 对话附件说明：已随请求提供的内容可以直接使用，不因包含路径就重复读取。

代码主要位于 `xgent_app/sections/services.py`；文件读取及缓存由 `xgent_app/sections/core.py` 的 `PromptFileManager` 管理。

技能来自 `skill-public/`、`skill-private/`，并兼容旧 `skill/` 目录。简介扫描会合并文件中全部有效且闭合的 `!` 围栏块，不限于文件开头；其他代码围栏内的示例不作为简介。建议将主要简介放在开头：

````text
```!
这里写简洁的用途和关键规则，可以有多行。
```
````

技能三种状态：启用时注入简介；普通关闭只保留名字和路径，可在有权限时按需读取；彻底隐藏时不列入索引。关闭或隐藏不删除文件，也不清除已进入对话历史的内容。正文不自动注入，但 read-x 回传的正文遵循工具结果留存规则。

## 4. 回复与执行循环

1. 各端校验授权并记录用户输入。
2. `process_conversation()` 管理会话处理锁和停止事件。
3. `_process_conversation_inner()` 取得对话模型、有效历史及普通系统提示词；每次实际请求还会恢复附件和留存的原生工具内容。
4. 按配置走 `send_streaming_response()`、`send_background_streaming_response()` 或 `send_non_streaming_response()`。
5. 保存 AI 回复、附件关联和兼容镜像；用量单独记账，UI 用量信息不成为模型历史。
6. Agent 开启时，按顺序解析并执行回复中的协议块，再回传实际结果。独立块不会自动变成并行执行或事务。
7. 新的真实用户消息重置持久化 Agent 轮数；工具和 trigger 系统结果沿用预算。超过上限时仍可显示工具结果，但不继续调用模型。

停止事件用于尽量中断模型等待和工具操作。停止或超时不证明命令完全没有执行，重试前必须检查已有结果和副作用。

## 5. 对话历史、工具留存、附件与压缩

### 普通历史

固定会话 ID 为 `global_memory`，主要记录表为 `global_messages`，`chat_messages` 是兼容镜像。

`get_conversation_messages(global_depth)` 按**有效模型消息条数**取最近历史，不按问答轮数计数。token 提示、Agent 状态、压缩辅助消息和已被完整工具上下文替代的展示记录不占名额。新记录优先恢复 metadata 中的 `model_context`，旧记录兼容读取已有正文；已经丢失的旧工具内容不会凭空恢复。

### 工具结果留存

“工具结果留存”沿用 `readx_persist_context` 配置值。开启时分别保存实际回传给模型的消息和界面展示内容；新提问、重启或压缩都使用实际内容，而不是以展示卡片替代。关闭只影响之后产生的结果，不删除已经留存的内容。

- read-x 的文本全文或选定行段可留存；未读取的部分不会因此保存。图片及受支持二进制保存读取时的原件副本和校验信息，原路径后来变化不改变已读版本。
- 文本工具结果遵循普通历史窗口；原生工具附件和其他已关联附件跨文本窗口恢复，直至压缩成功或清空解除关联。
- run-x 会将完整原始输出保存在 `xgent_storage/command_outputs/`，回传可能只有返回码、输出片段和路径。“完整留存回传结果”不等于自动展开整个日志。
- shell-x / stdin-x 保存实际回传的会话输出与状态说明；没有回传的输出不能当作已知。
- file-x / sendfile-x 只回传写入或发送结果，不包含文件本体。需要未提供的文件内容时使用 read-x。
- 优先使用当前请求已经提供的内容；缺失、只剩索引或需要核对最新磁盘版本时再读取。

### 压缩

压缩及重试共用流程：当前有效历史快照 → 导出并校验 → 发送系统记忆 ZIP → 显示“正在压缩” → 在原对话和原生附件最后追加一次压缩指令 → 生成完整摘要 → 事务提交后替换原历史。

压缩不受普通历史深度截断；包含当前仍保存的有效记录、上次摘要和关联附件，但不自动展开历次归档原文。压缩不额外注入普通人设、用户记忆目录或技能提示词。ZIP 是文本与附件索引备份，不等于打包了全部二进制原件。

失败、停止、截断、缺件、模型不支持附件或容量超限时保留原上下文，只增加系统失败提示，不保存半截摘要。生成期间出现新有效消息就拒绝提交旧快照；用户主动清空优先。重试重新读取当前历史和当前压缩指令，不沿用过期任务输入。

成功后旧附件退出自动上下文，磁盘原件和归档仍保留；摘要作为普通 AI 回复受历史深度限制，不永久置顶。完整迁移需同时保留数据库与 `xgent_storage/`。代码入口在 `xgent_app/sections/command_handlers.py`、`database.py` 和 `xgent_app/compression.py`。

## 6. 当前 Agent 协议

执行格式以 `prompts/agent_addon.txt` 和 `xgent_app/protocols.py` 为准：`*-x` 围栏、顶格 BEGIN/END、相同且唯一的标记。标记推荐 10–32 位 ASCII 字母、数字、下划线；后端容错不代表可以省略完整格式。

- `run-x`：一次性命令；`shell-x`：交互或长驻会话；`stdin-x:会话ID`：输入或捕获输出；`shellkill-x:会话ID`：关闭会话。会话 ID 必须来自系统回传。stdin 宏语法按技能索引中的真实绝对路径读取 `stdin-syntax.md`。
- `read-x`：正文写绝对路径及可选行段；`grep-x`：结构化定位；`intel-x`：代码符号与语义分析。
- `edit-x:/绝对路径`：用 `<<OLD` / `<<NEW` 精确替换，必要时使用匹配后缀；不是整文件覆盖。
- `file-x:/绝对路径`：新建或已授权整体重写；`file-x:base64:/绝对路径`：按 base64 字节写入。正文可包含普通 Markdown 围栏，仍由外层 BEGIN/END 定界，不使用旧 heredoc 文件协议。
- `sendfile-x`：交付服务器文件，只回传发送结果。大文件是否能直穿取决于本地 API 配置，不应保证任意部署都能发送。
- `search-x` / `fetch-x`：检索或抓取网页；`media-x`：调用默认媒体模型，参考文件和任务需在该请求中明确提供；`ask-x`：用户表单，私密答案只返回变量就绪信息。
- `over-x`：可选免回传收尾。只允许同条回复中的 run-x、edit-x、shellkill-x、file-x（含 base64 形式）全部成功时省略下一次模型调用。收尾语即使失败也可能已经发出，只能预写中性说明或之前已核验的事实；不能代替必要验证。

后台 trigger 是**命令行工具，不是独立协议**：由 run-x 调用 `trigger add --cmd ... --task ...`、`trigger show`、`trigger kill <id>`、`trigger kill-all`。调度参数为 `--after` / `--at` / `--cron` 三选一；`--repeat` 必须搭配 `--when`，`--tz` 指定时区。命令无时长上限，同一任务不重叠运行，不同任务之间没有全局并发上限。登记成功不等于任务完成，实际结果由后台投递。详情按技能索引读取 `trigger-x-protocol.md`。

以下只是格式示例；实际使用前替换路径和参数，讲解时不要把示例当作应执行的指令：

```run-x
<<BEGIN_trigger_list_a14f
trigger show
<<END_trigger_list_a14f
```

```file-x:/absolute/project/workspace/example.md
<<BEGIN_file_example_b26e
# Example
正文可以包含 Markdown。
<<END_file_example_b26e
```

```file-x:base64:/absolute/project/workspace/example.bin
<<BEGIN_file_binary_c38d
YQ==
<<END_file_binary_c38d
```

所有工具使用仍受用户授权和 Agent 开关约束；命令还受配置的黑名单约束。路径参数采用系统给出的真实绝对路径，不照抄示例路径。

## 7. 更新流程

`/update` 或设置菜单的更新入口从配置源下载代码并在完成后重启。默认源为 `https://api.github.com/repos/HANLINGVABCN/xgent-telegram/zipball/main`；私有仓库需要具有该仓库读取权限的 `UPDATE_GITHUB_TOKEN`。

更新前选择本地定制内容的处理方式：

- 保留：跳过 `prompts/`、`skill-public/` 和旧 `skill/`，服务器上的旧说明也会保留，需手动合并需要的修改。
- 覆盖并备份：先备份上述目录到 `xgent_storage/update_backups/custom_时间戳/`，再覆盖为仓库版本。
- `skill-private/` 始终不覆盖。数据库、运行存储、日志、虚拟环境及 Git 目录也不由代码更新覆盖。

提示词与技能文件更新后按各自读取逻辑生效；压缩提示词启动迁移只针对与旧内置默认内容完全相同的文件，不会自动覆盖用户自定义版本。

## 8. 用户记忆（memory）

用户记忆独立于对话历史和工具结果留存，是正常聊天的常驻提示词层。

- 项目根目录 `memory/` 下每条记忆一个 `.txt` 文件，按文件名排序。菜单创建的文件名形如 `memory_YYYYMMDD_HHMMSS_<随机>.txt`。
- 每轮正常聊天实时读取全部非空记忆并组成 `【用户记忆】` 段，位于全局追加提示词之后、Agent 提示词之前；与技能正文按需读取不同。Agent 关闭仍加载用户记忆，压缩请求不额外加载它。
- 对话历史窗口、对话压缩和清空不会自动删除记忆文件；不要把普通工具结果留存误称为写入长期记忆。
- “记忆”菜单支持添加、查看、删除、清空和文件导入；添加进入 `BotState.SET_MEMORY`，可以多条输入拼成一条，发送 `cancel` 取消。
- 代码入口：`list_memory_files()`、`read_memory_file()`、`save_memory_file()`、`delete_memory_file()`、`clear_all_memory()`、`build_memory_prompt_section()`；菜单与回调包含 `get_memory_menu()`、`build_memory_menu_text()`、`get_memory_delete_keyboard()`、`menu_memory`、`act_add_memory`、`act_confirm_memory`、`act_list_memory`、`act_delete_memory_menu:`、`act_delete_memory:`、`confirm_clear_user_memory`、`do_clear_user_memory`。
