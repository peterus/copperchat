# Phase 1C: Spike — headless `kicad-cli api-server` + kipy

**Goal:** Prove with real runs that KiCad 11 (master) `kicad-cli api-server`
plus `kicad-python` (kipy) supports everything CopperChat's foundation needs:
GetItems on schematic and board, SaveDocument, SaveCopyOfDocument (incl.
sub-sheets), PNG/SVG export, ERC/DRC JSON via `kicad-cli`, crash/hang
detection and restart. Pin the KiCad commit and the kipy git revision. Record
results and a go/no-go decision in `docs/spikes/1c-api-server.md`.

**Depends on:** —

**Parallel with:** 1A, 1B

**Read first:**
- `docs/superpowers/plans/2026-10-15-foundation-plan.md` — Global Constraints,
  "Go/no-go gates" (1C), C4 (KiCad session), Phase Conventions.
- `docs/superpowers/specs/2026-10-15-foundation-design.md` — §2 (stack), §3
  (components: `kicad_session`, `kicad_tools`, `views`), §5 step 6 (snapshot),
  §6 (tools), §7 (agent view), §10 (errors: lock files), §11 (testing).
- kipy wheel 0.8.0 (read-only): `/tmp/kipy-dl/x/kipy/` — `kicad.py`
  (`KiCad(socket_path, client_name, kicad_token, timeout_ms)`, `ping`,
  `get_version`, `get_open_documents`, `get_board`), `schematic.py`
  (`Schematic(client, doc)`, `get_symbols/get_labels/get_lines/get_hierarchy/
  get_netlist/save/save_as/revert`), `board.py`, `board_jobs.py`,
  `proto/common/types/jobs_pb2.pyi` (`RunJobResponse`).
- Owner's local KiCad source tree: `~/build/kicad` (demos used as fixtures).

**PR title:** `foundation phase 1C: spike kicad-cli api-server + kipy`

**Files:**
- Create: `spikes/1c-api-server/pyproject.toml`
- Create: `spikes/1c-api-server/.gitignore`
- Create: `spikes/1c-api-server/README.md` (how to run; 12 lines)
- Create: `spikes/1c-api-server/fetch_nightly.sh`
- Create: `spikes/1c-api-server/make_fixtures.sh`
- Create: `spikes/1c-api-server/harness.py`
- Create: `spikes/1c-api-server/q01_cli.py`
- Create: `spikes/1c-api-server/q02_connect.py`
- Create: `spikes/1c-api-server/q03_get_items.py`
- Create: `spikes/1c-api-server/q04_save.py`
- Create: `spikes/1c-api-server/q05_export.py`
- Create: `spikes/1c-api-server/q06_erc_drc.py`
- Create: `spikes/1c-api-server/q07_lifecycle.py`
- Create: `spikes/1c-api-server/run_all.py`
- Create: `docs/spikes/1c-api-server.md`

**Acceptance criteria:**
- Every question has a script with explicit `rec.check(...)` assertions that
  print `PASS`/`FAIL` per check with evidence and write
  `spikes/1c-api-server/results/<question>.json`; `run_all.py` writes
  `results/summary.md`.
- Every script prints exactly one line `SKIP: kicad-cli with api-server not found`
  and exits 0 when `KICAD_CLI` (default: `.kicad/squashfs-root/AppRun`) is
  missing.
- Fixtures under `spikes/1c-api-server/fixtures/` are produced only by
  `make_fixtures.sh` (copy from `~/build/kicad/demos` + `kicad-cli sch upgrade`
  / `pcb upgrade`); fixtures and `.kicad/` are gitignored; no KiCad file is
  hand-edited.
- `docs/spikes/1c-api-server.md` follows the template (Question(s), Setup,
  Results, Decision, Follow-ups) and records: nightly file name + full KiCad
  commit, exact `api-server` CLI syntax, socket URL form, **kipy revision**,
  exact kipy accessor for the schematic (for C4 `KicadHandle.schematic()`),
  GetItems coverage table, SaveCopyOfDocument sub-sheet behaviour, PNG/SVG
  export availability + pixel→mm mapping, ERC/DRC JSON shape + units, external
  change behaviour, crash/hang detection + restart timings, startup time,
  parallel servers, socket permissions, lock files, UUID format.
