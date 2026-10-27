# 项目内数据、备份与恢复

XGent 使用单目录模式：源码和运行数据都在当前项目目录中，但导入、更新和备份会按白名单区分“程序文件”和“实例数据”。不再读取 `XGENT_DATA_DIR`，旧的 `data-location.json` 也不会改变数据位置。

```text
xgent-telegram/
  .env
  xgent_memory.db
  memory/
  skill-private/
  workspace/
  server_assets/
  xgent_storage/
    uploads/
    generated_media/
    exports/
    attachment_versions/
    command_outputs/
  backup-schedule.json
  skill-public/              # 随 GitHub 代码更新，不作为私有数据恢复
  xgent_app/                 # 程序源码
  venv/                      # 本机依赖，不进入备份

../xgent-backups/            # 默认备份目录，位于项目之外，可自定义
```

相对路径统一以项目根目录为基准。AI 使用 `skill-public/`、`skill-private/`、`memory/`、`workspace/` 和 `xgent_storage/` 时不需要猜外部数据目录。

## 安装脚本中的数据功能

运行 `./install.sh`：

- `12) 数据迁移`：子选项为“导入”和“导出”。
- `13) 定时导出备份`：设置服务器备份目录、开关、频率、覆盖/累计模式、保留份数，以及是否包含完整命令回执。

也可以直接运行 `./install.sh data` 或 `./install.sh backup` 打开相同菜单。菜单会自动识别项目中的新旧数据库名称，用户不需要填写版本或数据目录。

## 无感导出

手动菜单导出、Web 导出和定时备份都不会停止或重启服务，也不会等待 AI 或命令执行结束。SQLite 使用在线一致性快照；普通文件在打包前后校验大小和修改时间。如果文件持续变化，当前备份失败且不发布半成品，原有备份不受影响。

默认备份保存到项目同级的 `xgent-backups/`，也可在安装脚本或 Web 的“数据与备份”页面填写其他服务器绝对路径。备份目录必须位于项目之外。基础包包括：

- `xgent_memory.db`；
- `.env` 原文；
- `memory/`、`skill-private/`、`workspace/`、`server_assets/`；
- 上传原件、附件版本、生成媒体和对话导出；
- `backup-schedule.json`（存在时）；
- `path-map.json`（用于恢复旧历史中的绝对附件路径）。

默认不包含 `xgent_storage/command_outputs/`。排除它不影响历史消息、调用摘要和聊天上下文，但备份无法恢复历史命令的完整原始终端输出。菜单或 Web 复选框明确开启后才会加入。

始终排除日志、`venv/`、`.venv/`、`__pycache__/`、构建缓存、PID、锁文件和临时文件。`skill-public/` 属于 GitHub 发布内容，不进入实例备份；恢复后继续使用当前代码版本自带的公共技能。

备份包含真实 Bot/API 密钥。包内 `manifest.json` 记录每个文件的大小、SHA-256、权限、生成时间和是否包含命令回执。归档校验成功后才会原子发布。

## 定时备份

在安装脚本选择 `13) 定时导出备份`：

1. 选择开启或关闭；
2. 输入频率，例如 `30m`、`6h`、`1d`；最低 1 分钟；
3. 选择保存方式：
   - 覆盖上一份：始终使用 `scheduled-latest.zip`，新包完整校验后原子替换旧包；
   - 累计保留：每次生成独立的 `scheduled-时间-编号.zip`，并设置最多保留份数；超过后自动删除最老的定时累计备份；
4. 选择是否包含 `command_outputs/`。

修改配置后运行中的服务会自动读取，通常 15 秒内生效。关闭定时备份不会删除已有 ZIP。数量清理只匹配 `scheduled-*.zip`，不会删除手动导出的 ZIP、导入前备份、回退目录或 `scheduled-latest.zip`。

## 导入恢复

在安装脚本选择 `12) 数据迁移` → `1) 导入`，选择 ZIP 或旧版项目数据目录。Web 可以从服务器备份列表直接选择，也可以上传本地 ZIP。目标固定为当前项目，无需选择 `XGENT_DATA_DIR`。

恢复流程会：

1. 在不触碰当前数据的情况下检查 ZIP 路径、清单、逐文件 SHA-256 和 SQLite 完整性；
2. 确认后短暂停止服务；
3. 在服务器备份目录创建 `pre-import-时间-编号.zip`，并显示在 Web 备份列表；
4. 只替换数据白名单中的项目，保留源码、`skill-public/`、`venv/` 和安装脚本；
5. 启动并检查新数据；失败时使用项目 `.install-state/` 内部原子回退副本恢复原数据。

导入是整套数据替换，不与当前数据库或附件混合。恢复包里的密码、Bot 和模型配置会成为当前配置。Web 备份列表支持下载、选择恢复和永久删除，但删除范围严格限制在当前配置的服务器备份目录内。

导出完全无感；导入必须短暂停服，因为不能让正在运行的进程继续写入已经替换的数据库和附件目录。

## 更新与旧版本兼容

安装脚本更新代码时会保留项目内的数据库、`.env`、`memory/`、`skill-private/`、`workspace/`、`server_assets/`、`xgent_storage/` 和定时备份配置。备份文件位于项目之外，代码更新不会触碰。

旧版 `bot_memory.db`、`bot_storage/`、旧目录备份和旧 ZIP 可由导入/导出菜单自动识别并转换为当前备份结构。旧 `.env` 中的 `XGENT_DATA_DIR` 和旧 `data-location.json` 仅被忽略，不会把新进程指向其他目录。

## Web 展示约定

AI 回复保存生成时的实际提供商、模型与详细时间；旧记录不猜测模型。附件和输出详情显示完整物理路径。对话只能属于一个分组，置顶只在所属分组生效；来源定位保留完整历史、草稿和附件，关闭定位条不会刷新聊天。
