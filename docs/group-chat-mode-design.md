# 群聊模式设计方案（Group Chat Mode）

> 状态：**待评审**。本文档只做设计与接线点定位，不改动任何运行代码。
> 目标：新增一种与 bot / cli / web **功能独立、记忆独立**的运行模式，把多台已部署的
> Telegram Bot 放进同一个群里，模拟真人群聊；人设、Skill、Prompt、模型提供商配置**继续共享**。

---

## 0. 结论先行

可行的，而且**不需要重写对话核心**。三条硬约束的落地路径分别是：

| 约束 | 落地方式 | 难度 |
| --- | --- | --- |
| 功能独立 | 群聊逻辑全部放 `xgent_app/group_*.py`（**不进 `sections/`**），只在 sections 做最小接线 | 低 |
| 记忆不共享 | 给 `global_messages` 加 `session_id` 过滤，群聊走独立命名空间 `group:<room_id>` | 中（有迁移风险） |
| 人设/Skill/配置共享 | 三者均为**进程级全局单例**，零改动直接复用 | 无 |

真正的难点不在"隔离"，而在**多 Bot 同场的入站去重**与**谁在什么时候说话的调度**。

---

## 1. 现状：三个必须先知道的事实

### 1.1 "三端共享记忆"的真相是一个硬编码常量

```python
# xgent_app/sections/services.py:881
SINGLE_MEMORY_SESSION_ID = "global_memory"
```

```python
# xgent_app/sections/services.py:69 / 83
session_id=session_id or UserDataManager.get('current_chat_id'),
```

写入时兜底成 `global_memory`；读取时**根本不看 session_id**：

```python
# xgent_app/sections/database.py:470
async def get_conversation_messages(self, limit: int = 50) -> List[Dict]:
    cursor = await conn.execute('''
        SELECT role, content, timestamp, msg_type FROM global_messages
        ORDER BY timestamp DESC LIMIT ?        # ← 无 WHERE，全表
    ''', (limit,))
```

**这是本方案最大的风险点，也是唯一必须动的核心。** 当前是"全表即上下文"，
任何写进 `global_messages` 的东西都会污染所有模式的上下文。

好消息：`idx_global_session ON global_messages(session_id)` 已存在
（`database.py:332`），加 `WHERE session_id = ?` **无需新建索引**。

**迁移要求**：历史行的 `session_id` 可能为 `NULL`。上线前必须跑一次性回填：

```sql
UPDATE global_messages SET session_id = 'global_memory' WHERE session_id IS NULL;
```

否则加了 `WHERE` 之后老用户会"失忆"。

### 1.2 Bot 是单 token 单 Application

```python
# xgent_app/sections/core.py:101-105
TOKEN = os.getenv("BOT_TOKEN", "")
WEB_ONLY = not TOKEN
```

```python
# xgent_app/sections/runtime.py:206
builder = Application.builder().token(BotConfig.TOKEN)
```

`build_application()`（`runtime.py:196`）唯一调用点在 `runtime.py:521`，
由 `telegram_supervisor`（`runtime.py:296`）监督。**没有多 token 的任何痕迹。**

### 1.3 授权中间件完全不适用于群

```python
# xgent_app/sections/ui.py:1161
async def check_authorized_user_middleware(update, context) -> bool:
    if not update.effective_user:
        return False
    if update.effective_user.id == BotConfig.AUTHORIZED_USER_ID:
        return True
    await handle_unauthorized_user(update, context)
    return False
```

只看发送者 ID。真实群里消息来自 `chat.type ∈ {group, supergroup}`、`chat_id < 0`，
且群里可能存在其他真人——需要一套独立的群聊授权判定。

---

## 2. 架构：七层设计

### 第 0 层 — Session Scope（隔离的地基）

引入 scope 概念，取值：

- `"global_memory"`：现有三端，行为**完全不变**；
- `"group:<room_id>"`：群聊房间，独立命名空间。

改动点（全部在 `sections/`，改动量小但必须精确）：

| 位置 | 改动 |
| --- | --- |
| `database.py:470` | `get_conversation_messages(scope, limit)` 增加 `WHERE session_id = ?` |
| `services.py:69 / 83` | 兜底值由 `UserDataManager.get('current_chat_id')` 改为 `current_session_scope()` |
| `services.py:754 / 1632` | 同上，scope 解析 |
| `messages.py:1728` | 主对话路径传 scope |
| `idle.py:46` | 空闲提醒传 scope |
| `command_handlers.py:97` | `/export` 传 scope |

