"""Per-project editing settings (E-C3): plan policy, change budget, annotation block."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from copperchat.db import Database
from copperchat.errors import CopperChatError

PLAN_POLICIES = frozenset({"always", "large", "never"})
ANNOTATION_BLOCKS = frozenset({100, 1000})
CHANGE_BUDGET_MIN, CHANGE_BUDGET_MAX = 1, 500


class InvalidEditingSettings(CopperChatError):
    code = "invalid_argument"
    http_status = 400


@dataclass(frozen=True)
class EditingSettings:
    plan_policy: str = "large"
    change_budget: int = 20
    annotation_block: int = 100
    show_agent_view: bool = True


_FIELDS = frozenset(f.name for f in fields(EditingSettings))


def _validate(values: dict[str, Any]) -> None:
    unknown = set(values) - _FIELDS
    if unknown:
        raise InvalidEditingSettings(f"unknown editing settings: {', '.join(sorted(unknown))}")
    policy = values.get("plan_policy", "large")
    if not isinstance(policy, str) or policy not in PLAN_POLICIES:
        raise InvalidEditingSettings("plan_policy must be one of always, large, never")
    if "change_budget" in values:
        budget = values["change_budget"]
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or not CHANGE_BUDGET_MIN <= budget <= CHANGE_BUDGET_MAX
        ):
            raise InvalidEditingSettings(
                f"change_budget must be an integer {CHANGE_BUDGET_MIN}-{CHANGE_BUDGET_MAX}"
            )
    if "annotation_block" in values:
        block = values["annotation_block"]
        if isinstance(block, bool) or not isinstance(block, int) or block not in ANNOTATION_BLOCKS:
            raise InvalidEditingSettings("annotation_block must be 100 or 1000")
    if "show_agent_view" in values and not isinstance(values["show_agent_view"], bool):
        raise InvalidEditingSettings("show_agent_view must be a boolean")


class EditingSettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> EditingSettings:
        row = await self._db.fetchone(
            "SELECT plan_policy, change_budget, annotation_block, show_agent_view "
            "FROM editing_settings WHERE project_id = ?",
            (project_id,),
        )
        if row is None:
            return EditingSettings()
        return EditingSettings(
            plan_policy=str(row["plan_policy"]),
            change_budget=int(row["change_budget"]),
            annotation_block=int(row["annotation_block"]),
            show_agent_view=bool(row["show_agent_view"]),
        )

    async def put(self, project_id: str, **values: Any) -> EditingSettings:
        """Partial update merged with the current settings; validates before writing."""
        _validate(values)
        async with self._db.transaction():  # read-modify-write: concurrent PUTs keep both
            merged = replace(await self.get(project_id), **values)
            row = asdict(merged)
            await self._db.execute(
                "INSERT INTO editing_settings"
                "(project_id, plan_policy, change_budget, annotation_block, show_agent_view) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "plan_policy = excluded.plan_policy, change_budget = excluded.change_budget, "
                "annotation_block = excluded.annotation_block, "
                "show_agent_view = excluded.show_agent_view",
                (
                    project_id,
                    row["plan_policy"],
                    row["change_budget"],
                    row["annotation_block"],
                    1 if row["show_agent_view"] else 0,
                ),
            )
        return merged
