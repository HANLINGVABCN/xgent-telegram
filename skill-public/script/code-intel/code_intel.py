#!/usr/bin/env python3
"""
Code-Intel: 工业级代码结构分析与语义检索工具
专为大模型/智能 Agent 设计的高效上下文压缩与代码导航引擎。

子命令：
  map       生成代码仓库骨架图 (Repo Map)，支持紧凑模式与语言过滤
  find-def  精确定位类、函数、接口、结构体声明 (跨语言)
  find-ref  精确查找符号调用点与引用 (AST/词法级别)
  find-sym  模糊/正则搜索符号名 (函数、类、接口)
  outline   单文件深度大纲分析 (含装饰器、签名、常量、Docstring)
  deps      模块依赖拓扑与 import 调用链分析
  impact    基于 Git 改动分析受影响的符号与调用方
"""

import os
import sys
import ast
import re
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Set

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    ".idea", ".vscode", "dist", "build", ".next", ".cache",
    "xgent_storage", "logs", "target", "vendor", ".tox",
    ".mypy_cache", ".pytest_cache", "coverage", ".turbo"
}

IGNORE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum"
}

LANG_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".mts", ".cts"},
    "go": {".go"},
    "rust": {".rs"},
    "c_cpp": {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"},
    "java": {".java"},
    "shell": {".sh", ".bash", ".zsh"},
    "php": {".php"},
}


def is_ignored(path: Path) -> bool:
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
    return path.name in IGNORE_FILES


def detect_language(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    for lang, exts in LANG_EXTENSIONS.items():
        if ext in exts:
            return lang
    return None


# ----------------------------------------------------------------------
# 1. Python AST 深入解析器
# ----------------------------------------------------------------------
class PythonDeepVisitor(ast.NodeVisitor):
    def __init__(self, include_constants: bool = True):
        self.outline = []  # list of (indent, text, lineno, kind, name)
        self.indent = 0
        self.include_constants = include_constants

    def _get_doc(self, node) -> str:
        doc = ast.get_docstring(node)
        if doc:
            first_line = doc.strip().split("\n")[0].strip()
            if first_line:
                return f"  # {first_line[:70]}"
        return ""

    def _format_args(self, args_node) -> str:
        args = []
        # Positional / normal args
        for a in args_node.args:
            s = a.arg
            if a.annotation:
                try:
                    s += f": {ast.unparse(a.annotation)}"
                except Exception:
                    pass
            args.append(s)
        # Varargs (*args)
        if args_node.vararg:
            args.append(f"*{args_node.vararg.arg}")
        # Keyword-only args
        for a in args_node.kwonlyargs:
            s = a.arg
            if a.annotation:
                try:
                    s += f": {ast.unparse(a.annotation)}"
                except Exception:
                    pass
            args.append(s)
        # Kwargs (**kwargs)
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
            self.outline.append((self.indent, dec, node.lineno, "decorator", ""))
        self.outline.append((self.indent, f"class {node.name}{base_str}:{doc}", node.lineno, "class", node.name))

        self.indent += 1
        self.generic_visit(node)
        self.indent -= 1

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
            self.outline.append((self.indent, dec, node.lineno, "decorator", ""))
        self.outline.append((self.indent, f"{prefix}{node.name}({args_str}){ret_str}:{doc}", node.lineno, "func", node.name))

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
                    self.outline.append((0, f"{target.id}{val_str}", node.lineno, "const", target.id))


def parse_python_file(file_path: Path, include_constants: bool = True):
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=str(file_path))
        visitor = PythonDeepVisitor(include_constants=include_constants)
        visitor.visit(tree)
        return visitor.outline
    except Exception:
        return []


