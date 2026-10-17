"""Assemble the effective skill set of a project into a per-session SDK plugin.

CopperChat generates one local plugin per project at
``<data_dir>/runtime/skills/<projectId>/`` named ``copperchat``
(``.claude-plugin/plugin.json`` + ``skills/<name>/SKILL.md``), passes it via
``plugins``, lists ``skills=["copperchat:<name>", ...]`` and sets
``setting_sources=[]`` so neither ``~/.claude`` nor a ``.claude/`` folder in the user's
repository is loaded. Agent-scoped skills (front matter ``agent``) never reach the main
agent: the part-researcher's ``part-notes`` body is returned as
``SkillBundle.researcher_skill`` and appended to the researcher's prompt.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.skill_prefs import disabled_skills
from copperchat.projects import Project
from copperchat.skills.store import (
    MAX_SKILL_BYTES,
    SkillLayers,
    effective_skills,
    parse_front_matter,
)

PLUGIN_NAME = "copperchat"
RESEARCHER_AGENT = "part-researcher"
# Front-matter keys that grant tools while a skill is active; allow rules bypass
# can_use_tool, so they are never passed on.
_TOOL_GRANT_KEYS = frozenset({"allowed-tools", "allowed_tools"})


def sdk_skill_name(name: str) -> str:
    return f"{PLUGIN_NAME}:{name}"


@dataclass(frozen=True)
class SkillBundle:
    plugin_dir: Path
    names: list[str]
    sdk_names: list[str]
    # Effective body of the agent-scoped part-notes skill (None when disabled/absent).
    researcher_skill: str | None = None


def strip_tool_grants(text: str) -> str:
    """Re-emit ``text`` without ``allowed-tools``/``allowed_tools`` front-matter keys
    (including indented continuation lines of a YAML list under them)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    out = [lines[0]]
    skipping = False
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(out + lines[i:])
        if line[:1].isspace() or line.lstrip().startswith("- "):
            if not skipping:
                out.append(line)
            continue
        key = line.partition(":")[0].strip()
        skipping = key in _TOOL_GRANT_KEYS
        if not skipping:
            out.append(line)
    return text  # unterminated front matter: not a valid skill anyway


def _copy_regular_files(src: Path, dest: Path) -> None:
    """Copy regular, non-hidden files only; symlinks and dotfiles never leave their layer."""
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if any((src / Path(*rel.parts[: i + 1])).is_symlink() for i in range(len(rel.parts))):
            continue
        if path.is_file() and path.stat().st_size <= MAX_SKILL_BYTES:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


async def assemble_skills(settings: Settings, project: Project, db: Database) -> SkillBundle:
    plugin_dir = settings.data_dir / "runtime" / "skills" / project.id
    if plugin_dir.is_symlink():
        plugin_dir.unlink()
    elif plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {"name": PLUGIN_NAME, "version": "1.0.0", "description": "CopperChat effective skills"}
        )
    )
    disabled = await disabled_skills(db, project.id)
    names: list[str] = []
    researcher_skill: str | None = None
    for skill in effective_skills(SkillLayers.for_project(settings, project)):
        if skill.name in disabled:
            continue
        if skill.agent is not None:
            # Agent-scoped: never offered to the main agent.
            if skill.agent == RESEARCHER_AGENT:
                text = (skill.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
                researcher_skill = parse_front_matter(text)[1].strip() or None
            continue
        dest = plugin_dir / "skills" / skill.name
        _copy_regular_files(skill.dir, dest)
        md = dest / "SKILL.md"
        md.write_text(strip_tool_grants(md.read_text(encoding="utf-8")), encoding="utf-8")
        names.append(skill.name)
    return SkillBundle(
        plugin_dir=plugin_dir,
        names=names,
        sdk_names=[sdk_skill_name(n) for n in names],
        researcher_skill=researcher_skill,
    )


def apply_skills(options: ClaudeAgentOptions, bundle: SkillBundle) -> ClaudeAgentOptions:
    """Main agent: built-in tools are exactly ``["Skill"]`` (or ``[]`` without skills)."""
    base = [t for t in (options.tools or []) if t != "Skill"]
    return replace(
        options,
        tools=[*base, "Skill"] if bundle.sdk_names else base,
        plugins=[{"type": "local", "path": str(bundle.plugin_dir)}],
        skills=list(bundle.sdk_names),
        setting_sources=[],
    )
