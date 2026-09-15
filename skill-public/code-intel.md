```!
- code-intel.md: 代码结构与语义分析引擎。`python3 skill-public/script/code-intel/code_intel.py <子命令>`。所有命令支持 `--json` `--limit N`。阅读/重构代码优先用 code-intel，禁止盲目大范围 grep 和全量 read。
  - `map <目录> [--depth N] [--compact] [--lang python,ts] [--stats]`: 仓库骨架图
  - `find-def <符号> [目录]`: 跨语言定位定义(Class/Func/Interface/Struct)
  - `find-ref <符号> [目录]`: 查找调用点与引用(grep预过滤)
  - `find-sym <正则> [目录]`: 正则搜索符号
  - `outline <文件>`: 单文件大纲(装饰器/签名/返回类型/Docstring)
  - `deps [目录]`: 依赖拓扑(Python/TS/JS/Go/Rust)
  - `impact [目录] [--base REF]`: Git diff 影响分析(改动波及的符号与调用方)
  - `context <符号> [目录] [--extra N]`: **edit-x搭档**，提取符号完整源码+行号，直接用于OLD块
  - `summary [目录]`: 项目快照(语言/框架/入口/Git状态)
  - `changes [目录] [--base REF]`: 任意commit间符号级变更
  - `callers <符号> [目录]`: 反查调用方(函数名+文件位置)
  - `callees <函数名> [目录]`: 列出函数内部调用的其他函数
  - `hotspots [目录] [--top N]`: Git修改热点(churn排序)
  - `todos [目录]`: TODO/FIXME/HACK/XXX扫描
  - `struct <类名> [目录]`: 类/结构体字段与方法
```

# Code-Intel v2 工业级代码语义与结构分析工具

## Agent 决策指南 — 何时用什么

| 场景 | 推荐命令 | 替代的低效方式 |
|------|----------|----------------|
| 接手新项目，需要理解架构 | `summary` → `map --compact` | 盲目 `read-x` 多个文件 |
| 要修改某个函数，需要精确旧代码 | `context <func>` | `read-x` 整个文件 |
| 修改后检查有无调用方受影响 | `callers <func>` | `grep-x` 符号名（不精确） |
| 理解一个函数做了什么 | `outline <file>` + `callees <func>` | `read-x` 整文件逐行看 |
| 排查 bug，定位入口 | `find-def <sym>` → `callers` 反查 | 多轮 `grep-x` |
| 识别高风险/高改动区域 | `hotspots --top 20` | 无从判断 |
| 重构前影响评估 | `impact --base main` | 手动 `git diff` + `grep-x` |
| 了解类的数据模型 | `struct <ClassName>` | `read-x` 整文件找 `__init__` |
| 检查项目待办 | `todos` | `grep-x` TODO |
| 比较两个版本的差异 | `changes --base v1.0` | `git diff` 手动分析 |

## 核心子命令详解

### 1. 生成项目骨架图（map）
```bash
python3 skill-public/script/code-intel/code_intel.py map . --depth 3 --compact
python3 skill-public/script/code-intel/code_intel.py map . --stats --lang python
```
*接手新项目，几百 Token 快速获得全局类/方法架构。`--compact` 极致省 Token，`--stats` 显示行数。*

### 2. 精确定位定义（find-def）
```bash
python3 skill-public/script/code-intel/code_intel.py find-def handle_message .
```

### 3. 调用链与引用定位（find-ref）
```bash
python3 skill-public/script/code-intel/code_intel.py find-ref send_file .
```
*自动使用 grep 预过滤加速，大项目不再卡顿。*

### 4. 符号完整源码提取（context）⭐ edit-x 搭档
```bash
python3 skill-public/script/code-intel/code_intel.py context handle_message .
python3 skill-public/script/code-intel/code_intel.py context MyClass . --extra 3
```
*输出函数/类的完整源码+行号范围。输出可直接作为 `edit-x` 的 `-----OLD-----` 块，省去 `read-x` 整文件。*
*`--extra N` 可额外包含前后 N 行上下文。*

### 5. 单文件深入大纲（outline）
```bash
python3 skill-public/script/code-intel/code_intel.py outline app/core/bot.py
```

### 6. 多语言依赖拓扑分析（deps）
```bash
python3 skill-public/script/code-intel/code_intel.py deps .
```
*支持 Python/TypeScript/JavaScript/Go/Rust，自动区分内部引用和第三方依赖。*

### 7. Git 改动影响分析（impact）
```bash
python3 skill-public/script/code-intel/code_intel.py impact .
python3 skill-public/script/code-intel/code_intel.py impact . --base main
```
*`--base` 可对比任意分支/tag/commit，不再只限于 HEAD。*

### 8. 调用方追踪（callers）
```bash
python3 skill-public/script/code-intel/code_intel.py callers send_message .
```
*精确找出"谁调用了这个函数"，Python 用 AST 精确分析，其他语言用词法+所属函数定位。*

### 9. 被调用方分析（callees）
```bash
python3 skill-public/script/code-intel/code_intel.py callees main .
```
*列出一个函数内部调用了哪些其他函数，快速理解函数的依赖关系。*

### 10. 项目快照摘要（summary）
```bash
python3 skill-public/script/code-intel/code_intel.py summary .
```
*一条命令输出：语言占比、框架检测、入口文件、Git 状态、文件/行数统计。*

### 11. 符号级变更分析（changes）
```bash
python3 skill-public/script/code-intel/code_intel.py changes . --base HEAD~5
python3 skill-public/script/code-intel/code_intel.py changes . --base main
```
*对比任意两个 Git 版本之间变化了哪些符号（函数/类），精准定位变更范围。*

### 12. Git 修改热点（hotspots）
```bash
python3 skill-public/script/code-intel/code_intel.py hotspots . --top 10
```
*基于最近 500 次提交分析最频繁修改的文件，识别技术债和高风险区域。*

### 13. TODO/FIXME 扫描（todos）
```bash
python3 skill-public/script/code-intel/code_intel.py todos .
```
*扫描 TODO/FIXME/HACK/XXX/BUG/OPTIMIZE/NOTE/WARN 标记，按类型统计。*

### 14. 类/结构体分析（struct）
```bash
python3 skill-public/script/code-intel/code_intel.py struct UserModel .
```
*提取类变量、实例属性（from __init__）、方法列表。非 Python 直接输出结构体源码。*

## 全局参数

所有子命令均支持以下参数：
- `--json`: 输出 JSON 格式，方便程序化解析
- `--limit N`: 最大输出条目数（默认 150，0=不限），防止大项目 Token 爆炸

## 最佳实践

1. **修改代码标准流程**: `context <func>` 取精确源码 → `edit-x` 修改 → `callers <func>` 检查影响
2. **新项目上手流程**: `summary` → `map --compact --depth 2` → `deps` → 针对性 `outline`
3. **Bug 排查流程**: `find-def <sym>` 定位 → `context` 读源码 → `callers` 追踪入口
4. **重构评估流程**: `hotspots` 识别热点 → `impact --base main` 评估变更 → `callers` 确认影响面