`current_session_scope()` 的解析规则：当前 update 带 `xgent_origin`（CLI 已有
`xgent_origin="cli"`，见 `cli_bridge.py:752`）或群聊标记时返回对应 scope，
否则回落 `"global_memory"`。**回落优先，保证零回归。**

### 第 1 层 — Persona（角色卡）

新增表：

```sql
CREATE TABLE IF NOT EXISTS group_personas (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,        -- 群内显示名
    bot_ref       TEXT,                 -- 关联的 bot 配置（token 来源）；虚拟角色为 NULL
    system_prompt TEXT,                 -- 人设正文
    prompt_file   TEXT,                 -- 或指向 prompts/ 下的文件（复用现有人设）
    model_binding TEXT,                 -- "provider::model"；NULL = 全局默认
    avatar        TEXT,                 -- emoji / 头像标识
    speak_policy  TEXT,                 -- JSON：话痨度 / 抢话概率 / 最大句数 / 冷却秒
    enabled       INTEGER DEFAULT 1,
    created_at    REAL
);
```

**人设共享**落在 `prompt_file` 字段：直接指 `prompts/` 下的文件，与全局 Prompt 管理
（`services.py:626` `get_runtime_prompt()`）同一套读写，改一处两边生效。

### 第 2 层 — Room（房间）

```sql
CREATE TABLE IF NOT EXISTS group_rooms (
    id           TEXT PRIMARY KEY,
    title        TEXT,
    mode         TEXT,                  -- 'telegram' | 'virtual'
    chat_id      INTEGER,               -- 真实 TG 群 chat_id；虚拟房间为 NULL
    director_model TEXT,                -- 调度用的模型（建议用便宜模型）
    topic        TEXT,                  -- 场景/话题设定
    state        TEXT,                  -- idle | running | paused
    max_turns    INTEGER DEFAULT 20,    -- 一轮对话最多几条，防刷屏
    turn_interval REAL DEFAULT 2.0,     -- 发言间隔，拟人化
    scope        TEXT,                  -- = 'group:' || id
    created_at   REAL
);

CREATE TABLE IF NOT EXISTS group_members (
    room_id      TEXT,
    persona_id   TEXT,
    prompt_override TEXT,               -- 仅在本房间生效的人设补丁
    mute         INTEGER DEFAULT 0,     -- 闭麦
    joined_at    REAL,
    PRIMARY KEY (room_id, persona_id)
);
```

两种 `mode` 共用同一套引擎，只是**入站来源与出站出口不同**：
- `telegram`：用户建一个真实 TG 群，把 N 个 bot 拉进去；
- `virtual`：不开真实群，在 XGent 自己的 Web 界面里渲染成群聊气泡。

### 第 3 层 — 群聊记忆（两套，这是"记忆不共享"的核心）

```sql
CREATE TABLE IF NOT EXISTS group_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT NOT NULL,
    turn_index  INTEGER,
    speaker_id  TEXT,                   -- persona_id | 'user' | 'system'
    speaker_name TEXT,
    role        TEXT,
    content     TEXT,
    msg_type    TEXT,
    timestamp   REAL,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_group_messages_room ON group_messages(room_id, id);
```

**房间公共 transcript 写 `group_messages`，不写 `global_messages`。**
这是"上下文单独一套系统"的物理保证——不只是靠 WHERE 隔开，而是根本不入同一张表。

第二套（v2，可选）：**角色私有记忆**

```sql
CREATE TABLE IF NOT EXISTS group_persona_memory (
    room_id    TEXT,
    persona_id TEXT,
    summary    TEXT,                    -- 该角色视角的群聊摘要
    updated_at REAL,
    PRIMARY KEY (room_id, persona_id)
);
```

定期把公共 transcript 压缩成**每个角色各自的立场摘要**。这样"记忆不共享"在角色之间
也成立——同一个事件，不同角色记住的东西不一样，群聊才不会变成一人群。

### 第 4 层 — Director（调度器，本方案的灵魂）

职责：**决定下一个谁说话、带着什么意图说，但不代写台词。**

输入：房间 transcript 最近 N 条 + 在场 persona 卡摘要（名字/人设一句话/说话策略）+ 刚发生的事件。

输出：

```json
{"next": "persona_2", "intent": "反驳刚才的观点，但留个台阶", "reason": "上一条点了它的名字"}
```

