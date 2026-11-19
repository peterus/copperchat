"""`fp_create.prepare` against a fake session (F4a Task 2, spec §7.6): schema and bounds,
name rules, the name-free check, source and model rules, the seed check, the check gate
and the planned change set. Nothing here writes."""

from __future__ import annotations

import contextlib
import hashlib
import math
from pathlib import Path
from typing import Any, cast

import pytest
from kipy.proto.board import board_types_pb2 as bt

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import SEED_LIB_ID, KicadSession
from copperchat.kicad_tools.footprints import build as build_mod
from copperchat.kicad_tools.footprints import create as create_mod
from copperchat.kicad_tools.footprints.create import apply_footprint_create, prepare_create
from copperchat.kicad_tools.footprints.engine import FootprintChangeSet, FpStage
from copperchat.kicad_tools.footprints.model import geometry_from_proto, geometry_hash
from copperchat.kicad_tools.footprints.specs import FP_CREATE_SCHEMA, TOOL_SPECS, fp_create_spec
from copperchat.library import service as service_mod
from copperchat.tools import ToolContext, ToolResult
from tests.unit.footprints.test_build import l201_args
from tests.unit.library.test_service import LOCAL_URI, REPO, LocalSession, _fixture, _policy

NAME = "L_Bourns_SRN3012BTA"


class FakeFpDocument:
    def __init__(self, instance: Any) -> None:
        self._instance = instance

    async def footprint(self) -> Any:
        return self._instance

    async def items(self) -> list[Any]:
        out = []
        for packed in self._instance.definition.items:
            msg = getattr(bt, packed.TypeName().rsplit(".", 1)[-1])()
            packed.Unpack(msg)
            out.append(msg)
        return out


class CreateSession(LocalSession):
    """LocalSession plus the local library listing (`names`) and an optional missing
    seed; footprint documents of `documents_ok` open with the seed instance."""

    def __init__(self, root: Path, *, board: bool = False, seed: bool = True) -> None:
        super().__init__(root, board=board)
        self.names: list[str] = []
        self.seed = seed
        self.documents_ok: set[str] = set()

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", fn)
        if func is service_mod._footprint_names:
            (nickname,) = fn.args
            assert nickname == REPO
            return list(self.names)
        if func is service_mod._fetch_footprint_proto and not self.seed:
            self.library_reads.append(fn.args)
            return None
        return await super().call(fn)

    @contextlib.asynccontextmanager
    async def footprint_document(self, lib_id: str) -> Any:
        self.documents.append(lib_id)
        assert lib_id in self.documents_ok, lib_id
        yield FakeFpDocument(_fixture("seed_srn4018.instance"))


