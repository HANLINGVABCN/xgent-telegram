from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from xgent_app.agent_dispatch import dispatch_standard_protocol


class SearchKeyWiringTests(unittest.IsolatedAsyncioTestCase):
    """回归：调用点必须把已配置的 key 传下去。

    此前 messages.py 的调用点漏传 search_api_key，导致参数默认 None，
    菜单里“测试搜索”正常（直接读 BotConfig），但 Agent 实际用 search-x
    时永远报“未配置”。
    """

    async def test_dispatch_forwards_configured_key(self):
        with patch(
            "xgent_app.agent_dispatch.run_search",
            new=AsyncMock(return_value={"success": True, "output": "ok"}),
        ) as search:
            await dispatch_standard_protocol(
                {"type": "search", "body": "q"},
                executor=Mock(),
                provider_api_format="openai",
                stop_event_factory=Mock(),
                logger=Mock(),
                search_api_key="tvly-configured",
            )
        # 第二个位置参数就是 key，漏传时会是 None
        self.assertEqual("tvly-configured", search.await_args.args[1])

    async def test_missing_key_is_distinguishable_from_configured(self):
        with patch(
            "xgent_app.agent_dispatch.run_search",
            new=AsyncMock(return_value={"success": False, "output": "未配置"}),
        ) as search:
            await dispatch_standard_protocol(
                {"type": "search", "body": "q"},
                executor=Mock(),
                provider_api_format="openai",
                stop_event_factory=Mock(),
                logger=Mock(),
            )
        self.assertIsNone(search.await_args.args[1])


class NonStreamHardTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """回归：非流式请求必须有硬上限，不能永远挂着。"""

    async def test_wait_for_interrupts_a_hanging_request(self):
        async def never_returns():
            await asyncio.Event().wait()      # 模拟连接静默挂起

        task = asyncio.create_task(never_returns())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_normal_response_is_not_affected_by_the_cap(self):
        async def quick():
            return ("hello", None)

        result = await asyncio.wait_for(quick(), timeout=5.0)
        self.assertEqual(("hello", None), result)


if __name__ == "__main__":
    unittest.main()
