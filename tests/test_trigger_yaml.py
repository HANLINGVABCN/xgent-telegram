"""trigger-x YAML 格式解析测试"""
import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock 共享命名空间中的依赖
mock_blacklist = MagicMock()
mock_blacklist.check = MagicMock(return_value=(False, None))

# 在导入之前设置 mock
with patch.dict('sys.modules', {
    'xgent_app.sections.shell_triggers.AgentCommandBlacklist': mock_blacklist
}):
    from xgent_app.sections.shell_triggers import TriggerConditionExpression, SelfTriggerManager

# 确保 AgentCommandBlacklist 被 mock
import xgent_app.sections.shell_triggers as shell_triggers_module
if not hasattr(shell_triggers_module, 'AgentCommandBlacklist'):
    shell_triggers_module.AgentCommandBlacklist = mock_blacklist


class TestConditionExpression:
    """测试智能条件表达式解析"""

    def test_literal_bare_word(self):
        """裸词自动视为字面量"""
        expr = TriggerConditionExpression("READY")
        assert not expr.is_satisfied()
        expr.feed("Server is READY")
        assert expr.is_satisfied()
        assert "READY" in expr.matched_literals()

    def test_literal_quoted(self):
        """引号字符串"""
        expr = TriggerConditionExpression('"error timeout"')
        expr.feed("Got error timeout message")
        assert expr.is_satisfied()

    def test_regex_pattern(self):
        """正则表达式匹配"""
        expr = TriggerConditionExpression("/error.*/i")
        assert not expr.is_satisfied()
        expr.feed("ERROR: connection failed")
        assert expr.is_satisfied()

    def test_logic_and(self):
        """AND 逻辑"""
        expr = TriggerConditionExpression("READY AND SUCCESS")
        expr.feed("READY")
        assert not expr.is_satisfied()
        expr.feed("SUCCESS")
        assert expr.is_satisfied()

    def test_logic_or(self):
        """OR 逻辑"""
        expr = TriggerConditionExpression("READY OR STARTED")
        expr.feed("STARTED")
        assert expr.is_satisfied()

    def test_parentheses(self):
        """括号分组"""
        expr = TriggerConditionExpression("(READY OR STARTED) AND SUCCESS")
        expr.feed("STARTED")
        assert not expr.is_satisfied()
        expr.feed("SUCCESS")
        assert expr.is_satisfied()

    def test_complex_regex(self):
        """复杂正则"""
        expr = TriggerConditionExpression("/ERROR|FATAL/i")
        expr.feed("fatal error occurred")
        assert expr.is_satisfied()


class TestYAMLParsing:
    """测试 YAML 格式解析"""

    def test_minimal_task(self):
        """最小任务：只有 task 和 command"""
        yaml_body = """
task: 检查服务状态
command: systemctl status nginx
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['summary'] == '检查服务状态'
        assert result['command'] == 'systemctl status nginx'
        assert result['schedule_type'] == 'immediate'
        assert result['condition_expr'] is None
        assert result['repeat'] is False

    def test_schedule_after(self):
        """延迟执行"""
        yaml_body = """
task: 延迟检查
schedule:
  after: 30s
command: echo test
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['schedule_type'] == 'once'
        assert result['schedule_expr'] == '30s'

    def test_schedule_at(self):
        """定时执行"""
        yaml_body = """
task: 定时任务
schedule:
  at: 2024-12-31 23:59:59
command: echo happy new year
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['schedule_type'] == 'once'
        assert result['schedule_expr'] == '2024-12-31 23:59:59'

    def test_schedule_cron(self):
        """周期执行"""
        yaml_body = """
task: 每小时检查
schedule:
  cron: 0 * * * *
command: df -h
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['schedule_type'] == 'cron'
        assert result['schedule_expr'] == '0 * * * *'

    def test_condition_when(self):
        """条件监控"""
        yaml_body = """
task: 等待启动
condition:
  when: READY
command: tail -f app.log
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['condition_expr'] == 'READY'
        assert result['repeat'] is False

    def test_condition_repeat(self):
        """重复监控"""
        yaml_body = """
task: 持续监控
condition:
  when: ERROR
  repeat: true
command: tail -f error.log
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['condition_expr'] == 'ERROR'
        assert result['repeat'] is True

    def test_multiline_command(self):
        """多行命令"""
        yaml_body = """
task: 复杂脚本
command: |
  echo "Starting..."
  sleep 5
  echo "Done"
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert 'Starting' in result['command']
        assert 'Done' in result['command']

    def test_full_featured(self):
        """完整功能"""
        yaml_body = """
task: 完整示例任务
schedule:
  after: 1h
  timezone: Asia/Shanghai
condition:
  when: (READY OR STARTED) AND SUCCESS
  repeat: true
command: |
  tail -f /var/log/app.log | grep -E 'READY|STARTED|SUCCESS'
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        assert result['summary'] == '完整示例任务'
        assert result['schedule_type'] == 'once'
        assert result['schedule_expr'] == '1h'
        assert result['timezone'] == 'Asia/Shanghai'
        assert result['condition_expr'] == '(READY OR STARTED) AND SUCCESS'
        assert result['repeat'] is True
        assert 'tail -f' in result['command']


class TestErrorHandling:
    """测试错误处理"""

    def test_missing_task(self):
        """缺少 task 字段"""
        with pytest.raises(ValueError, match='缺少必填字段 "task"'):
            SelfTriggerManager._parse_definition("command: echo test")

    def test_missing_command(self):
        """缺少 command 字段"""
        with pytest.raises(ValueError, match='缺少必填字段 "command"'):
            SelfTriggerManager._parse_definition("task: test task")

    def test_invalid_yaml(self):
        """无效的 YAML"""
        with pytest.raises(ValueError, match='YAML 格式错误'):
            SelfTriggerManager._parse_definition("task: test\n  invalid: : :")

    def test_conflicting_schedule(self):
        """冲突的调度字段"""
        yaml_body = """
task: 冲突任务
schedule:
  after: 30s
  cron: 0 * * * *
command: echo test
"""
        with pytest.raises(ValueError, match='只能使用一个时间字段'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_repeat_without_when(self):
        """repeat 必须配合 when"""
        yaml_body = """
task: 错误重复
condition:
  repeat: true
command: echo test
"""
        with pytest.raises(ValueError, match='repeat 为 true 时必须同时设置'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_invalid_timezone(self):
        """无效时区"""
        yaml_body = """
task: 时区错误
schedule:
  after: 30s
  timezone: Invalid/Timezone
command: echo test
"""
        with pytest.raises(ValueError, match='未知时区'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_invalid_condition_syntax(self):
        """无效条件语法"""
        yaml_body = """
task: 条件错误
condition:
  when: READY AND
command: echo test
"""
        with pytest.raises(ValueError, match='condition.when 语法错误'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_task_too_long(self):
        """task 字段过长"""
        long_task = "x" * 601
        yaml_body = f"""
task: {long_task}
command: echo test
"""
        with pytest.raises(ValueError, match='不能超过 600 字符'):
            SelfTriggerManager._parse_definition(yaml_body)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
