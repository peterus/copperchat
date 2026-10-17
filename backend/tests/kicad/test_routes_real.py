import io
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.web.app import create_app
from tests.support.kicad_project import copy_fixture, real_settings

pytestmark = pytest.mark.kicad
GOOD = {"username": "admin", "password": "test-password"}


def test_open_and_agent_view_over_http(make_settings: Callable[..., Settings]) -> None:
    settings = real_settings(make_settings)
    copy_fixture("complex_hierarchy", settings.projects_root)
    pid = discover_projects(settings.projects_root)[0].id
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        body = client.post(f"/api/projects/{pid}/open").json()
        assert body["viewFiles"]["sch"] == ["complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"]
        sub = client.get(f"/api/projects/{pid}/view/files/ampli_ht.kicad_sch")
        assert sub.status_code == 200 and sub.content.startswith(b"(kicad_sch")
        r = client.get(f"/api/projects/{pid}/agent-view?doc=pcb&mode=agent&layers=F.Cu,B.Cu")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(r.content)).format == "PNG"
        # Viewer renders are served, never persisted.
        project = discover_projects(settings.projects_root)[0]
        assert not list((project.dir / ".kicad-agent").glob("renders/*.png"))
        bad = client.get(f"/api/projects/{pid}/agent-view?doc=pcb&layers=Nope.Cu")
        assert bad.status_code == 422 and bad.json()["error"]["details"]["what"]