- Decision uses the index gate wording ("1C go" / "1C partial: <gap> →
  kicad-cli workaround" / "1C no-go → blocked, escalate to owner") and names
  the recommended commit for `docker/kicad-base/KICAD_COMMIT`.

**Verification commands:**
```bash
cd spikes/1c-api-server
./fetch_nightly.sh            # downloads + extracts nightly, prints commit
./make_fixtures.sh            # copies + upgrades demos via kicad-cli
uv sync
KICAD_CLI=/nonexistent uv run python run_all.py   # all SKIP, exit 0
uv run python run_all.py                          # real run, writes results/
uv run ruff check . && uv run ruff format --check .
```

**CI expectations:** No CI job runs spikes (needs a 1 GB AppImage and minutes
of runtime). No new required checks. The PR description pastes
`results/summary.md`.

> **Spike TDD adaptation.** The "failing test" is the experiment script with
> explicit assertions; "verify it fails" is the run before the harness piece or
> fixture exists (or with `KICAD_CLI=/nonexistent`, proving the skip path);
> "implementation" is the harness code + the real run; the real run's
> PASS/FAIL lines are the *result* (a FAIL is a finding, not a bug to fix).
> Each task copies its evidence into the spike doc and commits.

---

### Task 1C.1: Nightly AppImage, fixtures, harness, CLI syntax

**Files:**
- Create: `spikes/1c-api-server/pyproject.toml`, `.gitignore`, `README.md`
- Create: `spikes/1c-api-server/fetch_nightly.sh`
- Create: `spikes/1c-api-server/make_fixtures.sh`
- Create: `spikes/1c-api-server/harness.py`
- Test: `spikes/1c-api-server/q01_cli.py`

**Interfaces:**
- Consumes: nightly `kicad-nightly-10.99.0.<n>.g<sha>-x86_64.AppImage.tar`.
- Produces (spike-local): `kicad_cli() -> str`, `require_kicad() -> None`,
  `Recorder(question)` with `check(name, ok, evidence)`, `note(name, evidence)`,
  `finish() -> int`; `Server(project: Path, socket: Path)` context manager with
  `.proc`, `.url`, `.startup_s`, `.client(timeout_ms=5000) -> kipy.KiCad`;
  `fresh_fixture(name) -> Path` (copy into a tmp dir); `API_SERVER_ARGS`.

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q01_cli.py`:

```python
"""Q1: kicad-cli version, api-server CLI syntax, KiCad commit."""

import re
import subprocess

from harness import Recorder, kicad_cli, require_kicad


def main() -> int:
    require_kicad()
    rec = Recorder("q01_cli")
    v = subprocess.run([kicad_cli(), "version", "--format", "about"],
                       capture_output=True, text=True, timeout=60)
    rec.check("version_runs", v.returncode == 0, v.stdout[-400:] or v.stderr[-400:])
    m = re.search(r"10\.99\.\d+[-.\w]*", v.stdout)
    rec.check("is_master_10_99", m is not None, m.group(0) if m else v.stdout[:200])
    h = subprocess.run([kicad_cli(), "api-server", "--help"],
                       capture_output=True, text=True, timeout=60)
    text = h.stdout + h.stderr
    rec.check("api_server_help", h.returncode == 0 and "api-server" in text, text)
    rec.check("has_socket_option", "--socket" in text, "look for --socket in help")
    rec.note("api_server_help_full", text)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && python3 q01_cli.py
```
Expected: `ModuleNotFoundError: No module named 'harness'`.

- [ ] **Step 3: Write minimal implementation**

`spikes/1c-api-server/pyproject.toml` (the kipy source line is the first
candidate; Task 1C.2 decides the final revision):

```toml
[project]
name = "spike-1c-api-server"
version = "0.0.0"
description = "Throwaway spike: KiCad 11 kicad-cli api-server + kipy (CopperChat phase 1C)"
requires-python = ">=3.13"
license = "GPL-3.0-only"
dependencies = ["kicad-python", "pillow>=11"]

[tool.uv.sources]
kicad-python = { git = "https://gitlab.com/kicad/code/kicad-python.git", branch = "main" }

[dependency-groups]
dev = ["ruff>=0.7"]

[tool.ruff]
line-length = 100
```

`spikes/1c-api-server/.gitignore`:

```gitignore
.kicad/
fixtures/
results/
.venv/
```

`spikes/1c-api-server/README.md`:

```markdown
# Spike 1C — kicad-cli api-server + kipy

Throwaway. Not run in CI.

1. `./fetch_nightly.sh` — downloads the latest KiCad nightly AppImage into `.kicad/`.
2. `./make_fixtures.sh` — copies ecc83-pp and complex_hierarchy from
   `~/build/kicad/demos` and upgrades them with `kicad-cli` (never hand-edit).
3. `uv sync && uv run python run_all.py`
4. Results: `results/*.json`, `results/summary.md`. Findings: `docs/spikes/1c-api-server.md`.

`KICAD_CLI` overrides the binary (e.g. a local build). `KICAD_API_SERVER_ARGS`
overrides the launch template (default `api-server {project} --socket {socket}`).
```

`spikes/1c-api-server/fetch_nightly.sh`:

```bash
#!/usr/bin/env bash
# Downloads the newest KiCad nightly AppImage, extracts it, prints the full commit.
set -euo pipefail
cd "$(dirname "$0")"
BASE="https://downloads.kicad.org/kicad/linux/explore/nightlies"
mkdir -p .kicad && cd .kicad
NAME="${KICAD_NIGHTLY_NAME:-$(curl -fsSL "$BASE/" \
  | grep -oE 'kicad-nightly-10\.99\.0\.[0-9]+\.g[0-9a-f]+-x86_64\.AppImage\.tar' \
  | sort -t. -k4,4n -u | tail -n1)}"
echo "nightly: $NAME"
[ -f "$NAME" ] || curl -fL -o "$NAME" "$BASE/$NAME"
rm -rf squashfs-root ./*.AppImage
tar -xf "$NAME"
APPIMAGE="$(ls ./*.AppImage | head -n1)"
chmod +x "$APPIMAGE"
"$APPIMAGE" --appimage-extract >/dev/null
SHORT="$(echo "$NAME" | sed -E 's/.*\.g([0-9a-f]+)-x86_64.*/\1/')"
FULL="$(curl -fsSL "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/commits/$SHORT" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
printf '%s\n%s\n' "$NAME" "$FULL" > nightly.txt
echo "full commit: $FULL"
ls -l squashfs-root/AppRun
```

Note: the harness calls `.kicad/squashfs-root/usr/bin/kicad-cli` directly with
`LD_LIBRARY_PATH` set to the extracted `usr/lib`. If that binary does not start
that way, set `KICAD_CLI` to a wrapper that runs it via the AppImage's `AppRun`
and record the working invocation in the doc.

`spikes/1c-api-server/make_fixtures.sh`:

```bash
#!/usr/bin/env bash
# Copies KiCad demo projects verbatim and upgrades them with kicad-cli only.
set -euo pipefail
cd "$(dirname "$0")"
DEMOS="${KICAD_DEMOS:-$HOME/build/kicad/demos}"
CLI="${KICAD_CLI:-$PWD/.kicad/squashfs-root/usr/bin/kicad-cli}"
rm -rf fixtures && mkdir -p fixtures/ecc83 fixtures/complex_hierarchy
cp "$DEMOS"/ecc83/ecc83-pp* fixtures/ecc83/
for f in fp-lib-table sym-lib-table ecc83-pp.kicad_sym; do
  [ -e "$DEMOS/ecc83/$f" ] && cp "$DEMOS/ecc83/$f" fixtures/ecc83/
done
[ -d "$DEMOS/ecc83/footprints.pretty" ] && cp -r "$DEMOS/ecc83/footprints.pretty" fixtures/ecc83/
cp -r "$DEMOS"/complex_hierarchy/. fixtures/complex_hierarchy/
for sch in fixtures/*/*.kicad_sch; do "$CLI" sch upgrade --force "$sch"; done
for pcb in fixtures/*/*.kicad_pcb; do "$CLI" pcb upgrade --force "$pcb"; done
ls -R fixtures | head -n 60
```

If `--force` is not accepted by `sch upgrade`/`pcb upgrade`, drop it and record
the exact flags in the spike doc.

`spikes/1c-api-server/harness.py`:

```python
"""Shared spike helpers. Spike-local; not imported by the backend."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "results"
FIXTURES = HERE / "fixtures"
API_SERVER_ARGS = os.environ.get("KICAD_API_SERVER_ARGS", "api-server {project} --socket {socket}")


def kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    direct = HERE / ".kicad/squashfs-root/usr/bin/kicad-cli"
    return str(direct)


def require_kicad() -> None:
    if not Path(kicad_cli()).exists():
        print("SKIP: kicad-cli with api-server not found")
        raise SystemExit(0)


def cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    appdir = HERE / ".kicad/squashfs-root"
    if appdir.exists():
        libdirs = [str(appdir / "usr/lib"), str(appdir / "usr/lib/x86_64-linux-gnu")]
        env["LD_LIBRARY_PATH"] = ":".join(libdirs + [env.get("LD_LIBRARY_PATH", "")])
    return env


def fresh_fixture(name: str) -> Path:
    """Copy fixtures/<name> to a fresh tmp dir; returns the project dir."""
    src = FIXTURES / name
    if not src.exists():
        print(f"FAIL: fixture {name} missing - run ./make_fixtures.sh")
        raise SystemExit(1)
    dst = Path(tempfile.mkdtemp(prefix=f"c1-{name}-")) / name
    shutil.copytree(src, dst)
    return dst


def project_file(project_dir: Path) -> Path:
    return next(project_dir.glob("*.kicad_pro"))


class Recorder:
    def __init__(self, question: str) -> None:
        self.question = question
        self.checks: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, evidence: object) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {self.question}.{name}: {str(evidence)[:300]}")
        self.checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)})
        return bool(ok)

    def note(self, name: str, evidence: object) -> None:
        print(f"NOTE {self.question}.{name}: {str(evidence)[:300]}")
        self.notes[name] = evidence

    def finish(self) -> int:
        RESULTS.mkdir(exist_ok=True)
        out = {"question": self.question, "checks": self.checks, "notes": self.notes}
        (RESULTS / f"{self.question}.json").write_text(json.dumps(out, indent=2, default=str))
        failed = [c["name"] for c in self.checks if not c["ok"]]
        print(f"DONE {self.question}: {len(self.checks) - len(failed)} pass, {len(failed)} fail")
        return 1 if failed else 0


class Server:
    """Starts `kicad-cli api-server` for one project; waits until kipy can ping it."""

    def __init__(self, project_dir: Path, socket: Path | None = None, wait_s: float = 60.0):
        self.project_dir = project_dir
        self.socket = socket or Path(tempfile.mkdtemp(prefix="c1-sock-")) / "api.sock"
        self.url = f"ipc://{self.socket}"
        self.wait_s = wait_s
        self.proc: subprocess.Popen | None = None
        self.startup_s = -1.0
        self.log = self.socket.with_suffix(".log")

    def __enter__(self) -> Server:
        args = API_SERVER_ARGS.format(project=project_file(self.project_dir), socket=self.socket)
        cmd = [kicad_cli(), *shlex.split(args)]
        t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, cwd=self.project_dir, env=cli_env(),
                                     stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        while time.monotonic() - t0 < self.wait_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"api-server exited {self.proc.returncode}: {self.log.read_text()}")
            if self.socket.exists():
                try:
                    self.client(timeout_ms=1000).ping()
                    self.startup_s = time.monotonic() - t0
                    return self
                except Exception:  # noqa: BLE001 - polling until ready
                    pass
            time.sleep(0.1)
        self.stop()
        raise TimeoutError(f"api-server not ready after {self.wait_s}s: {self.log.read_text()}")

    def client(self, timeout_ms: int = 5000):
        from kipy import KiCad

        return KiCad(socket_path=self.url, client_name=f"spike1c-{time.monotonic_ns()}",
                     timeout_ms=timeout_ms)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)

    def __exit__(self, *exc: object) -> None:
        self.stop()


def run_cli(*args: str, cwd: Path | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([kicad_cli(), *args], cwd=cwd, env=cli_env(), capture_output=True,
                          text=True, timeout=timeout)


def main_guard(fn) -> None:  # used by every q*.py
    try:
        raise SystemExit(fn())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - a crash is a finding; print and fail
        print(f"FAIL crashed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd spikes/1c-api-server
chmod +x fetch_nightly.sh make_fixtures.sh
./fetch_nightly.sh && ./make_fixtures.sh
uv sync
KICAD_CLI=/nonexistent uv run python q01_cli.py   # prints the SKIP line, exit 0
uv run python q01_cli.py
```
Expected: SKIP line first; then `PASS q01_cli.version_runs`,
`PASS q01_cli.api_server_help`. Copy the full `--help` text and
`.kicad/nightly.txt` into the spike doc's Setup section. If `--socket` is not
the option name, set `KICAD_API_SERVER_ARGS` to the real syntax, re-run, and
record it.

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server docs/spikes/1c-api-server.md
git commit -m "test: spike 1C harness, nightly fetch, fixtures, CLI syntax

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(The spike doc is created in this step with the template headings from Task
1C.8 and the Setup section filled; later tasks fill Results.)

---

### Task 1C.2: Connect via socket; pick the kipy revision

**Files:**
- Modify: `spikes/1c-api-server/pyproject.toml` (kipy `rev = "<sha>"`)
- Test: `spikes/1c-api-server/q02_connect.py`

**Interfaces:**
- Consumes: `Server`, `Recorder`.
- Produces: doc fields "kipy revision" (full git SHA of kicad-python) and
  "schematic accessor" (exact expression yielding a `kipy.schematic.Schematic`).

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q02_connect.py`:

```python
"""Q2: kipy connects over ipc://; which kipy revision imports and works."""

import importlib
import subprocess

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad


def kipy_revision() -> str:
    out = subprocess.run(["uv", "pip", "show", "kicad-python"], capture_output=True, text=True)
    return out.stdout


def main() -> int:
    require_kicad()
    rec = Recorder("q02_connect")
    for mod in ("kipy", "kipy.schematic", "kipy.board", "kipy.board_jobs", "kipy.errors"):
        try:
            importlib.import_module(mod)
            rec.check(f"import_{mod}", True, "ok")
        except Exception as e:  # noqa: BLE001
            rec.check(f"import_{mod}", False, f"{type(e).__name__}: {e}")
    rec.note("kipy_pip_show", kipy_revision())

    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    proj = fresh_fixture("ecc83")
    with Server(proj) as srv:
        rec.note("socket_url", srv.url)
        k = srv.client()
        k.ping()
        rec.check("ping", True, srv.url)
        ver = k.get_version()
        rec.check("version", ver.major >= 10, ver.full_version)
        sch_docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        pcb_docs = k.get_open_documents(DocumentType.DOCTYPE_PCB)
        rec.check("sch_open", len(sch_docs) >= 1, sch_docs)
        rec.check("pcb_open", len(pcb_docs) >= 1, pcb_docs)
        sch = Schematic(k._client, sch_docs[0])
        rec.check("schematic_accessor", len(sch.get_symbols()) > 0,
                  "Schematic(kicad._client, kicad.get_open_documents(DOCTYPE_SCHEMATIC)[0])")
        if hasattr(k, "get_schematic"):
            rec.note("has_get_schematic", "KiCad.get_schematic() exists in this revision")
        board = k.get_board()
        rec.check("board_accessor", len(board.get_footprints()) > 0, "kicad.get_board()")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && uv run python q02_connect.py
```
Expected with `branch = "main"` if main is incompatible, or with PyPI 0.8.0:
`FAIL q02_connect.import_kipy.schematic: ImportError ... PageSettings` (finding 3).

- [ ] **Step 3: Write minimal implementation**

Bisect kicad-python revisions until all imports and checks pass against the
nightly. Try in order: `main` HEAD; the commit whose `proto/` submodule pointer
is closest to (not newer than) the nightly's KiCad commit; then tags
newer than 0.8.0. For each candidate:

```bash
cd spikes/1c-api-server
REV=<candidate-sha>
sed -i -E "s|^kicad-python = .*|kicad-python = { git = \"https://gitlab.com/kicad/code/kicad-python.git\", rev = \"$REV\" }|" pyproject.toml
uv sync && uv run python q02_connect.py
```

Keep the first full SHA where every check passes. If the API needs
regenerated protos (kipy built from the KiCad tree's `api/proto`), record the
build command instead and mark "kipy revision" as `git+<url>@<sha> (protos from
KiCad <commit>)`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python q02_connect.py
```
Expected: all `PASS`. Record in the doc: "kipy revision: `<full sha>`",
"socket URL: `ipc://<absolute path>`", "schematic accessor: `<expr>`".

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server docs/spikes/1c-api-server.md
git commit -m "test: spike 1C kipy connect and revision pin

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.3: GetItems on schematic and board

**Files:**
- Test: `spikes/1c-api-server/q03_get_items.py`

**Interfaces:**
- Consumes: `Server`, schematic accessor from Task 1C.2.
- Produces: doc table "GetItems coverage" (call → count → sample fields → UUID
  format → units).

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q03_get_items.py`:

```python
"""Q3: GetItems coverage on schematic (incl. hierarchy) and board; UUID format; units."""

import re

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def first_uuid(items) -> str:
    for it in items:
        ident = getattr(it, "id", None)
        if ident is not None:
            return str(getattr(ident, "value", ident))
    return ""


def schematic(k):
    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    return Schematic(k._client, k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0])


