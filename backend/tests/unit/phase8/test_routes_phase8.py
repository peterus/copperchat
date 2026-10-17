import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}


class Env:
    def __init__(self, client: TestClient, pid: str, settings: Settings) -> None:
        self.client, self.pid, self.settings = client, pid, settings

    @property
    def project_dir(self) -> Path:
        return self.settings.projects_root / "board"


@pytest.fixture
def env(make_settings: Callable[..., Settings]) -> Iterator[Env]:
    settings = make_settings(dev_fake_agent=True)
    d = settings.projects_root / "board"
    d.mkdir(parents=True)
    (d / "board.kicad_pro").write_text("")  # empty marker (owner decision 5)
    pid = discover_projects(settings.projects_root)[0].id
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/auth/login", json=GOOD).status_code == 204
        yield Env(c, pid, settings)


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    return buf.getvalue()


def test_skills_listing_customise_reset_and_toggle(env: Env) -> None:
    c, pid = env.client, env.pid
    listed = c.get(f"/api/skills?projectId={pid}").json()
    by_name = {s["name"]: s for s in listed}
    tools = by_name["copperchat-tools"]
    assert tools["origin"] == "builtin" and tools["enabled"] is True and tools["agent"] is None
    assert by_name["part-notes"]["agent"] == "part-researcher"
    assert c.post("/api/skills/copperchat-tools/customise").status_code == 201
    assert c.post("/api/skills/copperchat-tools/customise").status_code == 409
    detail = c.get(f"/api/skills/copperchat-tools?projectId={pid}").json()
    assert detail["origin"] == "user" and detail["editable"] is True
    assert detail["originDiff"] is None and "copperchat-tools" in detail["content"]
    new = detail["content"].replace("# Using the CopperChat tools", "# Mine")
    assert c.put("/api/skills/user/copperchat-tools", json={"content": new}).status_code == 204
    assert "# Mine" in c.get("/api/skills/copperchat-tools").json()["content"]
    r = c.put(f"/api/projects/{pid}/skills/copperchat-tools", json={"enabled": False})
    assert r.status_code == 204
    listed = c.get(f"/api/skills?projectId={pid}").json()
    assert next(s for s in listed if s["name"] == "copperchat-tools")["enabled"] is False
    assert c.post("/api/skills/copperchat-tools/acknowledge-update").status_code == 204
    assert c.post("/api/skills/copperchat-tools/reset").status_code == 204
    assert c.get(f"/api/skills/copperchat-tools?projectId={pid}").json()["origin"] == "builtin"
    assert c.post("/api/skills/copperchat-tools/reset").status_code == 404
    assert c.get("/api/skills/nope").status_code == 404
    assert c.post("/api/skills/nope/customise").status_code == 404


def test_user_skill_validation(env: Env) -> None:
    c = env.client
    bad = {"content": "---\nname: other\ndescription: d\n---\n"}
    assert c.put("/api/skills/user/mine", json=bad).status_code == 400
    ok = {"content": "---\nname: mine\ndescription: my skill\n---\nBody\n"}
    assert c.put("/api/skills/user/mine", json=ok).status_code == 204
    detail = c.get("/api/skills/mine").json()
    assert detail["origin"] == "user" and detail["hasBuiltin"] is False


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "UPPER", "a.b", "-x", "%2E%2E", "a%00b"])
def test_skill_name_traversal_rejected(env: Env, bad: str) -> None:
    c = env.client
    r = c.put(f"/api/skills/user/{bad}", json={"content": "---\nname: x\ndescription: y\n---\n"})
    assert r.status_code in (400, 404, 405)
    assert c.post(f"/api/skills/{bad}/customise").status_code in (400, 404, 405)
    assert c.put(f"/api/projects/{env.pid}/skills/{bad}", json={"enabled": False}).status_code in (
        400,
        404,
        405,
    )
    assert list(env.settings.data_dir.glob("**/SKILL.md")) == []
    assert not (env.settings.data_dir.parent / "etc").exists()


def test_unknown_project(env: Env) -> None:
    c = env.client
    assert c.get("/api/skills?projectId=ffffffffffff").status_code == 404
    assert c.get("/api/skills?projectId=..").status_code == 400
    assert c.get("/api/projects/ffffffffffff/parts").status_code == 404
    up = c.post("/api/projects/ffffffffffff/uploads", files={"file": ("a.pdf", b"%PDF-1.7\n")})
    assert up.status_code == 404


