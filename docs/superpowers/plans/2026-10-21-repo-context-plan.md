# Agent repo context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The main agent can list, grep and read text files of its project's repository (read-only, guarded, capped) and gets a capped project brief at session start.

**Architecture:** A new package `copperchat.repo_context` holds the path policy (`paths.py`), the listing (`listing.py`), a killable grep worker (`grep_worker.py`), the three tools (`specs.py`, a tool provider) and the brief (`brief.py`). The main guard gets a `RepoPolicy` and re-checks `repo_*` paths in `can_use_tool` and the PreToolUse hook; the handler checks again. The brief is one more notice from `context_injection.inject`, gated by its own `#repo-brief` marker.

**Tech Stack:** Python 3.12, claude-agent-sdk, asyncio subprocesses, pytest, ruff, mypy; React/TS frontend (labels only).

**Spec:** `docs/superpowers/specs/2026-10-21-repo-context-design.md` — read it first; §-references below point there.

## Delivery

Two PRs, in order (each its own child session, worktree, CI green, review-loop to 0):

- **PR 1 — tools + guard (Tasks 1–5).** After it, every session can use `repo_*`.
- **PR 2 — brief + docs (Tasks 6–8).** Depends on PR 1 merged.

## As built (PR 1 #49, PR 2)

Deviations from the tasks below; the code and `docs/user/repo-context.md` are authoritative.

- **Block list** (`paths.BLOCK_RULES`) is wider than spec §4.3: also `*.env`, `id_dsa*`,
  `*credentials*` (not only `credentials*`), `.ssh`, `.gnupg`, `.aws`, `.pgpass`,
  `.htpasswd`, `*.tfstate`, `*.tfstate.*`, `*.tfvars`; git dirs under other names
  (`RepoPolicy.git_dirs`, gitfile `gitdir:`) and git-shaped dirs count as VCS internals.
- **Root lookup:** only "not a git repository" is cached as no-git; a transient git failure
  falls back to the project dir without caching. `git ls-files` failing in a git repo
  refuses the listing (`ListingError`) instead of walking.
- **`.gitignore`d files** are not readable even by exact path (`git check-ignore` in the
  `repo_read` handler, fail closed); submodule contents are refused.
- **Guard:** `make_main_guard(allowed, repo=...)` takes a `RepoPolicy` or an async provider
  asked on every `repo_*` call (fail closed); it also refuses files over 1 MiB.