def probe(rec, name, fn, need_nonempty=True):
    try:
        items = list(fn())
    except Exception as e:  # noqa: BLE001
        return rec.check(name, False, f"{type(e).__name__}: {e}"), []
    ok = len(items) > 0 if need_nonempty else True
    rec.check(name, ok, f"count={len(items)} sample={str(items[:1])[:200]}")
    return ok, items


def main() -> int:
    require_kicad()
    rec = Recorder("q03_get_items")
    with Server(fresh_fixture("ecc83")) as srv:
        k = srv.client()
        sch = schematic(k)
        _, syms = probe(rec, "sch_symbols", sch.get_symbols)
        probe(rec, "sch_labels", sch.get_labels, need_nonempty=False)
        probe(rec, "sch_wires", sch.get_lines)
        net = sch.get_netlist()
        rec.check("sch_netlist", net is not None, str(net)[:300])
        u = first_uuid(syms)
        rec.check("uuid_full_36_chars", bool(UUID_RE.match(u)), u)
        if syms:
            pos = getattr(syms[0], "position", None)
            rec.note("sch_position_raw", pos)  # kipy Vector2 in nm; record conversion

        board = k.get_board()
        _, fps = probe(rec, "pcb_footprints", board.get_footprints)
        probe(rec, "pcb_pads", board.get_pads)
        probe(rec, "pcb_tracks", board.get_tracks)
        probe(rec, "pcb_vias", board.get_vias, need_nonempty=False)
        probe(rec, "pcb_nets", board.get_nets)
        stack = board.get_stackup()
        rec.check("pcb_stackup", stack is not None, str(stack)[:300])
        layers = board.get_enabled_layers()
        rec.check("pcb_enabled_layers", len(list(layers)) >= 2, layers)
        rec.check("pcb_uuid_full", bool(UUID_RE.match(first_uuid(fps))), first_uuid(fps))
        if fps:
            rec.note("pcb_position_raw_nm", fps[0].position)

    with Server(fresh_fixture("complex_hierarchy")) as srv:
        sch = schematic(srv.client())
        hier = sch.get_hierarchy()
        rec.check("sch_hierarchy", len(hier) >= 2, hier)
        rec.note("hierarchy_paths", [str(h)[:120] for h in hier])
        _, hsyms = probe(rec, "sch_symbols_all_sheets", sch.get_symbols)
        rec.note("symbols_scope", "compare count vs per-sheet totals in KiCad GUI; "
                 f"returned {len(hsyms)}")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && mv fixtures fixtures.off && uv run python q03_get_items.py; mv fixtures.off fixtures
