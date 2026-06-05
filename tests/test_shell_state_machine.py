import asyncio
import os
import sys


os.environ.setdefault("BOT_TOKEN", "123456:dummy")
os.environ.setdefault("AUTHORIZED_USER_ID", "1")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bot_server  # noqa: E402


async def run_case(label, command, expected_states, timeout=5):
    bot_server.UserDataManager.set("agent_command_timeout", timeout)
    result = await bot_server.AgentShellSessionManager.start(command)
    state = result.get("wait_state")
    running = result.get("running")
    reason = result.get("wait_state_reason")
    print(f"{label}: state={state} running={running} reason={reason}")
    assert state in expected_states, result
    return result


async def main():
    try:
        completed = await run_case(
            "completed",
            "printf 'done\\n'",
            {"completed"},
        )
        assert completed.get("running") is False
        assert "done" in str(completed.get("output") or "")

        silent = await run_case(
            "silent_running",
            "sleep 20",
            {"silent_running", "timeout"},
        )
        assert silent.get("running") is True
        await bot_server.AgentShellSessionManager.kill(str(silent["session_id"]))

        quiet = await run_case(
            "output_quiet",
            "printf 'ready\\n'; sleep 20",
            {"output_quiet", "output_stalled", "timeout"},
        )
        assert quiet.get("running") is True
        assert "ready" in str(quiet.get("output") or "")
        await bot_server.AgentShellSessionManager.kill(str(quiet["session_id"]))

        active = await run_case(
            "active_output",
            "i=0; while true; do i=$((i+1)); printf 'tick-%s abcdefghijklmnopqrstuvwxyz\\n' \"$i\"; sleep 0.2; done",
            {"active_output"},
        )
        assert active.get("running") is True
        assert int(active.get("recent_output_chunks") or 0) >= 3
        await bot_server.AgentShellSessionManager.kill(str(active["session_id"]))

        interactive = await run_case(
            "interactive_prompt",
            "printf 'Continue? [y/N] '; sleep 20",
            {"interactive_prompt"},
        )
        assert interactive.get("running") is True
        await bot_server.AgentShellSessionManager.kill(str(interactive["session_id"]))

        print("shell state machine tests passed")
    finally:
        bot_server.AgentShellSessionManager.kill_all()


if __name__ == "__main__":
    asyncio.run(main())
