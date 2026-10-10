# 工作台功能改进与来源定位

## 使用变化
- 左下角「设置」打开固定小菜单，任务、文件与输出、模型与技能、用量、归档、查找、偏好与安全、退出统一收纳；点击外部或 Esc 关闭。
- 顶部直接显示主题切换和终端，会话名靠左；输入区为单个圆角卡片，上层写消息、下层放工具。桌面模型／思考／Agent 均在框内同一行，手机将 Agent 与思考收进框内生成选项菜单，不再额外占两排。
- 对话正文只调整阅读排版。原消息内容、消息头、Token、Agent 轮次保留，代码与命令块明暗主题均用深色。Bot 当前标记为蓝色。
- 任务和文件列表左侧显示来源对话链接；有消息记录时加载对应历史页、定位并高亮。归档来源只读打开，返回后保留管理页筛选。没有持久消息关联的旧记录明确标注，不伪造消息位置。
- 文件／命令输出支持来源对话、关键词、类型或执行状态、文件状态、日期和排序；筛选在分页前完成，每页 50 项。
- 「查看完整输出」与「收起完整输出」使用同一入口，分页替换当前内容；收起中止未完成请求，不追加嵌套日志面板。
- 提供商增加模型／名称搜索；技能与记忆增加排序和清除筛选；用量价格与合并规则改为逐项表单；Agent 设置可直接编辑黑名单，系统详细诊断可折叠。

## 来源接口
任务与文件资源的 `source` 包含会话 ID、名称、归档标记及可用的 `message_key`。来源从已有记录和任务运行关联中只读解析，不修改聊天记录。链接格式：`#/chat?conversation=…&message=history:…`。
新增 `/api/workbench/settings/blacklist` 读写接口，用已有黑名单管理器保存，使用版本校验避免覆盖其他位置的更改，清空需确认。

## 测试
`tests/test_workbench_sources.py` 验证跨会话筛选、准确来源、分页、归档只读和消息内容不变。
`tests/test_workbench_rebuild_browser.py` 验证左下菜单、顶部按钮、来源跳转和返回、文件预览、Token／轮次、输出收起、黑名单和价格表单。
`tests/test_composer_compact.py` 验证单框高度、所有工具在框内、模型／思考实际保存、手机选项、键盘操作、失败反馈与草稿保留。
输入区截图：`python tools/composer_smoke.py --output workspace/composer-compact-qa`。
运行：`python -m pytest -o addopts= -n 4 --dist loadfile -q`。
截图：`python tools/workbench_smoke.py --output workspace/workbench-rebuild-qa`。夹具只使用临时数据库和占位配置，不启动模型、Telegram 或任务执行器。