```
Expected: `FAIL: fixture ecc83 missing - run ./make_fixtures.sh`, exit 1.

- [ ] **Step 3: Write minimal implementation**

No harness changes expected. If a call raises `kipy.errors.ApiError`
("unhandled"/"not implemented"), keep the FAIL, and add a `kicad-cli`
workaround probe for the same data to the script:

```python
        r = run_cli("sch", "export", "netlist", "--format", "kicadxml", "-o", "/tmp/c1-net.xml",
                    str(next(srv.project_dir.glob("*.kicad_sch"))))
        rec.check("workaround_netlist_cli", r.returncode == 0, r.stderr[-300:])
```
(import `run_cli` from `harness`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python q03_get_items.py
```
Expected: PASS for symbols, wires, netlist, hierarchy, footprints, pads,
tracks, nets, stackup, layers, full UUIDs. Fill the "GetItems coverage" table
in the doc; note whether `get_symbols()` returns all sheets or only the root
sheet, and the nm→mm factor (expected 1e-6).

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server/q03_get_items.py docs/spikes/1c-api-server.md
git commit -m "test: spike 1C GetItems coverage on schematic and board

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.4: SaveDocument and SaveCopyOfDocument (sub-sheets)

**Files:**
- Test: `spikes/1c-api-server/q04_save.py`

**Interfaces:**
- Consumes: `Schematic.save()`, `Schematic.save_as(filename, overwrite, include_project)`,
  `Board.save()`, `Board.save_as(...)`.
- Produces: doc fields "SaveCopyOfDocument writes sub-sheets: yes/no" and the
  exact copy procedure for C4 `KicadSession.save_copy`.

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q04_save.py`:

```python
"""Q4: SaveDocument round-trip; SaveCopyOfDocument to another dir incl. sub-sheets."""

