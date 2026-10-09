import tempfile
from tests.test_thinking_params import run_in_app


def test_native_bot_selection_and_management_states():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom tests.telegram_independent_probe import native_entry_and_state\n'
                            + f'print(json.dumps(asyncio.run(native_entry_and_state(bot, {root!r}))))')
    assert all(result.values())


def test_web_cli_replay_and_task_delivery_routes():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom tests.telegram_independent_probe import mirror_routes\n'
                            + f'print(json.dumps(asyncio.run(mirror_routes(bot, {root!r}))))')
    assert all(result.values())


def test_pure_telegram_labels_and_unselected_chooser():
    with tempfile.TemporaryDirectory() as root:
        result = run_in_app('import asyncio\nfrom tests.telegram_independent_probe import pure_telegram_and_chooser\n'
                            + f'print(json.dumps(asyncio.run(pure_telegram_and_chooser(bot, {root!r}))))')
    assert all(result.values())
