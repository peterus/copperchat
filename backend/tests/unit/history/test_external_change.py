from copperchat.history.store import compute_hashes
from tests.support.history import HIERARCHY, other_board, replace_with_kicad_file
from tests.unit.history.conftest import Env, Opened
from tests.unit.history.test_history_hooks import done, turn


async def test_external_change_reloads_session_and_posts_notice(opened: Opened) -> None:
    env, project, session = opened.env, opened.project, opened.session
    assert project.pcb_file is not None
    session.view_version = 3
    stored = await env.svc.hashes.get(project.id)
    assert stored
    replace_with_kicad_file(project.pcb_file, other_board())

    await opened.hooks.on_turn_start(turn("run-2"))

    rel = project.pcb_file.relative_to(project.dir).as_posix()
    text, meta = env.engine.notices[-1]
    assert meta == {"action": "external-change", "files": [rel]}
    assert "outside CopperChat" in text and "reloaded" in text
    assert env.engine.notes and rel in env.engine.notes[-1]
    # Notice into the turn's session; the note about the shared design is broadcast.
    assert env.engine.notice_conversations[-1] == turn().conversation_id
    assert env.engine.note_conversations[-1] is None
    new = env.sessions.current()
    assert new is not None and new is not session and session.closed  # stale session gone
    assert env.server.starts == 2
    assert (await env.svc.hashes.get(project.id))[rel] != stored[rel]
    frame = env.broadcaster.frames[-1]
    assert frame.type == "view-updated" and frame.version == 4 and new.view_version == 4
    assert sorted(frame.docs) == ["pcb", "sch"]


async def test_new_and_deleted_sheets_count_as_changes(env: Env) -> None:
    project = env.project(HIERARCHY)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    (project.dir / "ampli_ht.kicad_sch").unlink()
    await hooks.on_turn_start(turn())
    assert env.engine.notices[-1][1] == {
        "action": "external-change",
        "files": ["ampli_ht.kicad_sch"],
    }
    assert env.server.starts == 0  # not open: nothing to reload, KiCad reads it on open
    assert "reloaded" not in env.engine.notices[-1][0]
    assert await env.svc.hashes.get(project.id) == compute_hashes(project)


async def test_unchanged_files_do_not_reload(opened: Opened) -> None:
    await opened.hooks.on_turn_start(turn("run-2"))
    assert opened.env.engine.notices == [] and opened.env.sessions.current() is opened.session
    assert opened.env.server.starts == 1


async def test_own_autosave_is_not_reported_as_external(opened: Opened) -> None:
    await opened.hooks.on_turn_end(turn(), done("pcb"))
    await opened.hooks.on_turn_start(turn("run-2"))
    assert opened.env.engine.notices == []


async def test_failed_reload_still_notifies(opened: Opened) -> None:
    env, project = opened.env, opened.project
    assert project.pcb_file is not None
    replace_with_kicad_file(project.pcb_file, other_board())
    env.server.fail_start_with_exit_code = 3
    await opened.hooks.on_turn_start(turn("run-2"))
    text, meta = env.engine.notices[-1]
    assert meta is not None and meta["action"] == "external-change" and "failed" in text
    assert env.engine.errors == []  # the engine reports KiCad when a tool needs it (X18)


async def test_view_refresh_failure_does_not_abort_external_change(opened: Opened) -> None:
    env, project = opened.env, opened.project
    assert project.pcb_file is not None

    async def broken(session: object, doc: str) -> object:
        raise OSError("view dir not writable")

    env.svc.write_view = broken  # type: ignore[assignment]
    replace_with_kicad_file(project.pcb_file, other_board())
    await opened.hooks.on_turn_start(turn("run-2"))
    text, meta = env.engine.notices[-1]
    assert meta is not None and meta["action"] == "external-change" and "reloaded" in text
    assert await env.svc.hashes.get(project.id) == compute_hashes(project)
    await opened.hooks.on_turn_start(turn("run-3"))
    assert len(env.engine.notices) == 1 and env.server.starts == 2  # not re-detected


async def test_nested_projects_and_autosaves_are_not_documents(env: Env) -> None:
    from copperchat.history.store import document_files
    from tests.support.history import ECC83

    project = env.project(HIERARCHY)
    nested = project.dir / "sub"
    nested.mkdir()
    for f in ECC83.glob("ecc83-pp.kicad_*"):
        (nested / f.name).write_bytes(f.read_bytes())  # byte copy of a KiCad project
    (project.dir / "_autosave-ampli_ht.kicad_sch").write_bytes(
        (project.dir / "ampli_ht.kicad_sch").read_bytes()
    )
    names = {p.relative_to(project.dir).as_posix() for p in document_files(project)}
    assert names == {
        "complex_hierarchy.kicad_pro",
        "complex_hierarchy.kicad_sch",
        "ampli_ht.kicad_sch",
        "complex_hierarchy.kicad_pcb",
    }
