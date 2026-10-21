# CopperChat architecture

CopperChat is a self-hosted web app: one Docker image per instance, one user, a FastAPI
backend that drives KiCad 11 headless (`kicad-cli api-server`) and the Claude Agent SDK, and
a React PWA. The full design is in `docs/superpowers/specs/2026-10-15-foundation-design.md`
(schematic editing: `docs/superpowers/specs/2026-10-17-editing-design.md`); the phase plans
are in `docs/superpowers/plans/`.

## Images

- `ghcr.io/peterus/copperchat-kicad-base:<commit>` — KiCad 11 built from source at the
  commit in `docker/kicad-base/KICAD_COMMIT`, plus `kicad-python` (kipy) in `/opt/venv`,
  runtime user `copperchat` (uid 1000). Built by `.github/workflows/kicad-base.yml`.
  `docker/kicad-base/SUPPORTED_COMMITS` lists every base tag CI tests against.
  Headless and slim: stripped binaries, no docs/man pages/translations, no Mesa GL drivers
  (`docker/kicad-base/dpkg-slim.cfg`), and only the demos the tests use (`ecc83`,
  `complex_hierarchy`, the `jetson-agx-thor-baseboard` design rules). Every build is also
  tagged `<commit>-r<recipe>` (`scripts/kicad-base-image.sh`): a pull request that changes
  the recipe publishes its build under that tag and CI tests against it; main rebuilds and
  publishes `<commit>` itself (releases only take `<commit>`).
- `ghcr.io/peterus/copperchat:<version>` — `docker/app/Dockerfile`: base image + Python
  backend (`/app/backend/.venv`, synced with uv at build time; uv itself is not shipped) + built frontend (`/app/frontend/dist`). Built and
  pushed by `.github/workflows/release.yml` on tags `v<semver>` (`:latest` for
  non-prerelease versions). Runs `python -m copperchat` as `copperchat` (uid 1000).
- `docker-compose.yml` — the app plus an optional `cloudflared` service (profile `tunnel`),
  mounts `./projects` → `/projects` and `./data` → `/data`.

## Backend (`backend/src/copperchat`)

| Module | Role |
|---|---|
| `config` | `Settings` from env/.env; startup fails without model credentials (`claude setup-token` hint) unless `DEV_FAKE_AGENT` |
| `db` | SQLite (WAL) + numbered migrations `0001`–`0007` (`schema_migrations`) |
| `projects` | discovery of `*.kicad_pro`, ids, KiCad lock detection |
| `kicad_session` | one `kicad-cli api-server` per open project, restart on hang/crash |
| `tools`, `kicad_tools`, `views` | agent tools (structured JSON, full UUIDs, mm), ERC/DRC, agent views and KiCanvas snapshots; `kicad_tools/edit` holds the schematic editing tools (see "Schematic editing") |
| `library` | symbol/footprint library index and `lib_*` tools, part preferences, `parts_get_notes` |
| `editing`, `plans`, `changes` | per-turn editing state and change budget, the plan service and gate, change log, ERC baseline/delta, step checkpoints, `kicad-cli sch diff` |
| `agent` | run engine per project: gated streaming input, queue, interrupt, resume, rate-limit parking, message persistence, WebSocket protocol, tool guard |
| `history`, `git_tools` | per-turn snapshots/undo, autosave, external-change detection, crash recovery, git status/diff/commit |
| `skills` | built-in + user + project skills; the agent-scoped `part-notes` template |
| `repo_context` | read-only repository access for the main agent: `paths` (repository root, path checks, block list, text sniff), `listing` (cached file list), `specs` (`repo_list` / `repo_grep` / `repo_read`), `grep_worker` (killable scan process), `brief` (project brief at session start); see "Repository context" |
| `research` | `research_part` MCP tool: a separate, guarded SDK query (web + writes only under `docs/parts/<MPN>/`) whose messages are streamed into the chat as a nested "Part research" card |
| `push` | VAPID keys (`/data/vapid.json`), subscriptions, visibility registry, notifier on `run-finished` / `rate-limited` frames |
| `web` | FastAPI app, login cookie, REST routes, `/api/ws`, security headers (CSP), static PWA with SPA fallback |

