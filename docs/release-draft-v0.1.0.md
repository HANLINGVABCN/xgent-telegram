# v0.1.0 — First Public Preview（Release 草稿）

> 这是可直接粘贴到 GitHub Release 的草稿。创建 `v0.1.0` 标签前，请先确认安装、更新和回滚流程。

## 把你的 Linux 服务器装进 Telegram

XGent for Telegram 是一个面向单用户私有部署的 AI Agent。它不仅能完成多模型对话，还可以在授权服务器上执行命令、管理 Shell 会话、处理文件并通过 Skill 扩展工作流。

## 主要能力

- OpenAI、OpenAI 兼容接口、Gemini、Vertex 和 Claude 提供商
- 独立的默认对话模型与默认媒体模型
- SQLite 全局记忆与可配置记忆深度
- 流式回复以及文件、图片、贴纸上下文
- 一次性命令与长驻/交互式 Shell 会话
- 服务器文件读取、写入和 Telegram 文件发送
- Prompt 与 Skill 文件化管理
- 提供商配置 JSON 导入导出
- Telegram 内在线更新与重启
- 单用户 Telegram ID 访问控制

## 安装

准备一台安装了 Python 3.10+ 的 Linux 服务器，然后执行：

```bash
cd /opt && sudo git clone https://github.com/HANLINGVABCN/xgent-telegram.git && sudo chown -R "$USER":"$USER" xgent-telegram && cd xgent-telegram && chmod +x install.sh && ./install.sh
```

首次安装需要：

- Telegram Bot Token
- Telegram 用户 ID
- 至少一个模型 API Key

完整说明见仓库 README。

## 安全提醒

Agent 模式能够执行真实服务器命令，当前版本不是完整沙箱。

- 请使用独立低权限 Linux 用户运行；
- 不要直接授予不必要的 `root`、Docker Socket 或敏感目录权限；
- 默认关闭 Agent，确认权限和模型配置后再开启；
- 外部网页、文件及转发消息可能携带提示词注入内容；
- 命令黑名单只能降低风险，不能替代操作系统级隔离。

## 已知限制

- 当前面向单用户使用，不提供多租户权限系统；
- 需要自行准备服务器、Telegram Bot 和模型 API Key；
- Agent 工具在宿主系统权限范围内执行，不提供强隔离沙箱；
- 不同模型对工具协议的遵循能力可能存在差异；
- 主分支仍可能快速迭代，更新前建议备份自定义 Prompt、Skill 和数据库。

## 更新

已安装实例可在 Telegram 中执行：

```text
/update
```

更新时可选择保留或覆盖自定义 `prompts/` 与 `skill/`；覆盖前会自动创建备份。

## 验证

本版本发布前建议执行：

```bash
python tools/check_split_integrity.py
python -m unittest discover -s tests -v
bash -n install.sh
```

---

如果这个项目对你有帮助，欢迎 Star、提交 Issue，或者分享你的实际使用场景。
