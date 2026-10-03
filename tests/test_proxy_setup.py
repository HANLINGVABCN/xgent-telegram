"""Run proxy helpers in an isolated Bash process; never deploy or touch live services."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill-public/script/proxy-setup/proxy_setup.sh"


@pytest.fixture(scope="module")
def bash():
    candidates = []
    if os.name == "nt":
        # Avoid the Windows WSL launcher; Git Bash supports native workspace paths.
        git = shutil.which("git")
        if git:
            candidates.append(Path(git).resolve().parents[1] / "bin/bash.exe")
        candidates.append(Path("C:/Program Files/Git/bin/bash.exe"))
    else:
        found = shutil.which("bash")
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    pytest.skip("A native Bash interpreter is required")


@pytest.fixture
def run_bash(bash, tmp_path):
    def run(body, *, source=True, stdin=""):
        harness = tmp_path / "harness.sh"
        prefix = 'source "$PROXY_TEST_SCRIPT"\n' if source else ""
        harness.write_text(prefix + body, encoding="utf-8", newline="\n")
        env = dict(os.environ, PROXY_TEST_SCRIPT=SCRIPT.as_posix(), TRACE_ENABLED="0",
                   TMPDIR=tmp_path.as_posix(), BASH_ENV="")
        return subprocess.run(
            [bash, "--noprofile", "--norc", harness.as_posix()], cwd=tmp_path,
            env=env, input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
    return run


def assert_ok(result):
    assert result.returncode == 0, result.stdout + result.stderr


def test_syntax(bash):
    result = subprocess.run([bash, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
    assert_ok(result)


def test_source_and_help_do_not_write_files(run_bash, tmp_path):
    result = run_bash('printf "loaded:%s:%s\\n" "$LOG_DIR" "$TRACE_ENABLED"\n')
    assert_ok(result)
    assert result.stdout.strip() == "loaded::0"
    result = run_bash('bash "$PROXY_TEST_SCRIPT" --help\n', source=False)
    assert_ok(result)
    assert "--help" in result.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == ["harness.sh"]


@pytest.mark.parametrize("value,valid", [
    ("1", True), ("65535", True), ("00080", True), ("08", True),
    ("0", False), ("65536", False), ("-1", False), ("", False),
    ("1+2", False), ("80/tcp", False), ("9999999999999999999999999999", False),
    ("1; touch injected", False),
])
def test_port_validation(run_bash, value, valid):
    import shlex
    result = run_bash("is_port " + shlex.quote(value) + "\n")
    assert (result.returncode == 0) == valid


def test_ports_are_decimal_and_dual_duplicates_rejected(run_bash):
    result = run_bash('''
SERVER_PORT=00080; INTERNAL_PORT=08; DIRECT_PORT=00443; WARP_PORT=00444
OUTBOUND_MODE=dual
validate_install_settings || exit 1
printf '%s:%s:%s:%s\\n' "$SERVER_PORT" "$INTERNAL_PORT" "$DIRECT_PORT" "$WARP_PORT"
WARP_PORT=443
if validate_install_settings; then exit 2; fi
''')
    assert_ok(result)
    assert "80:8:443:444" in result.stdout


def test_multi_port_overflow_rejected(run_bash):
    result = run_bash('''
PROTOCOL=free-multi; SERVER_PORT=65535; INTERNAL_PORT=65535
MULTI_PORTS=(65535 65536)
validate_install_settings
''')
    assert result.returncode != 0


def test_eof_exits_instead_of_looping(run_bash):
    result = run_bash('ui_read "choose:" answer\nprintf "UNREACHABLE"\n')
    assert result.returncode != 0
    assert "UNREACHABLE" not in result.stdout


def test_reset_clears_previous_install(run_bash):
    result = run_bash('''
WARP_WG_MODE=wireguard; OUTBOUND_MODE=dual; WARP_PORT=443
DOMAIN=old.example; MULTI_PROTOCOLS=(reality-warp); SNI=old.example
reset_install_settings
[ -z "$WARP_WG_MODE$OUTBOUND_MODE$WARP_PORT$DOMAIN" ] || exit 1
[ "${#MULTI_PROTOCOLS[@]}" = 0 ] && [ "$SNI" = www.bing.com ]
''')
    assert_ok(result)


@pytest.mark.parametrize("service", ["systemd", "openrc"])
def test_service_failure_retains_existing_state(run_bash, service, tmp_path):
    keep = tmp_path / "existing-warp.json"
    keep.write_text("KEEP", encoding="utf-8")
    result = run_bash(f'''
SVC={service}
systemctl() {{ return 1; }}
rc-service() {{ return 1; }}
cleanup_proxy_artifacts() {{ printf forbidden; }}
cleanup_warp_client() {{ printf forbidden; }}
cleanup_common_dependencies() {{ printf forbidden; }}
close_proxy_firewall() {{ printf forbidden; }}
check_service_health
''')
    assert result.returncode != 0
    assert "forbidden" not in result.stdout
    assert keep.read_text(encoding="utf-8") == "KEEP"


# All mutation boundaries in the install orchestrator are mocked. Any unexpected
# helper call is an explicit failure, not a call through to the operating system.
INSTALL_HARNESS = r'''
is_root() { :; }
detect_os() { OS=debian; SVC=systemd; }
select_machine_mode() { MACHINE_MODE=standard; }
select_protocol() { PROTOCOL=reality; }
select_outbound_mode() { OUTBOUND_MODE=warp; }
collect_info() { SERVER_PORT=443; INTERNAL_PORT=443; WARP_WG_MODE=wireguard; }
install_singbox() { :; }
warp_wg_config_exists() { return 1; }
warp_register_wireguard() { return 1; }
gen_uuid() { :; }; gen_password() { :; }; gen_short_id() { :; }
optimize_tcp_stack() { :; }; optimize_system_limits() { :; }
generate_config() { printf forbidden-generation; return 1; }
check_generated_config() { printf forbidden-validation; return 1; }
setup_service() { printf forbidden-service; return 1; }
output_all() { printf forbidden-success; }
open_firewall() { printf 'opened:%s\n' "$1"; }
WORK_DIR="$PWD"; CONFIG_FILE="$PWD/config.json"
'''


def test_warp_failure_stops_without_changing_selected_route(run_bash):
    result = run_bash(INSTALL_HARNESS + r'''
if do_full_install; then exit 2; fi
printf 'route:%s:%s\n' "$OUTBOUND_MODE" "$WARP_WG_MODE"
''')
    assert_ok(result)
    assert "route:warp:wireguard" in result.stdout
    assert "forbidden" not in result.stdout
    assert "opened:" not in result.stdout


def test_dependency_failure_stops_install(run_bash):
    result = run_bash(INSTALL_HARNESS + r'''
install_singbox() { return 1; }
warp_register_wireguard() { printf forbidden-register; }
if do_full_install; then exit 2; fi
''')
    assert_ok(result)
    assert "forbidden" not in result.stdout


def test_invalid_config_is_not_dumped_or_started_and_backup_is_kept(run_bash, tmp_path):
    (tmp_path / "config.json").write_text("original", encoding="utf-8")
    result = run_bash(INSTALL_HARNESS + r'''
collect_info() { SERVER_PORT=443; INTERNAL_PORT=443; WARP_WG_MODE=""; }
generate_config() { printf secret-password > "$CONFIG_FILE"; }
check_generated_config() { return 1; }
if do_full_install; then exit 2; fi
''')
    assert_ok(result)
    assert "secret-password" not in result.stdout
    assert "forbidden" not in result.stdout
    assert "opened:" not in result.stdout
    backups = list(tmp_path.glob("config.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original"


def test_dual_firewall_uses_actual_inbound_ports(run_bash):
    result = run_bash(INSTALL_HARNESS + r'''
select_outbound_mode() { OUTBOUND_MODE=dual; }
collect_info() { SERVER_PORT=443; INTERNAL_PORT=443; DIRECT_PORT=8443; WARP_PORT=8444; }
generate_config() {
cat > "$CONFIG_FILE" <<'JSON'
{"inbounds":[
{"listen_port":8443},
{"listen_port":8444}
]}
JSON
}
check_generated_config() { :; }
setup_service() { :; }
output_all() { printf completed; }
do_full_install
''')
    assert_ok(result)
    assert "opened:8443" in result.stdout
    assert "opened:8444" in result.stdout
    assert "opened:443" not in result.stdout
    assert "completed" in result.stdout


def test_service_failure_does_not_publish_success_or_open_firewall(run_bash):
    result = run_bash(INSTALL_HARNESS + r'''
collect_info() { WARP_WG_MODE=""; }
generate_config() { :; }
check_generated_config() { :; }
setup_service() { return 1; }
if do_full_install; then exit 2; fi
''')
    assert_ok(result)
    assert "forbidden" not in result.stdout
    assert "opened:" not in result.stdout


def test_multi_ws_path_matches_client_and_metadata_is_not_evaluated(run_bash, tmp_path):
    result = run_bash(r'''
WORK_DIR="$PWD"; CONFIG_FILE="$PWD/config.json"; PROTOCOL=free-multi
MULTI_PROTOCOLS=(vless-ws vmess-ws); MULTI_PORTS=(443 444)
MULTI_DOMAINS=("node.example'; touch injected; #" "other.example")
gen_uuid() { UUID=test-uuid; }
openssl() { printf abcdef12; }
green() { :; }
generate_free_multi_config || exit 1
[ "$MULTI_DOMAIN_0" = "${MULTI_DOMAINS[0]}" ] || exit 2
[ "$MULTI_UUID_0" = test-uuid ] || exit 3
cat "$CONFIG_FILE"
''')
    assert_ok(result)
    config = json.loads(result.stdout)
    assert [i["transport"]["path"] for i in config["inbounds"]] == [
        "/ws-abcdef12", "/vmws-abcdef12",
    ]
    assert not (tmp_path / "injected").exists()


def test_single_ws_ignores_stale_multi_path(run_bash):
    result = run_bash(r'''
WORK_DIR="$PWD"; UUID=test; DOMAIN=example.com
printf /single > ws_path.txt
printf /stale > ws_path_443.txt
gen_inbound_vless_ws 443 in-main
''')
    assert_ok(result)
    assert json.loads(result.stdout)["transport"]["path"] == "/single"


@pytest.mark.parametrize("failure", ["package", "metadata"])
def test_downloader_fails_closed_without_installing(run_bash, failure):
    package_status = 1 if failure == "package" else 0
    result = run_bash(f'''
OS=debian
apt() {{ return {package_status}; }}
uname() {{ printf x86_64; }}
curl() {{ printf 'request:%s\\n' "$*" >&2; return 1; }}
jq() {{ cat >/dev/null; }}
tar() {{ printf forbidden-extract; }}
install() {{ printf forbidden-install; }}
install_singbox
''')
    assert result.returncode != 0
    assert "forbidden" not in result.stdout
    assert "ghfast" not in result.stderr
    assert "v1.11.1" not in result.stderr
    if failure == "package":
        assert "request:" not in result.stderr
    else:
        assert "--max-time 30" in result.stderr


def test_logging_is_private_unique_and_trace_is_opt_in(run_bash, tmp_path):
    body = r'''
init_runtime_logging || exit 1
trace_pause; trace_resume
[[ "$-" != *x* ]] || exit 2
printf 'log:%s\n' "$LOG_DIR"
'''
    assert_ok(run_bash(body))
    assert_ok(run_bash(body))
    directories = list(tmp_path.glob("proxy_setup_logs.*"))
    assert len(directories) == 2
    for directory in directories:
        assert not list(directory.glob("*trace*"))
        logs = list(directory.glob("*.log"))
        assert len(logs) == 1
        if os.name != "nt":
            assert directory.stat().st_mode & 0o777 == 0o700
            assert logs[0].stat().st_mode & 0o777 == 0o600
