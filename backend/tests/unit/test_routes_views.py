import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from kipy.errors import ApiError

from copperchat.config import Settings
from copperchat.projects import agent_dir, discover_projects
from copperchat.web.app import create_app
from copperchat.web.security import CONTENT_SECURITY_POLICY
from tests.support.fake_kicad import FakeClientFactory, FakeDocument, FakeLauncher, FakeServer
from tests.support.kicad_project import copy_fixture

GOOD = {"username": "admin", "password": "test-password"}


@contextmanager
def app_client(
    make_settings: Callable[..., Settings], server: FakeServer | None = None
) -> Iterator[tuple[TestClient, str]]:
    settings = make_settings()
    copy_fixture("ecc83", settings.projects_root)  # KiCad-generated files, served opaque
    project = discover_projects(settings.projects_root)[0]
    server = server or FakeServer()
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    with TestClient(app) as client:
        yield client, project.id


def test_open_writes_snapshots_and_lists_view_files(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        body = client.post(f"/api/projects/{pid}/open").json()
        assert body["viewFiles"] == {"sch": ["ecc83-pp.kicad_sch"], "pcb": ["ecc83-pp.kicad_pcb"]}
        # 6B large-board guard: byte size of every view file
        assert set(body["viewBytes"]) == {"ecc83-pp.kicad_sch", "ecc83-pp.kicad_pcb"}
        r = client.get(f"/api/projects/{pid}/view/pcb")  # X1: 200 right after open
        assert r.status_code == 200 and r.content.startswith(b"(kicad_pcb")
        assert body["viewBytes"]["ecc83-pp.kicad_pcb"] == len(r.content)
        etag = r.headers["etag"]
        assert etag.startswith('"0.') and r.headers["cache-control"] == "no-cache"
        again = client.get(f"/api/projects/{pid}/view/pcb", headers={"If-None-Match": etag})
        assert again.status_code == 304
        f = client.get(f"/api/projects/{pid}/view/files/ecc83-pp.kicad_sch")
        assert f.status_code == 200 and f.content.startswith(b"(kicad_sch")
        assert client.get(f"/api/projects/{pid}/view/xyz").status_code == 400


def test_view_files_are_traversal_safe(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        for bad in (
            "..%2Fecc83-pp.kicad_sch",
            "..%2F..%2Fecc83%2Fecc83-pp.kicad_sch",
            "%2Fetc%2Fpasswd.kicad_sch",
            "ecc83-pp.kicad_pro",
            "x.txt",
            "missing.kicad_sch",
        ):
            assert client.get(f"/api/projects/{pid}/view/files/{bad}").status_code == 404, bad


def test_view_files_follow_added_and_removed_sub_sheets(
    make_settings: Callable[..., Settings],
) -> None:
    """A sub-sheet a publish lists (also in a sub-directory) is served; one removed
    from the view directory (left the hierarchy) is a clean 404."""
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        project = discover_projects(client.app.state.settings.projects_root)[0]  # type: ignore[attr-defined]
        sheet = agent_dir(project) / "view" / "sub" / "new.kicad_sch"
        sheet.parent.mkdir(parents=True, exist_ok=True)
        sheet.write_bytes(b"(kicad_sch new)")
        r = client.get(f"/api/projects/{pid}/view/files/sub/new.kicad_sch")
        assert r.status_code == 200 and r.content == b"(kicad_sch new)"
        sheet.unlink()
        gone = client.get(f"/api/projects/{pid}/view/files/sub/new.kicad_sch")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"


def test_view_file_removed_after_resolve_is_a_clean_404(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-sheet a concurrent publish deletes between `resolve_view_file` and the
    stat for the ETag is a 404, not a server error."""
    from copperchat.web import routes_views

    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        project = discover_projects(client.app.state.settings.projects_root)[0]  # type: ignore[attr-defined]
        gone = agent_dir(project) / "view" / "gone.kicad_sch"
        monkeypatch.setattr(routes_views, "resolve_view_file", lambda _p, _n: gone)
        r = client.get(f"/api/projects/{pid}/view/files/gone.kicad_sch")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_view_needs_open_project(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.get(f"/api/projects/{pid}/view/pcb")
        assert r.status_code == 409 and r.json()["error"]["code"] == "project_not_open"
        assert client.get("/api/projects/000000000000/view/pcb").status_code == 404


def test_renders_traversal_rejected(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        for bad in ("..%2F..%2Fetc%2Fpasswd", ".png", "a%00.png", "A" * 32 + ".png"):
            assert client.get(f"/api/projects/{pid}/renders/{bad}").status_code in (400, 404)
        assert client.get(f"/api/projects/{pid}/renders/x.txt").status_code == 400
        name = "0123456789abcdef0123456789abcdef.png"
        assert client.get(f"/api/projects/{pid}/renders/{name}").status_code == 404
        project = discover_projects(client.app.state.settings.projects_root)[0]  # type: ignore[attr-defined]
        (agent_dir(project) / "renders").mkdir(parents=True)
        (agent_dir(project) / "renders" / name).write_bytes(b"\x89PNG")
        r = client.get(f"/api/projects/{pid}/renders/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_agent_view_validates_parameters(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        for query in ("doc=x", "mode=fancy", "theme=pink"):
            assert client.get(f"/api/projects/{pid}/agent-view?{query}").status_code == 400


def test_requires_login(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        for path in ("view/pcb", "view/files/ecc83-pp.kicad_pcb", "agent-view", "renders/x.png"):
            assert client.get(f"/api/projects/{pid}/{path}").status_code == 401


def test_open_survives_a_failing_snapshot(make_settings: Callable[..., Settings]) -> None:
    # A sub-sheet outside the project directory cannot be copied into the view dir.
    server = FakeServer(hierarchy=["ecc83-pp.kicad_sch", "../elsewhere.kicad_sch"])
    with app_client(make_settings, server) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{pid}/open")
        assert r.status_code == 200
        assert r.json()["viewFiles"] == {"sch": [], "pcb": []}
        assert r.json()["viewBytes"] == {}


def test_open_survives_a_kicad_save_error(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: FakeDocument, filename: str, **_: object) -> None:
        raise ApiError("KiCad returned error: cannot save")

    monkeypatch.setattr(FakeDocument, "save_as", refuse)
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{pid}/open")
        assert r.status_code == 200 and r.json()["viewFiles"] == {"sch": [], "pcb": []}


# --- renders route: PNG and the fp_render SVG (footprints spec §11, index call 3) -------

UUID = "0123456789abcdef0123456789abcdef"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


@contextmanager
def renders_client(
    make_settings: Callable[..., Settings],
) -> Iterator[tuple[TestClient, str, Path]]:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        project = discover_projects(client.app.state.settings.projects_root)[0]  # type: ignore[attr-defined]
        renders = agent_dir(project) / "renders"
        renders.mkdir(parents=True)
        yield client, pid, renders


def _sidecar(renders: Path, payload: object) -> None:
    (renders / f"{UUID}.svg.json").write_text(json.dumps(payload))


def test_png_unchanged(make_settings: Callable[..., Settings]) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.png").write_bytes(b"\x89PNG")
        r = client.get(f"/api/projects/{pid}/renders/{UUID}.png")
        assert r.status_code == 200 and r.content == b"\x89PNG"
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "private"
        # the app-wide policy (web/security.py), not the SVG one; no download name
        assert r.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert "content-disposition" not in r.headers
        assert set(r.headers) == {
            "content-type",
            "content-length",
            "cache-control",
            "last-modified",
            "etag",
            "accept-ranges",
            "content-security-policy",
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
        }


def test_svg_headers(make_settings: Callable[..., Settings]) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.svg").write_bytes(SVG)
        _sidecar(renders, {"download_name": "OpenDriveHub_L_Bourns_SRN3012BTA.svg"})
        r = client.get(f"/api/projects/{pid}/renders/{UUID}.svg")
        assert r.status_code == 200 and r.content == SVG
        assert r.headers["content-type"] == "image/svg+xml"
        assert r.headers["content-security-policy"] == SVG_CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-disposition"] == (
            'inline; filename="OpenDriveHub_L_Bourns_SRN3012BTA.svg"'
        )
        assert r.headers["cache-control"] == "private"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ('../a b".svg', ".._a_b_.svg"),
        ("Lib_ÄÖ name;x=1.svg", "Lib____name_x_1.svg"),
        ("a\r\nContent-Type: text/html.svg", "a__Content-Type__text_html.svg"),
        ("Lib_" + "x" * 300 + ".svg", "Lib_" + "x" * 112 + ".svg"),
        ("Lib_R+0805.svg", "Lib_R+0805.svg"),
    ],
)
def test_svg_name_sanitised(
    make_settings: Callable[..., Settings], name: str, expected: str
) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.svg").write_bytes(SVG)
        _sidecar(renders, {"download_name": name})
        r = client.get(f"/api/projects/{pid}/renders/{UUID}.svg")
        assert r.status_code == 200
        disposition = r.headers["content-disposition"]
        assert disposition == f'inline; filename="{expected}"'
        sent = disposition.removeprefix('inline; filename="').removesuffix('"')
        assert re.fullmatch(r"[A-Za-z0-9_.+-]+", sent) and len(sent) <= 120
        assert sent.endswith(".svg")


@pytest.mark.parametrize(
    "sidecar",
    [
        None,  # no sidecar
        {"download_name": "Lib_R.png"},  # not .svg
        {"download_name": ""},
        {"download_name": 5},
        {"name": "Lib_R.svg"},
        ["Lib_R.svg"],
        "not json",
    ],
)
def test_svg_without_sidecar(make_settings: Callable[..., Settings], sidecar: object) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.svg").write_bytes(SVG)
        if sidecar == "not json":
            (renders / f"{UUID}.svg.json").write_bytes(b"\xff{")
        elif sidecar is not None:
            _sidecar(renders, sidecar)
        r = client.get(f"/api/projects/{pid}/renders/{UUID}.svg")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f'inline; filename="{UUID}.svg"'
        assert r.headers["content-security-policy"] == SVG_CSP


@pytest.mark.parametrize("kind", ["dir", "symlink", "deep", "oversize"])
def test_svg_bad_sidecar_falls_back(make_settings: Callable[..., Settings], kind: str) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.svg").write_bytes(SVG)
        sidecar = renders / f"{UUID}.svg.json"
        if kind == "dir":
            sidecar.mkdir()
        elif kind == "symlink":
            target = renders.parent / "elsewhere.json"
            target.write_text(json.dumps({"download_name": "Leak_secret.svg"}))
            sidecar.symlink_to(target)
        elif kind == "deep":
            sidecar.write_text("[" * 4000)
        else:
            sidecar.write_text(json.dumps({"download_name": "Lib_R.svg", "pad": "x" * 5000}))
        r = client.get(f"/api/projects/{pid}/renders/{UUID}.svg")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f'inline; filename="{UUID}.svg"'


def test_json_not_served(make_settings: Callable[..., Settings]) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / f"{UUID}.svg").write_bytes(SVG)
        _sidecar(renders, {"download_name": "Lib_R.svg"})
        for name in (f"{UUID}.svg.json", f"{UUID}.json"):
            r = client.get(f"/api/projects/{pid}/renders/{name}")
            assert r.status_code in (400, 404), name
            assert b"download_name" not in r.content


def test_non_uuid_rejected(make_settings: Callable[..., Settings]) -> None:
    with renders_client(make_settings) as (client, pid, renders):
        (renders / "Lib_R.svg").write_bytes(SVG)
        for bad in (
            "Lib_R.svg",
            UUID[:31] + ".svg",
            UUID + "0.svg",
            UUID.upper() + ".svg",
            UUID + ".SVG",
            UUID + ".svgz",
            UUID + ".svg%00",
            "..%2F" + UUID + ".svg",
        ):
            r = client.get(f"/api/projects/{pid}/renders/{bad}")
            assert r.status_code in (400, 404), bad
        missing = client.get(f"/api/projects/{pid}/renders/{UUID}.svg")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
