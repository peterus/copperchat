import json
from pathlib import Path

import httpx
import pytest

from copperchat.errors import StructuredToolError
from copperchat.projects import Project
from copperchat.research import datasheets

from .conftest import public_resolver

FIXTURE = (Path(__file__).parents[2] / "fixtures/parts/CC1234/notes.md").read_text()


def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ok.pdf":
            return httpx.Response(200, content=b"%PDF-1.7\nx")
        if path == "/hop":
            return httpx.Response(302, headers={"location": "/ok.pdf"})
        if path == "/to-private":
            return httpx.Response(302, headers={"location": "http://192.168.1.10/ds.pdf"})
        if path == "/to-internal":
            return httpx.Response(301, headers={"location": "https://nas.internal/ds.pdf"})
        if path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if path == "/big.pdf":
            return httpx.Response(
                200, content=b"%PDF-" + b"0" * (datasheets.MAX_DATASHEET_BYTES + 1)
            )
        if path == "/404":
            return httpx.Response(404)
        return httpx.Response(200, content=b"<html>login</html>")

    return httpx.MockTransport(handler)


async def dl(project: Project, mpn: str, url: str) -> Path:
    async with httpx.AsyncClient(transport=transport()) as client:
        return await datasheets.download_datasheet(
            project, mpn, url, client=client, resolver=public_resolver
        )


async def test_download_saves_pdf(project: Project) -> None:
    path = await dl(project, "LM358/NOPB", "https://ti.com/ok.pdf")
    assert path == project.dir / "docs/parts/LM358_NOPB/datasheet.pdf"
    assert path.read_bytes().startswith(b"%PDF-")
    assert (await dl(project, "LM358", "https://ti.com/hop")).is_file()


@pytest.mark.parametrize(
    ("mpn", "url"),
    [
        ("LM358", "https://ti.com/page"),  # HTML, not a PDF
        ("LM358", "file:///etc/passwd"),
        ("LM358", "http://127.0.0.1:8080/api/health"),
        ("LM358", "https://ti.com/to-private"),  # redirect into the LAN
        ("LM358", "https://ti.com/to-internal"),
        ("LM358", "https://ti.com/loop"),
        ("LM358", "https://ti.com/big.pdf"),
        ("LM358", "https://ti.com/404"),
        ("../x", "https://ti.com/ok.pdf"),
        ("_uploads", "https://ti.com/ok.pdf"),
    ],
)
async def test_download_refusals(project: Project, mpn: str, url: str) -> None:
    with pytest.raises(StructuredToolError):
        await dl(project, mpn, url)
    assert not (project.dir / "docs/parts/LM358/datasheet.pdf").exists()


async def test_parts_server_tools(project: Project) -> None:
    server = datasheets.parts_mcp_server(project)
    assert server["name"] == "parts"
    d = project.dir / "docs/parts/CC1234"
    d.mkdir(parents=True)
    (d / "notes.md").write_text(FIXTURE)
    report = datasheets.notes_check(project, "CC1234")
    assert report["score"] == 1.0 and report["issues"] == []
    (d / "notes.md").write_text(FIXTURE.replace("## Layout guidelines", "## Layout"))
    assert datasheets.notes_check(project, "CC1234")["score"] < 1.0
    with pytest.raises(StructuredToolError):
        datasheets.notes_check(project, "MISSING")
    json.dumps(report)  # tool payload is JSON-serialisable
