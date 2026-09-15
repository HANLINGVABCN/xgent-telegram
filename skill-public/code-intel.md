```!
- code-intel.md: 专业代码结构与语义分析引擎 (AST/Repo Map/依赖拓扑/Git影响分析)。命令：`python3 skill-public/script/code-intel/code_intel.py <子命令>`。
  - `map <目录> [--depth N] [--compact] [--lang python,ts]`: 生成全景骨架图，秒级掌握架构。
  - `find-def <符号> [目录]`: 跨语言精确定位 Class/Def/Interface/Struct 声明行与完整签名。
  - `find-ref <符号> [目录]`: 精确查找符号调用点与引用，展示调用行上下文。
  - `find-sym <正则> [目录]`: 模糊/正则搜索全库符号。
  - `outline <文件>`: 单文件深度大纲（装饰器、签名、返回类型、Docstring）。
  - `deps [目录]`: 分析模块 import 依赖图与内外库拓扑。
  - `impact [目录]`: 结合 Git diff 自动分析当前修改影响了哪些函数与调用方。
  阅读/重构代码时优先使用 code-intel，严禁盲目大范围无序 grep 和全量 read！
```

# Code-Intel 工业级代码语义与结构分析工具

## 核心子命令与实战用法

1. **生成项目骨架图（Repo Map）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py map . --depth 3 --compact
   ```
   *适用于接手新项目，几百 Token 快速获得全局类/方法架构。*

2. **跨语言查找定义（find-def）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py find-def handle_message .
   ```

3. **调用链与引用定位（find-ref）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py find-ref send_file .
   ```

4. **单文件深入大纲（outline）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py outline app/core/bot.py
   ```

5. **依赖拓扑分析（deps）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py deps .
   ```

6. **Git 改动影响分析（impact）**：
   ```bash
   python3 skill-public/script/code-intel/code_intel.py impact .
   ```