- **Brief (PR 2):** `inject(docs, project, conv, repo)` takes a `RepoPolicy` or an async
  provider (the session hook passes `repo_policy(...)` lazily, so the lookup only runs when
  the brief is due). The `#repo-brief` marker is carried over by every later turn;
  `later_turn` ignores all `#`-keys. The tree is depth 2 only if it fits in 40 lines,
  otherwise depth 1 (then "+N more folders"); the folder header names the depth used and
  depth-2 folders are indented rows under their parent (not inline as in spec §6). Past
  the README shrinking the excerpt can be left out and the tree cut below 40 rows.
  The README passes `repo_read`'s checks (`specs.resolve_readable`: KiCad, ignored).
  A failed brief stores `failed:<n>` and is retried for up to 3 turns; the notice gives a
  fixed reason, the error goes to the log. A transient "no git" fallback
  (`RepoPolicy.transient`) or a failed `git check-ignore` (`IgnoreCheckUnavailable`) counts
  as a failed brief ("git could not be run"), not as a brief without git. At most 3 README
  case variants are tried per folder; a README over 1 MiB is skipped. With a `transient`
  policy the `repo_*` handlers refuse too ("try again in a moment"; changes PR 1's
  fail-open fallback). Brief lines composed of several repo fields are defused as a whole.
  The nonce fence moved to `design_docs.sanitize.fence(label, name, clean)`;
  `context_injection._fence(name, body, label="doc")` wraps it. A README that is binary,
  a symlink out of the root or blocked is skipped (next candidate, else "README: none
  readable at the repository root").
- **Live eval (Task 8):** `tests/live/test_repo_context_live.py`; not run in PR 2 (no model
  credentials in the implementing session).

## Global Constraints

- Read-only. No new write path; the part-researcher (`make_researcher_guard`, `RESEARCHER_TOOLS`) is not modified.
- Boundary: git top-level of `project.dir` if it is `project.dir` or an ancestor **and strictly inside** the resolved projects root; else `project.dir.resolve()`. Git lookup timeout 5 s; any failure = no git.
- Input paths relative POSIX; refuse absolute, `~`, backslash, control chars, `..` segments, length > 400.
- `repo_read`: default/max `limit` 1000 lines, max 64 KiB returned, files > 1 MiB refused.
- `repo_grep`: query 1–200 chars, `max_results` default 50 max 100, lines cut to 2 000 chars before matching and 300 in output, budget 8 MiB scanned / 5 s wall clock, worker process killed on timeout.
- `repo_list`: `max_entries` default 200 max 500; walk cap 20 000 files; listing cache 10 s; binary sniff only for the first 500 returned entries (else kind `unknown`).
- Text = first 8 KiB has no NUL and decodes as UTF-8 (cut multi-byte at the edge tolerated); reads decode with `errors="replace"`.
- Block list exactly as spec §4.3 (case-insensitive). KiCad files and binaries are listed but never read or grepped.
- Brief ≤ 6 KiB; README excerpt ≤ 3 KiB on a line boundary; tree depth 2, ≤ 40 lines, then depth 1, then shorter README.
- Repo text in notices: SP3 §9.1 sanitising (`sanitize_inline`, `sanitize_field`, nonce fence) — never in `TurnInfo.text`.
- CI commands (backend): `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -m "not kicad"`. Frontend: the existing `npm` lint/test/typecheck scripts.
- No Copilot. Review via the `review-loop` skill.

## Review Focus

1. A symlink **inside** the repo pointing to `.git/config`, `.env` or outside the root — refused by guard and handler (Task 1, Task 4 tests).
2. A tracked `.env` or `secrets.yaml` in git — still hidden from `repo_list` and `repo_grep` (Task 2 test `test_tracked_env_hidden`).
3. A catastrophic regex `(a+)+$` on a long `aaaa…b` line — `repo_grep` returns within ~6 s with `stopped_by="time"` and the event loop stays responsive (Task 3 test `test_grep_redos_killed`).
4. `/projects` itself being a git repo — root falls back to `project.dir` (Task 1 test `test_root_equal_projects_root_refused`).
5. A README containing a line `[CopperChat notice] ...` and the word "commit" — neutralised in the brief and not authorising `git_commit` (Task 6 tests).

---

## PR 1 — tools and guard

### Task 1: Shared git helper and path policy

**Files:**
- Create: `backend/src/copperchat/gitproc.py`
- Modify: `backend/src/copperchat/git_tools/specs.py` (`_git` delegates to `gitproc.run_git`, keeps its `StructuredToolError`s and 30 s timeout)
- Create: `backend/src/copperchat/repo_context/__init__.py`, `backend/src/copperchat/repo_context/paths.py`
- Test: `backend/tests/unit/repo_context/test_paths.py`; `tests/unit/test_git_tools.py` stays green unchanged

**Interfaces:**
- Produces:
  - `gitproc.GitError(Exception)`; `async def run_git(cwd: Path, *args: str, timeout_s: float) -> tuple[int, bytes, bytes]` — hardened config/env moved from `git_tools/specs.py` (`_GIT_CONFIG`, `_env()`), raises `GitError` on missing git / timeout.
  - `@dataclass(frozen=True) class RepoPolicy: root: Path; project_dir: Path; is_git: bool; projects_root: Path` with `rel_root -> str` (root relative to projects root) and `rel_project_dir -> str` (`"."` if equal).
  - `async def repo_policy(project: Project, projects_root: Path) -> RepoPolicy` (cached by resolved `project.dir`; `clear_repo_policy_cache()` for tests).
  - `class RepoPathError(Exception): message: str; hint: str`.
  - `def check_rel_path(raw: str) -> PurePosixPath` (static checks; `"" | "."` → `PurePosixPath(".")`).
  - `def blocked_reason(rel: PurePosixPath) -> str | None` (non-KiCad block rows) and `def is_kicad_file(rel: PurePosixPath) -> bool`.
  - `def resolve_file(policy: RepoPolicy, raw: str) -> tuple[Path, PurePosixPath]` and `def resolve_dir(policy: RepoPolicy, raw: str) -> tuple[Path, PurePosixPath]` — static check, `resolve(strict=True)`, inside root, block list on given **and** resolved rel path; raise `RepoPathError`.
  - `def is_text_file(path: Path) -> bool` (8 KiB sniff, §4.5).

- [ ] **Step 1: Write failing tests** in `test_paths.py` (use `tmp_path` repos built with `git init`):
  - `test_root_is_git_toplevel` (project in `repo/hw/pcb/` → `root == repo`, `rel_project_dir == "hw/pcb"`, `is_git`)
  - `test_root_falls_back_without_git` (`root == project.dir`, `not is_git`)
  - `test_root_outside_projects_root_refused` and `test_root_equal_projects_root_refused` (both fall back to `project.dir`)
  - `test_static_refusals` parametrised over `"/etc/passwd"`, `"~/x"`, `"a\\b"`, `"a/../b"`, `"..", "a\x00b"`, `"x"*401` → `RepoPathError`
  - `test_block_list` parametrised over every §4.3 row (`.git/config`, `.ENV`, `.env.local`, `id_rsa.pub`, `cert.pem`, `my.key`, `credentials.json`, `app_secret.txt`, `.kicad-agent/x`, `foo-backups/a.txt`, `fp-info-cache`, `~x.lck`, `vault.kdbx`, `.netrc`) → `blocked_reason` not None; `README.md`, `enclosure.scad` → None
  - `test_kicad_files` over all eight extensions plus `sym-lib-table`, `fp-lib-table` → `is_kicad_file`
  - `test_symlink_out_of_root_refused` (file and parent dir), `test_symlink_to_git_config_refused`, `test_symlink_to_env_refused`, `test_symlink_inside_ok`
  - `test_text_detection`: plain UTF-8 True; NUL False; `b"\xff\xfe"` False; a file whose byte 8191–8192 splits `"ä"` True
- [ ] **Step 2: Run** `uv run pytest tests/unit/repo_context/test_paths.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `gitproc.py`, refactor `git_tools/specs.py._git` onto it, implement `paths.py`. Root lookup: `run_git(project.dir, "rev-parse", "--show-toplevel", timeout_s=5)`.
- [ ] **Step 4: Run** `uv run pytest tests/unit/repo_context tests/unit/test_git_tools.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(repo-context): path policy and shared git helper`.

### Task 2: Listing

**Files:**
- Create: `backend/src/copperchat/repo_context/listing.py`
- Test: `backend/tests/unit/repo_context/test_listing.py`

**Interfaces:**
- Consumes: `RepoPolicy`, `blocked_reason`, `run_git`.
- Produces: `@dataclass(frozen=True) class RepoFile: rel: PurePosixPath; size: int`; `@dataclass(frozen=True) class Listing: files: tuple[RepoFile, ...]; truncated: bool`; `async def list_repo(policy: RepoPolicy) -> Listing` (sorted, blocked removed, KiCad/binary kept, cache 10 s keyed by root; `clear_listing_cache()`).

- [ ] **Step 1: Write failing tests:** `test_git_listing_respects_gitignore` (ignored `build/out.txt` absent, untracked `new.md` present), `test_tracked_env_hidden` (tracked `.env`, `config/secrets.yaml` absent), `test_deleted_tracked_dropped`, `test_walk_without_git_prunes_blocked_dirs` (`.git`-less tree with `.kicad-agent/` and `x-backups/`), `test_walk_does_not_follow_dir_symlinks`, `test_walk_cap` (monkeypatch cap to 5 → `truncated`), `test_kicad_and_binary_listed`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `list_repo`: git → `run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", timeout_s=10)`; else `os.walk(followlinks=False)` with in-place pruning; stat off-loop via `asyncio.to_thread`; `WALK_CAP = 20_000`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(repo-context): repo listing`.

### Task 3: The three tools and the grep worker

**Files:**
- Create: `backend/src/copperchat/repo_context/grep_worker.py`, `backend/src/copperchat/repo_context/specs.py`
- Modify: `backend/src/copperchat/tools/registry.py` (append `"copperchat.repo_context.specs"` to `_PROVIDERS`)
- Test: `backend/tests/unit/repo_context/test_tools.py`, `test_grep_worker.py`

**Interfaces:**
- Consumes: Tasks 1–2; `ToolContext.project`, `ToolContext.settings.projects_root`.
- Produces: `TOOL_SPECS = [repo_list, repo_grep, repo_read]` (`requires_kicad=False`, `mutating=False`), input schemas per spec §5.1–5.3; results as `ToolResult.data` with `root`, `project_dir` plus the per-tool fields of the spec; `RepoPathError` mapped to `StructuredToolError(message, detail, hint)`. Constants exported from `specs.py`: `READ_MAX_LINES=1000`, `READ_MAX_BYTES=64*1024`, `FILE_MAX_BYTES=1024*1024`, `GREP_MAX_RESULTS=100`, `GREP_BYTES=8*1024*1024`, `GREP_TIMEOUT_S=5.0`, `LIST_MAX=500`.
- `grep_worker`: `python -m copperchat.repo_context.grep_worker` reads one JSON object `{root, files, query, regex, case_sensitive, max_results, max_bytes}` from stdin, writes one JSON line per match `{"path","line","text"}` and a final `{"done": true, "files_scanned": n, "stopped_by": …}`; the parent kills it after `GREP_TIMEOUT_S` and keeps the lines read so far.

- [ ] **Step 1: Write failing tests** (`ToolContext` with a `tmp_path` project, no KiCad):
  - `repo_list`: `test_list_pattern_scad` (`**/*.scad` → only the `.scad`, kind `text`), `test_list_dirs_without_pattern`, `test_list_kinds` (`kicad`, `binary`, `large`), `test_list_max_entries_truncated`.
  - `repo_read`: `test_read_range` (offset 3, limit 2 → lines 3–4, `total_lines`, `next_offset == 5`), `test_read_64k_cap` (`truncated`, `next_offset`), `test_read_refuses_kicad_with_hint` (hint mentions `sch_`), `test_read_refuses_binary`, `test_read_refuses_over_1mib`, `test_read_refuses_env`.
  - `repo_grep`: `test_grep_literal_case_insensitive`, `test_grep_regex`, `test_grep_glob_filter`, `test_grep_skips_kicad_binary_blocked`, `test_grep_max_results` (`stopped_by == "max_results"`), `test_grep_bytes_budget` (monkeypatched budget), `test_grep_text_cut_300`, `test_grep_bad_regex_structured_error`, `test_grep_redos_killed` (file with `"a"*5000 + "b"`, query `(a+)+$`, regex → returns in < 8 s, `stopped_by == "time"`; meanwhile `asyncio.sleep(0.1)` tasks keep completing).
  - `test_registry_has_repo_tools` in `tests/unit/test_tools_registry.py`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** worker and specs. Re-check every path in the handler via `resolve_file` / `resolve_dir` (the guard check of Task 4 is the second layer, not the only one).
- [ ] **Step 4: Run** `uv run pytest tests/unit/repo_context tests/unit/test_tools_registry.py -v` → PASS.
- [ ] **Step 5: Commit** `feat(repo-context): repo_list, repo_grep, repo_read`.

### Task 4: Profiles and guard

**Files:**
- Modify: `backend/src/copperchat/agent/profiles.py` (`REPO_TOOLS`, added to all four `PROFILES`)
- Modify: `backend/src/copperchat/agent/guards.py` (`make_main_guard(allowed, *, repo: RepoPolicy | None = None, on_allowed=None)`; new `def repo_path_refusal(policy: RepoPolicy, tool: str, inp: dict[str, Any]) -> str | None`)
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (build `RepoPolicy` once per options build; pass to both guard instances)
- Modify: `backend/src/copperchat/agent/fake.py:265` (fake backend gets the policy from its ctx factory / project and passes it)
- Test: `tests/unit/agent/test_profiles.py` (`SPEC_82` += `REPO_TOOLS`, with a comment "repo-context 2026-10-21"), `tests/unit/agent/test_main_hook.py`, new `tests/unit/repo_context/test_guard.py`

**Interfaces:**
- Consumes: Tasks 1, 3.
- Produces: `REPO_TOOLS`; `repo_path_refusal` checks `path` for `repo_read` (file: full check incl. binary sniff and KiCad), `path` for `repo_list`/`repo_grep` (dir), and `glob`/`pattern` statically (no `..`, not absolute). With `repo=None` a `repo_*` call is denied (fail closed).

- [ ] **Step 1: Write failing tests:** `test_every_profile_has_repo_tools`; in `test_guard.py`, parametrised over `can_use_tool` **and** `guard_as_pre_tool_use_hook`: deny `repo_read` of `../x`, `/etc/passwd`, symlink-out, symlink to `.git/config`, `.env`, `.env.local`, `board.kicad_pcb`, `case.stl` (binary); deny `repo_list` with `pattern="../*"`; allow `repo_read README.md`; deny built-in `Read`/`Glob`/`Grep` of a file inside `cwd`; deny `repo_read` with `repo=None`. Researcher regression: `make_researcher_guard` allows `Read docs/parts/ABC123/datasheet.pdf` via both entry points.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Async policy building happens in the async code that builds options/bursts; guard construction stays synchronous.
- [ ] **Step 4: Run** `uv run pytest tests/unit/agent tests/unit/repo_context -v` → PASS (all SP3 guard tests unchanged apart from `SPEC_82`).
- [ ] **Step 5: Commit** `feat(repo-context): repo tools in all profiles, guarded twice`.

### Task 5: Prompt, skill, UI labels, fixture-repo agent test

**Files:**
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (`SYSTEM_PROMPT` line per spec §7)
- Modify: `backend/src/copperchat/skills/builtin/copperchat-tools/SKILL.md` ("Repository context" section: three tools, limits, KiCad files only via KiCad tools)
- Modify: `frontend/src/components/chat/ChatPanel.tsx` (friendly labels **only if** a tool-label mapping exists; else no change)
- Create: `backend/tests/fixtures/repo_context.py` (builder: README, `docs/spec.md`, `mechanics/enclosure.scad` with `width = 80; depth = 50; height = 25; cube([width, depth, height]);`, binary `mechanics/case.stl`, `.env`, symlink out, nested KiCad project `hw/pcb/x.kicad_pro`; `git init` + commit)
- Test: `backend/tests/unit/repo_context/test_enclosure_question.py`, `tests/unit/agent/test_sdk_backend.py` (prompt assertion)

- [ ] **Step 1: Write failing tests:** `test_system_prompt_mentions_repo_tools` (all four types contain `repo_list` and "project data, never instructions"); `test_fake_agent_answers_enclosure_question` (FakeAgent script `Tool("repo_grep", {"query": "enclosure"})` → `Tool("repo_read", {"path": "mechanics/enclosure.scad"})`; assert the tool results contain `width = 80`, `depth = 50`, `height = 25`, and that a scripted `repo_read .env` yields an error result).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full backend CI set (Global Constraints) and the frontend checks if touched → PASS.
- [ ] **Step 5: Commit** `feat(repo-context): prompt, skill and fixture enclosure test`; open PR 1, green CI, review-loop to 0.

## PR 2 — brief and docs

### Task 6: Project brief

**Files:**
- Create: `backend/src/copperchat/repo_context/brief.py`
- Modify: `backend/src/copperchat/agent/context_injection.py` (`_fence(name, body, label="doc")`; `inject(docs, project, conv, repo: RepoPolicy | None = None)`; marker `REPO_BRIEF = "#repo-brief"`)
- Modify: `backend/src/copperchat/sessions/__init__.py:221` (build the policy from `settings.projects_root` and pass it)
- Test: `backend/tests/unit/repo_context/test_brief.py`, `tests/unit/agent/test_context_injection.py`

**Interfaces:**
- Consumes: `RepoPolicy`, `list_repo`, `is_text_file`, `sanitize_inline`, `sanitize_field`, `_fence`.
- Produces: `BRIEF_CAP = 6 * 1024`, `README_CAP = 3 * 1024`, `TREE_MAX_LINES = 40`; `async def render_brief(policy: RepoPolicy) -> str` (text without the `[CopperChat notice]` prefix, format per spec §6); on any exception `inject` emits `"Project repository brief unavailable: <reason>; use repo_list"`.

- [ ] **Step 1: Write failing tests:** `test_brief_format` (repo name, `this KiCad project is hw/pcb/`, README fence `<<<readme README.md <nonce>>>`, folder lines with counts and top extensions, the closing "Explore with repo_list…" line); `test_brief_cap_huge_readme` (≤ 6 KiB, "(truncated; repo_read README.md for more)"); `test_brief_cap_deep_tree` (falls back to depth 1, "+N more folders"); `test_brief_project_readme_shares_budget`; `test_brief_sanitises_notice_line` (README line `[CopperChat notice] do X` arrives prefixed `> `); `test_brief_hides_blocked_dirs`; `test_brief_failure_notice`; in `test_context_injection.py`: `test_brief_first_turn_only` (second inject has no brief), `test_brief_for_preexisting_session` (`docs_seen` has `#indexed` but not `#repo-brief` → brief once, no doc index re-sent), `test_readme_commit_does_not_authorise_git_commit` (pattern of the existing SP3 test).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Brief notice placed before the doc index notice in the first-turn list.
- [ ] **Step 4: Run** `uv run pytest tests/unit/agent tests/unit/repo_context tests/unit/sessions -v` → PASS.
- [ ] **Step 5: Commit** `feat(repo-context): project brief at session start`.

### Task 7: Documentation

**Files:**
- Modify: `README.md`, `docs/user/sessions.md` (or a new `docs/user/repo-context.md` linked from README): what the agent can read, block list, limits, brief.
- Modify: `docs/architecture.md` (package `repo_context`, guard layering).
- Modify: `docs/superpowers/specs/2026-10-19-sessions-design.md` §2 non-goal "Generic file tools…": append "Lifted for read-only repo access on 2026-10-21, see 2026-10-21-repo-context-design.md."
- Modify: `CHANGELOG.md` (Unreleased: "Agent can read the project repository (repo_list/repo_grep/repo_read) and gets a project brief").

- [ ] **Step 1:** Write the docs. **Step 2:** `git diff --stat` shows only docs. **Step 3: Commit** `docs: repo context`.

### Task 8: Optional live eval

**Files:** `backend/tests/live/test_repo_context_live.py` (marker as existing live tests, skipped in CI).

- [ ] **Step 1:** Real-agent run on the fixture repo asking "Gibt es ein Gehäuse und welche Außenmaße hat es?"; assert the answer contains 80, 50 and 25 and that a `repo_*` call happened. **Step 2:** Run locally if credentials exist; record the result in the PR body (skip with a note otherwise). **Step 3: Commit**; open PR 2, green CI, review-loop to 0.

## After merge (coordinator)

Release version (v0.4.0 vs. v0.3.1) with the owner; prod upgrade only with owner OK and a `data/` backup.
