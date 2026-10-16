from __future__ import annotations

import importlib
import re

from copperchat.errors import CopperChatError
from copperchat.tools.spec import ToolSpec

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Fixed provider list. Each phase that adds tools appends one module name here.
# A provider that fails to import is an error - never skipped.
_PROVIDERS: tuple[str, ...] = ()


class ToolRegistryError(CopperChatError):
    code = "tool_registry_error"


def all_specs() -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for module_name in _PROVIDERS:
        module = importlib.import_module(module_name)
        provided = getattr(module, "TOOL_SPECS", None)
        if not isinstance(provided, list):
            raise ToolRegistryError(f"{module_name} does not define TOOL_SPECS: list[ToolSpec]")
        specs.extend(provided)
    seen: set[str] = set()
    for spec in specs:
        if not _NAME_RE.fullmatch(spec.name):
            raise ToolRegistryError(f"tool name {spec.name!r} is not snake_case")
        if spec.name in seen:
            raise ToolRegistryError(f"duplicate tool name {spec.name!r}")
        if spec.input_schema.get("type") != "object":
            raise ToolRegistryError(
                f"tool {spec.name!r}: input_schema must be a JSON object schema"
            )
        seen.add(spec.name)
    return specs