Design data is read and written only through the running API server (or `kicad-cli` for
ERC/DRC, netlist/diff checks and exports) — never by a CopperChat file parser or writer. The
main agent has no file, shell or web tools: its only SDK built-in is `Skill`; everything else
is CopperChat's own MCP tools (including the read-only `repo_*` tools).

## Repository context

The main agent reads the project's repository (README, specs, OpenSCAD enclosures,
firmware) through three read-only MCP tools, never through the CLI's built-in file tools.
Design: `docs/superpowers/specs/2026-10-21-repo-context-design.md`; user view:
`docs/user/repo-context.md`.

- **Boundary** (`paths.repo_policy`): the git top level if it is the project directory or
  an ancestor and lies strictly inside the projects root, else the project directory.
  Cached per project; a transient git failure falls back to the project directory without
  being cached and marks the policy `transient`: the tools then refuse (fail closed) and
  the brief counts as failed.
- **Path policy** (`paths`): relative POSIX paths only (no `..`, `~`, absolute, backslash,
  control characters, > 400 chars); `BLOCK_RULES` matched case-insensitively on every
  segment of the path as given *and* as resolved (so symlinks cannot leave the root or
  reach a blocked target); the repository's own git dirs under other names and git-shaped
  dirs count as `.git`. KiCad files and binaries (NUL or invalid UTF-8 in the first 8 KiB)
  are listed but not readable.
- **Listing** (`listing`): `git ls-files --cached --others --exclude-standard` in git
  repos (a failure refuses the listing instead of walking ignored files), else a walk that
  does not follow directory symlinks; capped at 20 000 files, cached 10 s.
- **Tools** (`specs`, a provider in `tools/registry.py`): `repo_read` (1000 lines / 64 KiB
  per call, files ≤ 1 MiB, `git check-ignore` refuses ignored and submodule paths),
  `repo_list` (200 / 500 entries), `repo_grep` (Python `re` in `grep_worker`, a separate
  process killed after 5 s; 8 MiB scanned, 100 results).
- **Guard layering:** the three tools are in every profile (`agent/profiles.REPO_TOOLS`),
  so the profile's tool list is the first layer. `make_main_guard(allowed, repo=...)`, used
  both as `can_use_tool` and as the PreToolUse hook (`guard_as_pre_tool_use_hook`),
  re-checks every `repo_*` path via `repo_path_refusal` against the cached policy (no
  policy → refused). The handler checks again (`resolve_file` / `resolve_dir`, plus the
  `.gitignore` check) and the grep worker re-checks each file it opens. Built-in
  `Read` / `Glob` / `Grep` stay denied for the main agent; the part-researcher's guard is
  unchanged.
- **Brief** (`brief`): a `before_model_input` notice from `context_injection.inject`, sent
  once per conversation (marker `#repo-brief` in `docs_seen`, next to `#indexed`): repo
  name, project location, a sanitised, nonce-fenced README excerpt (≤ 3 KiB) and a folder
  tree (depth 2 if it fits in 40 lines, else depth 1), ≤ 6 KiB in total. The README goes
  through `repo_read`'s acceptance checks (`specs.resolve_readable`). Like the doc index it
  never reaches the turn text, so it authorises nothing. A failure becomes a one-line
  "brief unavailable" notice with a fixed reason (marker `failed:<n>`, retried for up to
  three turns, 15 s budget per attempt); the full error goes to the log.

## Schematic editing

- **Two-phase tools.** A mutating tool is a `ToolSpec` with `prepare` and `apply`
  (`tools.edit_spec`). The adapter runs: read-only check → plan/budget gate → `prepare`
  (reads the scene, computes a `ChangeSet` of stages: create / update / delete / place,
  plus the intended net merges) → budget check → turn snapshot → `apply`. Every refusal
  happens before the snapshot and before any write.
