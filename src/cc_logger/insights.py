# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-session analytics: power-law distribution, peak hours, repeat-fail
domains, tool mix breakdown, root vs sub-agent split.

Same shape as the manual analyses produced during development. Runs against
any window of captured data.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from . import db


async def _query_all(cur, sql: str, params: tuple = ()) -> list[dict]:
    await cur.execute(sql, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


def _fmt_tokens(n: int | None) -> str:
    """Human-readable token count: 1_234_567 -> '1.2M'."""
    if not n:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


async def report(days: int = 30) -> None:
    print(f"\n=== cc-logger insights (last {days} days) ===\n")

    async with db.connection() as conn:
        async with conn.cursor() as cur:
            # 1) Headline numbers
            rows = await _query_all(cur, """
                SELECT
                  count(distinct s.session_id) AS sessions,
                  count(tc.tool_call_id) AS tools,
                  count(*) FILTER (WHERE tc.status = 'failure') AS fails,
                  count(distinct ai.invocation_id) FILTER (WHERE ai.parent_invocation_id IS NOT NULL) AS subs,
                  count(distinct tc.tool_name) AS distinct_tools
                FROM sessions s
                LEFT JOIN tool_calls tc ON tc.session_id = s.session_id
                LEFT JOIN agent_invocations ai ON ai.session_id = s.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
            """, (str(days),))
            r = rows[0]
            print(f"Totals: {r['sessions']} sessions, {r['tools'] or 0} tool calls, "
                  f"{r['fails'] or 0} failures, {r['subs'] or 0} sub-agents, "
                  f"{r['distinct_tools'] or 0} distinct tools\n")

            # 1b) Token usage + self-rating coverage (recovered from transcripts)
            rows = await _query_all(cur, """
                SELECT model,
                       count(*) AS sessions,
                       sum(input_tokens) AS in_tok,
                       sum(output_tokens) AS out_tok,
                       sum(cache_read_tokens) AS cr_tok,
                       sum(total_tokens) AS tot_tok
                FROM sessions
                WHERE started_at > now() - (%s || ' days')::interval
                GROUP BY model ORDER BY tot_tok DESC NULLS LAST
            """, (str(days),))
            if any(r["tot_tok"] for r in rows):
                print("Token usage by model:")
                print(f"  {'model':22s} {'sess':>5s} {'in':>7s} {'out':>7s} {'cache_rd':>9s} {'total':>7s}")
                for r in rows:
                    if not r["tot_tok"]:
                        continue
                    print(f"  {(r['model'] or '(unknown)'):22s} {r['sessions']:>5d} "
                          f"{_fmt_tokens(r['in_tok']):>7s} {_fmt_tokens(r['out_tok']):>7s} "
                          f"{_fmt_tokens(r['cr_tok']):>9s} {_fmt_tokens(r['tot_tok']):>7s}")
                print()
            rated = await _query_all(cur, """
                SELECT count(*) FILTER (WHERE self_rating IS NOT NULL) AS rated,
                       count(*) AS total,
                       round(avg(self_rating)::numeric, 1) AS avg_rating
                FROM sessions WHERE started_at > now() - (%s || ' days')::interval
            """, (str(days),))
            rr = rated[0]
            avg = f", avg {rr['avg_rating']}/5" if rr["avg_rating"] is not None else ""
            print(f"Self-rated sessions: {rr['rated']}/{rr['total']}{avg}  "
                  f"(rate one with `cc-logger rate <session> <1-5> --note \"…\"`)\n")

            # 2) Power-law check
            rows = await _query_all(cur, """
                SELECT s.session_id, count(*) AS tool_count
                FROM sessions s
                JOIN tool_calls tc ON tc.session_id = s.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
                GROUP BY 1 ORDER BY 2 DESC
            """, (str(days),))
            total = sum(r["tool_count"] for r in rows) or 1
            if rows:
                top4 = sum(r["tool_count"] for r in rows[:4])
                pct = round(100 * top4 / total)
                print(f"Power-law check: top 4 sessions hold {pct}% of all tool calls ({top4}/{total}).")
                print("(Anything above ~70% means your work is concentrated in a few big sessions.)\n")

            # 3) Tool mix + failure rates
            rows = await _query_all(cur, """
                SELECT tc.tool_name,
                       count(*) FILTER (WHERE tc.status='success') AS ok,
                       count(*) FILTER (WHERE tc.status='failure') AS fail,
                       count(*) FILTER (WHERE tc.status='pending') AS pend,
                       round(avg(tc.duration_ms)::numeric, 0) AS avg_ms
                FROM tool_calls tc
                JOIN sessions s ON s.session_id = tc.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
                GROUP BY 1
                ORDER BY (count(*) FILTER (WHERE tc.status='success') + count(*) FILTER (WHERE tc.status='failure')) DESC
            """, (str(days),))
            print("Tool usage:")
            print(f"  {'tool':36s} {'ok':>5s} {'fail':>5s} {'pend':>5s} {'avg':>7s}  fail%")
            for r in rows:
                tot = r["ok"] + r["fail"]
                pct = round(100 * r["fail"] / tot, 1) if tot else 0
                print(f"  {r['tool_name']:36s} {r['ok']:>5d} {r['fail']:>5d} {r['pend']:>5d} "
                      f"{r['avg_ms'] or 0:>5}ms  {pct}%")
            print()

            # 4) Root vs sub-agent work split
            rows = await _query_all(cur, """
                SELECT
                  CASE WHEN ai.parent_invocation_id IS NULL THEN 'root' ELSE 'sub-agent' END AS scope,
                  tc.tool_name, count(*) AS n
                FROM tool_calls tc
                LEFT JOIN agent_invocations ai ON ai.invocation_id = tc.invocation_id
                JOIN sessions s ON s.session_id = tc.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
                GROUP BY 1, 2 ORDER BY 1, 3 DESC
            """, (str(days),))
            print("Work split (root vs sub-agent):")
            current = None
            for r in rows:
                if r["scope"] != current:
                    print(f"  [{r['scope']}]")
                    current = r["scope"]
                print(f"    {r['tool_name']:30s} x{r['n']}")
            print()

            # 5) WebFetch failure by domain
            rows = await _query_all(cur, """
                SELECT split_part(replace(replace(tc.tool_input->>'url', 'https://', ''), 'http://', ''), '/', 1) AS host,
                       count(*) AS fails
                FROM tool_calls tc
                JOIN sessions s ON s.session_id = tc.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
                  AND tc.tool_name='WebFetch' AND tc.status='failure'
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """, (str(days),))
            if rows:
                print("Top WebFetch failure domains (timeout / unreachable):")
                for r in rows:
                    print(f"  x{r['fails']:<3d} {r['host']}")
                print()

            # 6) Hourly activity
            rows = await _query_all(cur, """
                SELECT EXTRACT(hour FROM tc.started_at)::int AS hr,
                       count(*) AS n
                FROM tool_calls tc
                JOIN sessions s ON s.session_id = tc.session_id
                WHERE s.started_at > now() - (%s || ' days')::interval
                GROUP BY 1 ORDER BY 1
            """, (str(days),))
            if rows:
                max_n = max(r["n"] for r in rows)
                print("Hourly activity (when do you use Claude Code?):")
                for r in rows:
                    bar = "#" * int(40 * r["n"] / max_n)
                    print(f"  {r['hr']:02d}:00  {r['n']:>5d} {bar}")
                print()


async def sessions(limit: int = 20) -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.session_id, s.started_at, s.ended_at, s.model,
                       s.total_tokens, s.self_rating,
                       (SELECT count(*) FROM tool_calls tc WHERE tc.session_id=s.session_id) AS tools,
                       (SELECT count(*) FROM agent_invocations ai
                          WHERE ai.session_id=s.session_id AND ai.parent_invocation_id IS NOT NULL) AS subs,
                       LEFT(COALESCE(s.initial_prompt,''), 64) AS prompt
                FROM sessions s
                ORDER BY s.started_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    print(f"\n{len(rows)} most recent sessions:\n")
    for r in rows:
        sid, started, ended, model, tokens, rating, tools, subs, prompt = r
        ended_str = ended.strftime("%m-%d %H:%M") if ended else "open       "
        model_str = (model or "?").replace("claude-", "")[:12]
        rate_str = f"{rating}★" if rating else "  "
        print(f"  {sid[:18]:18s}  {started.strftime('%m-%d %H:%M')}→{ended_str:11s}  "
              f"{model_str:12s} tok={_fmt_tokens(tokens):>6s} {rate_str}  "
              f"tools={tools:>4d} subs={subs:>2d}  {prompt!r}")


async def _resolve_session(cur, prefix: str) -> str:
    """Resolve a session id or unique prefix to a full session_id."""
    await cur.execute(
        "SELECT session_id FROM sessions WHERE session_id = %s OR session_id LIKE %s LIMIT 5",
        (prefix, prefix + "%"),
    )
    rows = [r[0] for r in await cur.fetchall()]
    if not rows:
        raise SystemExit(f"No session matching {prefix!r}.")
    # exact match wins even if it's also a prefix of others
    if prefix in rows:
        return prefix
    if len(rows) > 1:
        raise SystemExit(f"Prefix {prefix!r} is ambiguous ({len(rows)}+ matches); use more characters.")
    return rows[0]


async def set_rating(session_id: str, rating: int, note: str | None) -> None:
    if not 1 <= rating <= 5:
        raise SystemExit("rating must be between 1 and 5.")
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            sid = await _resolve_session(cur, session_id)
            await cur.execute(
                """
                UPDATE sessions
                SET self_rating = %s,
                    retro_note = COALESCE(%s, retro_note)
                WHERE session_id = %s
                """,
                (rating, note, sid),
            )
    suffix = f"  note: {note!r}" if note else ""
    print(f"Rated {sid[:18]} → {rating}/5{suffix}")


def run_insights(days: int) -> None:
    asyncio.run(report(days))


def run_sessions(limit: int) -> None:
    asyncio.run(sessions(limit))


def run_rate(session_id: str, rating: int, note: str | None) -> None:
    asyncio.run(set_rating(session_id, rating, note))
