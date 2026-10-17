"""Chat attachments: images → .kicad-agent/uploads/, PDFs → docs/parts/_uploads/.

The type is decided by magic bytes, never by the client's filename or content type.
Images are decoded and re-encoded (EXIF orientation applied, metadata dropped, at most
MAX_IMAGE_EDGE px), so only pixels CopperChat produced itself reach the model.
"""

from __future__ import annotations

import io
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from copperchat.errors import CopperChatError
from copperchat.fsutil import atomic_write_bytes
from copperchat.projects import Project
from copperchat.research.paths import uploads_image_root, uploads_pdf_root

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = max(MAX_IMAGE_BYTES, MAX_PDF_BYTES)
# Long edge the model sees without server-side downscaling; well under the API's
# 5 MB per-image limit even as PNG in most cases (see _normalise_image).
MAX_IMAGE_EDGE = 1568
MAX_ENCODED_IMAGE_BYTES = 3_500_000  # base64 adds a third: stays below 5 MB
MAX_IMAGE_PIXELS = 50_000_000
Kind = Literal["image", "pdf"]
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


class UploadRejected(CopperChatError):
    code = "upload_rejected"

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = status


def sniff(head: bytes) -> tuple[Kind, str] | None:
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    return None


def safe_filename(name: str | None, default: str = "upload") -> str:
    """Display/storage name: last path component, ASCII, ``[A-Za-z0-9._+-]`` only."""
    base = re.split(r"[/\\]", name or "")[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = _UNSAFE.sub("_", base.replace("\x00", "_")).strip("._-")
    if not base:
        return default
    stem, dot, ext = base.rpartition(".")
    if dot and stem and len(ext) <= 8:
        return f"{stem[:96]}.{ext}"
    return base[:100]


@dataclass(frozen=True)
class StoredUpload:
    id: str
    kind: Kind
    name: str
    rel_path: str
    mime: str
    size: int
    abs_path: Path


def _checked_dir(project: Project, d: Path) -> Path:
    real_base = project.dir.resolve()
    # Check every existing ancestor below the project dir: a symlinked docs/ or
    # docs/parts must not redirect uploads outside the project.
    if not d.resolve(strict=False).is_relative_to(real_base):
        raise UploadRejected("upload directory resolves outside the project", 400)
    d.mkdir(parents=True, exist_ok=True)
    if d.is_symlink() or not d.resolve().is_relative_to(real_base):
        raise UploadRejected("upload directory resolves outside the project", 400)
    return d


def _normalise_image(data: bytes) -> tuple[bytes, str, str]:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_IMAGE_PIXELS:
                raise UploadRejected("image has too many pixels", 413)
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            im = ImageOps.exif_transpose(opened)
            im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            out = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P", "PA"):
                rgba = im.convert("RGBA")
                rgba.save(out, "PNG", optimize=True)
                if out.tell() <= MAX_ENCODED_IMAGE_BYTES:
                    return out.getvalue(), "image/png", "png"
                # Photographic image with alpha: flatten onto white and use JPEG.
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
                out = io.BytesIO()
            for quality in (90, 80, 70, 60):
                out = io.BytesIO()
                im.convert("RGB").save(out, "JPEG", quality=quality)
                if out.tell() <= MAX_ENCODED_IMAGE_BYTES:
                    break
            return out.getvalue(), "image/jpeg", "jpg"
    except UploadRejected:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise UploadRejected(f"image could not be decoded: {exc}", 415) from exc
    except Image.DecompressionBombError as exc:
        raise UploadRejected("image has too many pixels", 413) from exc


def store_upload(project: Project, data: bytes, filename: str | None) -> StoredUpload:
    detected = sniff(data[:16])
    if detected is None:
        raise UploadRejected("only PNG, JPEG, WebP, GIF images and PDF files are accepted", 415)
    kind, mime = detected
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise UploadRejected(f"{kind} exceeds {limit // (1024 * 1024)} MB", 413)
    upload_id = uuid.uuid4().hex
    if kind == "pdf":
        name = safe_filename(filename, "document.pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        d = _checked_dir(project, uploads_pdf_root(project))
        path = d / f"{upload_id[:8]}-{name}"
        payload = data
    else:
        payload, mime, ext = _normalise_image(data)
        name = safe_filename(filename, f"image.{ext}")  # display name only
        d = _checked_dir(project, uploads_image_root(project))
        path = d / f"{upload_id}.{ext}"
    atomic_write_bytes(path, payload)
    return StoredUpload(
        id=upload_id,
        kind=kind,
        name=name,
        rel_path=path.relative_to(project.dir).as_posix(),
        mime=mime,
        size=len(payload),
        abs_path=path,
    )