def _make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> CreateSession:
    s = CreateSession(tmp_path / REPO, **kw)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(s.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    monkeypatch.setattr(create_mod, "repo_policy", policy)
    return s


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CreateSession:
    return _make(tmp_path, monkeypatch)


def _ctx(session: CreateSession) -> ToolContext:
    return ToolContext(
        project=cast(Any, session.project),
        session=cast(KicadSession, session),
        settings=cast(Any, session.settings),
    )


async def _refused(session: CreateSession, args: dict[str, Any]) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as err:
        await prepare_create(_ctx(session), args)
    return err.value


def _pretty(session: CreateSession) -> Path:
    return session.root / "footprints" / f"{REPO}.pretty"


# --- happy path ------------------------------------------------------------------------


async def test_prepare_l201_without_row(session: CreateSession) -> None:
    cs = await prepare_create(_ctx(session), l201_args())
    assert isinstance(cs, FootprintChangeSet)
    assert cs.tool == "fp_create" and cs.logical.changes == 5
    lib_id = f"{REPO}:{NAME}"
    assert cs.fp_stages == [
        FpStage("ensure_library"),
        FpStage("copy_seed", lib_id),
        FpStage("rebuild", lib_id),
    ]
    assert cs.library is not None and cs.library.nickname == REPO
    assert cs.target is not None and cs.target.lib_id == lib_id
    assert cs.pre_image is None
    assert cs.summary["lib_id"] == lib_id
    assert cs.summary["library_row_planned"] is True
    assert cs.summary["check"]["passed"] is True
    kinds = [n["kind"] for n in cs.summary["notes"]]
    assert kinds == ["footprint_libraries_loaded"]
    assert session.activations == 1
    assert not _pretty(session).exists()  # nothing written


async def test_prepare_with_row_plans_no_s1(session: CreateSession) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.names = ["Other"]
    cs = await prepare_create(_ctx(session), l201_args())
    assert [s.kind for s in cs.fp_stages] == ["copy_seed", "rebuild"]
    assert cs.summary["library_row_planned"] is False


# --- name rules --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "-x", "a/b", "..", "a b", "a" * 101, "a\n", 5])
async def test_name_rules(session: CreateSession, name: Any) -> None:
    err = await _refused(session, l201_args(name=name))
    assert err.code == "invalid_argument"
    assert session.activations == 0


@pytest.mark.parametrize("name", ["L_Bourns_SRN3012BTA", "R_0805+x.1", "a"])
async def test_name_rules_accept(session: CreateSession, name: str) -> None:
    cs = await prepare_create(_ctx(session), l201_args(name=name))
    assert cs.target is not None and cs.target.lib_id == f"{REPO}:{name}"


async def test_name_taken_with_row(session: CreateSession) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    err = await _refused(session, l201_args())
    assert err.code == "already_exists"
    assert "replace=true" in err.suggestion


async def test_name_taken_on_disk_without_row(session: CreateSession) -> None:
    _pretty(session).mkdir(parents=True)
    (_pretty(session) / f"{NAME}.kicad_mod").write_bytes(b"opaque")  # never parsed
    err = await _refused(session, l201_args())
    assert err.code == "already_exists"
    assert "sch_set_fields" in err.suggestion and "replace=true" not in err.suggestion
    assert (_pretty(session) / f"{NAME}.kicad_mod").read_bytes() == b"opaque"


async def test_cleanable_copy_seam(session: CreateSession) -> None:
    """The pending-cleanup classifier plugs in through `cleanable`: a copy it would
    clean counts as free."""
    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    seen: list[tuple[str, str]] = []

    async def cleanable(ctx: ToolContext, lib: Any, name: str) -> bool:
        seen.append((lib.nickname, name))
        return True

    cs = await prepare_create(_ctx(session), l201_args(), cleanable=cleanable)
    assert cs.target is not None and seen == [(REPO, NAME)]


async def test_replace_on_non_local(session: CreateSession) -> None:
    err = await _refused(session, l201_args(replace=True))  # no row
    assert err.code == "not_local" and "no library row" in err.why
    session.rows = {REPO: LOCAL_URI}
    session.names = ["Other"]
    err = await _refused(session, l201_args(replace=True))  # not in the local library
    assert err.code == "not_local" and "no footprint named" in err.why
    assert session.documents == []


async def test_replace_reads_preimage(session: CreateSession) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    lib_id = f"{REPO}:{NAME}"
    session.documents_ok.add(lib_id)
    _pretty(session).mkdir(parents=True)
    (_pretty(session) / f"{NAME}.kicad_mod").write_bytes(b"old bytes")
    cs = await prepare_create(_ctx(session), l201_args(replace=True))
    pre = cs.pre_image
    assert pre is not None
    instance = _fixture("seed_srn4018.instance")
    assert pre.definition == instance.SerializeToString()
    assert len(pre.items) == len(instance.definition.items)
    assert pre.file_sha256 == hashlib.sha256(b"old bytes").hexdigest()
    expected = geometry_from_proto(instance, instance.definition.items, lib_id, "local")
    assert pre.geometry_hash == geometry_hash(expected)
    assert pre.fields == expected.fields and pre.fields
    assert [s.kind for s in cs.fp_stages] == ["copy_seed", "rebuild"]
    assert session.documents == [lib_id]


async def test_replace_preimage_file_not_regular(
    session: CreateSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file swapped for a FIFO after the lstat check: a structured
    `library_conflict`, never a raw OSError."""
    import os

    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    session.documents_ok.add(f"{REPO}:{NAME}")
    _pretty(session).mkdir(parents=True)
    os.mkfifo(_pretty(session) / f"{NAME}.kicad_mod")
    monkeypatch.setattr(create_mod, "local_footprint_file_safe", lambda lib, name: None)
    err = await _refused(session, l201_args(replace=True))
    assert err.code == "library_conflict"
    assert "regular file" in err.why


async def test_replace_preimage_read_error_names_no_host_path(
    session: CreateSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KiCad's error text for the replace pre-image read carries the repo root scrubbed
    to `<repo>` - never the host path."""
    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    _pretty(session).mkdir(parents=True)
    (_pretty(session) / f"{NAME}.kicad_mod").write_bytes(b"old bytes")
    bad = _pretty(session) / f"{NAME}.kicad_mod"

    @contextlib.asynccontextmanager
    async def failing(lib_id: str) -> Any:
        raise RuntimeError(f"{bad}: cannot open")
        yield  # pragma: no cover

    monkeypatch.setattr(session, "footprint_document", failing)
    err = await _refused(session, l201_args(replace=True))
    assert err.code == "library_error"
    assert str(session.root) not in err.why
    assert f"<repo>/footprints/{REPO}.pretty/{NAME}.kicad_mod" in err.why


# --- source and model --------------------------------------------------------------------


async def test_source_rules_same_error(session: CreateSession) -> None:
    root = session.root
    for rel in (".env", ".git/config", ".kicad-agent/x", "docs/ds.pdf"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    cs = await prepare_create(
        _ctx(session), l201_args(source={"document": "docs/ds.pdf", "location": "p.2"})
    )
    assert cs.target is not None
    bad = [
        {"document": str(root / "docs/ds.pdf"), "location": "p.2"},
        {"document": "../x", "location": "p.2"},
        {"document": ".env", "location": "p.2"},
        {"document": ".git/config", "location": "p.2"},
        {"document": ".kicad-agent/x", "location": "p.2"},
        {"document": "docs/missing.pdf", "location": "p.2"},
        {"document": "docs", "location": "p.2"},
        {"document": "docs/ds.pdf", "location": ""},
        {"document": "docs/ds.pdf", "location": "   "},
    ]
    errors = [await _refused(session, l201_args(source=s)) for s in bad]
    assert {e.code for e in errors} == {"invalid_argument"}
    assert len({(e.what, e.why) for e in errors}) == 1, [(e.what, e.why) for e in errors]
    err = await _refused(session, l201_args(source={"document": "ftp://x", "location": "p"}))
    assert err.code == "invalid_argument"
    url = l201_args(source={"document": "https://x", "location": "p"})
    cs = await prepare_create(_ctx(session), url)
    assert cs.target is not None


@pytest.mark.parametrize(
    "path",
    [
        "/abs.step",
        "${HOME}/x.step",
        "${KIPRJMOD}/../x.step",
        "${KIPRJMOD}/a/../../x.step",
        "${KIPRJMOD}//x.step",
        "${KIPRJMOD}/x.obj",
        "${KICAD5_3DMODEL_DIR}/x.step",
        "${KICAD12_3DMODEL_DIR}/x.step",
        "${KIPRJMOD}/x\\y.step",
    ],
)
async def test_model_rules_refused(session: CreateSession, path: str) -> None:
    err = await _refused(session, l201_args(model={"path": path}))
    assert err.code == "invalid_argument"


async def test_model_rules(
    session: CreateSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    link = l201_args(model={"path": "${KICAD10_3DMODEL_DIR}/x.step"})
    # unset here (and so in the api-server's env): KiCad resolves it, no claim, no note
    monkeypatch.delenv("KICAD10_3DMODEL_DIR", raising=False)
    cs = await prepare_create(_ctx(session), link)
    assert [n["kind"] for n in cs.summary["notes"]] == ["footprint_libraries_loaded"]
    assert cs.target is not None and cs.target.models[0].path == "${KICAD10_3DMODEL_DIR}/x.step"
    # set, but the file is not there: model_missing
    models = tmp_path / "3dmodels"
    models.mkdir()
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(models))
    cs = await prepare_create(_ctx(session), link)
    assert [n["kind"] for n in cs.summary["notes"]] == ["model_missing"]
    (models / "x.step").write_text("x")
    cs = await prepare_create(_ctx(session), link)
    assert cs.summary["notes"] == []
    # a project-relative model
    rel = l201_args(model={"path": "${KIPRJMOD}/3d/x.STEP"})
    cs = await prepare_create(_ctx(session), rel)
    assert [n["kind"] for n in cs.summary["notes"]] == ["model_missing"]
    (session.project.dir / "3d").mkdir()
    (session.project.dir / "3d" / "x.STEP").write_text("x")
    cs = await prepare_create(_ctx(session), rel)
    assert cs.summary["notes"] == []


# --- check gate ----------------------------------------------------------------------------


async def test_check_error_refuses(session: CreateSession) -> None:
    """A pad without mask is an fp_check error: `check_failed` in prepare, before the
    turn snapshot (`on_mutation`) and before apply."""
    from copperchat.agent.sdk_tools import _two_phase_steps

    pads = l201_args()["pads"]
    pads[0] = {**pads[0], "layers": ["F.Cu", "F.Paste"]}
    snapshots: list[int] = []

    async def on_mutation() -> None:
        snapshots.append(1)

    async def apply(ctx: ToolContext, cs: Any) -> ToolResult:
        raise AssertionError("apply must not run")

    ctx = _ctx(session)
    ctx.on_mutation = on_mutation
    with pytest.raises(StructuredToolError) as err:
        await _two_phase_steps(fp_create_spec(apply), ctx, l201_args(pads=pads))
    assert err.value.code == "check_failed"
    rules = {f["rule"] for f in err.value.details["findings"]}
    assert "mask_missing" in rules
    assert snapshots == []


async def test_check_failed_details_capped(session: CreateSession) -> None:
    pads = [
        {"number": str(r * 32 + c + 1), "type": "smd", "shape": "rect",
         "at": [c * 0.55, r * 0.55], "size": [0.5, 0.5]}
        for r in range(16)
        for c in range(32)
    ]  # fmt: skip
    body = {"rect": [18.0, 9.0], "center": [8.525, 4.125]}
    err = await _refused(session, l201_args(pads=pads, body=body))
    assert err.code == "check_failed"
    details = err.details
    assert len(details["findings"]) <= 200
    assert details["truncated"] is True
    assert details["counts"]["error"] > 200


# --- bounds and limits ---------------------------------------------------------------------


def _pad(**kw: Any) -> list[dict[str, Any]]:
    pads = l201_args()["pads"]
    pads[0] = {**pads[0], **kw}
    return pads


@pytest.mark.parametrize(
    "args",
    [
        l201_args(pads=_pad(at=[501, 0])),
        l201_args(pads=_pad(at=[0, -501])),
        l201_args(pads=_pad(size=[0, 1])),
        l201_args(pads=_pad(size=[1, 201])),
        l201_args(pads=_pad(rotation=math.nan)),
        l201_args(pads=_pad(rotation=math.inf)),
        l201_args(pads=_pad(number="x" * 17)),
        l201_args(pads=_pad(drill={"size": [0.8, 0.8], "shape": "circle"})),  # smd + drill
        l201_args(pads=_pad(type="tht")),  # tht without drill
        l201_args(pads=_pad(layers=["F.Cu", "Edge.Cuts"])),
        l201_args(pads=_pad(shape="custom")),
        l201_args(body={"rect": [0, 3]}),
        l201_args(body={"rect": [3, 201]}),
        l201_args(body={"rect": [3, 3], "center": [501, 0]}),
        l201_args(courtyard={"rect": [3, 3], "margin": 0.25}),
        l201_args(silkscreen={"segments": [[[0, 0], [501, 0]]]}),
        l201_args(fields={"User review ": "x"}),
        l201_args(fields={"User\u00a0review": "x"}),
        l201_args(fields={"needs_user_review\u200b": "x"}),
        l201_args(fields={" ": "x"}),
        l201_args(fields={"A": "x", "A ": "y"}),
        l201_args(model={"path": "${KIPRJMOD}/x.step", "offset": [0, 0]}),
        l201_args(model={"path": "${KIPRJMOD}/x.step", "scale": [1, 1, math.nan]}),
        l201_args(symbol="x" * 257),
        l201_args(symbol="   "),
        l201_args(symbol=" L "),
        l201_args(replace="yes"),
        l201_args(mounting="unspecified"),
    ],
)
async def test_bounds(session: CreateSession, args: dict[str, Any]) -> None:
    err = await _refused(session, args)
    assert err.code == "invalid_argument"
    assert session.activations == 0  # refused before any KiCad access


def _long(n: int) -> str:
    return "a" * n


@pytest.mark.parametrize(
    "args",
    [
        l201_args(pads=[_pad()[0] | {"number": str(i)} for i in range(513)]),
        l201_args(silkscreen={"segments": [[[0, 0], [1, 0]]] * 257}),
        l201_args(fields={f"F{i}": "x" for i in range(17)}),
        l201_args(description=_long(1025)),
        l201_args(keywords=_long(1025)),
        l201_args(source={"document": "docs/" + _long(396), "location": "p"}),
        l201_args(source={"document": "https://x/" + _long(2039), "location": "p"}),
        l201_args(source={"document": "https://x", "location": _long(201)}),
        l201_args(source={"document": "https://x", "location": "p", "mpn": _long(65)}),
        l201_args(model={"path": "${KIPRJMOD}/" + _long(384) + ".step"}),
        l201_args(fields={_long(65): "x"}),
        l201_args(fields={"F": _long(1025)}),
    ],
)
async def test_limits(session: CreateSession, args: dict[str, Any]) -> None:
    err = await _refused(session, args)
    assert err.code == "invalid_argument"
    assert session.activations == 0


def test_limits_at_the_edge_pass_the_schema() -> None:
    build_mod.parse_create_args(
        l201_args(
            description=_long(1024),
            keywords=_long(1024),
            source={"document": "https://x/" + _long(2038), "location": _long(200),
                    "mpn": _long(64)},
            model={"path": "${KIPRJMOD}/" + _long(383) + ".step"},
            fields={f"F{i}": _long(1024) for i in range(15)} | {_long(64): "x"},
        )
    )  # fmt: skip


@pytest.mark.parametrize(
    "fields", [{"needs_user_review": "no"}, {"User Review": "x"}, {"footprint source": "x"},
               {"Reference": "R1"}, {"VALUE": "x"}]
)  # fmt: skip
async def test_reserved_field_names_refused(
    session: CreateSession, monkeypatch: pytest.MonkeyPatch, fields: dict[str, str]
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(create_mod, "build_target", lambda *a: built.append(a))
    err = await _refused(session, l201_args(fields=fields))
    assert err.code == "invalid_argument"
    assert built == [] and session.activations == 0


# --- seed ----------------------------------------------------------------------------------


async def test_seed_missing_refused_in_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _make(tmp_path, monkeypatch, board=True, seed=False)
    err = await _refused(session, l201_args())
    assert err.code == "seed_missing"
    assert SEED_LIB_ID in f"{err.what} {err.why}"
    assert session.activations == 1  # activated also on a board server
    assert tuple(session.library_reads[-1]) == tuple(SEED_LIB_ID.split(":"))
    assert not _pretty(session).exists()


async def test_tools_disabled(session: CreateSession) -> None:
    session.settings.footprint_tools = False
    err = await _refused(session, l201_args())
    assert err.code == "footprint_tools_disabled"


async def test_library_conflict(session: CreateSession) -> None:
    session.rows = {REPO: "${KIPRJMOD}/elsewhere.pretty"}
    err = await _refused(session, l201_args())
    assert err.code == "library_conflict"


# --- the tool spec -------------------------------------------------------------------------


def test_fp_create_schema_and_spec() -> None:
    schema = FP_CREATE_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "description", "mounting", "pads", "body", "source"}
    p = schema["properties"]
    assert p["name"]["maxLength"] == 100
    assert p["description"]["maxLength"] == 1024 and p["keywords"]["maxLength"] == 1024
    assert p["pads"]["maxItems"] == 512
    pad = p["pads"]["items"]
    assert pad["properties"]["number"]["maxLength"] == 16
    assert pad["properties"]["at"]["items"]["maximum"] == 500
    assert pad["properties"]["size"]["items"]["maximum"] == 200
    assert p["silkscreen"]["anyOf"][1]["properties"]["segments"]["maxItems"] == 256
    assert p["fields"]["maxProperties"] == 16
    assert p["fields"]["propertyNames"]["maxLength"] == 64
    assert p["fields"]["additionalProperties"]["maxLength"] == 1024
    src = p["source"]["properties"]
    assert src["document"]["maxLength"] == 2048
    assert (src["location"]["minLength"], src["location"]["maxLength"]) == (1, 200)
    assert src["mpn"]["maxLength"] == 64
    model = p["model"]["properties"]
    assert model["path"]["maxLength"] == 400
    for key in ("offset", "rotation", "scale"):
        assert (model[key]["minItems"], model[key]["maxItems"]) == (3, 3)
    assert p["symbol"]["maxLength"] == 256
    spec = fp_create_spec(cast(Any, None))
    assert spec.name == "fp_create" and spec.prepare is prepare_create and spec.mutating
    # registered with the write engine as its apply (F4a Task 3)
    registered = next(s for s in TOOL_SPECS if s.name == "fp_create")
    assert registered.prepare is prepare_create
    assert registered.apply is apply_footprint_create
