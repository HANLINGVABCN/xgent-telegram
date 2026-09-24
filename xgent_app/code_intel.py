#!/usr/bin/env python3
"""
Code-Intel v2: 工业级代码结构分析与语义检索工具
专为大模型/智能 Agent 设计的高效上下文压缩与代码导航引擎。

子命令：
  map        生成代码仓库骨架图 (Repo Map)
  find-def   精确定位类、函数、接口、结构体声明 (跨语言)
  find-ref   精确查找符号调用点与引用 (AST/词法级别)
  find-sym   模糊/正则搜索符号名
  outline    单文件深度大纲分析
  deps       模块依赖拓扑分析 (Python/TS/JS/Go/Rust)
  impact     基于 Git 改动分析受影响的符号与调用方
  context    提取符号完整源码与行号范围 (edit-x 最佳搭档)
  summary    项目级一句话快照摘要
  changes    对比任意 commit/分支的符号级变更
  callers    给定符号，反查所有调用方
  callees    给定函数，列出其内部调用的其他函数
  hotspots   基于 Git 历史的高频修改热点分析
  todos      全项目 TODO/FIXME/HACK/XXX 扫描
  struct     分析类/结构体的字段与属性
"""

import os
import sys
import ast
import re
import json
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Set
from fnmatch import fnmatch
import io

# 修复 Windows GBK 编码导致 emoji/中文输出崩溃的问题。
# 只在作为独立脚本运行时改写全局 stdout；被 import（intel-x 进程内调用）时
# 绝不动 bot 进程的 stdout —— run_intel 自己用 StringIO 捕获输出。
def _force_utf8_stdout() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ======================================================================
# 全局配置
# ======================================================================
IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    ".idea", ".vscode", "dist", "build", ".next", ".cache",
    "xgent_storage", "logs", "target", "vendor", ".tox",
    ".mypy_cache", ".pytest_cache", "coverage", ".turbo",
    ".eggs", ".nox", ".hg", ".svn", ".bundle",
}

IGNORE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum",
    "composer.lock", ".DS_Store", "Thumbs.db",
}

LANG_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".mts", ".cts"},
    "go": {".go"},
    "rust": {".rs"},
    "c_cpp": {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh"},
    "java": {".java"},
    "shell": {".sh", ".bash", ".zsh"},
    "php": {".php"},
    "ruby": {".rb"},
    "kotlin": {".kt", ".kts"},
    "swift": {".swift"},
    "csharp": {".cs"},
}

TODO_PATTERNS = re.compile(
    r"(?:#|//|/\*|<!--|--)\s*(TODO|FIXME|HACK|XXX|WARN|BUG|OPTIMIZE|NOTE)\b[:\s]*(.*)",
    re.IGNORECASE,
)

DEFAULT_LIMIT = 150


# ======================================================================
# .gitignore 解析器
# ======================================================================
class GitIgnoreFilter:
    """解析 .gitignore 文件并据此过滤路径。"""

    def __init__(self, root: Path):
        self.root = root
        self.patterns: List[Tuple[str, bool]] = []  # (pattern, is_negation)
        self._load(root / ".gitignore")

    def _load(self, gitignore_path: Path):
        if not gitignore_path.is_file():
            return
        try:
            for line in gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                negation = line.startswith("!")
                if negation:
                    line = line[1:]
                # 去除尾部斜杠（目录标记），fnmatch 不需要
                line = line.rstrip("/")
                self.patterns.append((line, negation))
        except Exception:
            pass

    def is_ignored(self, rel_path: str) -> bool:
        """检查相对路径是否被 .gitignore 匹配。"""
        result = False
        name = Path(rel_path).name
        for pattern, is_negation in self.patterns:
            # 匹配完整路径或仅文件名
            if fnmatch(rel_path, pattern) or fnmatch(name, pattern):
                result = not is_negation
            # 支持 ** 模式
            if "/" not in pattern:
                for part in Path(rel_path).parts:
                    if fnmatch(part, pattern):
                        result = not is_negation
        return result