import hashlib
import tempfile
from pathlib import Path

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad, run_cli


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def schematic(k):
    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    return Schematic(k._client, k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0])


def main() -> int:
    require_kicad()
    rec = Recorder("q04_save")
    proj = fresh_fixture("complex_hierarchy")
    sch_files = sorted(proj.glob("*.kicad_sch"))
    before = {p.name: p.stat().st_mtime_ns for p in sch_files}
    with Server(proj) as srv:
        k = srv.client()
        sch = schematic(k)
        sch.save()
        after = {p.name: p.stat().st_mtime_ns for p in sch_files}
        rec.check("sch_save_ok", True, "no exception")
        rec.note("sch_save_touched", [n for n in before if before[n] != after[n]])
        k.get_board().save()
        rec.check("pcb_save_ok", True, "no exception")
        saved_hashes = {p.name: sha(p) for p in sch_files}

        dest = Path(tempfile.mkdtemp(prefix="c1-copy-"))
        root = next(p for p in sch_files if p.stem == proj.name or p.stem == next(
            proj.glob("*.kicad_pro")).stem)
        sch.save_as(str(dest / root.name), overwrite=True, include_project=False)
        copied = sorted(p.name for p in dest.iterdir())
        rec.note("copy_dir_listing", copied)
        rec.check("copy_root_written", (dest / root.name).exists(), copied)
        subs = [p.name for p in sch_files if p != root]
        rec.check("copy_subsheets_written", all((dest / s).exists() for s in subs),
                  f"expected {subs}, got {copied}")
        k.get_board().save_as(str(dest / f"{root.stem}.kicad_pcb"), overwrite=True,
                              include_project=False)
        rec.check("pcb_copy_written", (dest / f"{root.stem}.kicad_pcb").exists(), copied)
        # copy must not change the open document's path or the originals
        rec.check("originals_unchanged", {p.name: sha(p) for p in sch_files} == saved_hashes,
                  "hash compare vs. right after save()")
        docs_path = str(k.get_open_documents(__import__("kipy.proto.common.types",
                        fromlist=["DocumentType"]).DocumentType.DOCTYPE_SCHEMATIC)[0])
        rec.check("open_doc_still_original", str(dest) not in docs_path, docs_path[:300])
        # the copy is a valid KiCad file
        r = run_cli("sch", "export", "svg", "-o", str(dest / "svg"), str(dest / root.name))
        rec.check("copy_loadable_by_cli", r.returncode == 0, r.stderr[-300:])
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && KICAD_API_SERVER_ARGS="api-server --bogus" uv run python q04_save.py
```
Expected: `RuntimeError: api-server exited ...` (proves the harness surfaces
launch errors instead of hanging).

- [ ] **Step 3: Write minimal implementation**

No new harness code. If `copy_subsheets_written` FAILs, add the workaround
probe (sub-sheets copied by `SaveCopyOfDocument` on each sheet is not
possible — sheets are one document), i.e. after `save()` copy the sub-sheet
files byte-for-byte (copy, not edit):

```python
        import shutil
        for s in subs:
            shutil.copy2(proj / s, dest / s)
        r = run_cli("sch", "export", "svg", "-o", str(dest / "svg2"), str(dest / root.name))
        rec.check("workaround_copy_saved_subsheets", r.returncode == 0, r.stderr[-300:])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python q04_save.py
```
Expected: saves PASS; sub-sheet result recorded either way (it is a finding).
Record in the doc the exact `KicadSession.save_copy` procedure (API copy only,
or `save()` + file copy of sub-sheets).

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server/q04_save.py docs/spikes/1c-api-server.md
git commit -m "test: spike 1C SaveDocument and SaveCopyOfDocument

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.5: PNG/SVG export jobs and pixel→mm mapping

**Files:**
- Test: `spikes/1c-api-server/q05_export.py`

**Interfaces:**
- Consumes: `kipy.proto.board.board_jobs_pb2`, `kipy.proto.schematic.*jobs*`
  (module name recorded), `kipy.proto.common.types.jobs_pb2.RunJobResponse`.
- Produces: doc fields "PNG export: API | kicad-cli+rasterize", "pixel→mm
  mapping: <formula> | unknown".

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q05_export.py`:

```python
"""Q5: PNG/SVG export via API jobs for sch + pcb; pixel->mm mapping."""

import importlib
import tempfile
from pathlib import Path

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad, run_cli


def find_msg(name: str):
    for mod in ("kipy.proto.board.board_jobs_pb2", "kipy.proto.schematic.schematic_jobs_pb2",
                "kipy.proto.common.commands.editor_commands_pb2"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        if hasattr(m, name):
            return mod, getattr(m, name)
    return None, None


def run_job(k, cmd):
    from kipy.proto.common.types.jobs_pb2 import RunJobResponse

    return k._client.send(cmd, RunJobResponse)


def main() -> int:
    require_kicad()
    rec = Recorder("q05_export")
    out = Path(tempfile.mkdtemp(prefix="c1-exp-"))
    proj = fresh_fixture("ecc83")
    with Server(proj) as srv:
        k = srv.client()
        from kipy.proto.common.types import DocumentType

        for name, doctype, fname in (
            ("RunSchematicJobExportPng", DocumentType.DOCTYPE_SCHEMATIC, "sch.png"),
            ("RunBoardJobExportPng", DocumentType.DOCTYPE_PCB, "pcb.png"),
            ("RunSchematicJobExportSvg", DocumentType.DOCTYPE_SCHEMATIC, "sch_svg"),
            ("RunBoardJobExportSvg", DocumentType.DOCTYPE_PCB, "pcb.svg"),
        ):
            mod, cls = find_msg(name)
            if cls is None:
                rec.check(f"proto_{name}", False, "message not in this kipy revision")
                continue
            rec.check(f"proto_{name}", True, mod)
            cmd = cls()
            rec.note(f"fields_{name}", [f.name for f in cls.DESCRIPTOR.fields])
            if hasattr(cmd, "job_settings"):
                cmd.job_settings.document.CopyFrom(k.get_open_documents(doctype)[0])
                cmd.job_settings.output_path = str(out / fname)
            try:
                resp = run_job(k, cmd)
                rec.check(f"run_{name}", list(resp.output_path) != [], resp)
            except Exception as e:  # noqa: BLE001
                rec.check(f"run_{name}", False, f"{type(e).__name__}: {e}")

        pngs = list(out.rglob("*.png"))
        if pngs:
            from PIL import Image

            for p in pngs:
                with Image.open(p) as im:
                    rec.note(f"png_size_{p.name}", im.size)
            board = k.get_board()
            rec.note("board_bbox_hint", "compare png size to board edge-cuts bbox (mm) and "
                     "the DPI field of the job; mapping = (px - offset_px) / dpi * 25.4")
            rec.note("edge_cuts_shapes", str(board.get_shapes())[:400])

    # Fallback path (finding 4): kicad-cli SVG on the saved files
    sch = next(proj.glob("*.kicad_sch"))
    pcb = next(proj.glob("*.kicad_pcb"))
    r1 = run_cli("sch", "export", "svg", "-o", str(out / "cli_sch"), str(sch))
    rec.check("cli_sch_svg", r1.returncode == 0, r1.stderr[-300:])
    r2 = run_cli("pcb", "export", "svg", "--layers", "F.Cu,B.Cu,Edge.Cuts", "--mode-single",
                 "-o", str(out / "cli_pcb.svg"), str(pcb))
    rec.check("cli_pcb_svg", r2.returncode == 0, r2.stderr[-300:])
    svg = next(out.rglob("cli_pcb*.svg"), None)
    if svg:
        head = svg.read_text()[:600]
        rec.note("svg_header", head)
        rec.check("svg_units_mm", 'mm"' in head or "viewBox" in head, head[:200])
    rec.note("outputs", sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()))
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && uv run python -c "import q05_export as q; print(q.find_msg('Nope'))"
```
Expected: `(None, None)` — then run the script; any `proto_*` FAIL is a finding.

