"""Exercise the complete Telegram reply lifecycle without contacting Telegram."""
import tempfile

from tests.test_thinking_params import run_in_app


def test_final_details_and_finished_buttons_across_all_bot_response_modes():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom tests.telegram_chat_probe import display_matrix\n'
                            + f'print(json.dumps(asyncio.run(display_matrix(bot,{root!r}))))')
    assert len(result['cases']) == 9
    assert all(case['one_details'] and case['buttons_clean'] for case in result['cases'])


def test_single_reply_missing_usage_no_reply_and_final_detail_overflow():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom tests.telegram_chat_probe import single_reply_edges\n'
                            + f'print(json.dumps(asyncio.run(single_reply_edges(bot,{root!r}))))')
    assert len(result['cases']) == 6
    assert all(case['details_once'] for case in result['cases'])


def test_native_rich_api_uses_same_multi_round_lifecycle_and_cleans_controls():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom unittest.mock import patch\n'
                            'from tests.telegram_rich_probe import RichCapture\n'
                            'from tests.telegram_chat_probe import display_matrix\n'
                            "with patch('tests.telegram_chat_probe.TelegramCapture',RichCapture):\n"
                            + f' print(json.dumps(asyncio.run(display_matrix(bot,{root!r}))))')
    assert len(result['cases']) == 9
    assert all(case['one_details'] and case['buttons_clean'] for case in result['cases'])


def test_native_stop_button_enters_outer_telegram_handler_with_phase_identity():
    with tempfile.TemporaryDirectory() as root:
        result=run_in_app('import asyncio\nfrom tests.telegram_rich_probe import outer_stop\n'
                          +f'print(json.dumps(asyncio.run(outer_stop(bot,{root!r}))))')
    assert result['active_stopped'] and result['old_phase_rejected'],result
