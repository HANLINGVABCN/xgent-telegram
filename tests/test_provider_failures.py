from tests.test_thinking_params import run_in_app


def test_raw_refusal_survives_native_cleanup_refresh_and_next_model_request(tmp_path):
    result = run_in_app('import asyncio\nfrom tests.provider_failure_probe import exercise\n' +
                        f'print(json.dumps(asyncio.run(exercise(bot,{str(tmp_path)!r}))))')
    assert len(result) == 16 and all(result.values())