- [ ] **Step 3: Write minimal implementation**

If the PNG job needs a size/DPI field (see `fields_*` notes), set it explicitly
(e.g. `cmd.dpi = 300` or `cmd.width = 2000`) and rerun so the mapping is
deterministic. Compute the mapping from the board's Edge.Cuts bounding box (mm)
and the PNG's content bounding box (`Image.getbbox()` on the alpha/non-white
area), and record: `x_mm = origin_x_mm + px / (dpi / 25.4)`. If no stable
mapping is found, record "unknown → region crop only in agent mode" (finding 9).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python q05_export.py
```
Expected: CLI SVG checks PASS; API PNG/SVG checks PASS or FAIL as findings.
Record availability per job and the mapping in the doc.

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server/q05_export.py docs/spikes/1c-api-server.md
git commit -m "test: spike 1C PNG/SVG export and pixel mapping

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.6: ERC/DRC JSON shape via kicad-cli

**Files:**
- Test: `spikes/1c-api-server/q06_erc_drc.py`

**Interfaces:**
- Consumes: `run_cli` (env `LANG=C`).
- Produces: doc section "ERC/DRC JSON" with key paths and units, used by Phase
  4's `run_erc`/`run_drc`.

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q06_erc_drc.py`:

```python
"""Q6: kicad-cli sch erc / pcb drc --format json shape (LANG=C)."""

import json
import tempfile
from pathlib import Path

from harness import Recorder, fresh_fixture, main_guard, require_kicad, run_cli


def main() -> int:
    require_kicad()
    rec = Recorder("q06_erc_drc")
    proj = fresh_fixture("ecc83")
    out = Path(tempfile.mkdtemp(prefix="c1-rc-"))
    sch = next(proj.glob("*.kicad_sch"))
    pcb = next(proj.glob("*.kicad_pcb"))

    r = run_cli("sch", "erc", "--format", "json", "--units", "mm", "-o", str(out / "erc.json"),
                str(sch))
    rec.check("erc_runs", (out / "erc.json").exists(), f"rc={r.returncode} {r.stderr[-200:]}")
    erc = json.loads((out / "erc.json").read_text())
    rec.note("erc_top_keys", sorted(erc))
    rec.check("erc_sheets_violations", "sheets" in erc and all(
        "violations" in s for s in erc["sheets"]), sorted(erc))
    v = next((v for s in erc.get("sheets", []) for v in s["violations"]), None)
    if v:
        rec.note("erc_violation_sample", v)
        rec.check("erc_violation_keys", {"type", "severity", "description", "items"} <= set(v),
                  sorted(v))
        rec.check("erc_item_uuid_pos", {"uuid", "pos"} <= set(v["items"][0]), v["items"][0])
    rec.note("erc_units", erc.get("coordinate_units"))

    r = run_cli("pcb", "drc", "--format", "json", "--units", "mm", "--schematic-parity",
                "-o", str(out / "drc.json"), str(pcb))
    rec.check("drc_runs", (out / "drc.json").exists(), f"rc={r.returncode} {r.stderr[-200:]}")
    drc = json.loads((out / "drc.json").read_text())
    rec.note("drc_top_keys", sorted(drc))
    rec.check("drc_keys", {"violations", "unconnected_items", "schematic_parity"} <= set(drc),
              sorted(drc))
    d = next(iter(drc.get("violations", []) + drc.get("unconnected_items", [])), None)
    if d:
        rec.note("drc_violation_sample", d)
        rec.check("drc_violation_keys", {"type", "severity", "description", "items"} <= set(d),
                  sorted(d))
    rec.note("drc_units", drc.get("coordinate_units"))
    rec.check("english_descriptions", all(ord(c) < 128 for c in json.dumps(drc)[:5000]),
              "LANG=C → ASCII descriptions")
    rec.note("exit_code_semantics", "rerun with --exit-code-violations and record rc")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && KICAD_CLI=/nonexistent uv run python q06_erc_drc.py
```
Expected: `SKIP: kicad-cli with api-server not found`, exit 0.

- [ ] **Step 3: Write minimal implementation**

None beyond the script. If `--units` is rejected, drop it, record the default
unit from `coordinate_units`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python q06_erc_drc.py
```
Expected: `erc_runs`, `drc_runs`, key checks PASS. Paste one ERC and one DRC
sample (trimmed) into the doc and state the units.

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server/q06_erc_drc.py docs/spikes/1c-api-server.md
git commit -m "test: spike 1C ERC/DRC JSON shape

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.7: Lifecycle — external change, crash, hang, startup, parallel, permissions, locks

**Files:**
- Test: `spikes/1c-api-server/q07_lifecycle.py`
- Create: `spikes/1c-api-server/run_all.py`

**Interfaces:**
- Consumes: `Server`, `kipy.errors.ConnectionError`, `kipy.errors.ApiError`.
- Produces: doc fields used by C4: health-check call + timeout, crash/hang
  detection time, restart time, revert/reload behaviour, lock file names,
  socket mode.

- [ ] **Step 1: Write the failing test**

`spikes/1c-api-server/q07_lifecycle.py`:

```python
"""Q7: external change, kill -9, SIGSTOP hang, startup, parallel servers, perms, locks."""

import os
import shutil
import signal
import stat
import time

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad


def schematic(k):
    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    return Schematic(k._client, k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0])


def main() -> int:
    require_kicad()
    from kipy.errors import ApiError, ConnectionError as KConnErr

    rec = Recorder("q07_lifecycle")
    starts = []
    for _ in range(3):
        proj = fresh_fixture("ecc83")
        before = set(os.listdir(proj))
        with Server(proj) as srv:
            starts.append(round(srv.startup_s, 2))
            created = sorted(set(os.listdir(proj)) - before)
            rec.note("files_created_in_project", created)
            rec.check("lock_files_created", any(n.endswith(".lck") for n in created), created)
            mode = stat.S_IMODE(srv.socket.stat().st_mode)
            rec.note("socket_mode", oct(mode))
            rec.check("socket_not_world_writable", not mode & 0o002, oct(mode))
    rec.note("startup_s", starts)
    rec.check("startup_under_15s", max(starts) < 15, starts)

    # external change: replace the sch on disk with the sub-sheet-free copy of itself
    proj = fresh_fixture("ecc83")
    with Server(proj) as srv:
        sch = schematic(srv.client())
        n0 = len(sch.get_symbols())
        f = next(proj.glob("*.kicad_sch"))
        backup = f.with_suffix(".orig")
        shutil.copy2(f, backup)
        other = fresh_fixture("complex_hierarchy")
        shutil.copy2(next(other.glob("*.kicad_sch")), f)  # whole-file replace, no edit
        time.sleep(2)
        n1 = len(sch.get_symbols())
        rec.note("auto_reload_on_external_change", n1 != n0)
        try:
            sch.revert()
            n2 = len(sch.get_symbols())
            rec.check("revert_reloads_from_disk", n2 != n0, f"{n0} -> {n1} -> {n2}")
        except ApiError as e:
            rec.check("revert_reloads_from_disk", False, e)

    # kill -9: detection time + restart time
    proj = fresh_fixture("ecc83")
    srv = Server(proj).__enter__()
    k = srv.client(timeout_ms=2000)
    srv.proc.send_signal(signal.SIGKILL)
    t0 = time.monotonic()
    try:
        k.ping()
        rec.check("kill9_detected", False, "ping succeeded after SIGKILL")
    except (KConnErr, ApiError, Exception) as e:  # noqa: BLE001
        rec.check("kill9_detected", True, f"{type(e).__name__} after {time.monotonic() - t0:.2f}s")
    rec.note("stale_socket_left", srv.socket.exists())
    t1 = time.monotonic()
    srv2 = Server(proj, socket=srv.socket).__enter__()
    rec.check("restart_same_socket", True, f"{time.monotonic() - t1:.2f}s")
    srv2.stop()

    # SIGSTOP hang: does a call time out (not block forever)?
    srv = Server(fresh_fixture("ecc83")).__enter__()
    k = srv.client(timeout_ms=2000)
    os.kill(srv.proc.pid, signal.SIGSTOP)
    t0 = time.monotonic()
    try:
        k.ping()
        rec.check("hang_detected", False, "ping returned while stopped")
    except Exception as e:  # noqa: BLE001
        dt = time.monotonic() - t0
        rec.check("hang_detected", dt < 5, f"{type(e).__name__} after {dt:.2f}s")
    os.kill(srv.proc.pid, signal.SIGCONT)
    try:
        k2 = srv.client(timeout_ms=2000)
        k2.ping()
        rec.note("recovers_after_sigcont", True)
    except Exception as e:  # noqa: BLE001
        rec.note("recovers_after_sigcont", f"no: {e}")
    srv.stop()

    # two servers in parallel on different projects/sockets
    with Server(fresh_fixture("ecc83")) as a, Server(fresh_fixture("complex_hierarchy")) as b:
        ka, kb = a.client(), b.client()
        na, nb = len(schematic(ka).get_symbols()), len(schematic(kb).get_symbols())
        rec.check("two_parallel_servers", na > 0 and nb > 0 and na != nb, f"{na} vs {nb}")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