# ======================================================================
# 输出格式化器
# ======================================================================
class OutputFormatter:
    """统一管理 text/json 输出和 --limit 截断。"""

    def __init__(self, as_json: bool = False, limit: int = DEFAULT_LIMIT):
        self.as_json = as_json
        self.limit = limit
        self.items: list = []
        self.header: str = ""
        self.footer_parts: List[str] = []

    def set_header(self, text: str):
        self.header = text

    def add(self, item: dict):
        self.items.append(item)

    def add_footer(self, text: str):
        self.footer_parts.append(text)

    def _text_line(self, item: dict) -> str:
        """将 item dict 转为人类可读行。子类/调用方也可直接用 add_raw。"""
        parts = []
        if "kind" in item:
            parts.append(f"[{item['kind'].upper():<9}]")
        if "file" in item:
            loc = item["file"]
            if "line" in item:
                loc += f":{item['line']}"
            parts.append(loc)
        if "text" in item:
            if parts:
                parts.append(f"\n{'':>14}> {item['text']}")
            else:
                parts.append(item["text"])
        if "indent" in item and "display" in item:
            space = "  " * item["indent"]
            ln = f"L{item.get('line', '?'):<4}"
            parts = [f"{ln} {space}{item['display']}"]
        return " ".join(parts) if parts else str(item)

    def flush(self):
        truncated = len(self.items) > self.limit > 0
        visible = self.items[:self.limit] if self.limit > 0 else self.items

        if self.as_json:
            output = {
                "total": len(self.items),
                "truncated": truncated,
                "limit": self.limit,
                "results": visible,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return

        if self.header:
            print(self.header)

        for item in visible:
            print(self._text_line(item))

        if truncated:
            print(f"\n⚠️  输出已截断：共 {len(self.items)} 条，仅显示前 {self.limit} 条。使用 --limit N 调整。")

        for f in self.footer_parts:
            print(f)


# ======================================================================
# 辅助函数
# ======================================================================
def is_ignored(path: Path, git_filter: Optional[GitIgnoreFilter] = None, root: Optional[Path] = None) -> bool:
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
    if path.name in IGNORE_FILES:
        return True
    if git_filter and root:
        try:
            rel = str(path.relative_to(root))
            if git_filter.is_ignored(rel):
                return True
        except ValueError:
            pass
    return False


def detect_language(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    for lang, exts in LANG_EXTENSIONS.items():
        if ext in exts:
            return lang
    return None


def iter_source_files(root: Path, git_filter: Optional[GitIgnoreFilter] = None,
                      lang_filter: Optional[Set[str]] = None, max_depth: int = 99) -> List[Path]:
    """遍历源码文件，应用所有过滤条件。"""
    results = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or is_ignored(path, git_filter, root):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth:
            continue
        if lang_filter:
            lang = detect_language(path)
            if not lang or lang not in lang_filter:
                continue
        results.append(path)
    return results


def grep_prefilter(symbol: str, root: Path, extensions: Optional[List[str]] = None) -> Set[Path]:
    """用系统 grep 快速定位包含目标符号的文件，加速后续精确解析。"""
    try:
        cmd = ["grep", "-rl", "--include=*", "-w", symbol, str(root)]
        if extensions:
            cmd = ["grep", "-rl"]
            for ext in extensions:
                cmd.extend(["--include", f"*{ext}"])
            cmd.extend(["-w", symbol, str(root)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return {Path(line.strip()) for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return set()  # 回退：返回空集表示无法预过滤，调用方应全量扫描


def read_file_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ======================================================================
# Python AST 深入解析器
# ======================================================================
class PythonDeepVisitor(ast.NodeVisitor):
    def __init__(self, include_constants: bool = True, class_prefix: str = ""):
        self.outline = []  # list of (indent, text, lineno, kind, name, end_lineno)
        self.indent = 0
        self.include_constants = include_constants
        self.class_prefix = class_prefix
        self._class_stack: List[str] = []

    def _get_doc(self, node) -> str:
        doc = ast.get_docstring(node)
        if doc:
            first_line = doc.strip().split("\n")[0].strip()
            if first_line:
                return f"  # {first_line[:70]}"
        return ""

    def _format_args(self, args_node) -> str:
        args = []
        for a in args_node.args:
            s = a.arg
            if a.annotation:
                try:
                    s += f": {ast.unparse(a.annotation)}"
                except Exception:
                    pass
            args.append(s)
        if args_node.vararg:
            args.append(f"*{args_node.vararg.arg}")
        for a in args_node.kwonlyargs:
            s = a.arg
            if a.annotation:
                try:
                    s += f": {ast.unparse(a.annotation)}"
                except Exception:
                    pass
            args.append(s)
        if args_node.kwarg:
            args.append(f"**{args_node.kwarg.arg}")
        return ", ".join(args)

    def _get_decorators(self, node) -> List[str]:
        decs = []
        for d in getattr(node, "decorator_list", []):
            try:
                decs.append(f"@{ast.unparse(d)}")
            except Exception:
                pass
        return decs

    def _qualified_name(self, name: str) -> str:
        if self._class_stack:
            return ".".join(self._class_stack) + "." + name
        return name

    def _end_lineno(self, node) -> int:
        return getattr(node, "end_lineno", node.lineno)

    def visit_ClassDef(self, node):
        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                pass
        base_str = f"({', '.join(bases)})" if bases else ""
        doc = self._get_doc(node)
        decs = self._get_decorators(node)

        for dec in decs:
            self.outline.append((self.indent, dec, node.lineno, "decorator", "", node.lineno))
        qname = self._qualified_name(node.name)
        self.outline.append((self.indent, f"class {node.name}{base_str}:{doc}", node.lineno, "class", qname, self._end_lineno(node)))

        self._class_stack.append(node.name)
        self.indent += 1
        self.generic_visit(node)
        self.indent -= 1
        self._class_stack.pop()

    def visit_FunctionDef(self, node):
        self._handle_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._handle_func(node, is_async=True)

    def _handle_func(self, node, is_async=False):
        prefix = "async def " if is_async else "def "
        args_str = self._format_args(node.args)
        ret_str = ""
        if node.returns:
            try:
                ret_str = f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        doc = self._get_doc(node)
        decs = self._get_decorators(node)

        for dec in decs:
            self.outline.append((self.indent, dec, node.lineno, "decorator", "", node.lineno))
        qname = self._qualified_name(node.name)
        self.outline.append((self.indent, f"{prefix}{node.name}({args_str}){ret_str}:{doc}", node.lineno, "func", qname, self._end_lineno(node)))

        self.indent += 1
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(item)
        self.indent -= 1

    def visit_Assign(self, node):
        if self.indent == 0 and self.include_constants:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    val_str = ""
                    try:
                        val_str = f" = {ast.unparse(node.value)}"
                        if len(val_str) > 30:
                            val_str = val_str[:27] + "..."
                    except Exception:
                        pass
                    self.outline.append((0, f"{target.id}{val_str}", node.lineno, "const", target.id, node.lineno))


def parse_python_file(file_path: Path, include_constants: bool = True):
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=str(file_path))
        visitor = PythonDeepVisitor(include_constants=include_constants)
        visitor.visit(tree)
        return visitor.outline
    except Exception:
        return []


# ======================================================================
# 多语言通用正则解析器
# ======================================================================
REGEX_PATTERNS = {
    "typescript": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*[<(]"), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\("), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_$]+)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_$]+)"), "interface"),
        (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z0-9_$]+)\s*[=<]"), "type"),
        (re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z0-9_$]+)"), "enum"),
        # React 组件 (const X = React.memo / forwardRef / ...)
        (re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Z][A-Za-z0-9_$]*)\s*[:=]\s*(?:React\.)?(?:memo|forwardRef|lazy)\b"), "component"),
        # Hook
        (re.compile(r"^\s*(?:export\s+)?(?:const|function)\s+(use[A-Z][A-Za-z0-9_$]*)\s*"), "hook"),
    ],
    "javascript": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\("), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\("), "func"),
        (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Z][A-Za-z0-9_$]*)\s*[:=]\s*(?:React\.)?(?:memo|forwardRef|lazy)\b"), "component"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|function)\s+(use[A-Z][A-Za-z0-9_$]*)\s*"), "hook"),
    ],
    "go": [
        (re.compile(r"^\s*func\s+\((\w+)\s+\*?(\w+)\)\s+(\w+)\s*\("), "method"),
        (re.compile(r"^\s*func\s+([A-Za-z0-9_]+)\s*\("), "func"),
        (re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+struct\b"), "struct"),
        (re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+interface\b"), "interface"),
        (re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+"), "type"),
    ],
    "rust": [
        (re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*[<(]"), "func"),
        (re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?struct\s+([A-Za-z0-9_]+)"), "struct"),
        (re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?enum\s+([A-Za-z0-9_]+)"), "enum"),
        (re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?trait\s+([A-Za-z0-9_]+)"), "trait"),
        (re.compile(r"^\s*impl(?:\s*<.*?>)?\s+(?:(\w+)\s+for\s+)?([A-Za-z0-9_]+)"), "impl"),
        (re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?mod\s+([A-Za-z0-9_]+)"), "mod"),
    ],
    "java": [
        (re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?(?:class|interface|enum|record)\s+([A-Za-z0-9_$]+)"), "class"),
        (re.compile(r"^\s*(?:@\w+\s+)*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?(?:abstract\s+)?(?:<[\w<>,?\s]+>\s+)?[\w<>\[\],?\s]+\s+([A-Za-z0-9_$]+)\s*\("), "func"),
    ],
    "shell": [
        (re.compile(r"^\s*(?:function\s+)?([A-Za-z0-9_-]+)\s*\(\)\s*\{?"), "func"),
    ],
    "c_cpp": [
        (re.compile(r"^\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?(?:struct\s+|enum\s+|union\s+)?[\w*:]+\s+\*?([A-Za-z_]\w*)\s*\("), "func"),
        (re.compile(r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_]\w*)"), "struct"),
        (re.compile(r"^\s*(?:typedef\s+)?union\s+([A-Za-z_]\w*)"), "union"),
        (re.compile(r"^\s*(?:typedef\s+)?enum\s+([A-Za-z_]\w*)"), "enum"),
        (re.compile(r"^\s*typedef\s+.*\s+([A-Za-z_]\w*)\s*;"), "typedef"),
        (re.compile(r"^\s*class\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^\s*namespace\s+([A-Za-z_]\w*)"), "namespace"),
        (re.compile(r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\b"), "macro"),
    ],
    "php": [
        (re.compile(r"^\s*(?:abstract\s+)?class\s+([A-Za-z0-9_]+)"), "class"),
        (re.compile(r"^\s*interface\s+([A-Za-z0-9_]+)"), "interface"),
        (re.compile(r"^\s*trait\s+([A-Za-z0-9_]+)"), "trait"),
        (re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?function\s+([A-Za-z0-9_]+)\s*\("), "func"),
        (re.compile(r"^\s*namespace\s+([A-Za-z0-9_\\]+)"), "namespace"),
    ],
    "ruby": [
        (re.compile(r"^\s*class\s+([A-Za-z0-9_:]+)"), "class"),
        (re.compile(r"^\s*module\s+([A-Za-z0-9_:]+)"), "module"),
        (re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z0-9_!?]+)"), "func"),
    ],
    "kotlin": [
        (re.compile(r"^\s*(?:data\s+)?(?:sealed\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_]+)"), "class"),
        (re.compile(r"^\s*(?:object)\s+([A-Za-z0-9_]+)"), "object"),
        (re.compile(r"^\s*(?:interface)\s+([A-Za-z0-9_]+)"), "interface"),
        (re.compile(r"^\s*(?:(?:suspend|private|internal|public|protected|override)\s+)*fun\s+(?:<.*?>\s+)?([A-Za-z0-9_]+)\s*\("), "func"),
    ],
    "swift": [
        (re.compile(r"^\s*(?:final\s+)?class\s+([A-Za-z0-9_]+)"), "class"),
        (re.compile(r"^\s*struct\s+([A-Za-z0-9_]+)"), "struct"),
        (re.compile(r"^\s*enum\s+([A-Za-z0-9_]+)"), "enum"),
        (re.compile(r"^\s*protocol\s+([A-Za-z0-9_]+)"), "protocol"),
        (re.compile(r"^\s*(?:(?:public|private|internal|open|fileprivate|static|class|override|mutating)\s+)*func\s+([A-Za-z0-9_]+)\s*[<(]"), "func"),
    ],
    "csharp": [
        (re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:partial\s+)?(?:abstract\s+)?(?:sealed\s+)?class\s+([A-Za-z0-9_]+)"), "class"),
        (re.compile(r"^\s*(?:public|private|protected|internal)?\s*interface\s+([A-Za-z0-9_]+)"), "interface"),
        (re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:abstract\s+)?(?:async\s+)?[\w<>\[\]?,\s]+\s+([A-Za-z0-9_]+)\s*\("), "func"),
        (re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:readonly\s+)?struct\s+([A-Za-z0-9_]+)"), "struct"),
        (re.compile(r"^\s*enum\s+([A-Za-z0-9_]+)"), "enum"),
        (re.compile(r"^\s*namespace\s+([A-Za-z0-9_.]+)"), "namespace"),
    ],
}


def parse_generic_file(file_path: Path, lang: str):
    """多语言正则解析器，提取文件级符号。"""
    outline = []
    patterns = REGEX_PATTERNS.get(lang, [])
    if not patterns:
        return outline

    lines = read_file_lines(file_path)
    if not lines:
        return outline

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*", "<!--")):
            continue

        for p, kind in patterns:
            m = p.match(stripped)
            if m:
                # Go method 特殊处理：group(3) 是方法名，group(2) 是 receiver 类型
                if kind == "method" and lang == "go" and m.lastindex >= 3:
                    sym_name = m.group(3)
                    receiver = m.group(2)
                    text = stripped.rstrip("{").strip()
                    outline.append((0, text, idx, "method", f"{receiver}.{sym_name}", _guess_end_line(lines, idx - 1)))
                # Rust impl 特殊处理
                elif kind == "impl" and lang == "rust" and m.lastindex >= 2:
                    sym_name = m.group(2) if m.group(2) else m.group(1)
                    text = stripped.rstrip("{").strip()
                    outline.append((0, text, idx, kind, sym_name, _guess_end_line(lines, idx - 1)))
                else:
                    sym_name = m.group(1)
                    text = stripped.rstrip("{").strip()
                    outline.append((0, text, idx, kind, sym_name, _guess_end_line(lines, idx - 1)))
                break
    return outline


def _guess_end_line(lines: List[str], start_idx: int) -> int:
    """对非 Python 语言，基于花括号层级估算符号体结束行。"""
    depth = 0
    started = False
    for i in range(start_idx, min(start_idx + 2000, len(lines))):
        line = lines[i]
        for ch in line:
            if ch == '{':
                depth += 1
                started = True
            elif ch == '}':
                depth -= 1
        if started and depth <= 0:
            return i + 1  # 转为 1-indexed
    return start_idx + 1


def get_file_outline(file_path: Path, include_constants: bool = True):
    lang = detect_language(file_path)
    if lang == "python":
        return parse_python_file(file_path, include_constants)
    elif lang:
        return parse_generic_file(file_path, lang)
    return []


# ======================================================================
# 多语言依赖解析
# ======================================================================
def extract_imports_python(file_path: Path) -> Tuple[Set[str], Set[str]]:
    """返回 (internal_imports, external_imports) 的模块名集合。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return set(), set()

    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            deps.add(mod)
    return deps, set()  # 内/外 在 cmd_deps 中统一区分


_TS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+.*?\s+from\s+['"]([^'"]+)['"]|"""
    r"""(?:require)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

_GO_IMPORT_RE = re.compile(r'^\s*(?:_\s+)?"([^"]+)"', re.MULTILINE)
_RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)", re.MULTILINE)


def extract_imports_generic(file_path: Path, lang: str) -> Set[str]:
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    deps = set()

    if lang in ("typescript", "javascript"):
        for m in _TS_IMPORT_RE.finditer(content):
            dep = m.group(1) or m.group(2)
            if dep:
                deps.add(dep)
    elif lang == "go":
        # 处理 import block
        in_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ("):
                in_block = True
                continue
            if in_block:
                if stripped == ")":
                    in_block = False
                    continue
                m = _GO_IMPORT_RE.match(stripped)
                if m:
                    deps.add(m.group(1))
            elif stripped.startswith("import "):
                m = re.search(r'"([^"]+)"', stripped)
                if m:
                    deps.add(m.group(1))
    elif lang == "rust":
        for m in _RUST_USE_RE.finditer(content):
            deps.add(m.group(1))

    return deps


# ======================================================================
# Python AST 调用分析
# ======================================================================
def find_python_calls_in_function(file_path: Path, func_name: str) -> List[str]:
    """解析 Python 文件，找到 func_name 函数内部调用的所有其他函数名。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return []

    callees = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = ""
                    if isinstance(child.func, ast.Name):
                        name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        name = child.func.attr
                    if name and name != func_name:
                        callees.append(name)
            break
    return sorted(set(callees))


def find_python_callers(file_path: Path, symbol: str, root: Path) -> List[dict]:
    """在 Python 文件中，AST 精确查找调用 symbol 的函数。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return []

    results = []
    rel = safe_relative(file_path, root)

    # 建立行号→所属函数的映射
    func_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno + 50)
            func_ranges.append((node.lineno, end, node.name))

    def _find_enclosing_func(lineno: int) -> Optional[str]:
        for start, end, name in func_ranges:
            if start <= lineno <= end:
                return name
        return None

    # 查找调用
    for node in ast.walk(tree):
        is_match = False
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == symbol:
                is_match = True
            elif isinstance(node.func, ast.Attribute) and node.func.attr == symbol:
                is_match = True

        if is_match and hasattr(node, "lineno"):
            caller = _find_enclosing_func(node.lineno)
            line_txt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            results.append({
                "file": rel,
                "line": node.lineno,
                "caller": caller or "<module>",
                "text": line_txt,
            })

    return results


# ======================================================================
# 函数体提取器
# ======================================================================
def extract_symbol_source(file_path: Path, symbol: str) -> Optional[dict]:
    """提取符号的完整源码（含行号范围），直接适配 edit-x 的 OLD 块。"""
    lang = detect_language(file_path)
    lines = read_file_lines(file_path)
    if not lines:
        return None

    if lang == "python":
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content)
        except Exception:
            return None

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    start = node.lineno
                    end = getattr(node, "end_lineno", start)
                    # 包含装饰器
                    if hasattr(node, "decorator_list") and node.decorator_list:
                        dec_start = min(d.lineno for d in node.decorator_list)
                        start = min(start, dec_start)
                    source_lines = lines[start - 1:end]
                    return {
                        "file": str(file_path),
                        "symbol": symbol,
                        "start_line": start,
                        "end_line": end,
                        "source": "\n".join(source_lines),
                        "lang": "python",
                    }
    else:
        # 非 Python：用 outline 的 end_lineno 估算
        outline = get_file_outline(file_path)
        for indent, text, lineno, kind, sym_name, end_lineno in outline:
            # 匹配符号名（支持 Receiver.Method 格式只匹配 Method 部分）
            bare_name = sym_name.split(".")[-1] if "." in sym_name else sym_name
            if bare_name == symbol or sym_name == symbol:
                source_lines = lines[lineno - 1:end_lineno]
                return {
                    "file": str(file_path),
                    "symbol": symbol,
                    "start_line": lineno,
                    "end_line": end_lineno,
                    "source": "\n".join(source_lines),
                    "lang": lang or "unknown",
                }

    return None


# ======================================================================
# 框架检测（用于 summary）
# ======================================================================
def detect_frameworks(root: Path) -> List[str]:
    """根据配置文件和依赖检测项目使用的框架/技术栈。"""
    frameworks = []
    indicators = {
        "package.json": ["node"],
        "tsconfig.json": ["typescript"],
        "requirements.txt": ["python"],
        "setup.py": ["python"],
        "pyproject.toml": ["python"],
        "go.mod": ["go"],
        "Cargo.toml": ["rust"],
        "pom.xml": ["java-maven"],
        "build.gradle": ["java-gradle"],
        "Gemfile": ["ruby"],
        "composer.json": ["php"],
        "Dockerfile": ["docker"],
        "docker-compose.yml": ["docker-compose"],
        "docker-compose.yaml": ["docker-compose"],
        ".github/workflows": ["github-actions"],
        "Makefile": ["make"],
        "CMakeLists.txt": ["cmake"],
    }

    for indicator, tags in indicators.items():
        if (root / indicator).exists():
            frameworks.extend(tags)

    # 深入检测 Python 框架
    for f in ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"]:
        fp = root / f
        if fp.is_file():
            try:
                content = fp.read_text(encoding="utf-8", errors="ignore").lower()
                for fw in ["django", "flask", "fastapi", "celery", "sqlalchemy",
                           "pytest", "scrapy", "tornado", "aiohttp", "starlette"]:
                    if fw in content and fw not in frameworks:
                        frameworks.append(fw)
            except Exception:
                pass

    # 深入检测 JS 框架
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            content = pkg.read_text(encoding="utf-8", errors="ignore").lower()
            for fw in ["react", "vue", "angular", "next", "nuxt", "express",
                       "nestjs", "svelte", "electron", "vite", "webpack"]:
                if f'"{fw}"' in content and fw not in frameworks:
                    frameworks.append(fw)
        except Exception:
            pass

    return sorted(set(frameworks))


# ======================================================================
# 子命令实现
# ======================================================================

# ---- map ----
def cmd_map(target_dir: str, max_depth: int = 4, compact: bool = False,
            lang_filter: Optional[str] = None, fmt: OutputFormatter = None,
            show_stats: bool = False, tree_mode: bool = False):
    root = Path(target_dir).resolve()
    if not root.exists():
        print(f"❌ 路径不存在: {root}")
        return

    git_filter = GitIgnoreFilter(root)
    filter_langs = set(lang_filter.split(",")) if lang_filter else None
    files = iter_source_files(root, git_filter, filter_langs, max_depth)

    fmt.set_header(f"📦 [Repo Map] 根目录: {root} (最大深度: {max_depth})\n{'=' * 60}")
    total_files = 0
    total_symbols = 0
    total_lines = 0

    for path in files:
        outline = get_file_outline(path, include_constants=not compact)
        ext = path.suffix.lower()
        rel = safe_relative(path, root)

        if outline or ext in {".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".md", ".sql", ".env"}:
            total_files += 1
            file_lines = len(read_file_lines(path)) if show_stats else 0
            total_lines += file_lines

            stat_suffix = f"  ({file_lines} lines)" if show_stats else ""
            fmt.add({"indent": 0, "display": f"📄 {rel}:{stat_suffix}", "line": "", "kind": "file", "file": rel})

            for indent, line, lineno, kind, sym_name, *rest in outline:
                if kind == "decorator" and compact:
                    continue
                total_symbols += 1
                fmt.add({"indent": indent + 1, "display": line, "line": lineno, "kind": kind, "name": sym_name, "file": rel})

    fmt.add_footer(f"\n{'=' * 60}")
    stats = f"📊 统计: {total_files} 个文件, {total_symbols} 个符号"
    if show_stats:
        stats += f", 合计 {total_lines} 行代码"
    fmt.add_footer(stats)
    fmt.flush()


# ---- find-def ----
def cmd_find_def(symbol: str, target_dir: str, fmt: OutputFormatter = None):
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"🎯 [查找定义] 目标: '{symbol}' 目录: {root}\n{'-' * 60}")

    for path in iter_source_files(root, git_filter):
        outline = get_file_outline(path, include_constants=True)
        rel = safe_relative(path, root)

        for indent, line, lineno, kind, sym_name, *rest in outline:
            bare = sym_name.split(".")[-1] if "." in sym_name else sym_name
            if bare == symbol or sym_name == symbol:
                fmt.add({"kind": kind, "file": rel, "line": lineno, "text": line, "name": sym_name})

    if not fmt.items:
        fmt.add_footer(f"未找到符号 '{symbol}' 的确切定义。")
    else:
        fmt.add_footer(f"\n✅ 找到 {len(fmt.items)} 处匹配定义。")
    fmt.flush()


# ---- find-ref ----
def cmd_find_ref(symbol: str, target_dir: str, fmt: OutputFormatter = None):
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"🔗 [查找调用/引用] 目标: '{symbol}' 目录: {root}\n{'-' * 60}")

    # 预过滤加速
    candidate_files = grep_prefilter(symbol, root)
    use_prefilter = len(candidate_files) > 0

    # 1. Python AST 精确查找
    for path in sorted(root.rglob("*.py")):
        if is_ignored(path, git_filter, root):
            continue
        if use_prefilter and path not in candidate_files:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            tree = ast.parse(content, filename=str(path))
        except Exception:
            continue

        rel = safe_relative(path, root)
        matched_lines = set()

        for node in ast.walk(tree):
            is_call = False
            if isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name == symbol:
                    is_call = True
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                is_call = True

            if is_call and hasattr(node, "lineno"):
                if node.lineno not in matched_lines:
                    matched_lines.add(node.lineno)
                    line_txt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    fmt.add({"kind": "py-call", "file": rel, "line": node.lineno, "text": line_txt})

    # 2. 其他语言词法搜索
    word_pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in iter_source_files(root, git_filter):
        if path.suffix == ".py":
            continue
        lang = detect_language(path)
        if not lang:
            continue
        if use_prefilter and path not in candidate_files:
            continue

        lines = read_file_lines(path)
        rel = safe_relative(path, root)
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if word_pattern.search(stripped) and not stripped.startswith(("//", "#", "/*")):
                fmt.add({"kind": lang, "file": rel, "line": idx, "text": stripped})

    if not fmt.items:
        fmt.add_footer(f"未找到 '{symbol}' 的调用或引用。")
    else:
        fmt.add_footer(f"\n✅ 找到 {len(fmt.items)} 处引用。")
    fmt.flush()


# ---- find-sym ----
def cmd_find_sym(pattern: str, target_dir: str, fmt: OutputFormatter = None):
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)
    regex = re.compile(pattern, re.IGNORECASE)

    fmt.set_header(f"🔍 [模糊/正则符号搜索] 表达式: /{pattern}/i 目录: {root}\n{'-' * 60}")

    for path in iter_source_files(root, git_filter):
        outline = get_file_outline(path, include_constants=True)
        rel = safe_relative(path, root)

        for indent, line, lineno, kind, sym_name, *rest in outline:
            if regex.search(sym_name) or regex.search(line):
                fmt.add({"kind": kind, "file": rel, "line": lineno, "text": line, "name": sym_name})

    fmt.add_footer(f"{'-' * 60}\n✅ 匹配到 {len(fmt.items)} 个符号。")
    fmt.flush()


# ---- outline ----
def cmd_outline(file_path: str, fmt: OutputFormatter = None):
    target = Path(file_path).resolve()
    if not target.exists() or target.is_dir():
        print(f"❌ 文件不存在: {target}")
        return

    fmt.set_header(f"📄 [单文件深度大纲] {target}\n{'=' * 60}")
    outline = get_file_outline(target, include_constants=True)

    if not outline:
        fmt.add_footer("未提取到结构化符号或该文件格式不支持。")
        fmt.flush()
        return

    for indent, line, lineno, kind, sym_name, *rest in outline:
        fmt.add({"indent": indent, "display": line, "line": lineno, "kind": kind, "name": sym_name})

    fmt.flush()


# ---- deps ----
def cmd_deps(target_dir: str, fmt: OutputFormatter = None):
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"🌲 [依赖拓扑分析] 目录: {root}\n{'=' * 60}")

    import_graph: Dict[str, Dict[str, List[str]]] = {}

    for path in iter_source_files(root, git_filter):
        lang = detect_language(path)
        if not lang:
            continue

        rel = safe_relative(path, root)
        deps = set()

        if lang == "python":
            deps, _ = extract_imports_python(path)
        elif lang in ("typescript", "javascript", "go", "rust"):
            deps = extract_imports_generic(path, lang)

        if deps:
            internal = []
            external = []
            for d in sorted(deps):
                # 判断内/外部
                if d.startswith(".") or d.startswith("/"):
                    internal.append(d)
                elif lang == "python":
                    # Python: 检查是否为项目内模块
                    mod_path = d.replace(".", "/")
                    if (root / (mod_path + ".py")).exists() or (root / mod_path).is_dir():
                        internal.append(d)
                    else:
                        external.append(d)
                elif lang == "go" and "/" not in d:
                    external.append(d)  # Go 标准库
                elif lang == "go":
                    external.append(d)
                else:
                    external.append(d)

            info = {}
            if internal:
                info["internal"] = internal
            if external:
                info["external"] = external[:15]
            import_graph[rel] = info

    for file, info in sorted(import_graph.items()):
        entry = {"file": file, "kind": "deps"}
        parts = [f"\n📄 {file}:"]
        if "internal" in info:
            entry["internal"] = info["internal"]
            parts.append("  ├── 🏠 项目内引用: " + ", ".join(info["internal"]))
        if "external" in info:
            entry["external"] = info["external"]
            ext_str = ", ".join(info["external"])
            parts.append("  └── 📦 第三方依赖: " + ext_str)
        entry["text"] = "\n".join(parts)
        fmt.add(entry)

    fmt.add_footer(f"\n📊 共分析 {len(import_graph)} 个模块的依赖关系。")
    fmt.flush()


# ---- impact ----
def cmd_impact(target_dir: str, base_ref: str = "HEAD", fmt: OutputFormatter = None):
    root = Path(target_dir).resolve()

    fmt.set_header(f"💥 [Git 改动影响分析] 目录: {root} (基准: {base_ref})\n{'=' * 60}")

    try:
        diff_cmd = ["git", "diff", "--unified=0", base_ref] if base_ref != "HEAD" else ["git", "diff", "--unified=0", "HEAD"]
        diff_out = subprocess.check_output(
            diff_cmd, cwd=str(root), stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        print("❌ 无法获取 Git diff 信息（可能不在 Git 仓库内或没有改动）。")
        return

    if not diff_out.strip():
        print("💡 当前工作区干净，无未提交的 Git 代码变更。")
        return

    current_file = None
    changed_lines: Dict[str, List[int]] = {}

    for line in diff_out.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            changed_lines[current_file] = []
        elif line.startswith("@@ ") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                changed_lines[current_file].extend(range(start, start + count))

    impacted_symbols = set()
    for rel_file, lines in changed_lines.items():
        file_path = root / rel_file
        if not file_path.exists():
            continue
        outline = get_file_outline(file_path)

        file_symbols = []
        for indent, txt, lineno, kind, sym_name, *rest in outline:
            end_ln = rest[0] if rest else lineno + 50
            if any(lineno <= l <= end_ln for l in lines):
                if sym_name:
                    file_symbols.append((kind, sym_name))
                    impacted_symbols.add(sym_name)

        lines_preview = str(lines[:5]) + ("..." if len(lines) > 5 else "")
        entry = {"file": rel_file, "changed_lines": lines_preview, "kind": "impact"}
        if file_symbols:
            entry["symbols"] = [f"{k}:{s}" for k, s in file_symbols]
            entry["text"] = f"✏️  {rel_file} (改动行: {lines_preview}) → 涉及: {', '.join(entry['symbols'])}"
        else:
            entry["text"] = f"✏️  {rel_file} (改动行: {lines_preview})"
        fmt.add(entry)

    if impacted_symbols:
        fmt.add_footer(f"\n🔍 被波及符号: {', '.join(sorted(impacted_symbols))}")
        fmt.add_footer("提示: 使用 `callers <符号名>` 进一步追踪调用方影响链。")

    fmt.flush()


# ---- context (新增) ----
def cmd_context(symbol: str, target_dir: str, context_lines: int = 0, fmt: OutputFormatter = None):
    """提取符号的完整源码+行号范围，直接适配 edit-x。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"📋 [符号上下文提取] 目标: '{symbol}' 目录: {root}\n{'-' * 60}")

    found = False
    for path in iter_source_files(root, git_filter):
        result = extract_symbol_source(path, symbol)
        if result:
            found = True
            rel = safe_relative(path, root)
            source = result["source"]

            # 可选上下文扩展
            if context_lines > 0:
                lines = read_file_lines(path)
                ext_start = max(0, result["start_line"] - 1 - context_lines)
                ext_end = min(len(lines), result["end_line"] + context_lines)
                source = "\n".join(lines[ext_start:ext_end])
                result["start_line"] = ext_start + 1
                result["end_line"] = ext_end

            fmt.add({
                "file": rel,
                "symbol": symbol,
                "start_line": result["start_line"],
                "end_line": result["end_line"],
                "lang": result["lang"],
                "source": source,
                "kind": "context",
                "text": f"📄 {rel}:{result['start_line']}-{result['end_line']} ({result['lang']})\n{source}",
            })

    if not found:
        fmt.add_footer(f"未找到符号 '{symbol}'。请用 find-sym 模糊搜索确认符号名。")
    else:
        fmt.add_footer(f"\n✅ 找到 {len(fmt.items)} 处定义。上方源码可直接用于 edit-x 的 OLD 块。")
    fmt.flush()


# ---- summary (新增) ----
def cmd_summary(target_dir: str, fmt: OutputFormatter = None):
    """项目级快照摘要。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"📊 [项目快照摘要] {root}\n{'=' * 60}")

    lang_stats: Dict[str, int] = {}
    total_lines = 0
    total_files = 0
    all_files = iter_source_files(root, git_filter)

    for path in all_files:
        lang = detect_language(path)
        if lang:
            lang_stats[lang] = lang_stats.get(lang, 0) + 1
        total_files += 1
        try:
            total_lines += len(read_file_lines(path))
        except Exception:
            pass

    # 框架检测
    frameworks = detect_frameworks(root)

    # 入口文件检测
    entry_candidates = ["main.py", "app.py", "index.ts", "index.js", "main.go",
                        "main.rs", "src/main.py", "src/app.py", "src/index.ts",
                        "src/index.js", "src/main.go", "src/main.rs", "cmd/main.go",
                        "manage.py", "server.py", "bot.py"]
    entry_files = [e for e in entry_candidates if (root / e).exists()]

    # Git 状态
    git_info = {}
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root), stderr=subprocess.DEVNULL, text=True
        ).strip()
        git_info["branch"] = branch
        commit_count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(root), stderr=subprocess.DEVNULL, text=True
        ).strip()
        git_info["commits"] = commit_count
        last_commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %s", "HEAD"],
            cwd=str(root), stderr=subprocess.DEVNULL, text=True
        ).strip()
        git_info["last_commit"] = last_commit
    except Exception:
        pass

    # 构建输出
    summary_data = {
        "root": str(root),
        "total_files": total_files,
        "total_lines": total_lines,
        "languages": lang_stats,
        "frameworks": frameworks,
        "entry_files": entry_files,
        "git": git_info,
        "kind": "summary",
    }

    lines_out = []
    lines_out.append(f"  📁 项目根目录: {root}")
    lines_out.append(f"  📄 源码文件数: {total_files}")
    lines_out.append(f"  📏 代码总行数: {total_lines}")

    if lang_stats:
        sorted_langs = sorted(lang_stats.items(), key=lambda x: -x[1])
        lang_str = ", ".join(f"{l}({c})" for l, c in sorted_langs)
        lines_out.append(f"  🌐 语言分布:  {lang_str}")

    if frameworks:
        lines_out.append(f"  🔧 技术栈:    {', '.join(frameworks)}")

    if entry_files:
        lines_out.append(f"  🚀 入口文件:  {', '.join(entry_files)}")

    if git_info:
        lines_out.append(f"  🌿 Git 分支:  {git_info.get('branch', '?')} ({git_info.get('commits', '?')} commits)")
        if "last_commit" in git_info:
            lines_out.append(f"  📝 最新提交:  {git_info['last_commit']}")

    summary_data["text"] = "\n".join(lines_out)
    fmt.add(summary_data)
    fmt.flush()


# ---- changes (新增) ----
def cmd_changes(target_dir: str, base_ref: str = "HEAD~1", fmt: OutputFormatter = None):
    """对比任意 commit/分支的符号级变更。"""
    root = Path(target_dir).resolve()

    fmt.set_header(f"🔄 [符号级变更分析] {root} (对比: {base_ref})\n{'=' * 60}")

    try:
        diff_out = subprocess.check_output(
            ["git", "diff", "--unified=0", base_ref],
            cwd=str(root), stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        print(f"❌ 无法获取对比 {base_ref} 的 diff（可能不是 Git 仓库或基准无效）。")
        return

    if not diff_out.strip():
        print(f"💡 与 {base_ref} 没有差异。")
        return

    current_file = None
    changed_lines: Dict[str, List[int]] = {}

    for line in diff_out.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            changed_lines[current_file] = []
        elif line.startswith("@@ ") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                changed_lines[current_file].extend(range(start, start + count))

    for rel_file, ch_lines in sorted(changed_lines.items()):
        file_path = root / rel_file
        if not file_path.exists():
            fmt.add({"file": rel_file, "kind": "deleted", "text": f"🗑️  [DELETED] {rel_file}"})
            continue

        outline = get_file_outline(file_path)
        symbols_hit = []
        for indent, txt, lineno, kind, sym_name, *rest in outline:
            end_ln = rest[0] if rest else lineno + 50
            if any(lineno <= l <= end_ln for l in ch_lines):
                if sym_name:
                    symbols_hit.append(f"{kind}:{sym_name}")

        entry = {"file": rel_file, "changed_line_count": len(ch_lines), "kind": "change"}
        if symbols_hit:
            entry["symbols"] = symbols_hit
            entry["text"] = f"✏️  {rel_file} ({len(ch_lines)} lines) → {', '.join(symbols_hit)}"
        else:
            entry["text"] = f"✏️  {rel_file} ({len(ch_lines)} lines changed)"
        fmt.add(entry)

    fmt.add_footer(f"\n📊 共 {len(changed_lines)} 个文件有变更。")
    fmt.flush()


# ---- callers (新增) ----
def cmd_callers(symbol: str, target_dir: str, fmt: OutputFormatter = None):
    """反查所有调用指定符号的函数。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"📞 [调用方追踪] 谁调用了 '{symbol}'?  目录: {root}\n{'-' * 60}")

    # Python AST 精确分析
    for path in sorted(root.rglob("*.py")):
        if is_ignored(path, git_filter, root):
            continue
        results = find_python_callers(path, symbol, root)
        for r in results:
            fmt.add({
                "kind": "py-caller",
                "file": r["file"],
                "line": r["line"],
                "caller": r["caller"],
                "text": f"← {r['caller']}()  {r['file']}:{r['line']}  > {r['text']}",
            })

    # 其他语言词法查找
    word_pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for path in iter_source_files(root, git_filter):
        if path.suffix == ".py":
            continue
        lang = detect_language(path)
        if not lang:
            continue
        lines = read_file_lines(path)
        outline = get_file_outline(path)
        rel = safe_relative(path, root)

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if word_pattern.search(stripped) and not stripped.startswith(("//", "#", "/*")):
                # 找到所属函数
                enclosing = "<module>"
                for _indent, _txt, _lineno, _kind, _sym, *rest in outline:
                    _end = rest[0] if rest else _lineno + 200
                    if _lineno <= idx <= _end and _kind in ("func", "method"):
                        enclosing = _sym
                fmt.add({
                    "kind": lang,
                    "file": rel,
                    "line": idx,
                    "caller": enclosing,
                    "text": f"← {enclosing}()  {rel}:{idx}  > {stripped}",
                })

    if not fmt.items:
        fmt.add_footer(f"未找到 '{symbol}' 的调用方。")
    else:
        fmt.add_footer(f"\n✅ 找到 {len(fmt.items)} 处调用。")
    fmt.flush()


# ---- callees (新增) ----
def cmd_callees(symbol: str, target_dir: str, fmt: OutputFormatter = None):
    """列出指定函数内部调用的其他函数。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"📤 [被调用方分析] '{symbol}()' 调用了什么?  目录: {root}\n{'-' * 60}")

    found_func = False

    # Python AST
    for path in sorted(root.rglob("*.py")):
        if is_ignored(path, git_filter, root):
            continue
        callees = find_python_calls_in_function(path, symbol)
        if callees:
            found_func = True
            rel = safe_relative(path, root)
            for callee in callees:
                fmt.add({
                    "kind": "py-callee",
                    "file": rel,
                    "caller": symbol,
                    "callee": callee,
                    "text": f"→ {callee}()",
                })

    # 非 Python: 提取函数体中的调用
    if not found_func:
        for path in iter_source_files(root, git_filter):
            if path.suffix == ".py":
                continue
            result = extract_symbol_source(path, symbol)
            if result:
                found_func = True
                rel = safe_relative(path, root)
                # 从函数体中提取调用
                call_pattern = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
                body = result["source"]
                callees_found = set()
                for m in call_pattern.finditer(body):
                    name = m.group(1)
                    # 过滤掉关键字和自身
                    keywords = {"if", "for", "while", "switch", "catch", "return", "typeof",
                                "instanceof", "new", "delete", "throw", "await", symbol}
                    if name not in keywords and not name[0].isupper():  # 排除类型名
                        callees_found.add(name)
                for callee in sorted(callees_found):
                    fmt.add({
                        "kind": result["lang"],
                        "file": rel,
                        "caller": symbol,
                        "callee": callee,
                        "text": f"→ {callee}()",
                    })

    if not found_func:
        fmt.add_footer(f"未找到函数 '{symbol}' 的定义。请先用 find-def 确认。")
    elif not fmt.items:
        fmt.add_footer(f"函数 '{symbol}' 内部未调用其他函数。")
    else:
        fmt.add_footer(f"\n✅ {symbol}() 共调用 {len(fmt.items)} 个不同函数。")
    fmt.flush()


# ---- hotspots (新增) ----
def cmd_hotspots(target_dir: str, top_n: int = 20, fmt: OutputFormatter = None):
    """基于 git log 的高频修改热点分析。"""
    root = Path(target_dir).resolve()

    fmt.set_header(f"🔥 [Git 修改热点分析] {root} (Top {top_n})\n{'=' * 60}")

    try:
        log_out = subprocess.check_output(
            ["git", "log", "--pretty=format:", "--name-only", "-n", "500"],
            cwd=str(root), stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        print("❌ 无法获取 Git 历史（可能不在 Git 仓库内）。")
        return

    file_counts: Dict[str, int] = {}
    for line in log_out.splitlines():
        line = line.strip()
        if line and not is_ignored(Path(line)):
            file_counts[line] = file_counts.get(line, 0) + 1

    ranked = sorted(file_counts.items(), key=lambda x: -x[1])[:top_n]

    for rank, (file, count) in enumerate(ranked, 1):
        bar = "█" * min(count, 30)
        fmt.add({
            "rank": rank,
            "file": file,
            "changes": count,
            "kind": "hotspot",
            "text": f"  {rank:>3}. {count:>4}x  {bar}  {file}",
        })

    if not fmt.items:
        fmt.add_footer("没有足够的 Git 历史数据。")
    else:
        fmt.add_footer(f"\n💡 高频修改文件往往是技术债或核心模块，重构时优先关注。")
    fmt.flush()


# ---- todos (新增) ----
def cmd_todos(target_dir: str, fmt: OutputFormatter = None):
    """扫描全项目的 TODO/FIXME/HACK/XXX 标记。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"📝 [TODO/FIXME 扫描] {root}\n{'=' * 60}")

    tag_counts: Dict[str, int] = {}

    for path in iter_source_files(root, git_filter):
        lines = read_file_lines(path)
        rel = safe_relative(path, root)

        for idx, line in enumerate(lines, start=1):
            m = TODO_PATTERNS.search(line)
            if m:
                tag = m.group(1).upper()
                content = m.group(2).strip()
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                fmt.add({
                    "kind": tag.lower(),
                    "file": rel,
                    "line": idx,
                    "tag": tag,
                    "text": f"[{tag}] {rel}:{idx}  {content}",
                    "content": content,
                })

    if tag_counts:
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(tag_counts.items()))
        fmt.add_footer(f"\n📊 统计: {summary}")
    else:
        fmt.add_footer("✨ 没有找到 TODO/FIXME 标记，代码很干净！")
    fmt.flush()


# ---- struct (新增) ----
def cmd_struct(symbol: str, target_dir: str, fmt: OutputFormatter = None):
    """分析类/结构体的字段和属性。"""
    root = Path(target_dir).resolve()
    git_filter = GitIgnoreFilter(root)

    fmt.set_header(f"🏗️  [结构/字段分析] '{symbol}' 目录: {root}\n{'-' * 60}")

    found = False

    # Python: AST 分析类属性
    for path in sorted(root.rglob("*.py")):
        if is_ignored(path, git_filter, root):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content)
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == symbol:
                found = True
                rel = safe_relative(path, root)

                # __init__ 参数和 self.xxx 赋值
                fields = []
                methods = []
                class_vars = []

                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # 提取方法签名
                        args_list = [a.arg for a in item.args.args if a.arg != "self"]
                        ret = ""
                        if item.returns:
                            try:
                                ret = f" -> {ast.unparse(item.returns)}"
                            except Exception:
                                pass
                        methods.append(f"{'async ' if isinstance(item, ast.AsyncFunctionDef) else ''}def {item.name}({', '.join(args_list)}){ret}")

                        # 在 __init__ 中查找 self.xxx = ...
                        if item.name == "__init__":
                            for stmt in ast.walk(item):
                                if isinstance(stmt, ast.Assign):
                                    for target in stmt.targets:
                                        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                                            type_hint = ""
                                            fields.append(f"self.{target.attr}{type_hint}")

                    elif isinstance(item, ast.AnnAssign):
                        # 类级类型注解
                        target = item.target
                        if isinstance(target, ast.Name):
                            ann = ""
                            try:
                                ann = f": {ast.unparse(item.annotation)}"
                            except Exception:
                                pass
                            class_vars.append(f"{target.id}{ann}")

                    elif isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                val = ""
                                try:
                                    val = f" = {ast.unparse(item.value)}"
                                    if len(val) > 40:
                                        val = val[:37] + "..."
                                except Exception:
                                    pass
                                class_vars.append(f"{target.id}{val}")

                entry_lines = [f"📄 {rel}  class {symbol}:"]
                if class_vars:
                    entry_lines.append("  [类变量]")
                    for cv in class_vars:
                        entry_lines.append(f"    · {cv}")
                if fields:
                    entry_lines.append("  [实例属性 (from __init__)]")
                    for f in fields:
                        entry_lines.append(f"    · {f}")
                if methods:
                    entry_lines.append(f"  [方法] ({len(methods)} 个)")
                    for m in methods:
                        entry_lines.append(f"    · {m}")

                fmt.add({
                    "file": rel,
                    "symbol": symbol,
                    "kind": "class",
                    "class_vars": class_vars,
                    "fields": fields,
                    "methods": methods,
                    "text": "\n".join(entry_lines),
                })

    # 非 Python: 提取结构体/类定义的源码并解析字段
    if not found:
        for path in iter_source_files(root, git_filter):
            if path.suffix == ".py":
                continue
            result = extract_symbol_source(path, symbol)
            if result:
                found = True
                rel = safe_relative(path, root)
                fmt.add({
                    "file": rel,
                    "symbol": symbol,
                    "kind": "struct",
                    "start_line": result["start_line"],
                    "end_line": result["end_line"],
                    "source": result["source"],
                    "text": f"📄 {rel}:{result['start_line']}-{result['end_line']}\n{result['source']}",
                })

    if not found:
        fmt.add_footer(f"未找到类/结构体 '{symbol}'。请用 find-sym 确认名称。")
    fmt.flush()


# ======================================================================
# CLI 入口
# ======================================================================
def make_formatter(args) -> OutputFormatter:
    return OutputFormatter(
        as_json=getattr(args, "json", False),
        limit=getattr(args, "limit", DEFAULT_LIMIT),
    )


def add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出，方便程序化解析")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"最大输出条目数 (默认: {DEFAULT_LIMIT}，0=不限)")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Code-Intel v2: 工业级代码语义与结构分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Agent 决策指南:
  接手新项目       → summary → map --compact
  修改函数前取源码  → context <func>
  修改后查影响      → callers <func>
  理解函数做了什么  → outline <file> + callees <func>
  排查 bug 入口    → find-def <sym> → callers 反查
  高风险代码识别    → hotspots --top 20
  重构前影响评估    → impact --base main
"""
    )
    subparsers = parser.add_subparsers(dest="command")

    # map
    p_map = subparsers.add_parser("map", help="生成代码仓库全景骨架图")
    p_map.add_argument("path", nargs="?", default=".", help="目标路径")
    p_map.add_argument("--depth", type=int, default=4, help="最大目录深度 (默认: 4)")
    p_map.add_argument("--compact", action="store_true", help="紧凑模式 (剔除常量和装饰器)")
    p_map.add_argument("--lang", type=str, default=None, help="限定语言 (如: python,typescript)")
    p_map.add_argument("--stats", action="store_true", help="显示每文件行数统计")
    p_map.add_argument("--tree", action="store_true", help="树形目录模式")
    add_common_args(p_map)

    # find-def
    p_def = subparsers.add_parser("find-def", help="精确定位类/函数/接口定义")
    p_def.add_argument("symbol", help="符号名称")
    p_def.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_def)

    # find-ref
    p_ref = subparsers.add_parser("find-ref", help="精确查找符号调用点与引用")
    p_ref.add_argument("symbol", help="符号名称")
    p_ref.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_ref)

    # find-sym
    p_sym = subparsers.add_parser("find-sym", help="模糊/正则搜索符号名")
    p_sym.add_argument("pattern", help="正则表达式或符号名")
    p_sym.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_sym)

    # outline
    p_out = subparsers.add_parser("outline", help="单文件深度大纲分析")
    p_out.add_argument("file", help="目标文件路径")
    add_common_args(p_out)

    # deps
    p_dep = subparsers.add_parser("deps", help="模块依赖拓扑分析 (多语言)")
    p_dep.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_dep)

    # impact
    p_imp = subparsers.add_parser("impact", help="基于 Git 改动分析受波及的符号与调用方")
    p_imp.add_argument("path", nargs="?", default=".", help="目标路径")
    p_imp.add_argument("--base", type=str, default="HEAD", help="对比基准 (默认: HEAD)")
    add_common_args(p_imp)

    # context (新增)
    p_ctx = subparsers.add_parser("context", help="提取符号完整源码 (edit-x 搭档)")
    p_ctx.add_argument("symbol", help="函数/类名")
    p_ctx.add_argument("path", nargs="?", default=".", help="目标路径")
    p_ctx.add_argument("--extra", type=int, default=0, help="额外上下文行数 (前后各 N 行)")
    add_common_args(p_ctx)

    # summary (新增)
    p_sum = subparsers.add_parser("summary", help="项目级快照摘要")
    p_sum.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_sum)

    # changes (新增)
    p_chg = subparsers.add_parser("changes", help="对比任意 commit/分支的符号级变更")
    p_chg.add_argument("path", nargs="?", default=".", help="目标路径")
    p_chg.add_argument("--base", type=str, default="HEAD~1", help="对比基准 (默认: HEAD~1)")
    add_common_args(p_chg)

    # callers (新增)
    p_clr = subparsers.add_parser("callers", help="反查所有调用指定符号的函数")
    p_clr.add_argument("symbol", help="被调用的符号名")
    p_clr.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_clr)

    # callees (新增)
    p_cle = subparsers.add_parser("callees", help="列出函数内部调用的其他函数")
    p_cle.add_argument("symbol", help="函数名")
    p_cle.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_cle)

    # hotspots (新增)
    p_hot = subparsers.add_parser("hotspots", help="基于 Git 历史的修改热点分析")
    p_hot.add_argument("path", nargs="?", default=".", help="目标路径")
    p_hot.add_argument("--top", type=int, default=20, help="显示前 N 个热点 (默认: 20)")
    add_common_args(p_hot)

    # todos (新增)
    p_todo = subparsers.add_parser("todos", help="全项目 TODO/FIXME/HACK/XXX 扫描")
    p_todo.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_todo)

    # struct (新增)
    p_str = subparsers.add_parser("struct", help="分析类/结构体字段与属性")
    p_str.add_argument("symbol", help="类/结构体名")
    p_str.add_argument("path", nargs="?", default=".", help="目标路径")
    add_common_args(p_str)

    args = parser.parse_args(argv)
    fmt = make_formatter(args)

    if args.command == "map":
        cmd_map(args.path, args.depth, args.compact, args.lang, fmt, args.stats, args.tree)
    elif args.command == "find-def":
        cmd_find_def(args.symbol, args.path, fmt)
    elif args.command == "find-ref":
        cmd_find_ref(args.symbol, args.path, fmt)
    elif args.command == "find-sym":
        cmd_find_sym(args.pattern, args.path, fmt)
    elif args.command == "outline":
        cmd_outline(args.file, fmt)
    elif args.command == "deps":
        cmd_deps(args.path, fmt)
    elif args.command == "impact":
        cmd_impact(args.path, args.base, fmt)
    elif args.command == "context":
        cmd_context(args.symbol, args.path, args.extra, fmt)
    elif args.command == "summary":
        cmd_summary(args.path, fmt)
    elif args.command == "changes":
        cmd_changes(args.path, args.base, fmt)
    elif args.command == "callers":
        cmd_callers(args.symbol, args.path, fmt)
    elif args.command == "callees":
        cmd_callees(args.symbol, args.path, fmt)
    elif args.command == "hotspots":
        cmd_hotspots(args.path, args.top, fmt)
    elif args.command == "todos":
        cmd_todos(args.path, fmt)
    elif args.command == "struct":
        cmd_struct(args.symbol, args.path, fmt)
    else:
        parser.print_help()


# ======================================================================
# 进程内入口（intel-x 协议用）
# ======================================================================

# 允许通过 intel-x 协议调用的子命令白名单。与 main() 的 subparsers 一一对应。
INTEL_SUBCOMMANDS = frozenset({
    "map", "find-def", "find-ref", "find-sym", "outline", "deps", "impact",
    "context", "summary", "changes", "callers", "callees", "hotspots",
    "todos", "struct",
})


def run_intel(argv: List[str]) -> str:
    """在进程内执行一次 code-intel 子命令，返回捕获到的文本输出。

    与 CLI 的区别：不打印、不退出进程。argparse 在参数错误时会调用
    ``parser.exit()``（抛 SystemExit），这里一并捕获转成文本，绝不让它
    掀翻调用方（bot 事件循环所在进程）。子命令白名单校验由调用方（agent_dispatch）
    负责；这里对空/未知子命令也做兜底，返回 help 文本而不是抛异常。
    """
    if not argv:
        return "code-intel: 缺少子命令。可用: " + ", ".join(sorted(INTEL_SUBCOMMANDS))

    buffer = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = buffer
    sys.stderr = buffer
    try:
        main(list(argv))
    except SystemExit:
        # argparse 的正常退出路径（--help、参数错误）都走 SystemExit，
        # 输出已经写进 buffer，吞掉退出码即可。
        pass
    except Exception as exc:  # noqa: BLE001 —— 分析失败也要回文本，不能崩事件循环
        return f"{buffer.getvalue()}\n[code-intel 执行异常] {type(exc).__name__}: {exc}".strip()
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return buffer.getvalue().strip() or "(code-intel 无输出)"


if __name__ == "__main__":
    _force_utf8_stdout()
    main()