三种策略（房间级可切换）：

| 策略 | 说明 | 适用 |
| --- | --- | --- |
| `round_robin` | 严格轮询 | MVP、角色数少 |
| `llm_director` | **默认推荐**。用便宜模型读 transcript 输出 next + intent | 正式使用 |
| `reactive` | @ 点名优先 + 关键词命中 + 静默超时随机 | 低消耗 |

被选中的 persona 才真正调用大模型生成台词：

```
system = 该 persona 的 system_prompt + 房间 topic + 该角色的私有记忆摘要
user   = 群聊形态的 transcript（"名字: 内容" 逐条） + 本轮 intent
```

**防失控三道闸**：`max_turns`（一轮上限）、冷却时间（同一角色连说限制）、
`turn_interval`（拟人间隔）。任一触发即停，等下一轮用户输入。

### 第 5 层 — 多 Bot 入站去重（真实 TG 群的关键，最容易被忽略）

N 个 bot 在同一个 TG 群，**同一条用户消息会被 N 个 Application 各收到一次**。
不加处理 = 一条消息触发 N 次回复，且每个 bot 都以为自己该说话。

解决方案：

1. N 个 token → N 个 `Application`，由 `build_group_applications()` 一次性构建，
   `asyncio.gather` 并行 `initialize()/start()/updater.start_polling()`；
2. 所有群消息先落 `GroupEventBus`，用 `(chat_id, message_id)` 做**幂等键**去重
   （内存 LRU + 持久化兜底，先到先得，后到丢弃）；
3. 事件与"哪个 bot 收到的"**彻底解耦**——导演选中 X 后，用 X 对应的那个 bot 实例发言。

另外两个 Telegram 侧的硬前提：
- 每个 bot 必须在 BotFather `/setprivacy` 设为 **DISABLED**，否则收不到群消息；
- 群内禁止 `GroupAnonymousBot` 之类干扰。

### 第 6 层 — 出站

- **真实群**：不复用 `fanout` 的单通道 `telegram` worker——因为每个 persona 只能以
  **自己的 bot 身份**发言。正确做法是**每个 persona-bot 一个轻量 outbox**，
  复用 `fanout.ChannelWorker` 的代码（`fanout.py:254`），按 `telegram-group:<bot_ref>` 注册。
- **虚拟房间**：`WebOutbox`（`web_bridge.py:164`）新增 `group_message` 帧类型，
  前端 `webui/group.html` 渲染群聊气泡。

### 第 7 层 — 接线点清单

新增文件（**强烈建议放 `xgent_app/` 根，不要放 `sections/`**）：

```
xgent_app/group_persona.py     # 角色卡 CRUD
xgent_app/group_room.py        # 房间与成员管理
xgent_app/group_memory.py      # 群聊 transcript + 角色私有记忆
xgent_app/group_director.py    # 调度器（三种策略）
xgent_app/group_engine.py      # 一轮对话的执行循环
xgent_app/group_bridge.py      # 入站去重 + 出站分发
```

> **为什么不放 `sections/`**：`sections/` 由 `bootstrap.py:110` 按 `MANIFEST.txt`
> 顺序 `exec` 进同一命名空间，且受 `SOURCE_BASELINE.sha256` 校验
> （`tools/check_split_integrity.py:22`）。任何正文改动或 MANIFEST 变动都会让
> 完整性检查和 `tests/test_split_integrity.py` 变红。放 `xgent_app/` 根**完全不受基线约束**。

必须改的既有文件：

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `sections/database.py` | 470 | `get_conversation_messages` 加 `WHERE session_id = ?` |
| `sections/database.py` | `_init_db` | 建 5 张新表 + session_id 回填迁移 |
| `sections/services.py` | 69 / 83 / 754 / 1632 | scope 解析 |
| `sections/messages.py` | 1728 | 传 scope |
| `sections/idle.py` | 46 | 传 scope |
| `sections/command_handlers.py` | 97 | 传 scope |
| `sections/core.py` | 101 | 多 token 配置（`BotConfig.GROUP_TOKENS`） |
| `sections/runtime.py` | 196 / 521 / 550 | 多 Application 构建与并行监督 |
| `sections/ui.py` | 1161 之外 | 群聊授权分支（chat_id 白名单） |
| `sections/lifecycle.py` | 8-31 | `/group` 命令描述 |
| `sections/runtime.py` | 233-258 | `/group` CommandHandler |
| `sections/commands.py` | — | `cmd_group` |
| `sections/callbacks.py` | 巨型 if/elif 链 | 追加 `grp_` 前缀子路由（**无注册机制，只能追加分支**） |
| `sections/idle.py` | 908-932 | Web 命令表补 `/group`（CLI 自动继承） |
| `web_server.py` | 333-405 | `/group` 页面 + `/api/group/*` |
| `webui/group.html` | 新增 | 群聊界面 |
| `install.sh` | 2828 / 2725+ | 第 4 个组件 `group` |
| `tools/check_split_integrity.py` | — | 改完 sections 后 `--write` 刷新基线 |