def test_parts_notes_and_mpn_traversal(env: Env) -> None:
    c, pid = env.client, env.pid
    assert c.get(f"/api/projects/{pid}/parts").json() == []
    r = c.put(f"/api/projects/{pid}/parts/TPS62133/notes", json={"content": "# n\n"})
    assert r.status_code == 204
    got = c.get(f"/api/projects/{pid}/parts/TPS62133/notes").json()
    assert got["userEdited"] is True and got["mpn"] == "TPS62133"
    assert [p["mpn"] for p in c.get(f"/api/projects/{pid}/parts").json()] == ["TPS62133"]
    assert c.get(f"/api/projects/{pid}/parts/OTHER/notes").json()["content"] == ""
    for bad in ("..", "_uploads", "..%2F..%2Fx", ".git", "a%5C..%5Cb", "x%00"):
        code = c.put(f"/api/projects/{pid}/parts/{bad}/notes", json={"content": "x"}).status_code
        assert code in (400, 404, 405), bad
        assert c.get(f"/api/projects/{pid}/parts/{bad}/datasheet").status_code in (400, 404, 405)
    assert c.get(f"/api/projects/{pid}/parts/TPS62133/datasheet").status_code == 404
    sheet = env.project_dir / "docs/parts/TPS62133/datasheet.pdf"
    sheet.write_bytes(b"%PDF-1.7\n")
    r = c.get(f"/api/projects/{pid}/parts/TPS62133/datasheet")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    big = {"content": "x" * (512 * 1024 + 1)}
    assert c.put(f"/api/projects/{pid}/parts/TPS62133/notes", json=big).status_code == 422


def test_upload_image_and_pdf(env: Env) -> None:
    c, pid = env.client, env.pid
    r = c.post(
        f"/api/projects/{pid}/uploads", files={"file": ("../../x.png", png_bytes(), "text/plain")}
    )
    assert r.status_code == 201
    ref = r.json()
    assert ref["kind"] == "image" and ref["name"] == "x.png" and len(ref["id"]) == 32
    got = c.get(f"/api/projects/{pid}/uploads/{ref['id']}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert got.headers["x-content-type-options"] == "nosniff"
    r = c.post(
        f"/api/projects/{pid}/uploads", files={"file": ("ds.pdf", b"%PDF-1.7\n", "application/pdf")}
    )
    assert r.status_code == 201 and r.json()["kind"] == "pdf"
    pdf = c.get(f"/api/projects/{pid}/uploads/{r.json()['id']}")
    assert pdf.headers["content-disposition"].startswith("attachment")
    r = c.post(
        f"/api/projects/{pid}/uploads", files={"file": ("x.pdf", b"MZ\x90\x00", "application/pdf")}
    )
    assert r.status_code == 415 and "accepted" in r.json()["detail"]
    r = c.post(
        f"/api/projects/{pid}/uploads", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}
    )
    assert r.status_code == 415
    assert c.get(f"/api/projects/{pid}/uploads/{'0' * 32}").status_code == 404
    assert c.get(f"/api/projects/{pid}/uploads/..%2F..%2Fboard.kicad_pro").status_code == 404


def test_uploaded_attachment_is_accepted_by_send(env: Env) -> None:
    c, pid = env.client, env.pid
    ref = c.post(
        f"/api/projects/{pid}/uploads", files={"file": ("p.png", png_bytes(), "image/png")}
    ).json()
    with c.websocket_connect("/api/ws") as sock:
        sub = {"type": "subscribe", "projectId": pid, "sinceMessageId": 0}
        sock.send_text(json.dumps(sub))
        frame = {
            "type": "send",
            "projectId": pid,
            "clientMessageId": "c1",
            "text": "look",
            # A client-supplied name/kind is replaced by the stored one.
            "attachments": [{"id": ref["id"], "kind": "pdf", "name": "evil.pdf"}],
        }
        sock.send_text(json.dumps(frame))
        while (msg := json.loads(sock.receive_text()))["type"] != "message":
            pass
        assert msg["message"]["role"] == "user"
        assert msg["message"]["metadata"]["attachments"] == [
            {"id": ref["id"], "kind": "image", "name": "p.png"}
        ]
        bad = {
            **frame,
            "clientMessageId": "c2",
            "attachments": [{"id": "f" * 32, "kind": "image", "name": "a"}],
        }
        sock.send_text(json.dumps(bad))
        while (err := json.loads(sock.receive_text()))["type"] != "error":
            pass
        assert err["code"] == "unknown_attachment"


def test_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings(dev_fake_agent=True))) as c:
        assert c.get("/api/skills").status_code == 401
        assert c.get("/api/projects/0123456789ab/parts").status_code == 401
        assert c.post("/api/projects/0123456789ab/uploads").status_code == 401


def test_upload_body_limits(env: Env) -> None:
    c, pid = env.client, env.pid
    url = f"/api/projects/{pid}/uploads"
    huge = c.post(
        url,
        content=b"x",
        headers={
            "content-length": str(64 * 1024 * 1024),
            "content-type": "multipart/form-data; boundary=b",
        },
    )
    assert huge.status_code == 413
    two = c.post(
        url, files=[("file", ("a.pdf", b"%PDF-1.7\n")), ("file", ("b.pdf", b"%PDF-1.7\n"))]
    )
    assert two.status_code == 400
    wrong = c.post(url, files={"other": ("a.pdf", b"%PDF-1.7\n")})
    assert wrong.status_code == 400