```

`spikes/1c-api-server/run_all.py`:

```python
"""Runs all spike questions; writes results/summary.md."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
QUESTIONS = ["q01_cli", "q02_connect", "q03_get_items", "q04_save", "q05_export",
             "q06_erc_drc", "q07_lifecycle"]


def main() -> int:
    rows, worst = [], 0
    for q in QUESTIONS:
        r = subprocess.run([sys.executable, f"{q}.py"], cwd=HERE, capture_output=True, text=True,
                           timeout=900)
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        if r.stdout.startswith("SKIP:"):
            rows.append(f"| {q} | SKIP | |")
            continue
        worst = max(worst, r.returncode)
        res = HERE / "results" / f"{q}.json"
        checks = json.loads(res.read_text())["checks"] if res.exists() else []
        fails = [c["name"] for c in checks if not c["ok"]]
        rows.append(f"| {q} | {len(checks) - len(fails)} pass / {len(fails)} fail | "
                    f"{', '.join(fails)} |")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "summary.md").write_text(
        "| Question | Result | Failed checks |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    return 0 if all("SKIP" in r for r in rows) else worst


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1c-api-server && KICAD_CLI=/nonexistent uv run python run_all.py; echo rc=$?
```
Expected: seven `SKIP:` lines, `rc=0`, `results/summary.md` lists all SKIP.

- [ ] **Step 3: Write minimal implementation**

Real run: `uv run python q07_lifecycle.py`. If `hang_detected` FAILs because
kipy's `timeout_ms` does not bound a blocked socket, record it and note that
C4 `KicadSession.call` must enforce its own timeout (worker thread +
`asyncio.wait_for`) and restart the server.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python run_all.py && cat results/summary.md
uv run ruff check . && uv run ruff format --check .
```
Expected: summary table; lifecycle numbers recorded in the doc.

- [ ] **Step 5: Commit**

```bash
git add spikes/1c-api-server docs/spikes/1c-api-server.md
git commit -m "test: spike 1C lifecycle, crash/hang, parallel servers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1C.8: Result doc and decision

**Files:**
- Modify: `docs/spikes/1c-api-server.md`

**Interfaces:**
- Produces: the recommended `KICAD_COMMIT` (consumed by Phase 2), "kipy
  revision" (consumed by Phase 2 Dockerfile and Phase 3 `[tool.uv.sources]`),
  schematic accessor (C4), export path (Phases 4/7), ERC/DRC JSON (Phase 4).

- [ ] **Step 1: Write the failing test**

A doc completeness check (run from repo root):

```bash
python3 - <<'EOF'
import re, sys
t = open("docs/spikes/1c-api-server.md").read()
need = ["## Question(s)", "## Setup", "## Results", "## Decision", "## Follow-ups",
        "Nightly:", "KiCad commit:", "kipy revision:", "Schematic accessor:",
        "api-server syntax:", "Socket URL:", "Sub-sheets in SaveCopyOfDocument:",
        "PNG export:", "Pixel to mm:", "ERC/DRC JSON units:", "UUID format:",
        "Lock files:", "Socket permissions:", "Recommended KICAD_COMMIT:", "Gate:"]
miss = [n for n in need if n not in t]
bad = re.findall(r"<[a-z ]+>", t)
print("missing:", miss, "unfilled:", bad); sys.exit(1 if miss or bad else 0)
EOF
```

- [ ] **Step 2: Run test to verify it fails**

Expected after Task 1C.1's stub: `missing: [...]`, exit 1.

- [ ] **Step 3: Write minimal implementation**

`docs/spikes/1c-api-server.md` — fill every `<...>` from `results/*.json`:

```markdown
# Spike 1C — headless kicad-cli api-server + kipy

## Question(s)
1. Nightly AppImage + full commit. 2. Exact `kicad-cli api-server` syntax.
3. kipy connect via `ipc://` socket; which kipy revision works.
4. GetItems on schematic (symbols, labels, wires, hierarchy, netlist) and board
   (footprints, pads, tracks, vias, nets, stackup, enabled layers).
5. SaveDocument; SaveCopyOfDocument to another path incl. sub-sheets.
6. PNG/SVG export jobs (sch + pcb); pixel→mm mapping.
7. `kicad-cli sch erc` / `pcb drc --format json` shape with `LANG=C`.
8. External file change → revert/reload. 9. kill -9 / SIGSTOP detection and
   restart timing. 10. Startup time. 11. Two servers in parallel.
12. Socket permissions. 13. Lock files. 14. UUID format.

## Setup
- Nightly: <file name from .kicad/nightly.txt>
- KiCad commit: <full 40-char sha>
- Host: <OS, CPU>; Python <version>; uv <version>
- api-server syntax: `<exact command line>` (full `--help` below)
- Socket URL: `ipc://<absolute socket path>`
- kipy revision: `<full sha of kicad-python>` (source: <git url>)
- Fixtures: ecc83-pp (+ lib tables, footprints.pretty, ecc83-pp.kicad_sym) and
  complex_hierarchy from `~/build/kicad/demos` at <kicad tree commit>, upgraded
  with `kicad-cli sch upgrade` / `pcb upgrade` (<exact flags>).

<details><summary>kicad-cli api-server --help</summary>

<paste>
</details>

## Results
<paste results/summary.md>

| Call | Works | Count (ecc83 / hier.) | Notes |
|---|---|---|---|
| sch.get_symbols | <y/n> | <n / n> | <all sheets or root only> |
| sch.get_labels | … | … | … |
| sch.get_lines (wires) | … | … | … |
| sch.get_hierarchy | … | … | … |
| sch.get_netlist | … | … | … |
| board.get_footprints / pads / tracks / vias / nets | … | … | … |
| board.get_stackup / get_enabled_layers | … | … | … |

- Schematic accessor: `<exact expression>`
- Coordinates: kipy returns nm; mm = nm / 1e6 (<confirmed>)
- UUID format: <36-char lowercase hyphenated / other>
- SaveDocument: <result; files touched>
- Sub-sheets in SaveCopyOfDocument: <yes/no>; KicadSession.save_copy procedure: <...>
- PNG export: <API job name per doc type | kicad-cli svg + cairosvg>
- SVG export: <...>
- Pixel to mm: <formula | unknown → region crop only in agent mode>
- ERC/DRC JSON units: <mm>; key paths: <...>; sample: <trimmed JSON>
- External change: <auto reload yes/no>; revert(): <reloads from disk yes/no>
- kill -9: detected after <s> as <exception>; stale socket <yes/no>; restart <s>
- SIGSTOP: <timeout after s | blocks> → C4 enforces own timeout <yes/no>
- Startup: <s, s, s>
- Parallel servers: <ok/no>
- Socket permissions: <mode>
- Lock files: <names, e.g. ~ecc83-pp.kicad_sch.lck | none>

## Decision
- Gate: <1C go | 1C partial: <gap> → kicad-cli workaround <command> | 1C no-go → blocked, escalate to owner>
- Recommended KICAD_COMMIT: <full sha> (the nightly's commit unless a check
  failed there and passed on another tested commit)
- kipy revision for backend `[tool.uv.sources]` and the base image: <full sha>

## Follow-ups
- Phase 2: `docker/kicad-base/KICAD_COMMIT` = recommended commit; install kipy
  at the recorded revision; smoke uses the api-server syntax above.
- Phase 3 (C4): schematic accessor, health check (`ping`, timeout), restart
  timings, lock-file names for `lock_status`, socket path form.
- Phase 4: GetItems gaps + workarounds; ERC/DRC JSON paths; units.
- Phase 7: export path for `mode=kicad`, pixel→mm mapping.
- Phase 6B: SaveCopyOfDocument sub-sheet procedure for KiCanvas snapshots.
```

Gate rule (index): any FAIL among GetItems on schematic, SaveDocument,
SaveCopyOfDocument, or *both* PNG paths (API job and kicad-cli SVG+rasterize)
→ "1C no-go → blocked, escalate to owner"; report `blocked` to the
coordinator. One missing export or GetItems sub-call with a working
`kicad-cli` alternative → "1C partial" with the exact command.

- [ ] **Step 4: Run tests to verify they pass**

Re-run the Step 1 check. Expected: `missing: [] unfilled: []`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add docs/spikes/1c-api-server.md
git commit -m "docs: spike 1C results and decision

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

- Spec coverage: §2 (KiCad 11 pinned commit, kipy), §3 (`kicad_session` start
  command, socket, restart; `views` SaveCopyOfDocument; `kicad_tools` data
  only from API / kicad-cli), §5 step 6, §6 (`run_erc`/`run_drc` JSON), §7
  (`mode=kicad` PNG export), §10 (lock files), §11 (real api-server tests,
  fixtures upgraded via kicad-cli only), §12 (spike list).
- Deviations: none from the index. The doc adds field "Schematic accessor"
  (finding 3: kipy 0.8.0 has no `KiCad.get_schematic()`), consumed by C4
  `KicadHandle.schematic()`. Private `kicad._client` use is spike-only; if the
  chosen kipy revision has a public accessor, the doc records that instead.
- Sub-sheet copying by file copy (if needed) copies files KiCad itself just
  saved — no content is edited, consistent with "never hand-edit".
