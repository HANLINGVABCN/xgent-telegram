"""Offline maintenance entrypoint: no application section imports or writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .data_bundles import BundleError, DataBundles
from .data_discovery import DiscoveredBundles, discover_instance
from .data_lock import DataLease
from .data_paths import DataPaths


def main(argv=None, code_root=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from .data_migration import MigrationMenu

        return MigrationMenu(code_root or Path(__file__).resolve().parents[1]).run()
    parser = argparse.ArgumentParser(prog="xgent data")
    sub = parser.add_subparsers(dest="action", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("target", nargs="?")
    migrate.add_argument("--dry-run", action="store_true")
    export = sub.add_parser("export")
    export.add_argument("--output")
    export.add_argument(
        "--with-command-outputs", "--include-outputs", action="store_true"
    )
    export.add_argument("--yes", action="store_true", help="确认导出含密钥数据")
    restore = sub.add_parser("import")
    restore.add_argument("bundle")
    restore.add_argument(
        "--yes", action="store_true", help="确认整套切换，不合并现有数据"
    )
    verify = sub.add_parser("verify")
    verify.add_argument("bundle")
    sub.add_parser("paths")
    args = parser.parse_args(arguments)
    code = Path(code_root or Path(__file__).resolve().parents[1])
    try:
        paths = (
            discover_instance(code)
            if args.action == "export"
            else DataPaths.discover(code)
        )
        if args.action == 'migrate':
            raise BundleError('已恢复单目录模式，无需迁出；使用数据导入/导出菜单即可')
        bundles = (
            DiscoveredBundles(paths) if args.action == "export" else DataBundles(paths)
        )
        if args.action == "paths":
            result = {
                "code_dir": str(code),
                "data_dir": str(paths.root),
                "legacy": paths.legacy,
            }
        elif args.action == "verify":
            staging, manifest = bundles.prepare_import(args.bundle)
            import shutil

            shutil.rmtree(staging)
            result = {"ok": True, "manifest": manifest}
        else:
            if args.action in {"export", "import"} and not args.yes:
                raise BundleError(
                    "请用 --yes 确认：备份包含 .env/Bot/API 密钥；导入按整套恢复并保留旧目录，不合并。"
                )
            if args.action == 'export':
                result = bundles.export(args.output, outputs=args.with_command_outputs)
            else:
                with DataLease(paths.root, exclusive=True):
                    staging, manifest = bundles.prepare_import(args.bundle)
                    result = bundles.activate(staging)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BundleError, ValueError, OSError) as exc:
        print("数据操作未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
