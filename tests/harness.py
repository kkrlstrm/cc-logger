"""End-to-end test harness.

Boots the FastAPI app in-process, POSTs a realistic simulated session
covering all 8 hook events including parallel sub-agent fan-out and an
oversize payload that should spill to artifacts. Verifies DB state.

Run:
    uv run python tests/harness.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from cc_logger import db  # noqa: E402

PORT = int(os.getenv("HOOK_PORT", "8788"))  # different port from real service to avoid collision
BASE = f"http://127.0.0.1:{PORT}"


SESSION_ID = f"test-session-{uuid.uuid4()}"
CWD = str(Path.cwd())

# Three parallel Agent calls of the SAME subagent_type to force the
# candidate-list linking path.
AGENT_TOOL_USE_IDS = [f"tool-agent-{i}-{uuid.uuid4()}" for i in range(3)]
SUBAGENT_IDS = [f"sub-agent-{i}" for i in range(3)]
SUBAGENT_TYPE = "Explore"

# One per sub-agent: a Bash and an Edit call
BASH_TOOL_USE_IDS = [f"tool-bash-{i}-{uuid.uuid4()}" for i in range(3)]
EDIT_TOOL_USE_IDS = [f"tool-edit-{i}-{uuid.uuid4()}" for i in range(3)]

OVERSIZE_OUTPUT = "x" * (60 * 1024)  # 60KB to force artifact spillover


def _common() -> dict:
    return {
        "session_id": SESSION_ID,
        "transcript_path": "/tmp/test-transcript.jsonl",
        "cwd": CWD,
        "permission_mode": "default",
    }


def events() -> list[dict]:
    """Build the full event sequence."""
    ev = []
    ev.append({**_common(), "hook_event_name": "SessionStart", "source": "startup", "model": "claude-opus-4-7"})
    ev.append({**_common(), "hook_event_name": "UserPromptSubmit", "prompt": "Find all callers of foo() across the repo."})

    # Root agent fires three parallel Agent tool_uses in one turn
    for i, tool_id in enumerate(AGENT_TOOL_USE_IDS):
        ev.append({
            **_common(),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_use_id": tool_id,
            "tool_input": {
                "subagent_type": SUBAGENT_TYPE,
                "description": f"Search batch {i}",
                "prompt": f"Search the {i}th third of the repo for foo()",
            },
        })

    # Each spawned subagent fires
    for i in range(3):
        ev.append({
            **_common(),
            "hook_event_name": "SubagentStart",
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "prompt": f"Search the {i}th third of the repo for foo()",
        })
        # Inside that subagent: Bash + Edit
        ev.append({
            **_common(),
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": BASH_TOOL_USE_IDS[i],
            "tool_input": {"command": f"grep -rn 'foo(' src/dir{i}/"},
        })
        ev.append({
            **_common(),
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": BASH_TOOL_USE_IDS[i],
            "tool_input": {"command": f"grep -rn 'foo(' src/dir{i}/"},
            "tool_response": {
                "stdout": OVERSIZE_OUTPUT if i == 0 else f"src/dir{i}/a.py:42:    foo()",
                "stderr": "",
                "exit_code": 0,
            },
        })
        ev.append({
            **_common(),
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_use_id": EDIT_TOOL_USE_IDS[i],
            "tool_input": {"file_path": f"/tmp/out{i}.txt", "old_string": "old", "new_string": "new"},
        })
        ev.append({
            **_common(),
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_use_id": EDIT_TOOL_USE_IDS[i],
            "tool_input": {"file_path": f"/tmp/out{i}.txt", "old_string": "old", "new_string": "new"},
            "tool_response": {"success": True},
        })
        ev.append({
            **_common(),
            "hook_event_name": "SubagentStop",
            "agent_id": SUBAGENT_IDS[i],
            "agent_type": SUBAGENT_TYPE,
            "last_message": f"Found 1 reference in dir{i}.",
        })

    # Now PostToolUse for the three Agent tool calls (in completion order)
    for i, tool_id in enumerate(AGENT_TOOL_USE_IDS):
        ev.append({
            **_common(),
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_use_id": tool_id,
            "tool_input": {"subagent_type": SUBAGENT_TYPE},
            "tool_response": {"agent_id": SUBAGENT_IDS[i], "summary": f"dir{i} done"},
        })

    # One failing tool call to exercise PostToolUseFailure
    fail_tool_id = f"tool-fail-{uuid.uuid4()}"
    ev.append({
        **_common(),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_use_id": fail_tool_id,
        "tool_input": {"command": "false"},
    })
    ev.append({
        **_common(),
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_use_id": fail_tool_id,
        "tool_input": {"command": "false"},
        "error": "exit code 1",
    })

    ev.append({**_common(), "hook_event_name": "SessionEnd", "reason": "exit", "total_tokens": 123456})
    return ev


async def post_all(events_: list[dict]) -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        # Wait for /healthz
        for _ in range(50):
            try:
                r = await client.get("/healthz")
                if r.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("service never came up")

        for e in events_:
            r = await client.post("/hook", json=e)
            assert r.status_code == 200, f"{e['hook_event_name']}: {r.text}"

        # Drain queue (be patient — fresh-conn-per-event is slow but reliable)
        last_processed = -1
        stalled_polls = 0
        for _ in range(600):
            r = await client.get("/healthz")
            j = r.json()
            if j["queue_depth"] == 0:
                break
            if j["processed"] == last_processed and j["failed"] == 0:
                stalled_polls += 1
                if stalled_polls > 60:  # 30s with no progress
                    raise RuntimeError(f"worker stalled: {j}")
            else:
                stalled_polls = 0
                last_processed = j["processed"]
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"queue never drained: {j}")
        print(f"Posted {len(events_)} events. processed={j['processed']} failed={j['failed']}")
        assert j["failed"] == 0, f"worker reported {j['failed']} failures"


async def verify_db() -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM sessions WHERE session_id=%s", (SESSION_ID,))
            assert (await cur.fetchone())[0] == 1, "session row missing"

            await cur.execute(
                "SELECT initial_prompt, ended_at, end_reason, total_tokens FROM sessions WHERE session_id=%s",
                (SESSION_ID,),
            )
            row = await cur.fetchone()
            assert row[0] and "foo()" in row[0], "initial_prompt not captured"
            assert row[1] is not None, "ended_at not set"
            assert row[2] == "exit", f"end_reason wrong: {row[2]}"
            assert row[3] == 123456, f"total_tokens wrong: {row[3]}"

            await cur.execute(
                "SELECT count(*) FROM agent_invocations WHERE session_id=%s",
                (SESSION_ID,),
            )
            inv_count = (await cur.fetchone())[0]
            assert inv_count == 4, f"expected 4 invocations (1 root + 3 sub), got {inv_count}"

            await cur.execute(
                """
                SELECT count(*) FROM agent_invocations
                WHERE session_id=%s AND candidate_parent_tool_call_ids IS NOT NULL
                """,
                (SESSION_ID,),
            )
            cand_count = (await cur.fetchone())[0]
            assert cand_count == 3, f"expected 3 subagents with candidates, got {cand_count}"

            await cur.execute(
                """
                SELECT tool_name, status, count(*) FROM tool_calls
                WHERE session_id=%s GROUP BY tool_name, status ORDER BY tool_name, status
                """,
                (SESSION_ID,),
            )
            tool_summary = await cur.fetchall()
            print("Tool summary:")
            for r in tool_summary:
                print(f"  {r[0]:8s} {r[1]:10s} count={r[2]}")

            await cur.execute(
                """
                SELECT count(*) FROM tool_calls
                WHERE session_id=%s AND tool_name='Bash' AND status='failure'
                """,
                (SESSION_ID,),
            )
            fail_count = (await cur.fetchone())[0]
            assert fail_count == 1, f"expected 1 failure, got {fail_count}"

            await cur.execute(
                """
                SELECT count(*) FROM artifacts a
                JOIN tool_calls tc ON tc.tool_call_id = a.tool_call_id
                WHERE tc.session_id=%s
                """,
                (SESSION_ID,),
            )
            artifact_count = (await cur.fetchone())[0]
            assert artifact_count >= 1, f"expected >=1 artifact (60KB Bash stdout), got {artifact_count}"
            print(f"Artifacts spilled: {artifact_count}")

            await cur.execute(
                "SELECT count(*) FROM tool_calls WHERE session_id=%s AND status='orphaned'",
                (SESSION_ID,),
            )
            orphan_count = (await cur.fetchone())[0]
            assert orphan_count == 0, f"expected 0 orphans, got {orphan_count}"

    await db.close_pool()
    print("\nALL ASSERTIONS PASSED.")


async def cleanup() -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sessions WHERE session_id=%s", (SESSION_ID,))
    await db.close_pool()


async def run() -> None:
    os.environ["HOOK_PORT"] = str(PORT)
    config = uvicorn.Config("cc_logger.app:app", host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        await post_all(events())
        # Brief pause so the worker writes are flushed (queue_depth=0 doesn't guarantee
        # the in-flight cursor.execute returned to commit).
        await asyncio.sleep(0.2)
        await verify_db()
    finally:
        if "--keep" not in sys.argv:
            await cleanup()
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(run())
