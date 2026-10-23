import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = re.findall(r'```python\n(.*?)\n```', (ROOT / 'docs/reliable-execution.md').read_text(encoding='utf-8'), re.S)


def test_shipped_guidance_requires_scripts_without_forcing_simple_tasks():
    text = (ROOT / 'prompts/agent_addon.txt').read_text(encoding='utf-8')
    for clause in ('必须先用 file-x 写完整脚本', '等待写入成功回传', '首次出现引号', '只读任务没有写入授权',
                   'shell=False', '不为简单任务安装依赖', 'git status --short', 'os.environ.get()'):
        assert clause in text


def test_config_script_validates_missing_values_and_serializes_special_characters(tmp_path):
    assert len(SCRIPTS) == 2
    script = tmp_path / 'make_config.py'
    script.write_text(SCRIPTS[0], encoding='utf-8')
    target = tmp_path / "space $;' config.json"
    env = {**os.environ, 'SERVICE_LABEL': '', 'CONFIG_PATH': str(target)}
    failed = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=10)
    assert failed.returncode != 0 and not target.exists()
    value = 'quotes \' " $ & ; 中文 \\ \nsecond line'
    env['SERVICE_LABEL'] = value
    subprocess.run([sys.executable, '-m', 'py_compile', str(script)], check=True, capture_output=True)
    result = subprocess.run([sys.executable, str(script)], env=env, check=True, capture_output=True, timeout=10)
    assert not result.stdout and json.loads(target.read_text(encoding='utf-8'))['service']['label'] == value
    second = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=10)
    assert second.returncode != 0 and not list(tmp_path.glob('.config-*'))


def test_parameter_array_preserves_special_paths_and_runs_without_shell(tmp_path):
    script = tmp_path / 'format_config.py'
    script.write_text(SCRIPTS[1], encoding='utf-8')
    source = tmp_path / "source $;' and spaces.json"
    target = tmp_path / "target $;' and spaces.json"
    source.write_text('{"preserved":"value & ; $"}', encoding='utf-8')
    env = {**os.environ, 'INPUT_PATH': str(source), 'OUTPUT_PATH': str(target)}
    subprocess.run([sys.executable, str(script)], env=env, check=True, capture_output=True, timeout=10)
    assert json.loads(source.read_text()) == json.loads(target.read_text())
