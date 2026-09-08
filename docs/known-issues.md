# 已知问题

## bot 流式输出中无声无息退出（待抓证据）

### 现象
- bot 在流式输出过程中偶尔突然退出，进行中的「流式输出中...」占位消息卡住没人收尾，pm2 重启后自动发主菜单（看起来像「自己 /start」）。
- 很偶然，从项目开始就有，偶尔第一条回复就死。
- pm2 `restarts` 累积增长，`max memory restart` 设了 1G（cloudcone 2G 机器）。

### 已排查的结论（2026-09-06）
1. **不是代码主动异常退出**：pm2 err 日志为空，`main.py` 的 `except Exception` 没打出 Fatal Error。
2. **pm2 out 日志曾抓到一次崩溃栈**：`httpcore.ConnectTimeout` → `telegram.error.TimedOut`，抛在 PTB 的 `get_me()` 里（`_bootstrap_initialize` → `initialize` → `get_me`）。
3. **代码里理论上能接住**：`telegram_supervisor`（`runtime.py:296`）+ `retry_forever`（`runtime.py:160`）双层 `except Exception` 都该接住 `TimedOut`（它是 `Exception` 子类）。但 traceback 显示它穿透了。
4. **最可能的漏网点**：游离 task（`asyncio.create_task`）抛的未捕获异常。PTB 的 `add_error_handler`（`global_error_handler`）只接 handler 协程链里的异常，接不住 `create_task` 起的后台任务抛的。这类异常走 asyncio 默认处理（打日志 + 丢弃），在特定时序下可能穿透导致进程崩。这跟「很偶然、偶尔第一条回复就死」的特征吻合。
5. **内存也有嫌疑但未定论**：pm2 设了 1G 上限，回灌给模型的图 base64（上限 8MB）留在对话历史里不清理，多轮带图累积可能突破 1G。但用户反馈「第一条回复也会死」，不完全符合长期累积特征——可能内存重启和 ConnectTimeout 崩溃两种混在一起。

### 已做的（commit 39c86de）
加了崩溃捕获器，抓三类死因写进 `xgent_crash.log`：
- `sys.excepthook`：同步未捕获异常（含 `BaseException` 子类）
- `loop.set_exception_handler`：游离 task 异常（PTB 的 `add_error_handler` 接不到这类，这是最可能的漏网点）
- `main.py except Exception` 兜底记录

注册在 `run_app` 开头（事件循环一拿到就装 asyncio handler），`main.py` 模块层装 `sys.excepthook`。

### 下次死了该做什么
1. 第一时间跑（别先做别的，crash log 可能被覆盖）：
   ```
   cat /home/hanling/xgent-telegram/xgent_crash.log
   ```
2. 把完整内容贴回来，根据栈定位真实死因，再修。
3. 如果 `xgent_crash.log` 不存在或为空：说明崩溃捕获器没生效（代码没更新到 VPS，或死因是信号/OOM 不走 Python 异常路径），改查 `dmesg -T | grep -i killed` 和 `pm2 logs`。

### VPS 部署注意
VPS 路径是 `/home/hanling/xgent-telegram`（无 `-test`），**不是 git 仓库**，更新代码要手动 curl：
```bash
cd /home/hanling/xgent-telegram
curl -sL https://raw.githubusercontent.com/HANLINGVABCN/xgent-telegram-test/main/xgent_app/sections/main.py -o xgent_app/sections/main.py
curl -sL https://raw.githubusercontent.com/HANLINGVABCN/xgent-telegram-test/main/xgent_app/sections/runtime.py -o xgent_app/sections/runtime.py
pm2 restart xgent-telegram
```
崩溃捕获器没更新到 VPS 之前，`xgent_crash.log` 不会生成。