---

## 3. 分阶段落地计划

| 阶段 | 交付 | 风险 | 预估 |
| --- | --- | --- | --- |
| **P0 沙盘** | `virtual` 房间 + Web 界面 + Persona 管理 + `round_robin` 调度。验证隔离与调度链路，**不碰 Telegram** | 低（不改 sections 启动层） | 2 天 |
| **P1 真实群** | 多 token / 多 Application / 入站去重 / 群授权 / `telegram` 模式 | 中（动 `runtime.py` 启动矩阵，有 `test_startup_matrix.py` 回归风险） | 3 天 |
| **P2 智能调度** | `llm_director`、角色私有记忆压缩、话题设定、@ 点名 | 中（成本与延迟） | 3 天 |
| **P3 打磨** | 打字延迟/撤回/复读等拟人细节、群聊记录导出、token 统计按 persona 分组 | 低 | 2 天 |

**建议从 P0 起步**：P0 能把"隔离是否真的隔离""调度是否自然"这两个核心问题验证完，
且不触碰任何启动层代码。P1 的多 Application 是本项目从未有过的形态，
应该等引擎跑通再动。

---

## 4. 已识别的风险

1. **上下文污染（最严重）**：`get_conversation_messages` 无 WHERE 是历史遗留，
   改它对现有三端是**行为变更**。必须配 `session_id` 回填迁移 + 回归测试，
   否则会出现"升级后 AI 失忆"。
2. **多 Bot 消息风暴**：去重失效的后果是一条消息触发 N 次回复。
   幂等键必须在**事件进入总线之前**判定，不能放在调度之后。
3. **成本放大**：一轮群聊 N 个角色 = N 次模型调用（外加导演 1 次）。
   需要房间级预算上限与按 persona 的 token 统计（`token_usage_stats` 表需加 persona 维度）。
4. **启动矩阵回归**：`tests/test_startup_matrix.py` 有 6 个子进程探针钉住
   "一端挂了别端照跑"。新增 Application 必须保证某个群 bot token 失效时
   主 bot 与 Web 不受影响。
5. **完整性基线**：只要动了 `sections/`，`check_split_integrity.py` 和
   `test_split_integrity.py` 就会红，需 `python tools/check_split_integrity.py --write` 刷新。
6. **群里的真人**：真实 TG 群若混入他人，其消息会进入 transcript。
   建议默认**只接受授权用户的消息**，其他人消息直接忽略而不是报错。

---

## 5. 待确认的两个分歧点

**A. 群聊载体**：真实 Telegram 群（多 bot token，P1 才能用）vs XGent 内置虚拟房间
（P0 即可用，界面里渲染成群聊）。两者共用引擎，但前者要动启动层。

**B. "我部署过的电报机器人"的范围**：
- 同一台 XGent 实例上登记多个 bot token（推荐，实现简单，共享一切）；
- 还是跨实例/跨服务器的多个独立 XGent 部署（需要联邦总线：HTTP webhook 或中央 hub，
  复杂度高一个数量级）。

---

## 附：为什么"人设 / Skill / 配置共享"是免费的

这三者都是**进程级全局单例**，新模式在同一个进程里天然继承：

| 资源 | 加载位置 | 性质 |
| --- | --- | --- |
| 模型提供商 | `UserDataManager.reload_providers()` — `database.py:1463` | 类级缓存，全局 |
| Prompt 文件 | `core.py:1199` `PROMPTS_DIR`，`services.py:626` 读取 | 按 `__file__` 定位，全局 |
| Skill 索引 | `services.py:901` `list_skill_files()`，每次扫盘 | 全局目录，全局 |
| 数据库 | `BotMemoryDB._instance` 单例 — `database.py:51` | 全局 |

所以"共享"这一条**不需要任何设计**，它是现状的默认结果。真正要花力气的是"不共享"。
