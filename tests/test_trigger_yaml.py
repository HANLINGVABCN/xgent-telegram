"""trigger-x YAML 格式解析测试"""
import unittest
from unittest.mock import MagicMock

# AgentCommandBlacklist 定义在 core.py 的共享命名空间里，shell_triggers 作为独立
# 模块导入时该裸名不存在。直接给模块对象注入 mock：注意不能用 patch.dict 包裹
# import——退出时会把 with 块内 import 的模块对象移除，导致后续重新 import 拿到第
# 二个模块对象，而 SelfTriggerManager 方法的 __globals__ 仍指向已被移除的第一个，
# mock 赋值给错对象 → NameError。
import xgent_app.sections.shell_triggers as _shell_triggers_module
mock_blacklist = MagicMock()
mock_blacklist.check = MagicMock(return_value=(False, None))
_shell_triggers_module.AgentCommandBlacklist = mock_blacklist
from xgent_app.sections.shell_triggers import TriggerConditionExpression, SelfTriggerManager


class TestConditionExpression(unittest.TestCase):
    """测试智能条件表达式解析"""

    def test_literal_bare_word(self):
        """裸词自动视为字面量"""
        expr = TriggerConditionExpression("READY")
        self.assertFalse(expr.is_satisfied())
        expr.feed("Server is READY")
        self.assertTrue(expr.is_satisfied())
        self.assertIn("READY", expr.matched_literals())

    def test_literal_quoted(self):
        """引号字符串"""
        expr = TriggerConditionExpression('"error timeout"')
        expr.feed("Got error timeout message")
        self.assertTrue(expr.is_satisfied())

    def test_regex_pattern(self):
        """正则表达式匹配"""
        expr = TriggerConditionExpression("/error.*/i")
        self.assertFalse(expr.is_satisfied())
        expr.feed("ERROR: connection failed")
        self.assertTrue(expr.is_satisfied())

    def test_logic_and(self):
        """AND 逻辑"""
        expr = TriggerConditionExpression("READY AND SUCCESS")
        expr.feed("READY")
        self.assertFalse(expr.is_satisfied())
        expr.feed("SUCCESS")
        self.assertTrue(expr.is_satisfied())

    def test_logic_or(self):
        """OR 逻辑"""
        expr = TriggerConditionExpression("READY OR STARTED")
        expr.feed("STARTED")
        self.assertTrue(expr.is_satisfied())

    def test_parentheses(self):
        """括号分组"""
        expr = TriggerConditionExpression("(READY OR STARTED) AND SUCCESS")
        expr.feed("STARTED")
        self.assertFalse(expr.is_satisfied())
        expr.feed("SUCCESS")
        self.assertTrue(expr.is_satisfied())

    def test_complex_regex(self):
        """复杂正则"""
        expr = TriggerConditionExpression("/ERROR|FATAL/i")
        expr.feed("fatal error occurred")
        self.assertTrue(expr.is_satisfied())


class TestYAMLParsing(unittest.TestCase):
    """测试 YAML 格式解析"""

    def test_minimal_task(self):
        """最小任务：只有 task 和 command"""
        yaml_body = """
task: 检查服务状态
command: systemctl status nginx
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        self.assertEqual(result['summary'], '检查服务状态')
        self.assertEqual(result['command'], 'systemctl status nginx')
        self.assertEqual(result['schedule_type'], 'immediate')
        self.assertIsNone(result['condition_expr'])
        self.assertFalse(result['repeat'])

    def test_schedule_after(self):
        """延迟执行"""
        yaml_body = """
task: 延迟检查
schedule:
  after: 30s
command: echo test
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        self.assertEqual(result['schedule_type'], 'once')
        self.assertEqual(result['schedule_expr'], '30s')

    def test_schedule_at(self):
        """定时执行"""
        yaml_body = """
task: 定时任务
schedule:
  at: 2024-12-31 23:59:59
command: echo happy new year
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        self.assertEqual(result['schedule_type'], 'once')
        self.assertEqual(result['schedule_expr'], '2024-12-31 23:59:59')

    def test_schedule_cron(self):
        """周期执行"""
        yaml_body = """
task: 每小时检查
schedule:
  cron: 0 * * * *
command: df -h
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        self.assertEqual(result['schedule_type'], 'cron')
        self.assertEqual(result['schedule_expr'], '0 * * * *')

    def test_condition_when(self):
        """条件监控"""
        yaml_body = """
task: 等待启动
condition:
  when: READY
command: tail -f app.log
"""
        result = SelfTriggerManager._parse_definition(yaml_body)
        self.assertEqual(result['condition_expr'], 'READY')
        self.assertFalse(result['repeat'])

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
        self.assertEqual(result['condition_expr'], 'ERROR')
        self.assertTrue(result['repeat'])

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
        self.assertIn('Starting', result['command'])
        self.assertIn('Done', result['command'])

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
        self.assertEqual(result['summary'], '完整示例任务')
        self.assertEqual(result['schedule_type'], 'once')
        self.assertEqual(result['schedule_expr'], '1h')
        self.assertEqual(result['timezone'], 'Asia/Shanghai')
        self.assertEqual(result['condition_expr'], '(READY OR STARTED) AND SUCCESS')
        self.assertTrue(result['repeat'])
        self.assertIn('tail -f', result['command'])


class TestErrorHandling(unittest.TestCase):
    """测试错误处理"""

    def test_missing_task(self):
        """缺少 task 字段"""
        with self.assertRaisesRegex(ValueError, '缺少必填字段 "task"'):
            SelfTriggerManager._parse_definition("command: echo test")

    def test_missing_command(self):
        """缺少 command 字段"""
        with self.assertRaisesRegex(ValueError, '缺少必填字段 "command"'):
            SelfTriggerManager._parse_definition("task: test task")

    def test_invalid_yaml(self):
        """无效的 YAML"""
        with self.assertRaisesRegex(ValueError, 'YAML 格式错误'):
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
        with self.assertRaisesRegex(ValueError, '只能使用一个时间字段'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_repeat_without_when(self):
        """repeat 必须配合 when"""
        yaml_body = """
task: 错误重复
condition:
  repeat: true
command: echo test
"""
        with self.assertRaisesRegex(ValueError, 'repeat 为 true 时必须同时设置'):
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
        with self.assertRaisesRegex(ValueError, '未知时区'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_invalid_condition_syntax(self):
        """无效条件语法"""
        yaml_body = """
task: 条件错误
condition:
  when: READY AND
command: echo test
"""
        with self.assertRaisesRegex(ValueError, 'condition.when 语法错误'):
            SelfTriggerManager._parse_definition(yaml_body)

    def test_task_too_long(self):
        """task 字段过长"""
        long_task = "x" * 601
        yaml_body = f"""
task: {long_task}
command: echo test
"""
        with self.assertRaisesRegex(ValueError, '不能超过 600 字符'):
            SelfTriggerManager._parse_definition(yaml_body)


if __name__ == '__main__':
    unittest.main()
