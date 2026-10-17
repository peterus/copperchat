import base64
import io

from PIL import Image

from copperchat.agent.attachments import attachment_exists, build_user_content, resolve_attachment
from copperchat.agent.protocol import AttachmentRef
from copperchat.db import Database
from copperchat.db.repos.uploads import UploadRow, insert_upload
from copperchat.projects import Project
from copperchat.uploads import store_upload


async def _store(db: Database, project: Project, data: bytes, name: str) -> AttachmentRef:
    up = store_upload(project, data, name)
    await insert_upload(
        db, UploadRow(up.id, project.id, up.kind, up.name, up.rel_path, up.mime, up.size, "t")
    )
    return AttachmentRef(id=up.id, kind=up.kind, name=up.name)


async def test_plain_text_unchanged(project: Project, db: Database) -> None:
    assert await build_user_content(db, project, "hi", []) == "hi"


async def test_image_and_pdf_blocks(project: Project, db: Database) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    img = await _store(db, project, buf.getvalue(), "photo.png")
    pdf = await _store(db, project, b"%PDF-1.7\n", "ds.pdf")
    content = await build_user_content(db, project, "look", [img, pdf])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    block = content[1]
    assert block["type"] == "image" and block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(block["source"]["data"]).startswith(b"\xff\xd8\xff")
    note = content[2]
    assert note["type"] == "text" and "docs/parts/_uploads/" in note["text"]
    assert "research_part" in note["text"] and "attachment_paths" in note["text"]
    assert await attachment_exists(db, project.id, img.id)
    assert not await attachment_exists(db, "other", img.id)
    assert not await attachment_exists(db, project.id, "../x")


async def test_missing_or_foreign_attachment(project: Project, db: Database) -> None:
    pdf = await _store(db, project, b"%PDF-1.7\n", "ds.pdf")
    (project.dir / "docs/parts/_uploads").joinpath(f"{pdf.id[:8]}-ds.pdf").unlink()
    gone = AttachmentRef(id="f" * 32, kind="image", name="x.png")
    content = await build_user_content(db, project, "t", [pdf, gone])
    assert isinstance(content, list)
    assert all(b["type"] == "text" and "no longer available" in b["text"] for b in content[1:])


async def test_resolve_attachment_returns_stored_fields(project: Project, db: Database) -> None:
    pdf = await _store(db, project, b"%PDF-1.7\n", "ds.pdf")
    forged = AttachmentRef(id=pdf.id, kind="image", name="other.png")
    assert await resolve_attachment(db, project.id, forged) == pdf
    assert await resolve_attachment(db, "ffffffffffff", forged) is None


async def test_total_inlined_image_size_is_bounded(
    project: Project, db: Database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from copperchat.agent import attachments

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    refs = [await _store(db, project, buf.getvalue(), f"p{i}.png") for i in range(3)]
    size = (project.dir / ".kicad-agent/uploads").joinpath(f"{refs[0].id}.jpg").stat().st_size
    monkeypatch.setattr(attachments, "MAX_INLINE_IMAGE_BYTES", size * 2)
    content = await build_user_content(db, project, "t", refs)
    assert isinstance(content, list)
    assert [b["type"] for b in content] == ["text", "image", "image", "text"]
    assert "omitted" in content[3]["text"]
