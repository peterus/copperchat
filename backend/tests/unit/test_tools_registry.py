import re
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


def _schema_names(schema: object, acc: set[str]) -> set[str]:
    if isinstance(schema, dict):
        for key, sub in schema.get("properties", {}).items():
            acc.add(key)
            _schema_names(sub, acc)
        _schema_names(schema.get("items"), acc)
        _schema_names(schema.get("additionalProperties"), acc)
        for combo in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(combo, []):
                _schema_names(sub, acc)
    return acc


# Names a description mentions as `name=` that are result fields, not parameters.
_RESULT_FIELDS = {("parts_get_notes", "researched"), ("doc_append", "stale_base")}
# A parameter-looking token: `name=...` or the "name (field X)" shorthand for a field name
# that is only reachable through a map parameter. Limitation: a bare list of parameter names
# ("value, footprint, datasheet") carries no marker and is not detected; descriptions should
# name parameters in the `name=` form.
_PARAM_TOKEN = re.compile(r"`?\b([a-z][a-z_]*)\b`?(?:=| \(field )")


def test_descriptions_name_only_schema_params() -> None:
    problems = []
    for spec in registry.all_specs():
        known = _schema_names(spec.input_schema, set())
        for token in set(_PARAM_TOKEN.findall(spec.description)):
            if token not in known and (spec.name, token) not in _RESULT_FIELDS:
                problems.append(f"{spec.name}: description names `{token}`, not in its schema")
    assert not problems, "\n".join(problems)
