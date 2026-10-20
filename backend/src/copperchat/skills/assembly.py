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
from copperchat.db.repos.conversations import CONVERSATION_TYPES
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
# The only front-matter keys passed to the CLI (see main_skill_text).
_ALLOWED_KEYS = ("name", "description")


def sdk_skill_name(name: str) -> str:
    return f"{PLUGIN_NAME}:{name}"


@dataclass(frozen=True)
class SkillBundle:
    plugin_dir: Path
    names: list[str]
    sdk_names: list[str]
    # Effective body of the agent-scoped part-notes skill (None when disabled/absent).
    researcher_skill: str | None = None


def main_skill_text(text: str) -> str:
    """SKILL.md as handed to the CLI: the front matter is rebuilt from an allowlist
    (``name``, ``description``; JSON-quoted, which YAML reads verbatim). Everything else —
    notably ``allowed-tools`` (allow rules bypass can_use_tool), ``model`` or hooks — is
    dropped, so a parser difference between ours and the CLI's YAML cannot smuggle a key
    through."""
    meta, body = parse_front_matter(text)
    header = "".join(f"{key}: {json.dumps(meta[key])}\n" for key in _ALLOWED_KEYS if key in meta)
    return f"---\n{header}---\n{body}"


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


async def assemble_skills(
    settings: Settings, project: Project, db: Database, conversation_type: str = "free"
) -> SkillBundle:
    if conversation_type not in CONVERSATION_TYPES:  # also a path component below
        raise ValueError(f"unknown session type: {conversation_type!r}")
    # One plugin dir per type: a skill list never leaks between session types (spec §8.4).
    plugin_dir = settings.data_dir / "runtime" / "skills" / project.id / conversation_type
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
    # Agent-scoped skills become part of the web-enabled researcher's *system prompt*, so
    # they come only from trusted layers (built-in, user), never from a project's
    # repository (.kicad-agent/skills/ of a cloned project could carry injected orders).
    trusted = {s.name: s for s in effective_skills(SkillLayers.for_project(settings, None))}
    for name, skill in trusted.items():
        if skill.agent == RESEARCHER_AGENT and name not in disabled:
            text = (skill.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            researcher_skill = parse_front_matter(text)[1].strip() or None
    for skill in effective_skills(SkillLayers.for_project(settings, project)):
        if skill.name in disabled:
            continue
        # A trusted (built-in/user) restriction holds even when a project-layer copy of
        # the skill drops the key.
        trusted_sessions = trusted[skill.name].sessions if skill.name in trusted else None
        if any(
            types is not None and conversation_type not in types
            for types in (skill.sessions, trusted_sessions)
        ):
            continue
        if skill.agent is not None or trusted.get(skill.name, skill).agent is not None:
            continue  # agent-scoped (or shadowing one): never offered to the main agent
        dest = plugin_dir / "skills" / skill.name
        _copy_regular_files(skill.dir, dest)
        md = dest / "SKILL.md"
        md.write_text(main_skill_text(md.read_text(encoding="utf-8")), encoding="utf-8")
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
