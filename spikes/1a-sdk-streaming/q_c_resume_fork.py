"""(c) resume + fork_session at burst start: memory carries over, ids fork, branches isolate."""

from __future__ import annotations

import asyncio
import secrets

from claude_agent_sdk import ClaudeSDKClient

from harness import CONFIG_DIR, Recorder, base_options, collect, require_token, user_msg


async def burst(prompt: str, **opts) -> tuple[str, str]:
    """One burst = one connect with one queued item; returns (session_id, text)."""

    async def gen():
        yield user_msg(prompt)

    client = ClaudeSDKClient(base_options(**opts))
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()
    _, result = got.results[0]
    return result.session_id, got.texts_per_turn[0]


async def main() -> int:
    require_token()
    rec = Recorder("c_resume_fork")
    word1 = f"PAPAYA{secrets.token_hex(3).upper()}"
    word2 = f"KIWI{secrets.token_hex(3).upper()}"

    s1, _ = await burst(f"Remember the codeword {word1}. Reply OK.")
    s2, t2 = await burst(
        f"What is the codeword? Also remember second word {word2}. Reply with the codeword only.",
        resume=s1,
        fork_session=True,
    )
    rec.check("c1_resume_remembers", word1 in t2, t2)
    rec.check("c2_fork_new_session_id", s2 != s1, {"s1": s1, "s2": s2})

    s3, t3 = await burst(
        "Did I tell you a second word? Reply with it, or NONE.", resume=s1, fork_session=True
    )
    rec.check("c3_fork_branches_isolated", word2 not in t3, t3)
    rec.check("c3_new_id_again", s3 not in (s1, s2), [s1, s2, s3])

    _s4, t4 = await burst("Reply with the second word only.", resume=s2, fork_session=True)
    rec.check("c4_resume_from_fork", word2 in t4, t4)

    files = sorted(str(p.relative_to(CONFIG_DIR)) for p in CONFIG_DIR.rglob("*.jsonl"))
    rec.note("c5_session_store_files", files)
    rec.check("c5_sessions_in_config_dir", any(s1 in f for f in files), files)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
