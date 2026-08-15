"""trigger-x YAML format parsing test (simplified)"""
from unittest.mock import MagicMock

# Mock dependencies before import
import xgent_app.sections.shell_triggers as st
st.AgentCommandBlacklist = MagicMock()
st.AgentCommandBlacklist.check = MagicMock(return_value=(False, None))

from xgent_app.sections.shell_triggers import TriggerConditionExpression, SelfTriggerManager


def test_condition_literal():
    """Test literal matching"""
    expr = TriggerConditionExpression("READY")
    assert not expr.is_satisfied()
    expr.feed("Server is READY")
    assert expr.is_satisfied()


def test_condition_regex():
    """Test regex matching"""
    expr = TriggerConditionExpression("/error.*/i")
    expr.feed("ERROR: connection failed")
    assert expr.is_satisfied()


def test_condition_logic():
    """Test AND/OR logic"""
    expr = TriggerConditionExpression("READY AND SUCCESS")
    expr.feed("READY")
    assert not expr.is_satisfied()
    expr.feed("SUCCESS")
    assert expr.is_satisfied()


def test_yaml_minimal():
    """Test minimal YAML task"""
    yaml_body = """
task: Check service status
command: systemctl status nginx
"""
    result = SelfTriggerManager._parse_definition(yaml_body)
    assert result['summary'] == 'Check service status'
    assert result['command'] == 'systemctl status nginx'
    assert result['schedule_type'] == 'immediate'


def test_yaml_schedule_after():
    """Test delayed execution"""
    yaml_body = """
task: Delayed check
schedule:
  after: 30s
command: echo test
"""
    result = SelfTriggerManager._parse_definition(yaml_body)
    assert result['schedule_type'] == 'once'
    assert result['schedule_expr'] == '30s'


def test_yaml_condition():
    """Test condition monitoring"""
    yaml_body = """
task: Wait for startup
condition:
  when: READY
command: tail -f app.log
"""
    result = SelfTriggerManager._parse_definition(yaml_body)
    assert result['condition_expr'] == 'READY'
    assert result['repeat'] is False


def test_yaml_repeat():
    """Test repeat monitoring"""
    yaml_body = """
task: Continuous monitoring
condition:
  when: ERROR
  repeat: true
command: tail -f error.log
"""
    result = SelfTriggerManager._parse_definition(yaml_body)
    assert result['condition_expr'] == 'ERROR'
    assert result['repeat'] is True


def test_error_missing_task():
    """Test missing task field"""
    try:
        SelfTriggerManager._parse_definition("command: echo test")
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert 'task' in str(e).lower()


def test_error_missing_command():
    """Test missing command field"""
    try:
        SelfTriggerManager._parse_definition("task: test task")
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert 'command' in str(e).lower()


if __name__ == '__main__':
    print("Running trigger-x YAML tests...")
    test_condition_literal()
    print("  [PASS] condition_literal")
    test_condition_regex()
    print("  [PASS] condition_regex")
    test_condition_logic()
    print("  [PASS] condition_logic")
    test_yaml_minimal()
    print("  [PASS] yaml_minimal")
    test_yaml_schedule_after()
    print("  [PASS] yaml_schedule_after")
    test_yaml_condition()
    print("  [PASS] yaml_condition")
    test_yaml_repeat()
    print("  [PASS] yaml_repeat")
    test_error_missing_task()
    print("  [PASS] error_missing_task")
    test_error_missing_command()
    print("  [PASS] error_missing_command")
    print("\nAll tests passed!")
