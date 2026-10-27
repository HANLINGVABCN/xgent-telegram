# 外部数据目录与实例备份

XGent 的源码、公开技能、提示词模板和 Python 依赖属于代码；数据库、配置、用户文件、长期记忆和私有脚本属于实例数据。

新安装默认使用代码目录同级的 `xgent-data/`。也可在服务启动环境中设置绝对路径 `XGENT_DATA_DIR=/home/hanling/xgent-data`。代码内已有数据的旧安装不会在更新时自动搬迁，也不会静默新建空库；应在维护窗口显式迁移。

```text
xgent-data/
  .env
  xgent_memory.db
  memory/
  skill-private/
  workspace/
  server_assets/vps-manager/servers.json
  xgent_storage/{uploads,generated_media,exports,attachment_versions,command_outputs}/
  logs/
```

## 迁移现有实例（推荐：只用菜单）

你不需要自己填写 `XGENT_DATA_DIR`，也不需要手动复制数据库。进入原项目目录后运行：

```sh
bash install.sh
```

在主菜单选择 `12) 数据迁移`，再选择：

- `1) 导入`：选择 ZIP 备份或旧版数据目录，再选择数据保存目录；如果系统已经配置过数据目录，会先显示旧配置并询问是否修改。
- `2) 导出`：自动查找当前实例，无论数据库叫 `xgent_memory.db` 还是旧版 `bot_memory.db`，自动创建已校验的备份。

菜单会自动检查服务、停止和恢复对应服务、保留原目录、处理 SQLite/WAL、校验备份并在失败时回退。你不需要手动运行下面的高级命令；它们只给自动化维护使用。

只更新了安装脚本、旧程序还没有迁移工具时，导出会自动下载隔离的兼容工具，不覆盖旧程序。导入到另一台机器时，目标需先用菜单安装/完整更新准备好当前程序；脚本会自动检查，避免拿不支持外部目录的旧程序启动空库。只有对话文本、没有数据库的旧“导出记忆”包不能冒充整套实例恢复。

```sh
xgent data migrate /home/hanling/xgent-data --dry-run
bash install.sh stop
xgent data migrate /home/hanling/xgent-data
bash install.sh start
```

`bash install.sh data ...` 是相同维护命令的等价入口。数据库被服务或其他 CLI 打开时，迁移/整套恢复会拒绝执行；先退出这些进程，再重试。不要强制删除锁文件。目标必须位于代码之外，已存在的目录不会被合并或覆盖。

迁移先复制到同级 staging、计算文件 SHA-256、对 SQLite 做包含 WAL 数据的一致性快照并校验，最后切换路径标记。源目录不会删除。`data-location.json` 是代码旁的路径指针；正式托管服务仍建议显式配置 `XGENT_DATA_DIR`。历史消息原文不改写，受验证的旧附件/回执/工作区引用通过 `path-map.json` 解析到新目录。

迁移与导出的过滤规则不同：迁移保留原始状态文件（包括用户上传的 `.log`）；备份按下述白名单/排除规则过滤。日志和本地 Bot API 缓存移到外部目录，但不进入普通便携备份。迁移本地 Bot API 后需让容器卷挂载指向新的 `.local-api-data`，不要继续让容器写旧目录。

## 日常备份

Web：设置 → 数据与备份。点击“创建核心数据备份”，确认包含密钥后开始。打包与校验在后台进行，显示实际进度、下载链接和完整路径；不会调用 AI 或同步 Telegram。

CLI（离线/维护场景）：

```sh
xgent data export --yes
xgent data export --yes --with-command-outputs
xgent data export --yes --output /secure-backups/xgent.zip
xgent data verify /secure-backups/xgent.zip
```

基础包包括整库（对话、Token 统计、模型渠道、队列等）、`.env` 原文、`memory/`、`skill-private/`、`workspace/`、服务器资产、上传原件、附件版本、生成媒体和对话导出。默认输出到数据根目录同级的 `xgent-backups/`，不会把备份自身递归打包；不会覆盖同名文件。

默认不含 `command_outputs/`。排除它**不会删除数据库里的历史聊天、调用摘要和已有上下文**，但无法从这份备份恢复历史命令的完整原始终端输出文件；“查看完整输出”会提示原件缺失。`--with-command-outputs`（别名 `--include-outputs`）或 Web 独立复选框才会加入。体积和耗时按实际文件计算，没有固定的 150 MB/2.5 GB 假设。

始终排除 `.log` 及滚动日志、虚拟环境、字节码、构建缓存和临时锁/PID。包内 `manifest.json` 包含格式版本、时间、逐文件大小/SHA-256、权限和 command outputs 选项。归档、`.env` 和数据库受敏感文件权限保护；Windows 使用 owner/System ACL。**这些包含真实 Bot/API 密钥，不能公开分享或提交 GitHub。** 服务器配置里引用的、位于数据目录外的 SSH 私钥不会被擅自收集；需要便携恢复时由管理员事先将它们纳入 `server_assets/` 或另行妥善备份。

Web 在线备份遇到正在执行的 AI/命令时拒绝开始，不能为备份杀掉命令。备份期间锁定数据库写入；若文件在打包期间发生变化则报失败，不发布半成品包。

## 整套恢复

```sh
bash install.sh stop
xgent data import /secure-backups/xgent.zip --yes
bash install.sh start
```

恢复不是合并：只允许清单内的原生目录布局，拒绝路径穿越、重复/越界路径、符号链接/设备文件和损坏 ZIP；展开前检查磁盘空间，展开时逐文件验 SHA-256，随后验 SQLite 和应用表结构。

先解压到目标目录旁的 staging，验证通过后才切换。当前目录保留为 `*.rollback-时间戳-*`；切换失败会恢复原目录。Web 支持先上传和校验，再单独确认切换；Linux 托管部署通过现有 `install.sh` 停止/启动服务，启动健康检查失败会尝试回退。没有可安全自动重启的启动器（包括 Windows 直接运行）会明确提示使用上述离线 CLI，不会在运行中硬换数据库。

恢复后的密码、端口、Bot 和模型配置以备份为准，可能需要使用新地址和备份中的密码重新登录。旧进程遗留的待发用户消息仍沿用队列的人工恢复保护，不应手动重跑上一轮已执行命令。

## Web 展示约定

AI 回复按生成时的实际提供商/模型与详细时间保存，显示在 XGent 作者标识下方；旧记录不猜测模型。复制/引用/朗读操作留在各自气泡内部。文件页面和输出详情提供完整路径及复制按钮，聊天中的路径以小字号辅助信息显示。

对话只能属于一个分组；组内置顶只在该分组内生效，“全部对话”和搜索忽略置顶排序。移组/删组会清除原组置顶，删除组不会删除对话。来源定位返回前后双向游标，并保留普通聊天渲染、草稿和附件；关闭定位条不刷新页面。
