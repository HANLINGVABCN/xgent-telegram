# 项目发布素材

这里集中保存 GitHub 仓库设置、演示录制和社区发布时可直接复制的文案。

## GitHub About

### Description（推荐）

```text
把 Linux 服务器装进 Telegram：一个支持多模型、记忆、文件处理与真实 Shell 工具执行的私有 AI Agent。
```

### English Description

```text
A self-hosted private AI agent that lets you operate your Linux server from Telegram with multi-model chat, memory, files, and real shell tools.
```

### Topics

```text
telegram-bot
ai-agent
self-hosted
llm
python
openai
claude
gemini
linux
server-management
devops
personal-assistant
```

### Website

暂时没有独立网站时，可以留空，或填写仓库 README/文档站地址。不要为了填满字段而使用无内容的页面。

## Social Preview 设计稿

建议尺寸：`1280 × 640`。

主标题：

```text
把你的 Linux 服务器装进 Telegram
```

副标题：

```text
Self-hosted · Multi-model · Memory · Files · Real Shell Agent
```

画面建议：左侧使用 Telegram 对话界面，右侧使用终端执行结果，中间以箭头连接。避免堆砌功能列表；移动端缩略图中应仍能看清主标题。

## 30 秒演示脚本

录屏前准备一台无敏感数据的演示服务器，并隐藏 Token、API Key、IP 和用户名。

1. **0～3 秒**：展示 Telegram Bot 主菜单。
2. **3～8 秒**：发送“检查磁盘和内存状态，找出最大的日志目录”。
3. **8～18 秒**：展示 Agent 执行状态和命令结果陆续返回。
4. **18～24 秒**：让 Agent 生成 `server-report.md`。
5. **24～28 秒**：展示报告文件被直接发送到 Telegram。
6. **28～30 秒**：结束画面显示“Your Linux server, inside Telegram.”和仓库名。

建议同时导出：

- README 使用的短 GIF：15～20 秒、尽量小于 10 MB；
- 社区发布的视频：30～45 秒、MP4；
- 3 张静态截图：主菜单、Agent 执行、文件发送。

## 社区发布长文案

### 标题

```text
我做了一个能在 Telegram 里直接操作自己 Linux 服务器的私人 AI Agent
```

### 正文

```text
最近做了一个自托管的 XGent for Telegram。

它不只是把模型 API 接到 Telegram：开启 Agent 后，可以在自己的服务器上执行命令、查看持续日志、读写文件，并把服务器文件直接发回 Telegram。

目前支持：
- OpenAI / Claude / Gemini / Vertex / OpenAI 兼容接口
- SQLite 全局记忆
- 文件、图片和贴纸上下文
- 一次性命令与长驻 Shell 会话
- 对话模型和媒体模型分别配置
- Skill 扩展
- 提供商配置导入导出

项目面向单用户私有部署，需要自己的 Linux 服务器、Telegram Bot Token 和模型 API Key。Agent 不是完整沙箱，建议使用独立低权限用户运行。

如果你平时会维护 VPS、家庭服务器或者开发机，欢迎试用并告诉我最想让它完成什么任务。
```

## 社区发布短文案

```text
做了一个自托管 Telegram 私人 AI Agent：支持多模型和长期记忆，还能真实执行 Shell、管理长驻任务、处理服务器文件并直接发回 Telegram。适合 VPS / 家庭服务器 / 开发机场景。Agent 非完整沙箱，建议低权限隔离运行。
```

## 发布前清单

- [ ] GitHub About 填写 Description
- [ ] 添加 Topics
- [ ] 上传 Social Preview
- [ ] README 顶部放真实 GIF 或截图
- [ ] 创建 `v0.1.0` 标签和 Release
- [ ] 确认 GitHub Actions 全部通过
- [ ] 在一台干净 Linux 服务器验证安装
- [ ] 验证 `/update` 与回滚/备份路径
- [ ] 检查录屏中没有 Token、Key、IP 或个人信息
- [ ] 开启 Issues，并准备一个用于收集安装问题的 Issue

## 首批用户反馈问题

不要只问“好不好用”，优先问：

1. 你在哪一步差点放弃安装？
2. README 第一屏让你以为它是做什么的？
3. 你最想通过 Telegram 让服务器完成哪三件事？
4. 哪个权限或安全问题最让你担心？
5. 如果明天不能再用，你最舍不得哪个能力？
