import functools
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = (ROOT / "install.sh").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def bash_harness_usable() -> bool:
    """这台机器上的 bash 能不能承载"跑一段脚本并断言行为"这件事。

    Windows 上由原生 Python 拉起 Git Bash 时，shell 函数在命令替换里看不见、
    函数内的 "$@" 会展开成空串——脚本本身没问题，是这条调用链坏了。与其让
    这些测试长期红着（红着就没人能靠它判断改动有没有问题），不如先探一下，
    探不通就明确跳过。Linux/macOS 上这个探针总是通过，测试照常跑。
    """
    if not shutil.which("bash"):
        return False
    probe = 'f() { printf "%s" "$*"; }\ng() { printf ok; }\nout="$(g)"\nf A B\nprintf "|%s" "$out"\n'
    try:
        result = subprocess.run(
            ["bash", "-c", probe], text=True, capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout == "A B|ok"


@functools.lru_cache(maxsize=1)
def can_make_executables() -> bool:
    """能不能造出真的可执行的 mock 脚本（Windows 上 chmod +x 不生效）。"""
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            probe = Path(temp_dir) / "probe.sh"
            probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            probe.chmod(0o755)
            return os.access(probe, os.X_OK)
    except Exception:
        return False


requires_bash_harness = unittest.skipUnless(
    bash_harness_usable(), "本机的 bash 调用链不可用（函数在 $() 里丢失）"
)
requires_executables = unittest.skipUnless(
    can_make_executables(), "本平台无法创建可执行的 mock 脚本"
)


class InstallScriptMigrationTests(unittest.TestCase):
    def test_launchers_force_canonical_entrypoint(self):
        self.assertRegex(
            INSTALL_SCRIPT,
            r'XGENT_APP_ENTRY="\$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \\\s+run_bot_python python',
        )
        self.assertRegex(
            INSTALL_SCRIPT,
            r'XGENT_APP_ENTRY="\$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \\\s+run_bot_python pm2 start',
        )

    def test_pm2_restart_overwrites_legacy_entrypoint_environment(self):
        self.assertIn(
            'TELEGRAM_AI_BOT_APP_ENTRY="" XGENT_APP_ENTRY="$APP_ENTRY"',
            INSTALL_SCRIPT,
        )

    def test_pm2_install_recovers_nonstandard_npm_global_bin(self):
        self.assertIn('npm prefix -g', INSTALL_SCRIPT)
        self.assertIn('npm root -g', INSTALL_SCRIPT)

        ensure_pm2 = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('ensure_pm2() {'):
            INSTALL_SCRIPT.index('setup_pm2_startup() {')
        ]
        self.assertGreaterEqual(
            ensure_pm2.count('refresh_npm_global_bin_path'),
            2,
            'PATH should be refreshed both before and after npm installs PM2',
        )

    @requires_bash_harness
    @requires_executables
    def test_npm_prefix_bin_is_added_to_path_and_pm2_is_discovered(self):
        helpers = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('prepend_path_dir() {'):
            INSTALL_SCRIPT.index('ensure_pm2() {')
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mock_bin = root / "mock-bin"
            npm_prefix = root / "user-packages" / "node"
            pm2_bin = npm_prefix / "bin" / "pm2"
            mock_bin.mkdir()
            pm2_bin.parent.mkdir(parents=True)

            npm = mock_bin / "npm"
            npm.write_text(
                """#!/bin/sh
case "$1 $2" in
  'prefix -g') printf '%s\n' "$MOCK_NPM_PREFIX" ;;
  'root -g') printf '%s\n' "$MOCK_NPM_PREFIX/lib/node_modules" ;;
  *) exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            pm2_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            npm.chmod(0o755)
            pm2_bin.chmod(0o755)

            script = f'''set -eu
command_exists() {{ command -v "$1" >/dev/null 2>&1; }}
run_privileged() {{ "$@"; }}
success() {{ :; }}
warn() {{ :; }}
{helpers}
before="$(command -v pm2 2>/dev/null || true)"
refresh_npm_global_bin_path
after="$(command -v pm2)"
printf 'before=%s\nafter=%s\npath=%s\n' "$before" "$after" "$PATH"
'''
            env = os.environ.copy()
            env["MOCK_NPM_PREFIX"] = str(npm_prefix)
            env["PATH"] = f"{mock_bin}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            values = dict(line.split("=", 1) for line in result.stdout.splitlines())
            self.assertEqual("", values["before"])
            self.assertEqual(str(pm2_bin), values["after"])
            self.assertTrue(values["path"].startswith(f"{npm_prefix / 'bin'}:"))

    @requires_bash_harness
    @requires_executables
    def test_nonstandard_pm2_gets_persistent_command_link(self):
        helpers = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('prepend_path_dir() {'):
            INSTALL_SCRIPT.index('ensure_pm2() {')
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pm2_bin = root / "npm-prefix" / "bin" / "pm2"
            link_dir = root / "standard-bin"
            pm2_bin.parent.mkdir(parents=True)
            link_dir.mkdir()
            pm2_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pm2_bin.chmod(0o755)

            script = f'''set -eu
command_exists() {{ command -v "$1" >/dev/null 2>&1; }}
run_privileged() {{ "$@"; }}
success() {{ :; }}
warn() {{ :; }}
{helpers}
persist_pm2_command
'''
            env = os.environ.copy()
            env["XGENT_PM2_LINK_DIR"] = str(link_dir)
            env["PATH"] = f"{pm2_bin.parent}:{link_dir}:/usr/bin:/bin"
            subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            command_link = link_dir / "pm2"
            self.assertTrue(command_link.is_symlink())
            self.assertEqual(pm2_bin.resolve(), command_link.resolve())

    def test_child_processes_do_not_keep_legacy_entrypoint_variable(self):
        self.assertGreaterEqual(
            INSTALL_SCRIPT.count('-u TELEGRAM_AI_BOT_APP_ENTRY'),
            6,
        )
        self.assertIn(
            'unset XGENT_APP_ENTRY TELEGRAM_AI_BOT_APP_ENTRY || true',
            INSTALL_SCRIPT,
        )


def write_lf(path: Path, body: str) -> None:
    """按 LF 写文件。

    Path.write_text 默认走平台换行翻译，在 Windows 上会写出 CRLF——沙箱里的
    .env 于是每个值都多带一个回车符，测的就不再是被测逻辑本身了。
    """
    path.write_bytes(body.encode("utf-8"))


def make_executable(path: Path, body: str) -> bool:
    """写一个可执行的 mock 脚本；平台不支持执行位时返回 False。"""
    write_lf(path, body)
    path.chmod(0o755)
    return os.access(path, os.X_OK)


class InstallScriptLibraryMixin:
    """把 install.sh 当函数库 source 进来，直接调它的判定函数。

    这些逻辑的 bug（"venv 里的 python3 抢了系统 python3"就是其中一个）靠对
    脚本正文做正则断言是抓不到的——必须真的跑一遍。
    """

    @staticmethod
    def run_lib(script_body: str, sandbox: Path, env_extra=None, path=None):
        # 变量在脚本里 export，而不是走 subprocess 的 env：Windows 上原生
        # Python 起 MSYS2 的 bash.EXE 时，附加的环境变量到不了子 shell。
        exports = ['export XGENT_INSTALL_SH_LIB=1']
        if path is not None:
            exports.append(f'export PATH={shlex.quote(path)}')
        for key, value in (env_extra or {}).items():
            exports.append(f'export {key}={shlex.quote(value)}')
        script = "set -u\n" + "\n".join(exports) + "\nsource ./install.sh\n" + script_body
        return subprocess.run(
            ["bash", "-c", script],
            cwd=sandbox,
            text=True,
            # 脚本的提示与状态文案是中文；不钉死 UTF-8 的话，Windows 上会用
            # 本地代码页解码并直接抛 UnicodeDecodeError。
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
        )

    def sandbox(self, temp_dir: str) -> Path:
        root = Path(temp_dir)
        shutil.copy(ROOT / "install.sh", root / "install.sh")
        (root / "bin").mkdir(exist_ok=True)
        write_lf(root / "bin" / "xgent", "#!/bin/sh\n")
        return root

    @staticmethod
    def temp_dir():
        # Windows 上 bash 退出后目录可能还被短暂占用，清理失败不该判测试挂掉。
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


@requires_bash_harness
class PythonDiscoveryTests(InstallScriptLibraryMixin, unittest.TestCase):
    """选解释器不能被 venv 抢走。

    用户报告的"时好时坏"就是这条：SSH 干净 shell 里 PATH 首位是
    /usr/local/bin，`python3` 是 3.11，装得上；从跑在 venv 里的 Bot 进程调起
    同一个脚本时 PATH 首位变成 venv/bin，`python3` 是创建 venv 那天的 3.9.2，
    于是同一台机器同一份脚本报"需要 Python 3.10"。
    """

    def test_venv_python_is_recognised_without_resolving_symlinks(self):
        # venv/bin/python3 往往就是指向 /usr/bin/pythonX.Y 的软链，
        # 先 readlink -f 再判断就永远认不出它来自 venv。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'for p in "$VENV_DIR/bin/python3" /usr/bin/python3 '
                '/home/x/other/venv/bin/python3.11 /usr/local/bin/python3; do\n'
                '  if is_project_venv_python "$p"; then echo "venv $p"; else echo "sys $p"; fi\n'
                'done\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            lines = result.stdout.split()
            self.assertEqual("venv", lines[0])
            self.assertEqual("sys", lines[2])
            self.assertEqual("venv", lines[4])
            self.assertEqual("sys", lines[6])

    def test_python_is_preferred_over_a_newer_but_incomplete_interpreter(self):
        # `python3` 必须排在带版本号的候选前面：它是系统默认解释器，配套的
        # python3-venv / python3-pip 一定装齐了。反过来 Debian 上
        # `apt install python3.13` 不会带 python3.13-venv，挑中它就建不了 venv。
        names = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('python_candidate_names() {'):
            INSTALL_SCRIPT.index('python_search_dirs() {')
        ]
        line = [ln for ln in names.splitlines() if "printf" in ln][0]
        self.assertLess(line.index("python3\n".rstrip()), line.index("python3.14"))

    @requires_executables
    def test_discovery_skips_a_stale_venv_shadowing_python3(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            venv_bin = root / "venv" / "bin"
            sys_bin = root / "sysbin"
            venv_bin.mkdir(parents=True)
            sys_bin.mkdir()

            old = make_executable(venv_bin / "python3", (
                '#!/bin/sh\n'
                'case "$*" in\n'
                '  *"version_info >= (3, 10)"*) exit 1 ;;\n'
                '  --version) echo "Python 3.9.2" ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'
            ))
            new = make_executable(sys_bin / "python3", (
                '#!/bin/sh\n'
                'case "$*" in\n'
                '  *"version_info >= (3, 10)"*) exit 0 ;;\n'
                '  --version) echo "Python 3.11.15" ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'
            ))
            if not (old and new):
                self.skipTest("平台不支持可执行位，无法构造 mock 解释器")

            # 重现用户的环境：venv/bin 排在 PATH 最前面。
            path = f"{venv_bin}:{sys_bin}:/usr/bin:/bin"
            result = self.run_lib('discover_python\n', root, path=path)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(str(sys_bin / "python3"), result.stdout.strip())

    @requires_executables
    def test_discovery_looks_past_the_first_path_hit(self):
        # command -v 只给第一个命中；venv 排在最前面时它后面的解释器全被挡住。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            shadow = root / "shadow"
            good = root / "good"
            shadow.mkdir()
            good.mkdir()
            a = make_executable(shadow / "python3", '#!/bin/sh\nexit 1\n')
            b = make_executable(good / "python3", (
                '#!/bin/sh\n'
                'case "$*" in\n'
                '  *"version_info >= (3, 10)"*) exit 0 ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'
            ))
            if not (a and b):
                self.skipTest("平台不支持可执行位，无法构造 mock 解释器")
            path = f"{shadow}:{good}:/usr/bin:/bin"
            result = self.run_lib('discover_python\n', root, path=path)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(str(good / "python3"), result.stdout.strip())

    def test_explicit_xgent_python_is_never_silently_replaced(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'discover_python && echo FOUND || echo NOTFOUND\n',
                root,
                env_extra={"XGENT_PYTHON": "/definitely/not/here/python3"},
            )
            self.assertIn("NOTFOUND", result.stdout)

    def test_stale_venv_is_rebuilt_instead_of_reused(self):
        ensure_virtualenv = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('ensure_virtualenv() {'):
            INSTALL_SCRIPT.index('activate_virtualenv() {')
        ]
        self.assertIn('python_version_ok "$VENV_PYTHON"', ensure_virtualenv)
        self.assertIn('safe_remove_venv', ensure_virtualenv)
        # 建 venv 必须用挑好的解释器，不能再退回裸 python3。
        self.assertIn('"$PYTHON_BIN" -m venv "$VENV_DIR"', ensure_virtualenv)
        self.assertNotIn('python3 -m venv "$VENV_DIR"', ensure_virtualenv)


@requires_bash_harness
class ComponentBoardTests(InstallScriptLibraryMixin, unittest.TestCase):
    """cli / web / bot 三态清单。"""

    def bot_state(self, root: Path, env_lines: str) -> str:
        write_lf(root / ".env", env_lines)
        result = self.run_lib('component_state_bot\n', root)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_bot_state_covers_missing_broken_and_installed(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            self.assertTrue(self.bot_state(root, "").startswith("missing|"))
            self.assertTrue(
                self.bot_state(root, "BOT_TOKEN=1:abc\n").startswith("error|"),
                "有 Token 但没有 AUTHORIZED_USER_ID 是坏状态，不是未安装",
            )
            self.assertTrue(
                self.bot_state(root, "BOT_TOKEN=1:abc\nAUTHORIZED_USER_ID=系统信息\n")
                .startswith("error|")
            )
            # 仅 Web 模式留下的占位 1 不能当成真实 ID：装完 Bot 会拒绝所有人。
            self.assertTrue(
                self.bot_state(root, "BOT_TOKEN=1:abc\nAUTHORIZED_USER_ID=1\n")
                .startswith("error|")
            )
            self.assertTrue(
                self.bot_state(root, "BOT_TOKEN=1:abc\nAUTHORIZED_USER_ID=987654\n")
                .startswith("installed|")
            )

    def test_board_renders_one_line_per_component(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / ".env", "BOT_TOKEN=1:abc\nAUTHORIZED_USER_ID=987654\n")
            result = self.run_lib('render_component_board\n', root)
            self.assertEqual(0, result.returncode, result.stderr)
            out = result.stdout
            self.assertIn("1 cli", out)
            self.assertIn("2 web", out)
            self.assertIn("3 bot", out)
            self.assertIn("✅", out)   # bot 已配置
            self.assertIn("❌", out)   # cli/web 还没有

    def test_yellow_marks_the_broken_state(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'component_icon installed; echo\n'
                'component_icon error; echo\n'
                'component_icon missing; echo\n',
                root,
            )
            icons = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertIn("✅", icons[0])
            self.assertIn("🟡", icons[1])
            self.assertIn("❌", icons[2])

    def test_runtime_mode_round_trip_and_garbage_rejection(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'echo "start=$(get_runtime_mode)"\n'
                'set_runtime_mode systemd; echo "set=$(get_runtime_mode)"\n'
                'printf "junk\\n" > "$RUNTIME_MODE_FILE"; echo "junk=$(get_runtime_mode)"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("start=none", result.stdout)
            self.assertIn("set=systemd", result.stdout)
            self.assertIn("junk=none", result.stdout)

    def test_deploy_mode_state_follows_the_env_file(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / ".env", "BOT_TOKEN=1:abc\n")
            result = self.run_lib(
                'sync_deploy_mode_state; echo "a=$DEPLOY_MODE"\n'
                'env_unset BOT_TOKEN\n'
                'sync_deploy_mode_state; echo "b=$DEPLOY_MODE"\n'
                'echo "file=$(cat "$STATE_DIR/deploy-mode")"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("a=telegram", result.stdout)
            self.assertIn("b=web-only", result.stdout)
            self.assertIn("file=web-only", result.stdout)


@requires_bash_harness
class KeepAliveTests(InstallScriptLibraryMixin, unittest.TestCase):
    def test_systemd_unit_delegates_to_service_exec(self):
        # unit 里不写具体启动参数：IP 出站模式、PYTHONPATH、入口文件都由
        # run_bot_python 决定，抄进 unit 就等于第二份真相。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'SYSTEMD_UNIT_PATH="$PWD/xgent.service"\n'
                'run_privileged() { "$@"; }\n'
                'write_systemd_unit\n'
                'cat "$SYSTEMD_UNIT_PATH"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            unit = result.stdout
            self.assertIn("install.sh service-exec", unit)
            self.assertIn("Restart=always", unit)
            # 78 = 应用主动要求别再拉起来（Token 失效），和 PM2 的
            # --stop-exit-codes 78 必须一致，否则 systemd 会无限重启死循环。
            self.assertIn("RestartPreventExitStatus=78", unit)
            self.assertIn("WantedBy=multi-user.target", unit)

    def test_pm2_and_systemd_agree_on_the_stop_exit_code(self):
        self.assertIn("--stop-exit-codes 78", INSTALL_SCRIPT)
        self.assertIn("RestartPreventExitStatus=78", INSTALL_SCRIPT)

    def test_service_running_is_safe_with_a_corrupt_pid_file(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / "xgent.pid", "not-a-pid\n")
            result = self.run_lib(
                'set_runtime_mode nohup\n'
                'if service_running; then echo RUNNING; else echo STOPPED; fi\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("STOPPED", result.stdout)

    def test_port_check_does_not_claim_failure_without_tools(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'command_exists() { return 1; }\n'
                'if port_is_listening 8790; then echo OK; else echo CLAIMS_DOWN; fi\n',
                root,
            )
            self.assertIn("OK", result.stdout)

    def test_port_check_does_not_trust_an_ss_that_could_not_answer(self):
        # Android/Termux 上 ss 装着、但读不了 /proc/net/tcp：退出码非零、stdout 空。
        # "查不了"绝不能塌成"没监听"——那正是那句黄色误报的来源。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                'venv_ready() { return 1; }\n'
                'command_exists() { [ "$1" = ss ]; }\n'
                'ss() { echo "ss: Permission denied" >&2; return 1; }\n'
                'if port_is_listening 8790; then echo OK; else echo CLAIMS_DOWN; fi\n',
                root,
            )
            self.assertIn("OK", result.stdout)

    def test_port_check_still_trusts_an_ss_that_did_answer(self):
        # 反面：ss 正常答话、里面确实没有这个端口，这才是"确定没监听"。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            listing = 'State  Recv-Q Send-Q Local Address:Port\\nLISTEN 0 128 127.0.0.1:22'
            result = self.run_lib(
                'venv_ready() { return 1; }\n'
                'command_exists() { [ "$1" = ss ]; }\n'
                f'ss() {{ printf "%b\\n" "{listing}"; }}\n'
                'if port_is_listening 8790; then echo OK; else echo CLAIMS_DOWN; fi\n'
                'if port_is_listening 22; then echo FOUND22; fi\n',
                root,
            )
            self.assertIn("CLAIMS_DOWN", result.stdout)
            self.assertIn("FOUND22", result.stdout)

    def web_state(self, root, *, password="yes", enabled="yes",
                  running="0", listening="0") -> str:
        """跑 component_state_web，但把"要有 venv 和数据库"那两步换成桩。

        真去读配置得先建 venv、装依赖、开数据库，那是集成测试的活；这里要钉的
        是判定顺序本身——尤其是"开关关着"必须排在"端口没监听"前面。
        """
        result = self.run_lib(
            'venv_ready() { return 0; }\n'
            'load_web_state() { return 0; }\n'
            f'WEB_HAS_PASSWORD={password}\n'
            f'WEB_ENABLED={enabled}\n'
            'WEB_PORT=8790\n'
            f'service_running() {{ return {running}; }}\n'
            f'port_is_listening() {{ return {listening}; }}\n'
            'component_state_web\n',
            root,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_web_disabled_in_bot_is_reported_instead_of_bare_port_failure(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)

            # 从老版本升上来的典型现场：装了 web 组件、密码也设了，但 bot 菜单里
            # 的 web 开关还是关的。以前这里只说"端口没监听"，用户根本不知道去
            # 哪儿开——必须把真正的原因说出来。
            state = self.web_state(root, enabled="no", running="0", listening="1")
            self.assertTrue(state.startswith("error|"), state)
            self.assertIn("已在 bot 内关闭 Web 功能", state)

            # 开关开着、端口确实不监听，才是"去看日志"那种异常。
            state = self.web_state(root, enabled="yes", running="0", listening="1")
            self.assertTrue(state.startswith("error|"), state)
            self.assertNotIn("已在 bot 内关闭", state)

            # 没密码的优先级最高：开关再开也起不来。
            state = self.web_state(root, password="no", enabled="no")
            self.assertTrue(state.startswith("missing|"), state)

            state = self.web_state(root, enabled="yes", running="0", listening="0")
            self.assertTrue(state.startswith("installed|"), state)

    def test_web_toggle_writes_the_same_key_as_the_bot_menu(self):
        # 装置页和 Telegram 菜单必须写同一个 key，否则两边显示会打架。
        # 具体读写已经收进 tools/xgent_config.py，install.sh 只负责调子命令。
        config_py = (ROOT / "tools" / "xgent_config.py").read_text(encoding="utf-8")
        self.assertIn('"web_enabled"', config_py)
        self.assertIn("save_config", config_py)
        toggle = INSTALL_SCRIPT[INSTALL_SCRIPT.index("toggle_web_enabled() {"):]
        self.assertIn("set_web_enabled 0", toggle)
        self.assertIn("set_web_enabled 1", toggle)
        self.assertIn("run_config_py set-web-enabled", INSTALL_SCRIPT)

    def test_web_config_bootstrap_lives_in_exactly_one_place(self):
        # 曾经有五段内联 Python 各抄一遍 load_sections 的 15 行 preamble。
        self.assertNotIn("from xgent_app.bootstrap import load_sections", INSTALL_SCRIPT)
        self.assertIn("run_config_py() {", INSTALL_SCRIPT)

    def test_deploy_web_reads_a_freshly_loaded_state(self):
        """deploy_web 必须在**当前 shell** 里加载一次 Web 状态。

        component_state_web 是在 $(...) 里跑的，它内部 load_web_state 对
        WEB_ENABLED / WEB_HAS_PASSWORD 的赋值随子 shell 一起消失。少了这次显式加载，
        本页第 3 项就恒显示"开启 Web 功能（当前: 已关闭）"，点下去 toggle_web_enabled
        又以为没设过密码——装好之后没有任何路径能把 Web 关掉。
        """
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            result = self.run_lib(
                # 真的 load_web_state 要建 venv、开数据库；这个桩只做它成功时
                # 该做的事——把四个全局变量填好。
                'load_web_state() { WEB_STATE_LOADED=1; WEB_HAS_PASSWORD=yes;'
                ' WEB_ENABLED=yes; WEB_PORT=8790; return 0; }\n'
                'ensure_identity_placeholder() { :; }\n'
                'venv_ready() { return 0; }\n'
                'service_running() { return 0; }\n'
                'port_is_listening() { return 0; }\n'
                'deploy_web <<< ""\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("关闭 Web 功能", result.stdout)
            self.assertNotIn("开启 Web 功能", result.stdout)

    def test_detached_restart_helper_never_interpolates_the_proxy_url(self):
        # 生成 helper 用的是不加引号的 <<EOF 时，代理地址会被拼进脚本正文——
        # 密码里一个反引号或 $( 就成了在 helper 里执行的代码，而那个值只过了
        # "^socks5://" 前缀检查。值必须走环境变量。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(
                root / ".env",
                "XGENT_IP_MODE=sock5\n"
                "XGENT_SOCKS5_PROXY=socks5://u:$(touch pwned)@h:1080\n",
            )
            result = self.run_lib(
                # 只要生成出来的 helper，不真的把它跑起来。
                'nohup() { :; }\n'
                'restart_pm2_detached >/dev/null\n'
                'cat "$STATE_DIR"/pm2-restart.*.sh\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("touch pwned", result.stdout)
            self.assertIn("XGENT_RESTART_SOCKS5", result.stdout)
            self.assertFalse((root / "pwned").exists())

    def test_restart_dispatches_on_the_recorded_mode_not_on_leftovers(self):
        # start_service / stop_service 都按 runtime mode 分发，restart 必须一致。
        # 以前它第二步看的是"存在 PM2 进程就走 PM2"：把保活方式改成 nohup 之后，
        # 只要那条 PM2 记录还在，restart 就又从 PM2 起，和用户刚选的方式正好相反。
        stubs = (
            'start_background() { echo TOOK_NOHUP; }\n'
            'restart_pm2_detached() { echo TOOK_PM2; }\n'
            'start_with_systemd() { echo TOOK_SYSTEMD; }\n'
            'start_service() { echo TOOK_FALLBACK; }\n'
            'stop_background_process() { :; }\n'
        )
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            # 记录是 nohup，但 PM2 看起来装着、进程也还在。
            result = self.run_lib(
                'set_runtime_mode nohup\n'
                'command_exists() { return 0; }\n'
                'pm2() { return 0; }\n'
                + stubs + 'restart_app\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("TOOK_NOHUP", result.stdout)
            self.assertNotIn("TOOK_PM2", result.stdout)

            # 反过来：记录是 pm2 就走 pm2，哪怕 xgent.pid 还躺在目录里。
            write_lf(root / "xgent.pid", "1\n")
            result = self.run_lib(
                'set_runtime_mode pm2\n' + stubs + 'restart_app\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("TOOK_PM2", result.stdout)
            self.assertNotIn("TOOK_NOHUP", result.stdout)

    def test_switching_keepalive_mode_stops_the_previous_one(self):
        # 不停旧的那套，两套会同时跑：同一个 SQLite、同一个 Web 端口、同一个
        # token 长轮询（Telegram 直接回 409），而两边看起来都"启动成功了"。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            probe = 'stop_service_in_mode() { echo "STOPPED=$1"; }\n'
            result = self.run_lib(
                'set_runtime_mode pm2\n' + probe
                + 'systemd_available() { return 0; }\n'
                + 'choose_runtime_mode <<< "1"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("STOPPED=pm2", result.stdout)

            # 选的还是原来那个，就不该去停任何东西。
            result = self.run_lib(
                'set_runtime_mode pm2\n' + probe + 'choose_runtime_mode <<< "2"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("STOPPED=", result.stdout)

    def test_uninstall_removes_the_systemd_unit(self):
        uninstall = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('uninstall_app() {'):
            INSTALL_SCRIPT.index('prepare_base_environment() {')
        ]
        self.assertIn("remove_systemd_service", uninstall)
        self.assertIn("remove_runtime_mode_state", uninstall)


class PortProbeTests(unittest.TestCase):
    """probe-port 是端口检测的主判据：Android 上 ss/netstat 全失灵时只剩它。

    不需要 bash，也不需要 venv——它只用 stdlib 的 socket，刻意不走 xgent_config
    的 _load()。
    """

    @staticmethod
    def _probe(port) -> int:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "xgent_config.py"),
             "probe-port", str(port)],
            cwd=ROOT, text=True, capture_output=True, timeout=60,
        ).returncode

    def test_listening_port_is_found_and_closed_port_is_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertEqual(0, self._probe(port))
        # 出了 with 端口就没人听了，同一个端口现在必须给出确定的否定结论。
        self.assertEqual(1, self._probe(port))

    def test_unusable_port_value_is_inconclusive_not_down(self):
        # 2 = 没探成。调用方会把它当"查不出来"往下退，而不是据此报异常。
        self.assertEqual(2, self._probe("不是端口"))
        self.assertEqual(2, self._probe(0))


class NonInteractivePathTests(unittest.TestCase):
    """脱离终端跑的路径一旦弹提问，就是无声挂起。"""

    def test_service_exec_is_dispatched_and_does_not_prepare_environment(self):
        self.assertIn("    service-exec)", INSTALL_SCRIPT)
        block = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index("    service-exec)"):
            INSTALL_SCRIPT.index("    status|--status)")
        ]
        self.assertIn("exec_app_process", block)
        self.assertNotIn("prepare_environment", block)
        self.assertNotIn("read -r", block)

    def test_prepare_environment_asks_nothing(self):
        prepare = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('prepare_base_environment() {'):
            INSTALL_SCRIPT.index('main() {')
        ]
        self.assertNotIn("read -r", prepare)
        # 缺配置时要明确报错，而不是把 pm2/systemd 拉起一个连不上的空服务。
        self.assertIn("require_deployment", prepare)


@requires_bash_harness
class EnvFileTests(InstallScriptLibraryMixin, unittest.TestCase):
    """.env 的读写只有 env_get / env_set / env_unset 三个入口。

    这里以前有七份各写一遍的实现，踩过的坑各修在其中一两份里。三条一起钉住。
    """

    def test_reads_are_anchored_and_strip_carriage_returns(self):
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(
                root / ".env",
                "BOT_TOKEN=abc\r\nXGENT_BOT_TOKEN=keepme\r\nA=1\r\nB=2\r\n",
            )
            result = self.run_lib(
                'echo "token=[$(env_get BOT_TOKEN)]"\n'
                'env_unset A B\n'
                'echo "--- env ---"\n'
                'cat .env\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            # 值末尾的 \r 必须吃掉：带着它发给 Telegram 会直接鉴权失败，
            # 而 AUTHORIZED_USER_ID 会被判成"不是合法数字"。
            self.assertIn("token=[abc]", result.stdout)
            # 行首锚定：动 BOT_TOKEN 不能连带碰到 XGENT_BOT_TOKEN。
            self.assertIn("XGENT_BOT_TOKEN=keepme", result.stdout)
            # env_unset 支持一次删多个键。
            self.assertNotIn("A=1", result.stdout)
            self.assertNotIn("B=2", result.stdout)

    def test_writes_stage_the_temp_file_next_to_the_env(self):
        # mktemp 默认落在 /tmp，跨文件系统时 mv 会退化成复制+删除，复制中途失败
        # 就把含 BOT_TOKEN 和全部 api_key 的 .env 截断且无备份。同目录才是原子
        # rename。set_ip_mode / migrate_env_key 以前各自违反过这条。
        for name in ("env_set", "env_unset"):
            block = INSTALL_SCRIPT[INSTALL_SCRIPT.index(f"{name}() {{"):]
            block = block[:block.index("\n}\n")]
            self.assertIn("mktemp ./.env.XXXXXX", block, name)
        # 恰好两处，说明没有第三个函数又自己拼了一遍 .env 的写入。
        self.assertEqual(2, INSTALL_SCRIPT.count("mktemp ./.env.XXXXXX"))

    def test_socks5_proxy_value_is_cleaned_like_every_other_value(self):
        # 这个读法以前是唯一没去 \r 的：代理地址带个回车符传给 httpx，
        # 报出来的错和换行符八竿子打不着。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / ".env",
                     "XGENT_IP_MODE=sock5\r\nXGENT_SOCKS5_PROXY=socks5://1.2.3.4:1080\r\n")
            result = self.run_lib(
                'echo "mode=[$(get_ip_mode)]"\n'
                'echo "proxy=[$(get_socks5_proxy)]"\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("mode=[sock5]", result.stdout)
            self.assertIn("proxy=[socks5://1.2.3.4:1080]", result.stdout)


@requires_bash_harness
class DependencyInstallTests(InstallScriptLibraryMixin, unittest.TestCase):
    """重启不该被 pip 的联网需求绑住。"""

    def test_pip_is_skipped_when_requirements_did_not_change(self):
        # restart / start / 菜单 2·3·9 全都经过 install_requirements，而 pip 每次
        # 都要联网（--upgrade pip 必然去问一次 PyPI），一断网就被 set -e 掀掉整个
        # 重启。可"重启一个已经装好的服务"跟装依赖本来是两件事。
        stubs = (
            'core_imports_ok() { return 0; }\n'
            'print_core_versions() { :; }\n'
            'python() { echo "PIP_RAN $*"; }\n'
        )
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / "requirements.txt", "aiosqlite\n")
            result = self.run_lib(
                'ensure_state_dir\n'
                'cp requirements.txt "$REQUIREMENTS_SNAPSHOT"\n'
                + stubs + 'install_requirements\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("PIP_RAN", result.stdout)

            # requirements.txt 变了就必须真的装一遍——快路不能变成"永远不装"。
            write_lf(root / "requirements.txt", "aiosqlite\nopenai\n")
            result = self.run_lib(stubs + 'install_requirements\n', root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("PIP_RAN", result.stdout)

    def test_a_half_broken_venv_still_gets_repaired(self):
        # 快路的两个条件必须同时满足：requirements 没变**且**核心包真的 import 得动。
        # 只看前者的话，被人删掉半截的 venv 就再也自愈不了。
        with self.temp_dir() as temp_dir:
            root = self.sandbox(temp_dir)
            write_lf(root / "requirements.txt", "aiosqlite\n")
            result = self.run_lib(
                'ensure_state_dir\n'
                'cp requirements.txt "$REQUIREMENTS_SNAPSHOT"\n'
                'core_imports_ok() { return 1; }\n'
                'print_core_versions() { :; }\n'
                'python() { echo "PIP_RAN $*"; }\n'
                'install_requirements\n',
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("PIP_RAN", result.stdout)


class UninstallSafetyTests(unittest.TestCase):
    """卸载是最不可逆的一步，几条底线用正文断言钉住。"""

    def test_process_sweep_is_scoped_to_this_checkout(self):
        # bot_python_code 生成的内联 Python 本身就含 XGENT_APP_ENTRY 字面量，
        # 拿它当判据等于每份副本都命中——在 /tmp/xgt 里 stop 一下，会顺手杀掉
        # /home/... 那份正在服务的进程。
        sweep = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('stop_background_process() {'):
            INSTALL_SCRIPT.index('remove_pm2_process() {')
        ]
        self.assertNotIn('*"XGENT_APP_ENTRY"*', sweep)
        self.assertIn('*"$SCRIPT_DIR"*', sweep)
        # 卡住的进程要真的收掉：kill 默认就是 TERM，第二发不能又是 TERM。
        self.assertIn('kill -9 "$pid"', sweep)

    def test_apt_purge_is_previewed_and_spares_the_python_base(self):
        block = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('remove_recorded_apt_packages() {'):
            INSTALL_SCRIPT.index('remove_ip_mode_state() {')
        ]
        # 记的是 dpkg 全量差集，purge 时 apt 还会级联带走别的包——先预演给人看。
        self.assertIn("apt-get -s purge", block)
        # python3 系是系统基础包，一律不动。
        self.assertIn("python3|python3-*", block)
        # autoremove --purge 会清掉系统上所有孤立包，默认不能跑。
        self.assertIn("XGENT_APT_AUTOREMOVE", block)

    def test_uninstall_shuts_down_the_local_api_container(self):
        # 容器是 --restart unless-stopped 起的：不关掉，卸载完还在跑，
        # 机器重启还会自己回来，端口一直占着。
        uninstall = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('uninstall_app() {'):
            INSTALL_SCRIPT.index('ensure_virtualenv() {')
        ]
        self.assertIn("stop_local_api_container no-restart", uninstall)

    def test_venv_guard_actually_checks_something(self):
        # 原来比的是 resolve_path "$SCRIPT_DIR/venv" 和 resolve_path "$VENV_DIR"，
        # 而 VENV_DIR 就是前者——判据永远成立，等于给 rm -rf 装了个假保险。
        guard = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('safe_remove_venv() {'):
            INSTALL_SCRIPT.index('confirm_uninstall() {')
        ]
        self.assertNotIn('resolve_path "$SCRIPT_DIR/venv"', guard)
        self.assertIn('basename "$resolved"', guard)


if __name__ == "__main__":
    unittest.main()
