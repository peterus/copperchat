"""Uploaded attachments → user message content for the agent (Phase 8)."""

from __future__ import annotations

import base64
from typing import Any

from copperchat.agent.protocol import AttachmentRef
from copperchat.db import Database
from copperchat.db.repos.uploads import get_upload
from copperchat.projects import Project
from copperchat.uploads import UPLOAD_ID_RE


async def attachment_exists(db: Database, project_id: str, attachment_id: str) -> bool:
    if not UPLOAD_ID_RE.fullmatch(attachment_id):
        return False
    return await get_upload(db, project_id, attachment_id) is not None


async def resolve_attachment(
    db: Database, project_id: str, ref: AttachmentRef
) -> AttachmentRef | None:
    """The stored attachment (canonical kind/name) for ``ref`` of this project, or None."""
    if not UPLOAD_ID_RE.fullmatch(ref.id):
        return None
    row = await get_upload(db, project_id, ref.id)
    if row is None or row.kind not in ("image", "pdf"):
        return None
    return AttachmentRef(id=row.id, kind=row.kind, name=row.name)  # type: ignore[arg-type]


def _pdf_note(name: str, rel_path: str) -> str:
    return (
        f'[Attached PDF "{name}" stored at {rel_path}. You cannot open files yourself; call '
        f'research_part with attachment_paths=["{rel_path}"] to have the part-researcher '
        "read it.]"
    )


async def build_user_content(
    db: Database, project: Project, text: str, attachments: list[AttachmentRef]
) -> str | list[dict[str, Any]]:
    """Plain ``text`` without attachments (unchanged Phase 5 behaviour), else content blocks:
    the text, one base64 image block per image, one text note per PDF."""
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    real_base = project.dir.resolve()
    for ref in attachments:
        row = await get_upload(db, project.id, ref.id) if UPLOAD_ID_RE.fullmatch(ref.id) else None
        path = None if row is None else project.dir / row.rel_path
        if (
            row is None
            or path is None
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(real_base)
        ):
            blocks.append(
                {"type": "text", "text": f"[Attachment {ref.name} is no longer available.]"}
            )
            continue
        if row.kind == "image":
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": row.mime, "data": data},
                }
            )
        else:
            blocks.append({"type": "text", "text": _pdf_note(row.name, row.rel_path)})
    return blocks
