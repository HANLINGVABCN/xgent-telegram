from tests.test_thinking_params import run_in_app


def test_web_choices_custom_text_update_the_same_native_bot_form(tmp_path):
    result = run_in_app('import asyncio\nfrom tests.ask_web_bot_sync_probe import exercise\n' +
                        f'print(json.dumps(asyncio.run(exercise(bot,{str(tmp_path)!r}))))')
    assert all(result.values())
