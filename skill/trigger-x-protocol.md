---
name: trigger-x
description: trigger-x 后台任务调度协议，支持延迟/定时/条件触发，YAML 格式，1 小时超时限制
tags: [protocol, scheduling, yaml, automation]
---

# trigger-x 协议技术文档

trigger-x 是后台任务调度协议，支持延迟执行、定时任务、条件触发和持续监控。使用 YAML 结构化格式，专为 AI 理解和生成优化。

## ⚠️ 核心限制

**1 小时硬性超时**：所有命令运行 3600 秒后强制终止（SIGTERM → SIGKILL）
- 定时周期任务（cron）：不受影响，每次执行独立预算
- 持续监控（tail -f）：必须配合 `cron` + `timeout` 规避
- 长时间任务（> 1h）：使用 `shell-x` 或拆分为子任务

## YAML 格式速查

```yaml
trigger-x <<AGENT_BEGIN_随机10-32字符
task: 一句话任务概述（必填，≤600字符）
schedule:              # 可选，默认立即执行
  after: 30s           # 或 at: 2024-12-31 23:59 / cron: 0 * * * *
  timezone: Asia/Shanghai
condition:             # 可选
  when: READY          # 字面量/正则/逻辑组合
  repeat: true         # 条件满足后自动重启（需配合 when）
command: |             # 必填
  实际命令
AGENT_END_随机字符
```

### 字段规则

**task**：必填，简洁概述，脱离上下文也能理解
**command**：必填，非交互式 Shell 命令，禁止后台化（&/nohup）、密码明文、TUI 程序
**schedule**：可选，after/at/cron 三选一
- `after: 30s/15m/2h/1d/1w` 延迟执行
- `at: 2024-12-31 23:59:59` 定时执行（ISO 8601）
- `cron: 0 * * * *` 周期执行（5 字段标准 cron）
**condition**：可选，输出监控
- `when`: 条件表达式（字面量 `READY`、正则 `/error.*/i`、逻辑 `A AND B`）
- `repeat: true` 必须配合 `when` 使用，自动去重防消息风暴

## 智能条件表达式

| 语法 | 示例 | 说明 |
|------|------|------|
| 裸词字面量 | `READY` | 自动匹配 "READY" |
| 引号字符串 | `"error timeout"` | 包含空格的字面量 |
| 正则表达式 | `/error.*/i` | 斜杠包裹，支持 i/m/s 标志 |
| 逻辑与 | `READY AND SUCCESS` | 两个条件都满足 |
| 逻辑或 | `READY OR STARTED` | 任一条件满足 |
| 括号分组 | `(A OR B) AND C` | 控制优先级 |

**匹配机制**：增量匹配，条件在输出流中累积满足，满足后发送 SIGTERM 终止进程

## 常用场景速查

### 1. 延迟检查（短时等待）
```yaml
task: 30秒后检查服务
schedule:
  after: 30s
command: systemctl status nginx && curl -I http://localhost
```

### 2. 等待条件（1小时内）
```yaml
task: 等待应用启动
condition:
  when: READY
command: tail -f /var/log/app.log
```
⚠️ 如果超过 1 小时未出现条件会超时

### 3. 定时任务
```yaml
task: 每天凌晨备份
schedule:
  cron: 0 2 * * *
  timezone: Asia/Shanghai
command: mysqldump -u backup mydb > /backups/$(date +%Y%m%d).sql
```

### 4. 持续监控（正确做法）
```yaml
task: 每小时监控错误日志
schedule:
  cron: 0 * * * *
condition:
  when: /ERROR|FATAL/i
  repeat: true
command: timeout 3500 tail -f /var/log/app.log
```
✅ 关键：`cron` 每小时重启 + `timeout 3500` 限制单次运行

### 5. 复杂条件
```yaml
task: 等待服务完全就绪
schedule:
  after: 10s
condition:
  when: (READY OR STARTED) AND "listening on port"
command: tail -f /var/log/service.log
```

### 6. 定期检查并报警
```yaml
task: 每小时检查磁盘并在>80%时报警
schedule:
  cron: 0 * * * *
condition:
  when: /[89][0-9]%|100%/
command: |
  df -h | grep -E '^/dev/' | awk '{
    if ($5+0 > 80) print "WARNING: " $6 " at " $5
  }'
```

## 管理命令

```yaml
# 查看所有任务
trigger-x:show <<AGENT_BEGIN_xxx
AGENT_END_xxx

# 取消指定任务
trigger-x:kill:trg_abc123 <<AGENT_BEGIN_yyy
AGENT_END_yyy

# 取消所有任务
trigger-x:kill:all <<AGENT_BEGIN_zzz
AGENT_END_zzz
```

## 1小时超时详解

### 实现机制
```python
MAX_RUN_SECONDS = 3600  # 可通过环境变量 TRIGGER_MAX_RUN_SECONDS 调整
```
- 达到 3600 秒：发送 SIGTERM
- 等待 5 秒：仍未退出则发送 SIGKILL
- 任务状态：变为 `timeout`
- 投递消息：超时结果通知用户

### 不受影响场景
- ✅ 短时任务（< 1h）
- ✅ 定时任务（`cron`，每次独立预算）
- ✅ 延迟执行（`after`，命令本身快速完成）
- ✅ 条件触发（1h 内条件出现）