# ----------------------------------------------------------------------
# 2. 多语言通用词法与正则解析器 (TS/JS/Go/Rust/C++/Java/PHP/Shell)
# ----------------------------------------------------------------------
REGEX_PATTERNS = {
    "typescript": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)"), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_$]+)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_$]+)"), "interface"),
        (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z0-9_$]+)\s*="), "type"),
        (re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z0-9_$]+)"), "enum"),
    ],
    "javascript": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\((.*?)\)"), "func"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "func"),
        (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)"), "class"),
    ],
    "go": [
        (re.compile(r"^\s*func\s+\((?:.*?\s+)?\*?([A-Za-z0-9_$]+)\)\s+([A-Za-z0-9_$]+)\s*\((.*?)\)"), "method"),
        (re.compile(r"^\s*func\s+([A-Za-z0-9_$]+)\s*\((.*?)\)"), "func"),
        (re.compile(r"^\s*type\s+([A-Za-z0-9_$]+)\s+struct"), "struct"),
        (re.compile(r"^\s*type\s+([A-Za-z0-9_$]+)\s+interface"), "interface"),
    ],
    "rust": [
        (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_$]+)\s*\((.*?)\)"), "func"),
        (re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z0-9_$]+)"), "struct"),
        (re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z0-9_$]+)"), "enum"),
        (re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z0-9_$]+)"), "trait"),
        (re.compile(r"^\s*impl(?:\s+<.*?>)?\s+([A-Za-z0-9_$]+)"), "impl"),
    ],
    "java": [
        (re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:class|interface|enum)\s+([A-Za-z0-9_$]+)"), "class"),
        (re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:synchronized\s+)?[\w<>[\]]+\s+([A-Za-z0-9_$]+)\s*\((.*?)\)\s*(?:throws\s+\w+)?\s*\{?"), "func"),
    ],
    "shell": [
        (re.compile(r"^\s*(?:function\s+)?([A-Za-z0-9_-]+)\s*\(\)\s*\{?"), "func"),
    ]
}


def parse_generic_file(file_path: Path, lang: str):
    outline = []
    patterns = REGEX_PATTERNS.get(lang, [])
    if not patterns:
        return outline

    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return outline

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue

        for p, kind in patterns:
            m = p.match(stripped)
            if m:
                sym_name = m.group(1)
                text = stripped.rstrip("{").strip()
                outline.append((0, text, idx, kind, sym_name))
                break
    return outline


def get_file_outline(file_path: Path, include_constants: bool = True):
    lang = detect_language(file_path)
    if lang == "python":
        return parse_python_file(file_path, include_constants)
    elif lang:
        return parse_generic_file(file_path, lang)
    return []


