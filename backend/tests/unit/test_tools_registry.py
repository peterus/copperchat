import sys
import types
from collections.abc import Iterator

import pytest

from copperchat.tools import ToolContext, ToolResult, ToolSpec, registry
from copperchat.tools.registry import ToolRegistryError


async def _noop(ctx: ToolContext, args: dict[str, object]) -> ToolResult:
    return ToolResult(data={"ok": True})


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name, description="d", input_schema={"type": "object", "properties": {}}, handler=_noop
    )


@pytest.fixture
def fake_module() -> Iterator[types.ModuleType]:
    mod = types.ModuleType("copperchat_test_provider")
    mod.TOOL_SPECS = [_spec("alpha"), _spec("beta")]  # type: ignore[attr-defined]
    sys.modules[mod.__name__] = mod
    yield mod
    del sys.modules[mod.__name__]


def test_empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", ())
    assert registry.all_specs() == []


def test_collects_specs(monkeypatch: pytest.MonkeyPatch, fake_module: types.ModuleType) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    assert [s.name for s in registry.all_specs()] == ["alpha", "beta"]


def test_missing_provider_import_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", ("copperchat.does_not_exist",))
    with pytest.raises(ModuleNotFoundError):
        registry.all_specs()


def test_provider_without_tool_specs_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", ("copperchat.errors",))
    with pytest.raises(ToolRegistryError, match="TOOL_SPECS"):
        registry.all_specs()


def test_duplicate_names_raise(
    monkeypatch: pytest.MonkeyPatch, fake_module: types.ModuleType
) -> None:
    fake_module.TOOL_SPECS.append(_spec("alpha"))  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError, match="duplicate"):
        registry.all_specs()


@pytest.mark.parametrize("bad", ["CamelCase", "with-dash", "", "1abc"])
def test_rejects_bad_names_and_schemas(
    monkeypatch: pytest.MonkeyPatch, fake_module: types.ModuleType, bad: str
) -> None:
    fake_module.TOOL_SPECS = [_spec(bad)]  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError):
        registry.all_specs()


def test_rejects_non_object_schema(
    monkeypatch: pytest.MonkeyPatch, fake_module: types.ModuleType
) -> None:
    fake_module.TOOL_SPECS = [
        ToolSpec(
            name="x",
            description="d",  # type: ignore[attr-defined]
            input_schema={"type": "string"},
            handler=_noop,
        )
    ]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError, match="object"):
        registry.all_specs()


def test_registry_has_repo_tools() -> None:
    specs = {s.name: s for s in registry.all_specs()}
    for name in ("repo_list", "repo_grep", "repo_read"):
        assert name in specs
        assert specs[name].requires_kicad is False
        assert specs[name].mutating is False