### 受影响场景及规避

#### 持续监控（tail -f）
❌ **错误**：
```yaml
task: 持续监控
condition:
  when: ERROR
  repeat: true
command: tail -f app.log  # 会在 1h 后超时
```

✅ **正确**：
```yaml
task: 每小时监控
schedule:
  cron: 0 * * * *  # 每小时重启
condition:
  when: ERROR
  repeat: true
command: timeout 3500 tail -f app.log  # 略小于 3600，留缓冲
```

#### 长时间等待条件
❌ **问题**：部署超过 1h，任务超时但不会重试
✅ **方案 1**：增加 `repeat: true`
✅ **方案 2**：改用轮询 `cron: */5 * * * *`

#### 长时间脚本（> 1h）
❌ **问题**：数据迁移超 1h 被强制终止
✅ **方案 1**：拆分为多个批次任务
✅ **方案 2**：使用 `shell-x`（无超时限制）
✅ **方案 3**：脚本支持断点续传 + `cron` 每小时执行

### 为什么需要超时？
防止挂起命令永久占用任务槽位（`_task_locks`、`_runtime_tasks`），导致：
- 后续调度只能记录"上一次仍在运行，跳过"
- 任务到进程重启前完全无法执行
- 用户看不到任何提示

## AI 生成决策树

```
用户需求
├─ 立即执行，等结果 → run-x
├─ 后台执行，稍后看 → shell-x
└─ 延迟/定时/条件 → trigger-x
    ├─ 稍后执行（"30秒后"） → schedule.after
    ├─ 定时执行（"明天上午"） → schedule.at
    ├─ 周期执行（"每小时"） → schedule.cron
    ├─ 等待条件（"等到出现X"）
    │   ├─ < 1h 完成 → condition.when
    │   └─ 可能 > 1h → condition.when + repeat: true
    └─ 持续监控（"一直监控"）
        → schedule.cron + condition.when + repeat: true + timeout命令
```

## 生成注意事项

1. **task 简洁**：`task: 等待应用启动` ✅ | `task: 这个任务的目的是...` ❌
2. **schedule 互斥**：after/at/cron 三选一
3. **repeat 必须配 when**：`{when: ERROR, repeat: true}` ✅ | `{repeat: true}` ❌
4. **持续监控必加 cron**：防止 1h 超时
5. **条件简洁优先正则**：`/ERROR|FATAL/i` ✅ | `"ERROR" OR "error" OR ...` ❌
6. **命令禁止交互和后台化**：vim/nano/nohup/& 都禁止
7. **多行用管道符**：`command: |` 后续行缩进
8. **非默认时区显式指定**：`timezone: America/New_York`

## 常见错误速查

| 错误信息 | 原因 | 解决 |
|----------|------|------|
| `缺少必填字段 "task"` | 没有 task | 添加 `task: ...` |
| `缺少必填字段 "command"` | 没有 command | 添加 `command: ...` |
| `只能使用一个时间字段` | after/at/cron 多选 | 只保留一个 |
| `repeat 为 true 时必须同时设置 condition.when` | repeat 无 when | 添加 `when: ...` |
| `YAML 格式错误` | 缩进/语法错误 | 检查缩进（2空格） |
| `未知时区` | 时区标识符错误 | 用 IANA 标准时区名 |
| `condition.when 语法错误` | 表达式语法错误 | 检查引号/斜杠/括号 |
| `task 字段不能超过 600 字符` | 描述过长 | 精简为一句话 |

## 技术细节（供参考）

### 条件匹配流程
1. **词法分析**：识别字面量/正则/逻辑运算符/括号
2. **语法分析**：构建 AST，支持运算符优先级
3. **增量匹配**：每次接收新输出更新匹配状态
4. **终止机制**：条件满足时发送 SIGTERM

### 去重机制（repeat: true）
```python
signature = sha256({
    'status': run.status,
    'trigger_reason': run.trigger_reason,
    'matched_conditions': run.matched_conditions,
    'exit_code': run.exit_code,
    'output': run.output.strip()
})
```
连续相同签名指数退避：5s → 10s → 20s → 40s → 80s → 160s → 300s（上限）

### 输出限制
- 捕获上限：200,000 字符（超过截断）
- 等待提示：60 秒未执行投递"仍在等待"

### 性能指标
| 指标 | 数值 |
|------|------|
| YAML 解析 | < 1ms |
| 条件编译 | < 1ms |
| 增量匹配 | < 0.1ms/chunk |
| 内存占用 | ~200KB/任务 |

## 最佳实践汇总

### ✅ 推荐
1. 任务描述清晰简洁
2. 条件优先用正则（`/502|503/` 比 `"502" OR "503"` 简洁）
3. 命令幂等可重试
4. 持续监控配合 cron（`cron: 0 * * * *` + `timeout 3500 tail -f`）
5. 显式指定非默认时区

### ❌ 避免
1. 命令含密码明文
2. 无 when 的无限循环（会超时）
3. 交互式命令（vim/nano）
4. 后台化命令（nohup/&）
5. 持续监控不配 cron（期望 tail -f 无限运行）

---

**版本**：v2.0.0 | **更新**：2026-08-12 | **兼容**：Python 3.10+, PyYAML 6.0+, APScheduler 3.0+