# ----------------------------------------------------------------------
# 3. 核心功能实现 (map, find-def, find-ref, find-sym, outline, deps, impact)
# ----------------------------------------------------------------------
def cmd_map(target_dir: str, max_depth: int = 4, compact: bool = False, lang_filter: Optional[str] = None):
    root = Path(target_dir).resolve()
    if not root.exists():
        print(f"❌ 路径不存在: {root}")
        return

    filter_langs = set(lang_filter.split(",")) if lang_filter else None
    print(f"📦 [Repo Map] 根目录: {root} (最大深度: {max_depth})")
    print("=" * 60)

    total_files = 0
    total_symbols = 0

    for path in sorted(root.rglob("*")):
        if is_ignored(path) or path.is_dir():
            continue

        try:
            rel_path = path.relative_to(root)
        except ValueError:
            rel_path = path

        if len(rel_path.parts) > max_depth:
            continue

        lang = detect_language(path)
        if filter_langs and (not lang or lang not in filter_langs):
            continue

        outline = get_file_outline(path, include_constants=not compact)
        ext = path.suffix.lower()

        if outline or ext in {".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".md", ".sql"}:
            total_files += 1
            print(f"\n📄 {rel_path}:")
            for indent, line, lineno, kind, sym_name in outline:
                if kind == "decorator" and compact:
                    continue
                total_symbols += 1
                space = "  " * (indent + 1)
                print(f"{space}L{lineno:<4} {line}")

    print("\n" + "=" * 60)
    print(f"📊 统计: 共扫描 {total_files} 个文件, 提取 {total_symbols} 个核心符号结构。")


def cmd_find_def(symbol: str, target_dir: str):
    root = Path(target_dir).resolve()
    print(f"🎯 [查找定义] 目标: '{symbol}' 目录: {root}")
    print("-" * 60)
    found = 0

    for path in sorted(root.rglob("*")):
        if is_ignored(path) or path.is_dir():
            continue

        outline = get_file_outline(path, include_constants=True)
        rel_path = path.relative_to(root)

        for indent, line, lineno, kind, sym_name in outline:
            if sym_name == symbol:
                found += 1
                print(f"  [{kind.upper():<9}] {rel_path}:{lineno}")
                print(f"             > {line}")

    if found == 0:
        print(f"未找到符号 '{symbol}' 的确切定义。")
    else:
        print(f"\n✅ 找到 {found} 处匹配定义。")


def cmd_find_ref(symbol: str, target_dir: str):
    root = Path(target_dir).resolve()
    print(f"🔗 [查找调用/引用] 目标: '{symbol}' 目录: {root}")
    print("-" * 60)
    found = 0

    # 1. 对 Python 执行精确 AST Call & Name Walk
    for path in sorted(root.rglob("*.py")):
        if is_ignored(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            tree = ast.parse(content, filename=str(path))
        except Exception:
            continue

        rel_path = path.relative_to(root)
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
                    found += 1
                    line_txt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    print(f"  [PY-CALL]  {rel_path}:{node.lineno}")
                    print(f"             > {line_txt}")

    # 2. 对其他语言执行精准单词边界检索
    word_pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in sorted(root.rglob("*")):
        if is_ignored(path) or path.is_dir() or path.suffix == ".py":
            continue
        lang = detect_language(path)
        if not lang:
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue

        rel_path = path.relative_to(root)
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if word_pattern.search(stripped) and not stripped.startswith(("//", "#", "/*")):
                found += 1
                print(f"  [{lang.upper():<9}] {rel_path}:{idx}")
                print(f"             > {stripped}")

    if found == 0:
        print(f"未找到 '{symbol}' 的调用或引用。")
    else:
        print(f"\n✅ 找到 {found} 处引用。")


def cmd_find_sym(pattern: str, target_dir: str):
    root = Path(target_dir).resolve()
    regex = re.compile(pattern, re.IGNORECASE)
    print(f"🔍 [模糊/正则符号搜索] 表达式: /{pattern}/i 目录: {root}")
    print("-" * 60)
    found = 0

    for path in sorted(root.rglob("*")):
        if is_ignored(path) or path.is_dir():
            continue

        outline = get_file_outline(path, include_constants=True)
        rel_path = path.relative_to(root)

        for indent, line, lineno, kind, sym_name in outline:
            if regex.search(sym_name) or regex.search(line):
                found += 1
                print(f"  [{kind.upper():<9}] {rel_path}:{lineno}  ->  {line}")

    print("-" * 60)
    print(f"✅ 匹配到 {found} 个符号。")


def cmd_outline(file_path: str):
    target = Path(file_path).resolve()
    if not target.exists() or target.is_dir():
        print(f"❌ 文件不存在: {target}")
        return

    print(f"📄 [单文件深度大纲] {target}")
    print("=" * 60)
    outline = get_file_outline(target, include_constants=True)

    if not outline:
        print("未提取到结构化符号或该文件格式不支持。")
        return

    for indent, line, lineno, kind, sym_name in outline:
        space = "  " * indent
        print(f"L{lineno:<4} {space}{line}")


def cmd_deps(target_dir: str):
    root = Path(target_dir).resolve()
    print(f"🌲 [依赖拓扑分析] 目录: {root}")
    print("=" * 60)

    import_graph: Dict[str, Set[str]] = {}

    for path in sorted(root.rglob("*.py")):
        if is_ignored(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content, filename=str(path))
        except Exception:
            continue

        rel_path = str(path.relative_to(root))
        deps = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    deps.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                deps.add(mod)

        if deps:
            import_graph[rel_path] = deps

    for file, deps in sorted(import_graph.items()):
        print(f"\n📄 {file}:")
        internal_deps = [d for d in sorted(deps) if (root / (d.replace(".", "/") + ".py")).exists() or (root / d.split(".")[0]).exists()]
        external_deps = [d for d in sorted(deps) if d not in internal_deps]

        if internal_deps:
            print("  ├── 🏠 项目内引用: " + ", ".join(internal_deps))
        if external_deps:
            print("  └── 📦 第三方依赖: " + ", ".join(external_deps[:10]) + ("..." if len(external_deps) > 10 else ""))


def cmd_impact(target_dir: str):
    root = Path(target_dir).resolve()
    print(f"💥 [Git 改动影响分析] 目录: {root}")
    print("=" * 60)

    try:
        diff_out = subprocess.check_output(
            ["git", "diff", "--unified=0", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True
        )
    except Exception:
        print("❌ 无法获取 Git diff 信息（可能不在 Git 仓库内或没有改动）。")
        return

    if not diff_out.strip():
        print("💡 当前工作区干净，无未提交的 Git 代码变更。")
        return

    # 解析改动的文件与行号
    current_file = None
    changed_lines: Dict[str, List[int]] = {}

    for line in diff_out.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            changed_lines[current_file] = []
        elif line.startswith("@@ ") and current_file:
            # @@ -10,0 +12,5 @@
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
        print(f"\n✏️  修改文件: {rel_file} (改动行: {lines[:5]}{'...' if len(lines)>5 else ''})")

        file_symbols = []
        for indent, txt, lineno, kind, sym_name in outline:
            if any(lineno <= l <= lineno + 50 for l in lines):
                if sym_name:
                    file_symbols.append((kind, sym_name))
                    impacted_symbols.add(sym_name)

        if file_symbols:
            print("   ↳ 涉及的局部符号: " + ", ".join([f"{k}:{s}" for k, s in file_symbols]))

    if impacted_symbols:
        print("\n🔍 正在全工程反查波及的调用方...")
        for sym in sorted(impacted_symbols):
            print(f"\n[被波及符号] {sym}:")
            cmd_find_ref(sym, str(root))


def main():
    parser = argparse.ArgumentParser(description="Code-Intel: 工业级代码语义与结构分析工具")
    subparsers = parser.add_subparsers(dest="command")

    # map
    p_map = subparsers.add_parser("map", help="生成代码仓库全景骨架图")
    p_map.add_argument("path", nargs="?", default=".", help="目标路径")
    p_map.add_argument("--depth", type=int, default=4, help="最大目录深度 (默认: 4)")
    p_map.add_argument("--compact", action="store_true", help="紧凑模式 (剔除常量和装饰器，极度节省 Token)")
    p_map.add_argument("--lang", type=str, default=None, help="限定语言 (如: python,typescript)")

    # find-def
    p_def = subparsers.add_parser("find-def", help="精确定位类/函数/接口定义")
    p_def.add_argument("symbol", help="符号名称")
    p_def.add_argument("path", nargs="?", default=".", help="目标路径")

    # find-ref
    p_ref = subparsers.add_parser("find-ref", help="精确查找符号调用点与引用")
    p_ref.add_argument("symbol", help="符号名称")
    p_ref.add_argument("path", nargs="?", default=".", help="目标路径")

    # find-sym
    p_sym = subparsers.add_parser("find-sym", help="模糊/正则搜索符号名")
    p_sym.add_argument("pattern", help="正则表达式或符号名")
    p_sym.add_argument("path", nargs="?", default=".", help="目标路径")

    # outline
    p_out = subparsers.add_parser("outline", help="单文件深度大纲分析")
    p_out.add_argument("file", help="目标文件路径")

    # deps
    p_dep = subparsers.add_parser("deps", help="模块依赖拓扑分析")
    p_dep.add_argument("path", nargs="?", default=".", help="目标路径")

    # impact
    p_imp = subparsers.add_parser("impact", help="基于 Git 改动分析受波及的符号与调用方")
    p_imp.add_argument("path", nargs="?", default=".", help="目标路径")

    args = parser.parse_args()
    if args.command == "map":
        cmd_map(args.path, args.depth, args.compact, args.lang)
    elif args.command == "find-def":
        cmd_find_def(args.symbol, args.path)
    elif args.command == "find-ref":
        cmd_find_ref(args.symbol, args.path)
    elif args.command == "find-sym":
        cmd_find_sym(args.pattern, args.path)
    elif args.command == "outline":
        cmd_outline(args.file)
    elif args.command == "deps":
        cmd_deps(args.path)
    elif args.command == "impact":
        cmd_impact(args.path)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()