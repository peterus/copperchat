"""The five doc tools in a KiCad-less context (C3 Task 5, acceptance smoke test)."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.config import Settings
from copperchat.design_docs.paths import DocError
from copperchat.design_docs.service import DesignDocs, DocNotice
from copperchat.design_docs.specs import TOOL_SPECS
from copperchat.kicad_session import KicadSession
from copperchat.projects import Project
from copperchat.tools import ToolContext, registry

NAMES = ["doc_list", "doc_read", "doc_write", "doc_patch_section", "doc_append"]
SPECS = {s.name: s for s in TOOL_SPECS}


def make_ctx(project: Project, settings: Settings, docs: DesignDocs, **kw: Any) -> ToolContext:
    kw.setdefault("run_id", "r1")
    return ToolContext(
        project=project, session=cast(KicadSession, None), settings=settings, docs=docs, **kw
    )


async def call(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    result = await SPECS[name].handler(ctx, args)
    assert result.data is not None
    return result.data


def test_specs_are_kicadless_non_mutating_and_registered() -> None:
    assert [s.name for s in TOOL_SPECS] == NAMES
    for s in TOOL_SPECS:
        assert s.requires_kicad is False and s.mutating is False
        assert s.changes_documents is False and s.prepare is None
        assert s.input_schema["additionalProperties"] is False
    names = {s.name for s in registry.all_specs()}
    assert set(NAMES) <= names


async def test_smoke_write_then_read(
    project: Project, settings: Settings, docs: DesignDocs
) -> None:
    ctx = make_ctx(project, settings, docs)
    out = await call(
        ctx,
        "doc_write",
        path="requirements.md",
        body="## Function\n\nBlink.\n",
        base_version=None,
        summary="first draft",
    )
    assert set(out) == {"version", "revision_id", "added", "removed"}
    got = await call(ctx, "doc_read", path="docs/design/requirements.md")
    assert got["exists"] and got["version"] == out["version"]
    assert got["body"] == "## Function\n\nBlink.\n" and got["status"] == "draft"
    assert "template" not in got
    listed = await call(ctx, "doc_list")
    assert listed["root"] == "docs/design"
    assert listed["docs"][0]["path"] == "docs/design/requirements.md"
    assert listed["docs"][0]["last_author"] == "agent"


async def test_read_missing_fixed_doc_returns_template(
    project: Project, settings: Settings, docs: DesignDocs
) -> None:
    got = await call(make_ctx(project, settings, docs), "doc_read", path="architecture.md")
    assert got["exists"] is False and got["version"] is None
    assert "## Power tree" in got["template"]


async def test_patch_append_and_notices(
    project: Project, settings: Settings, docs: DesignDocs
) -> None:
    posted: list[DocNotice] = []

    async def post(n: DocNotice) -> None:
        posted.append(n)

    ctx = make_ctx(
        project,
        settings,
        docs,
        conversation_id="c" * 32,
        conversation_type="free",
        conversation_seq=3,
        conversation_title="Session 3",
        post_doc_notice=post,
    )
    v = (
        await call(
            ctx,
            "doc_write",
            path="parts.md",
            body="## Key parts\n\n## Open points\n",
            base_version=None,
            summary="s",
        )
    )["version"]
    out = await call(
        ctx,
        "doc_patch_section",
        path="parts.md",
        heading="Open points",
        body="- MCU?",
        base_version=v,
        summary="s",
        mode="append",
    )
    assert out["added"] == 1
    app = await call(
        ctx,
        "doc_append",
        path="decisions.md",
        text="Use STM32.",
        title="MCU",
        summary="s",
        base_version="vstale",
    )
    assert app["stale_base"] is True and set(app) == {"version", "revision_id", "stale_base"}
    assert [n.kind for n in posted] == ["changed", "changed", "changed"]
    assert posted[1].sections == ("Open points",)
    body = (await call(ctx, "doc_read", path="decisions.md"))["body"]
    assert "*Session: Session 3 (#3, free)*" in body
    revs = await docs.revisions(project, "docs/design/decisions.md")
    assert revs[0].conversation_label == "Session 3 — Free"


async def test_fail_closed_contexts(project: Project, settings: Settings, docs: DesignDocs) -> None:
    no_type = make_ctx(project, settings, docs, conversation_id="c1")
    with pytest.raises(DocError) as err:
        await call(no_type, "doc_write", path="a.md", body="x", base_version=None, summary="s")
    assert err.value.code == "doc_scope_refused"
    no_turn = make_ctx(project, settings, docs, run_id=None)
    with pytest.raises(DocError) as err:
        await call(no_turn, "doc_append", path="a.md", text="x", summary="s")
    assert err.value.code == "doc_scope_refused"
    no_docs = ToolContext(project=project, session=cast(KicadSession, None), settings=settings)
    with pytest.raises(Exception) as exc:
        await call(no_docs, "doc_list")
    assert "not available" in str(exc.value)


async def test_read_too_large_lists_headings(
    project: Project, settings: Settings, docs: DesignDocs
) -> None:
    body = "## A\n" + "x" * 40_000 + "\n## B\n" + "y" * 40_000 + "\n"
    await docs.write(project, "requirements.md", body, None, author="user")
    ctx = make_ctx(project, settings, docs)
    with pytest.raises(DocError) as err:
        await call(ctx, "doc_read", path="requirements.md")
    assert err.value.code == "doc_read_too_large" and "A, B" in err.value.suggestion
    got = await call(ctx, "doc_read", path="requirements.md", section="B")
    assert got["body"].startswith("## B\n")


async def test_tools_through_the_sdk_adapter_with_kicad_stopped(
    project: Project, settings: Settings, docs: DesignDocs
) -> None:
    from copperchat.kicad_session import KicadUnavailable

    def kicad_ctx() -> ToolContext:
        raise KicadUnavailable("stopped")

    tools = {
        t.name: t
        for t in build_sdk_tool_list(
            TOOL_SPECS, kicad_ctx, lambda: make_ctx(project, settings, docs)
        )
    }
    res = await tools["doc_write"].handler(
        {"path": "requirements.md", "body": "B\n", "base_version": None, "summary": "s"}
    )
    assert not res.get("is_error"), res
    res = await tools["doc_read"].handler({"path": "requirements.md"})
    assert json.loads(res["content"][0]["text"])["body"] == "B\n"
    for name, args in (
        ("doc_list", {}),
        (
            "doc_patch_section",
            {
                "path": "requirements.md",
                "heading": "X",
                "body": "y",
                "base_version": None,
                "summary": "s",
                "create": True,
            },
        ),
        ("doc_append", {"path": "requirements.md", "text": "t", "summary": "s"}),
    ):
        res = await tools[name].handler(args)
        if name == "doc_patch_section":
            # stale base: a structured conflict, not a crash
            assert res.get("is_error") and "doc_conflict" in res["content"][0]["text"]
        else:
            assert not res.get("is_error"), res
    bad = await tools["doc_read"].handler({"path": "../../etc/passwd"})
    assert bad.get("is_error") and "doc_path_refused" in bad["content"][0]["text"]
    assert str(project.dir) not in bad["content"][0]["text"]