- **Apply, verify, compensate** (`kicad_tools/edit/engine.py`): each stage is one KiCad
  commit (`KicadSession.commit`, dropped on any error or cancellation); after the last push
  the items are read back and the live `GetSchematicNetlist` is compared with the intent
  (merged pins, pins allowed to change, expected names, joined items). A mismatch undoes the
  stages in reverse; when that is impossible (e.g. a deleted symbol of a project with a
  board) the turn ends `document_inconsistent` and the last checkpoint is restored.
- **Geometry** (`geometry.py`, `scene.py`, `router.py`, `space.py`): 1.27 mm grid, canonical
  transforms, body and field boxes, an indexed router for orthogonal routes with T-joins
  onto the target net, free-space search that keeps pin corridors free.
- **Budget and plans** (`editing`, `plans`): logical changes per turn (default 20, ≤ 3
  deletions of pre-existing parts); policy `always` / `large` / `never` per project. A plan
  open blocks edits outside a begun step; steps run in order, each ends with an ERC delta
  against the previous checkpoint (new errors fixed or justified) and a step checkpoint
  (`history_snapshots.kind = step`). Turn end, Stop, rate limits, restarts and external
  changes pause a running plan.
- **Visibility** (`changes`): every applied call records change rows (incl. KiCad's cleanup
  as side effects) → `changes-updated` frame, Changes panel, `sch_changes`; the diff layer
  is `kicad-cli sch diff` against the run's turn snapshot; the KiCanvas fork draws the
  highlight overlay. The `kicad-schematic-editing` skill tells the agent the tool order,
  plan etiquette, ERC policy and self-check.
- **Tests**: real-KiCad tool tests in `backend/tests/kicad/edit/`, golden scenarios
  (`test_scenarios.py`, role-based checker `golden.py`) and undo (`test_undo.py`); the opt-in
  live eval `tests/live/test_editing_eval.py` runs the real model on the same circuits.

## Frontend (`frontend/src`)

React 19 + Vite + zustand + zod + Tailwind 4. `protocol/frames.ts` mirrors the backend
protocol; `protocol-fixtures/*.json` are validated by both test suites. The chat store merges
the REST baseline and the WebSocket replay by server id. The viewer embeds a vendored
KiCanvas built from the fork `peterus/kicanvas` (`frontend/vendor-patches/kicanvas/README.md`). `public/sw.js` handles push only and
never caches; `push/visibility.ts` reports page visibility so pushes go out only while no
window is visible.

## Data flow of a turn

Composer → `send` frame → run engine queue → SDK streaming input → tool calls through the
in-process MCP server (the first edit of a turn takes the turn snapshot) → messages persisted
and broadcast → turn end: ERC delta of an editing turn, plan pause, autosave, snapshot,
`view-updated` → viewer reload; a push notification if no client is visible.

## CI

`.github/workflows/ci.yml`:

- `backend` — ruff, mypy, pytest without KiCad, `scripts/check-docs.sh` (README compliance
  statements, compose pin = `KICAD_COMMIT`).
- `frontend` — typecheck, lint, vitest, build, KiCanvas bundle check, Playwright on the real
  KiCanvas bundle (API mocked).
- `kicanvas-reproducible` — rebuilds the vendored bundle and diffs it.
- `kicad (<commit>)` — matrix over `SUPPORTED_COMMITS`, real-KiCad tests inside the base
  image (incl. the golden editing scenarios and undo tests).
- `e2e` — builds the app image, checks the token-missing refusal, starts it with
  `DEV_FAKE_AGENT=true` and the `ecc83` fixture, and runs the Playwright happy path
  (`frontend/e2e/smoke-docker.spec.ts`: login → project → message → reply → viewer, service
  worker, no CSP violations).

`release.yml` (tags `v*` only) builds, re-checks the token refusal and pushes the image.
