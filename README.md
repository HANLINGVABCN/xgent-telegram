# Telegram AI Bot

一个面向个人使用的 Telegram AI 助手服务端。项目支持多模型提供商、全局记忆、流式回复、文件与图片上下文，以及可选的 Agent 工具执行能力。

公开仓库地址：[HANLINGVABCN/telegram-ai-bot](https://github.com/HANLINGVABCN/telegram-ai-bot)

## 一键命令

以下命令会把项目下载到 `/opt` 目录下，生成 `/opt/telegram-ai-bot` 文件夹，然后进入该文件夹并运行安装脚本。`/opt` 可以换成任意想安装的位置。

```bash
cd /opt && sudo git clone https://github.com/HANLINGVABCN/telegram-ai-bot.git && sudo chown -R "$USER":"$USER" telegram-ai-bot && cd telegram-ai-bot && chmod +x install.sh && ./install.sh
```

## 功能特性

- 私有单用户访问控制：只有 `.env` 中配置的 Telegram 用户 ID 可以使用。
- 多提供商支持：OpenAI、OpenAI 兼容接口、Gemini、Vertex、Claude。
- 默认模型分离：对话模型和媒体模型可以分别配置。
- 全局记忆：使用 SQLite 保存对话、系统操作、按钮操作和 Agent 结果。
- 流式回复：支持流式和非流式两种模型请求方式。
- 文件与图片处理：可接收文件、图片、贴纸，并按路径索引保存上下文。
- Prompt 管理：主提示词、全局附加提示词、Agent 提示词均可通过文件维护。
- Agent 模式：通过 shell 会话执行命令、读写文件、发送服务器文件、管理交互式 shell 会话、调用媒体生成。
- 命令黑名单：Agent 命令可通过 `prompts/extras/agent_command_blacklist.txt` 管理拦截规则。

## 项目结构

```text
telegram-ai-bot/
├─ bot_server.py
├─ install.sh
├─ requirements.txt
├─ prompts/
│  ├─ main.txt
│  ├─ global_addon.txt
│  ├─ agent_addon.txt
│  ├─ agent_disabled_addon.txt
│  └─ extras/
│     ├─ agent_command_blacklist.txt
│     ├─ idle_message.txt
│     └─ unauthorized_reply_messages.txt
└─ skill/
   ├─ bot-system.md
   ├─ proxy-setup.md
   ├─ speedtest.md
   └─ script/
      ├─ proxy-setup/
      │  └─ proxy_setup.sh
      └─ speedtest/
         └─ deploy_speedtest.sh
```

运行后会自动生成：

- `.env`：Telegram Bot Token 和授权用户 ID。
- `bot_memory.db`：SQLite 数据库。
- `bot_storage/`：上传文件、命令输出、生成媒体等存储目录。
- `bot_server.log`：服务日志。
- `bot_output.log`：后台 nohup 运行时的输出日志。
- `bot.pid`：后台 nohup 运行时的进程 ID 文件。

## 环境要求

- Python 3.8+
- Telegram Bot Token
- 至少一个可用的模型 API Key

依赖见 `requirements.txt`：

```text
python-telegram-bot[job-queue]
openai
python-dotenv
aiosqlite
```

## 快速开始

克隆项目。下面的命令会把项目下载到 `/opt` 目录下，并生成 `/opt/telegram-ai-bot` 文件夹。`/opt` 可以换成任意想安装的位置：

```bash
cd /opt
sudo git clone https://github.com/HANLINGVABCN/telegram-ai-bot.git
sudo chown -R "$USER":"$USER" telegram-ai-bot
cd telegram-ai-bot
```

运行安装脚本：

```bash
chmod +x install.sh
./install.sh
```

`install.sh` 会自动完成环境检查、虚拟环境创建、依赖安装、`.env` 检查和 Telegram Token 校验。

如果项目根目录下还没有 `.env`，脚本会自动创建。若缺少必要配置，脚本会在终端中提示输入：

```env
BOT_TOKEN=你的 Telegram Bot Token
AUTHORIZED_USER_ID=你的 Telegram 用户 ID

# 私有仓库更新需要。Fine-grained token 只需要该仓库 Contents: Read-only。
UPDATE_GITHUB_TOKEN=你的 GitHub Token
UPDATE_ZIP_URL=https://api.github.com/repos/HANLINGVABCN/telegram-ai-bot/zipball/main
```

`UPDATE_ZIP_URL` 可以不写，默认就是上面的地址。仓库改为 private 后，`/update` 仍可使用，但必须给运行中的机器人配置 `UPDATE_GITHUB_TOKEN`；否则 GitHub 会返回 404/403，更新会失败。

配置写入完成后，脚本会继续进入启动方式菜单：

- 前台运行
- 后台运行
- PM2 守护运行
- 仅检查环境

### PM2 重启后列表为空

选择 PM2 守护运行时，安装脚本会自动执行 `pm2 startup` 和 `pm2 save`，用于保存当前进程列表并配置系统重启后自动恢复。

如果服务器重启后 `pm2 list` 仍然为空，优先检查这几件事：

- 是否用同一个 Linux 用户执行命令。`pm2 list` 和 `sudo pm2 list` 是两套不同的进程表。
- 手动确认保存状态：`pm2 save`。
- 手动恢复保存的进程表：`pm2 resurrect`。
- 手动重新配置开机自启：`pm2 startup`，按它输出的命令执行后再运行 `pm2 save`。

## 获取 Telegram 配置

1. 在 Telegram 中打开 [BotFather](https://t.me/BotFather)。
2. 使用 `/newbot` 创建机器人，获取 Bot Token。
3. 获取自己的 Telegram 用户 ID，可以使用 `@userinfobot` 或其他 ID 查询机器人。
4. 运行 `./install.sh`，按提示输入 Token 和用户 ID。

## 首次使用

启动服务后，向机器人发送 `/start` 打开主菜单。

常用步骤：

1. 进入「提供商」添加 API 连接。
2. 进入「默认模型」选择默认对话模型。
3. 如需媒体生成，再选择默认媒体模型。
4. 根据需要开启或关闭 Agent 模式、流式输出、记忆深度等设置。

## Telegram 命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 打开主菜单 |
| `/config` | 打开配置菜单 |
| `/providers` | 管理提供商与模型列表 |
| `/models` | 选择默认模型 |
| `/chat_model` | 选择默认对话模型 |
| `/media_model` | 选择默认媒体模型 |
| `/prompts` | 管理提示词 |
| `/clear_memory` | 清空上下文 |
| `/depth` | 设置记忆深度 |
| `/timeout` | 设置请求超时 |
| `/agent` | 开关 Agent 模式 |
| `/blacklist` | 管理 Agent 命令黑名单 |
| `/stream` | 开关流式输出 |
| `/status` | 查看状态 |
| `/show_chat_info` | 查看状态与记忆统计，等同 `/status` |
| `/export` | 导出全部记忆 |
| `/show_all` | 导出全部记忆，等同 `/export` |
| `/update` | 更新代码并重启 |
| `/restart` | 重启 Bot |

## Prompt 说明

项目的提示词由 `prompts/` 目录维护：

- `main.txt`：基础身份提示。
- `global_addon.txt`：运行环境、上下文读取和记忆规则。
- `agent_addon.txt`：Agent 工具协议和执行规则。
- `agent_disabled_addon.txt`：Agent 关闭时的限制说明。

每轮请求会按以下顺序拼接系统提示词：

1. 主提示词。
2. 全局附加提示词。
3. Agent 提示词。
4. 当前运行目录、上传目录、命令输出目录等绝对路径信息。
5. `skill/` 文件索引。
6. 如果 Agent 关闭，追加 Agent 关闭说明。

`skill/` 目录只会以索引形式进入提示词，不会自动全文注入。模型需要时应通过 `read` 工具读取具体文件。

`/update` 更新时会询问是否覆盖 `prompts/` 与 `skill/`。选择保留会跳过这两个目录；选择覆盖会先把当前 `prompts/` 和 `skill/` 一起备份到 `bot_storage/update_backups/custom_时间戳/`，再应用更新包里的最新版本。

skill 文件简介只使用 `!` 围栏协议块；同一文件内多个 `!` 块会按出现顺序合并到索引中：

````text
```!
这里写会进入索引的简介；可以写多行。
适合放用途、触发场景和重要边界。
```
````

## Agent 模式

Agent 模式开启后，模型可以通过协议块调用真实工具能力：

- 通过 run 协议执行一次性命令并保存结果。
- 通过 shell 协议管理长驻、持续输出和交互式命令。
- 管理交互式 shell 会话。
- 读取服务器文件。
- 创建或覆盖服务器文件。
- 发送服务器文件给用户。
- 调用默认媒体模型生成媒体。

Agent 命令会受到自定义黑名单影响。一次性命令优先使用 `run`，完整输出会保存到 `bot_storage/command_outputs/`；交互式、长驻或持续输出命令使用 `shell`。黑名单文件位于：

```text
prompts/extras/agent_command_blacklist.txt
```

每行写一个禁止片段，命令中包含该片段时会被拦截。

## 安全提示

- 不要提交 `.env`、数据库、日志或 `bot_storage/` 目录。
- Agent 模式具备真实服务器执行能力，只建议在可信环境中开启。
- 项目不是完整沙箱；请根据部署环境维护命令黑名单。
- 建议使用单独的低权限服务器账号运行服务。
- 如果 API Key 或 Telegram Token 泄露，请立即在对应平台重置。

## 开发与检查

如果只是本地开发或排错，也可以在安装依赖后直接运行主程序：

```bash
python3 bot_server.py
```

语法检查：

```bash
python -m py_compile bot_server.py
```

## License

本项目基于 MIT License 开源，详见 [LICENSE](./LICENSE)。
