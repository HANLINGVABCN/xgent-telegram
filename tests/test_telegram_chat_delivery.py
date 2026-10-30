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
