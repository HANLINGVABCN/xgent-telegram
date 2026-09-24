```!
trigger 后台任务命令完整文档。用 run-x 调 `trigger` 命令实现延迟/定时/周期/条件监控任务，不是独立协议。命令无时长限制、按有界并发调度。需要 --when 条件表达式语法、cron/at 格式细节时 read 本文件。
```

# trigger 后台任务命令

后台任务调度已从「trigger-x 协议」改为普通命令行工具：用 `run-x` 调 `trigger` 命令。
AI 不再需要学 YAML 协议格式，只写熟悉的 shell。执行引擎（调度、条件匹配、并发闸门、
投递、重启恢复）不变，只是输入层换成了 CLI。

调用方式：在 `run-x` 块里写 `trigger <子命令> ...`：

```run-x
<<BEGIN_run_trigger_a1b2
trigger add --cmd "systemctl status nginx" --after 30s --task "检查 nginx"
<<END_run_trigger_a1b2
```

## 子命令

| 子命令 | 作用 |
|--------|------|
| `trigger add --cmd "<命令>" [调度] [条件]` | 登记一个后台任务 |
| `trigger show` | 列出活跃任务 |
| `trigger kill <任务ID>` | 取消指定任务 |
| `trigger kill-all` | 取消全部任务 |

## `trigger add` 参数

- `--cmd "<命令>"`：**必填**，要执行的 shell 命令。非交互式，禁止后台化（`&`/`nohup`）、密码明文、TUI 程序。
- `--task "<概述>"`：一句话任务概述（省略则自动取命令前 80 字）。
- **调度（三者互斥，省略即立即执行）**：
  - `--after 30s`：延迟执行。单位 `s/m/h/d/w`（如 `30s`/`15m`/`2h`/`1d`/`1w`）。
  - `--at "2026-12-31 23:59:59"`：定时执行（ISO 8601）。
  - `--cron "0 * * * *"`：周期执行（5 字段标准 cron），语义是「每隔固定周期重跑一次」。
- **条件（可选）**：
  - `--when "<条件表达式>"`：监控命令输出，满足即终止进程并投递。
  - `--repeat`：条件命中并投递后自动重启监控（**必须配合 `--when`**；自动去重防消息风暴）。
- `--tz Asia/Shanghai`：时区（IANA 名，默认服务端配置）。

## 运行时长与并发

**命令没有时间上限**：`tail -f`、长时数据迁移、持续监控都能真·长跑常驻，不会被超时杀掉。
- 一个长跑任务只占自己的槽（同一任务上次没跑完，本次调度自动跳过），不影响别的任务。
- 不同任务之间不设并发上限（个人 bot 场景任务数有限，够用）；若确需同时挂大量常驻进程，请自行控制任务数量。

## 智能条件表达式（`--when` 的值）

| 语法 | 示例 | 说明 |
|------|------|------|
| 裸词字面量 | `READY` | 自动匹配 "READY" |
| 引号字符串 | `"error timeout"` | 包含空格的字面量（注意 shell 引号嵌套） |
| 正则表达式 | `/error.*/i` | 斜杠包裹，支持 i/m/s 标志 |
| 逻辑与 | `READY AND SUCCESS` | 两个条件都满足 |
| 逻辑或 | `READY OR STARTED` | 任一条件满足 |
| 括号分组 | `(A OR B) AND C` | 控制优先级 |

**匹配机制**：增量匹配，条件在输出流中累积满足，满足后发送 SIGTERM 终止进程。

## 常用场景

延迟检查：
```run-x
<<BEGIN_run_trig_delay_c3d4
trigger add --cmd "systemctl status nginx && curl -I http://localhost" --after 30s --task "30秒后查服务"
<<END_run_trig_delay_c3d4
```

等待条件（条件出现前一直等，无时长限制）：
```run-x
<<BEGIN_run_trig_wait_e5f6
trigger add --cmd "tail -f /var/log/app.log" --when READY --task "等应用启动"
<<END_run_trig_wait_e5f6
```

定时备份：
```run-x
<<BEGIN_run_trig_cron_a7b8
trigger add --cmd "mysqldump -u backup mydb > /backups/db.sql" --cron "0 2 * * *" --tz Asia/Shanghai --task "每天凌晨备份"
<<END_run_trig_cron_a7b8
```

持续监控（直接长跑，命中即投递并自动重启）：
```run-x
<<BEGIN_run_trig_mon_c9d0
trigger add --cmd "tail -f /var/log/app.log" --when "/ERROR|FATAL/i" --repeat --task "持续监控错误"
<<END_run_trig_mon_c9d0
```

## 生成注意事项

1. **`--cmd` 的引号**：命令含空格/特殊字符时整体用引号包住；命令内部再用引号时注意 shell 转义（可用单引号包 `--cmd`，内部用双引号）。
2. **调度三选一**：`--after`/`--at`/`--cron` 只能给一个。
3. **`--repeat` 必配 `--when`**：否则报错。
4. **持续监控直接 `tail -f` + `--repeat`**：命令无时长限制，命中后自动重启。
5. **条件优先正则**：`--when "/ERROR|FATAL/i"` 比 `--when "ERROR OR error OR ..."` 简洁。
6. **命令禁止交互和后台化**：vim/nano/nohup/`&` 都禁止——trigger 自己管理进程生命周期。
7. **登记成功 ≠ 完成**：`trigger add` 只表示已调度，实际结果稍后经 `[后台任务结果]` 系统消息投递回对话。

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| `缺少必填字段 "command"` | 没给 `--cmd` | 加 `--cmd "..."` |
| `只能使用一个时间字段` | `--after`/`--at`/`--cron` 给了多个 | 只留一个 |
| `repeat 为 true 时必须同时设置 condition.when` | `--repeat` 无 `--when` | 加 `--when "..."` |
| `未知时区` | `--tz` 值非法 | 用 IANA 标准时区名 |
| `condition.when 语法错误` | 条件表达式语法错误 | 检查引号/斜杠/括号 |
| `缺少对话上下文` | 不是在对话中经 run-x 调用 | trigger 命令只能由 AI 在对话里调 |

## 去重机制（`--repeat`）

连续相同结果（按 status/trigger_reason/matched_conditions/exit_code/output 的 sha256 签名）
指数退避：5s → 10s → 20s → 40s → 80s → 160s → 300s（上限），防消息风暴。
