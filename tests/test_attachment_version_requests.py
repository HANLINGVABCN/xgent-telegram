import tempfile

import pytest

from tests.test_thinking_params import run_in_app


@pytest.mark.parametrize('scenario', ['api_cycle', 'rejected_and_rollback', 'ingress_failure_and_isolation', 'set_bot', 'set_bot_joins_current_reply', 'upload_acknowledgement', 'restore_cancellation_and_delete_race'])
def test_full_attachment_version_requests(scenario):
    with tempfile.TemporaryDirectory() as folder:
        result = run_in_app('import asyncio\nfrom tests import attachment_version_probe as probe\n'
                            f'print(json.dumps(asyncio.run(probe.{scenario}(bot, {folder!r}))))')
    assert all(result.values()), result
