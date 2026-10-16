# Phase 2: KiCad base image

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X11, X12); they supersede conflicting code in this file.

**Goal:** Build the pinned KiCad 11 (master) commit from source in CI and
publish `ghcr.io/peterus/copperchat-kicad-base:<commit>` (+ `:<short>`): a
Debian trixie runtime image with `kicad-cli` (incl. `api-server`), pinned
`kicad-symbols`/`kicad-footprints`, Python 3 and kipy at the revision recorded
by spike 1C, running as non-root `copperchat` (uid 1000). A smoke script proves
the image works and is the phase's failing test.

**Depends on:** 1C (Decision: recommended `KICAD_COMMIT`, "kipy revision",
api-server syntax).

**Parallel with:** 3, 5

**Read first:**
- `docs/superpowers/plans/2026-10-15-foundation-plan.md` — Global Constraints
  (KiCad pinned commit in `docker/kicad-base/KICAD_COMMIT`, GHCR images),
  Repository Layout, Phase Conventions (CI expectation for the `kicad` job).
- `docs/superpowers/specs/2026-10-15-foundation-design.md` — §2 (stack), §4
  (images: base image only on commit change, 1–2 h cached), §11 (CI matrix
  over supported `kicad-base` tags).
- `docs/spikes/1c-api-server.md` — Setup ("KiCad commit", "api-server syntax",
  "Socket URL", "kipy revision") and Decision ("Recommended KICAD_COMMIT").

**PR title:** `foundation phase 2: KiCad base image`

**Files:**
- Create: `scripts/kicad-base-smoke.sh`
- Create: `docker/kicad-base/Dockerfile`
- Create: `docker/kicad-base/KICAD_COMMIT`
- Create: `docker/kicad-base/LIBS_COMMITS`
- Create: `docker/kicad-base/SUPPORTED_COMMITS`
- Create: `docker/kicad-base/smoke_ping.py`
- Create: `docker/kicad-base/.dockerignore`
- Create: `.github/workflows/kicad-base.yml`

**Acceptance criteria:**
- `scripts/kicad-base-smoke.sh <image>` exits 0 against the built image and
  non-zero against `debian:trixie-slim`; it checks `kicad-cli version`
  (commit matches `KICAD_COMMIT`), `kicad-cli api-server --help`, starts the
  api-server on a *copy* of the installed `ecc83` demo and pings it with kipy,
  imports `kipy.schematic` and `kipy.board_jobs`, runs as uid 1000, and runs
  without `DISPLAY`/Xvfb.
- `docker/kicad-base/KICAD_COMMIT` holds exactly the 40-char SHA from the 1C
  Decision; `SUPPORTED_COMMITS` holds that SHA as its only line;
  `LIBS_COMMITS` pins `KICAD_SYMBOLS_COMMIT=` and `KICAD_FOOTPRINTS_COMMIT=`
  (40-char SHAs).
- Dockerfile: multi-stage `debian:trixie`; Release build with Ninja and
  `KICAD_IPC_API=ON`, `KICAD_BUILD_I18N=OFF`, `KICAD_SCRIPTING_WXPYTHON=OFF`,
  `KICAD_USE_OCC=ON`, `KICAD_SPICE=ON`; ccache via BuildKit cache mount;
  runtime stage contains no compilers or `-dev` packages.
- Workflow triggers on `docker/kicad-base/**` and `workflow_dispatch`; skips
  the build when `ghcr.io/peterus/copperchat-kicad-base:<commit>` already
  exists; pushes `:<commit>` and `:<short>`; a `smoke` job runs
  `scripts/kicad-base-smoke.sh` against the pushed image.
- No KiCad file is created or edited by hand; the smoke uses the demo shipped
  by the KiCad install, copied to a temp dir.

**Verification commands:**
```bash
bash -n scripts/kicad-base-smoke.sh && shellcheck scripts/kicad-base-smoke.sh
scripts/kicad-base-smoke.sh debian:trixie-slim; echo "expect non-zero: $?"
DOCKER_BUILDKIT=1 docker build -f docker/kicad-base/Dockerfile \
  --build-arg KICAD_COMMIT="$(cat docker/kicad-base/KICAD_COMMIT)" \
  -t copperchat-kicad-base:local docker/kicad-base          # 1-2 h cold, minutes warm
scripts/kicad-base-smoke.sh copperchat-kicad-base:local      # exit 0
actionlint .github/workflows/kicad-base.yml
gh workflow run kicad-base.yml && gh run watch
```

**CI expectations:** `kicad-base.yml` is green on the PR's push (build or
"tag exists" skip, then smoke). Other workflows unchanged. After merge the
image `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>` exists and is
public (owner sets package visibility once; noted in the PR body).

---

### Task 2.1: Smoke script and pin files

**Files:**
- Create: `scripts/kicad-base-smoke.sh`
- Create: `docker/kicad-base/smoke_ping.py`
- Create: `docker/kicad-base/KICAD_COMMIT`
- Create: `docker/kicad-base/LIBS_COMMITS`
- Create: `docker/kicad-base/SUPPORTED_COMMITS`
- Test: `scripts/kicad-base-smoke.sh` (it *is* the test)

**Interfaces:**
- Consumes: 1C doc fields "Recommended KICAD_COMMIT", "api-server syntax",
  "Socket URL", "kipy revision".
- Produces: `scripts/kicad-base-smoke.sh <image>` (exit 0 = image OK), used by
  the `smoke` job here and by Phase 9's release workflow;
  `docker/kicad-base/SUPPORTED_COMMITS` (one SHA per line, read by the CI
  matrix in later phases); `/opt/copperchat/smoke_ping.py` inside the image.

- [ ] **Step 1: Write the failing test**

`scripts/kicad-base-smoke.sh`:

```bash
#!/usr/bin/env bash
# Smoke test for ghcr.io/peterus/copperchat-kicad-base images.
# Usage: scripts/kicad-base-smoke.sh <image>
set -euo pipefail

IMAGE="${1:?usage: $0 <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED_COMMIT="$(tr -d '[:space:]' < "$ROOT/docker/kicad-base/KICAD_COMMIT")"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
run() { docker run --rm --env LANG=C "$IMAGE" "$@"; }   # no DISPLAY passed, no X socket

echo "== image: $IMAGE (expect KiCad ${EXPECTED_COMMIT:0:10})"

uid="$(run id -u)" || fail "image does not start"
[ "$uid" = "1000" ] || fail "runs as uid $uid, expected 1000 (copperchat)"
[ "$(run id -un)" = "copperchat" ] || fail "user is not copperchat"

ver="$(run kicad-cli version --format about)" || fail "kicad-cli version failed"
echo "$ver" | head -n 5
echo "$ver" | grep -q "${EXPECTED_COMMIT:0:10}" \
  || fail "kicad-cli version does not mention commit ${EXPECTED_COMMIT:0:10}"

help="$(run kicad-cli api-server --help 2>&1)" || fail "kicad-cli api-server --help failed"
echo "$help" | grep -q -- "--socket" || fail "api-server --help lacks --socket"

run sh -c 'test -z "${DISPLAY:-}" && ! command -v Xvfb' \
  || fail "DISPLAY set or Xvfb installed (must run headless without X)"

run python3 -c "import kipy.schematic, kipy.board_jobs; print('kipy imports ok')" \
  || fail "kipy.schematic / kipy.board_jobs import failed"

run sh -c 'test -d /usr/share/kicad/symbols && test -d /usr/share/kicad/footprints' \
  || fail "symbol/footprint libraries missing"

run python3 /opt/copperchat/smoke_ping.py \
  || fail "api-server did not start or kipy ping failed"

echo "SMOKE OK"
```

`docker/kicad-base/smoke_ping.py` (copied into the image at
`/opt/copperchat/smoke_ping.py`; copies the installed demo, never edits it):

```python
"""Start kicad-cli api-server on a copy of the ecc83 demo and ping it via kipy."""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEMO = Path("/usr/share/kicad/demos/ecc83")


def main() -> int:
    from kipy import KiCad
    from kipy.proto.common.types import DocumentType
    from kipy.schematic import Schematic

    work = Path(tempfile.mkdtemp(prefix="smoke-")) / "ecc83"
    shutil.copytree(DEMO, work)
    pro = next(work.glob("ecc83-pp*.kicad_pro"))
    sock = work.parent / "api.sock"
    # api-server syntax from docs/spikes/1c-api-server.md ("api-server syntax")
    proc = subprocess.Popen(["kicad-cli", "api-server", str(pro), "--socket", str(sock)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 90:
            if proc.poll() is not None:
                print(f"api-server exited {proc.returncode}: {proc.stdout.read()}")
                return 1
            if sock.exists():
                try:
                    k = KiCad(socket_path=f"ipc://{sock}", client_name="smoke", timeout_ms=2000)
                    k.ping()
                    break
                except Exception:  # noqa: BLE001 - polling until ready
                    pass
            time.sleep(0.2)
        else:
            print("api-server not ready after 90s")
            return 1
        print(f"api-server ready after {time.monotonic() - t0:.1f}s; {k.get_version().full_version}")
        # schematic accessor from docs/spikes/1c-api-server.md ("Schematic accessor")
        sch = Schematic(k._client, k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)[0])
        n_sym = len(sch.get_symbols())
        n_fp = len(k.get_board().get_footprints())
        print(f"symbols={n_sym} footprints={n_fp}")
        return 0 if n_sym > 0 and n_fp > 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
```

If `docs/spikes/1c-api-server.md` records a different api-server syntax,
socket URL form or schematic accessor, use that in `smoke_ping.py`.

- [ ] **Step 2: Run test to verify it fails**

```bash
chmod +x scripts/kicad-base-smoke.sh
mkdir -p docker/kicad-base && grep -oE 'Recommended KICAD_COMMIT: `?[0-9a-f]{40}' docs/spikes/1c-api-server.md \
  | grep -oE '[0-9a-f]{40}' > docker/kicad-base/KICAD_COMMIT
scripts/kicad-base-smoke.sh debian:trixie-slim; echo "rc=$?"
```
Expected: `SMOKE FAIL: runs as uid 0, expected 1000 (copperchat)`, `rc=1`.

- [ ] **Step 3: Write minimal implementation**

`docker/kicad-base/KICAD_COMMIT` (one line, the 40-char SHA from the 1C
Decision; written by the command in Step 2, then verified):

```bash
test "$(wc -l < docker/kicad-base/KICAD_COMMIT)" -eq 1 \
  && grep -qE '^[0-9a-f]{40}$' docker/kicad-base/KICAD_COMMIT && echo ok
```

`docker/kicad-base/SUPPORTED_COMMITS` — initially the same single SHA:

```bash
cp docker/kicad-base/KICAD_COMMIT docker/kicad-base/SUPPORTED_COMMITS
```

`docker/kicad-base/LIBS_COMMITS` — pin the library repos to their `master`
HEAD at the time of writing (resolved, not a branch name):

```bash
sym="$(git ls-remote https://gitlab.com/kicad/libraries/kicad-symbols.git refs/heads/master | cut -f1)"
fp="$(git ls-remote https://gitlab.com/kicad/libraries/kicad-footprints.git refs/heads/master | cut -f1)"
printf 'KICAD_SYMBOLS_COMMIT=%s\nKICAD_FOOTPRINTS_COMMIT=%s\n' "$sym" "$fp" > docker/kicad-base/LIBS_COMMITS
cat docker/kicad-base/LIBS_COMMITS
```

Resulting file format (exactly two lines):

```text
KICAD_SYMBOLS_COMMIT=<40-char sha>
KICAD_FOOTPRINTS_COMMIT=<40-char sha>
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
bash -n scripts/kicad-base-smoke.sh && shellcheck scripts/kicad-base-smoke.sh
python3 -m py_compile docker/kicad-base/smoke_ping.py
grep -cE '^KICAD_(SYMBOLS|FOOTPRINTS)_COMMIT=[0-9a-f]{40}$' docker/kicad-base/LIBS_COMMITS   # 2
scripts/kicad-base-smoke.sh debian:trixie-slim; echo "rc=$? (still 1: no image yet)"
```
Expected: lint clean, `2`, `rc=1`. The smoke turns green in Task 2.2.

- [ ] **Step 5: Commit**

```bash
git add scripts/kicad-base-smoke.sh docker/kicad-base/smoke_ping.py \
  docker/kicad-base/KICAD_COMMIT docker/kicad-base/SUPPORTED_COMMITS docker/kicad-base/LIBS_COMMITS
git commit -m "test: kicad-base smoke script and pinned commits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2.2: Multi-stage Dockerfile

**Files:**
- Create: `docker/kicad-base/Dockerfile`
- Create: `docker/kicad-base/.dockerignore`
- Test: `scripts/kicad-base-smoke.sh copperchat-kicad-base:local`

**Interfaces:**
- Consumes: `KICAD_COMMIT`, `LIBS_COMMITS`, 1C "kipy revision".
- Produces: image contract used by Phases 3/4/9: `kicad-cli` on `PATH`
  (`/usr/bin/kicad-cli`), libraries under `/usr/share/kicad/{symbols,footprints}`,
  demos under `/usr/share/kicad/demos`, `python3` (trixie, 3.13) with
  `kicad-python` installed system-wide in `/opt/venv` (on `PATH`), user
  `copperchat` uid/gid 1000, `WORKDIR /home/copperchat`, labels
  `org.opencontainers.image.revision=<KICAD_COMMIT>` and
  `io.copperchat.kipy-revision=<sha>`.

- [ ] **Step 1: Write the failing test**

The test is the smoke script from Task 2.1 against a local build tag:

```bash
scripts/kicad-base-smoke.sh copperchat-kicad-base:local
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `Unable to find image 'copperchat-kicad-base:local' locally` →
`SMOKE FAIL: image does not start`, exit 1.

- [ ] **Step 3: Write minimal implementation**

`docker/kicad-base/.dockerignore`:

```text
*
!smoke_ping.py
!LIBS_COMMITS
!KICAD_COMMIT
```

`docker/kicad-base/Dockerfile` (`KIPY_REV` default = 1C "kipy revision"; the
workflow passes it explicitly too):

```dockerfile
# syntax=docker/dockerfile:1.7
# CopperChat KiCad base image: KiCad 11 (master) built from source at a pinned commit.
ARG DEBIAN=debian:trixie

FROM ${DEBIAN} AS build
ARG KICAD_COMMIT
ARG KICAD_REPO=https://gitlab.com/kicad/code/kicad.git
ENV DEBIAN_FRONTEND=noninteractive CCACHE_DIR=/ccache CCACHE_MAXSIZE=5G
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build ccache git ca-certificates pkg-config \
      libwxgtk3.2-dev libwxgtk-webview3.2-dev libglew-dev libglm-dev libcurl4-openssl-dev \
      libcairo2-dev libboost-dev libboost-locale-dev libboost-test-dev \
      libocct-data-exchange-dev libocct-visualization-dev libocct-foundation-dev \
      libocct-modeling-data-dev libocct-modeling-algorithms-dev libocct-ocaf-dev \
      libngspice0-dev libharfbuzz-dev libfontconfig-dev libfreetype-dev \
      libgit2-dev libsecret-1-dev libzstd-dev libssl-dev unixodbc-dev \
      libprotobuf-dev protobuf-compiler libnng-dev libpoppler-glib-dev \
      python3-dev swig gettext shared-mime-info libspnav-dev
RUN test -n "${KICAD_COMMIT}" || (echo "KICAD_COMMIT build-arg required" && exit 1)
WORKDIR /src
RUN git init kicad && cd kicad \
 && git remote add origin "${KICAD_REPO}" \
 && git fetch --depth 1 origin "${KICAD_COMMIT}" \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${KICAD_COMMIT}"
RUN --mount=type=cache,target=/ccache \
    cmake -S kicad -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr \
      -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
      -DKICAD_IPC_API=ON \
      -DKICAD_BUILD_I18N=OFF \
      -DKICAD_SCRIPTING_WXPYTHON=OFF \
      -DKICAD_USE_OCC=ON \
      -DKICAD_SPICE=ON \
      -DKICAD_BUILD_QA_TESTS=OFF \
      -DKICAD_INSTALL_DEMOS=ON \
      -DKICAD_VERSION_EXTRA="${KICAD_COMMIT}" \
 && cmake --build build \
 && DESTDIR=/out cmake --install build \
 && ccache --show-stats

FROM ${DEBIAN} AS libs
COPY LIBS_COMMITS /tmp/LIBS_COMMITS
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates cmake \
 && . /tmp/LIBS_COMMITS \
 && for spec in "kicad-symbols:${KICAD_SYMBOLS_COMMIT}" "kicad-footprints:${KICAD_FOOTPRINTS_COMMIT}"; do \
      name="${spec%%:*}"; sha="${spec##*:}"; \
      git init "/src/$name" && git -C "/src/$name" remote add origin "https://gitlab.com/kicad/libraries/$name.git" \
      && git -C "/src/$name" fetch --depth 1 origin "$sha" && git -C "/src/$name" checkout --detach FETCH_HEAD \
      && cmake -S "/src/$name" -B "/build/$name" -DCMAKE_INSTALL_PREFIX=/usr \
      && DESTDIR=/out cmake --install "/build/$name" || exit 1; \
    done

FROM ${DEBIAN} AS runtime
ARG KICAD_COMMIT
ARG KIPY_REV
ARG KIPY_REPO=https://gitlab.com/kicad/code/kicad-python.git
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PATH=/opt/venv/bin:$PATH
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv git \
      libwxgtk3.2-1t64 libwxgtk-webview3.2-1t64 libglew2.2 libcurl4t64 libcairo2 \
      libboost-locale1.83.0 libngspice0 libharfbuzz0b libfontconfig1 libfreetype6 \
      libgit2-1.9 libsecret-1-0 libzstd1 libssl3t64 libodbc2 \
      libprotobuf32t64 libnng1 libpoppler-glib8t64 libspnav0 \
      libocct-data-exchange-7.8 libocct-visualization-7.8 libocct-foundation-7.8 \
      libocct-modeling-data-7.8 libocct-modeling-algorithms-7.8 libocct-ocaf-7.8 \
      fonts-dejavu-core shared-mime-info
COPY --from=build /out/usr /usr
COPY --from=libs /out/usr /usr
RUN test -n "${KIPY_REV}" || (echo "KIPY_REV build-arg required (docs/spikes/1c-api-server.md)" && exit 1) \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir "kicad-python @ git+${KIPY_REPO}@${KIPY_REV}" \
 && apt-get purge -y git && apt-get autoremove -y \
 && ldconfig \
 && ! ldd /usr/bin/kicad-cli | grep -q "not found"
RUN groupadd --gid 1000 copperchat \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash copperchat
COPY smoke_ping.py /opt/copperchat/smoke_ping.py
LABEL org.opencontainers.image.source="https://github.com/peterus/copperchat" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.revision="${KICAD_COMMIT}" \
      io.copperchat.kipy-revision="${KIPY_REV}"
USER copperchat
WORKDIR /home/copperchat
CMD ["kicad-cli", "version", "--format", "about"]
```

Runtime package names are trixie's; if `apt-get install` reports a missing
package, find the right one with
`docker run --rm debian:trixie sh -c 'apt-get update -qq && apt-cache search <lib>'`
and, after the build, confirm nothing is missing with the `ldd` guard above
(it fails the build). The build stage's `-dev` packages never reach `runtime`.

If kipy at `KIPY_REV` needs protos generated from the KiCad tree (1C doc
records "protos from KiCad <commit>"), install it in the `build` stage from
`/src/kicad/api/proto` per that recorded command and copy `/opt/venv` into
`runtime` instead of the `pip install` line.

- [ ] **Step 4: Run tests to verify they pass**

```bash
KIPY_REV="$(grep -oE 'kipy revision: `?[0-9a-f]{40}' docs/spikes/1c-api-server.md | grep -oE '[0-9a-f]{40}')"
DOCKER_BUILDKIT=1 docker build -f docker/kicad-base/Dockerfile \
  --build-arg KICAD_COMMIT="$(cat docker/kicad-base/KICAD_COMMIT)" \
  --build-arg KIPY_REV="$KIPY_REV" \
  -t copperchat-kicad-base:local docker/kicad-base
scripts/kicad-base-smoke.sh copperchat-kicad-base:local
docker run --rm copperchat-kicad-base:local sh -c '! command -v gcc && ! command -v cmake && echo no-toolchain'
docker image inspect copperchat-kicad-base:local --format '{{.Size}}'
```
Expected: `SMOKE OK`; `no-toolchain`; record image size and cold/warm build
times in the PR body.

- [ ] **Step 5: Commit**

```bash
git add docker/kicad-base/Dockerfile docker/kicad-base/.dockerignore
git commit -m "feat: multi-stage KiCad base image built from pinned master commit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2.3: GitHub Actions workflow `kicad-base.yml`

**Files:**
- Create: `.github/workflows/kicad-base.yml`
- Test: `actionlint` + a real `workflow_dispatch` run

**Interfaces:**
- Consumes: `docker/kicad-base/{Dockerfile,KICAD_COMMIT,LIBS_COMMITS}`,
  `scripts/kicad-base-smoke.sh`, repo variable `KIPY_REV` fallback to the 1C
  value written into the workflow env.
- Produces: `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>` and
  `:<first 10 chars>`.

- [ ] **Step 1: Write the failing test**

```bash
actionlint .github/workflows/kicad-base.yml
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `no such file or directory` / exit 1 (workflow absent).

- [ ] **Step 3: Write minimal implementation**

`.github/workflows/kicad-base.yml` (replace the `KIPY_REV` value with the 1C
"kipy revision" SHA when writing the file):

```yaml
name: kicad-base

on:
  push:
    branches: [main]
    paths: ["docker/kicad-base/**", "scripts/kicad-base-smoke.sh", ".github/workflows/kicad-base.yml"]
  pull_request:
    paths: ["docker/kicad-base/**", "scripts/kicad-base-smoke.sh", ".github/workflows/kicad-base.yml"]
  workflow_dispatch:
    inputs:
      force:
        description: "Rebuild even if the tag exists"
        type: boolean
        default: false

permissions:
  contents: read
  packages: write

concurrency:
  group: kicad-base-${{ github.ref }}
  cancel-in-progress: false

env:
  IMAGE: ghcr.io/peterus/copperchat-kicad-base
  KIPY_REV: "0000000000000000000000000000000000000000"  # = "kipy revision" from docs/spikes/1c-api-server.md

jobs:
  meta:
    runs-on: ubuntu-24.04
    outputs:
      commit: ${{ steps.c.outputs.commit }}
      short: ${{ steps.c.outputs.short }}
      exists: ${{ steps.e.outputs.exists }}
    steps:
      - uses: actions/checkout@v4
      - id: c
        run: |
          commit="$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)"
          [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "bad KICAD_COMMIT"; exit 1; }
          echo "commit=$commit" >> "$GITHUB_OUTPUT"
          echo "short=${commit:0:10}" >> "$GITHUB_OUTPUT"
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: e
        run: |
          if [ "${{ inputs.force }}" != "true" ] && \
             docker manifest inspect "$IMAGE:${{ steps.c.outputs.commit }}" >/dev/null 2>&1; then
            echo "exists=true" >> "$GITHUB_OUTPUT"; echo "tag exists - skipping build"
          else
            echo "exists=false" >> "$GITHUB_OUTPUT"
          fi

  build:
    needs: meta
    if: needs.meta.outputs.exists != 'true'
    runs-on: ubuntu-24.04
    # GitHub-hosted jobs are capped at 6 h. A cold KiCad build on 4 vCPUs is
    # ~1.5-3 h, so this fits; ccache (cache mount, exported via the GHA cache)
    # makes later builds of nearby commits much faster. If a cold build ever
    # exceeds the cap: (a) run on a self-hosted runner (runs-on: [self-hosted, linux, x64]),
    # or (b) split: dispatch once with target=build and push-by-cache only
    # (cache-to type=gha,mode=max) to warm ccache, then rerun for the full image.
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4
      - name: Free disk space
        run: sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL && df -h /
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Restore ccache mount
        uses: actions/cache@v4
        with:
          path: ccache-mount
          key: kicad-ccache-${{ needs.meta.outputs.commit }}
          restore-keys: kicad-ccache-
      - name: Inject ccache into BuildKit
        uses: reproducible-containers/buildkit-cache-dance@v3
        with:
          cache-map: '{"ccache-mount": "/ccache"}'
      - uses: docker/build-push-action@v6
        with:
          context: docker/kicad-base
          file: docker/kicad-base/Dockerfile
          build-args: |
            KICAD_COMMIT=${{ needs.meta.outputs.commit }}
            KIPY_REV=${{ env.KIPY_REV }}
          push: ${{ github.event_name != 'pull_request' }}
          load: ${{ github.event_name == 'pull_request' }}
          tags: |
            ${{ env.IMAGE }}:${{ needs.meta.outputs.commit }}
            ${{ env.IMAGE }}:${{ needs.meta.outputs.short }}
          cache-from: type=gha,scope=kicad-base
          cache-to: type=gha,scope=kicad-base,mode=max
      - name: Smoke (PR builds are not pushed; test the loaded image here)
        if: github.event_name == 'pull_request'
        run: scripts/kicad-base-smoke.sh "$IMAGE:${{ needs.meta.outputs.commit }}"

  smoke:
    needs: [meta, build]
    if: always() && needs.meta.result == 'success' && github.event_name != 'pull_request' && (needs.build.result == 'success' || needs.build.result == 'skipped')
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - run: docker pull "$IMAGE:${{ needs.meta.outputs.commit }}"
      - run: scripts/kicad-base-smoke.sh "$IMAGE:${{ needs.meta.outputs.commit }}"
```

Note: on pull requests the image is built but not pushed (no package write
from forks); the PR's smoke runs on the loaded image. On a PR where the tag
already exists, `build` is skipped and the PR shows only `meta` (green). The
first image is therefore pushed by the `workflow_dispatch` run on the phase
branch (step 4) so later phases can use it before merge.

- [ ] **Step 4: Run tests to verify they pass**

```bash
sed -i "s/KIPY_REV: \"0\{40\}\"/KIPY_REV: \"$KIPY_REV\"/" .github/workflows/kicad-base.yml
grep -E 'KIPY_REV: "[0-9a-f]{40}"' .github/workflows/kicad-base.yml | grep -v '"0\{40\}"'
actionlint .github/workflows/kicad-base.yml
git push -u origin HEAD
gh workflow run kicad-base.yml --ref "$(git branch --show-current)"
gh run watch "$(gh run list --workflow kicad-base.yml --limit 1 --json databaseId -q '.[0].databaseId')"
docker manifest inspect ghcr.io/peterus/copperchat-kicad-base:"$(cat docker/kicad-base/KICAD_COMMIT)" >/dev/null && echo pushed
gh workflow run kicad-base.yml --ref "$(git branch --show-current)"   # second run: meta says "tag exists", smoke green
```
Expected: actionlint clean; first run builds, pushes both tags, smoke prints
`SMOKE OK`; second run skips `build` and `smoke` passes. Record wall-clock of
the cold and warm builds in the PR body. If the cold build hits the 6 h cap,
apply fallback (a) or (b) from the workflow comment and note it in the PR.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/kicad-base.yml
git commit -m "ci: build and publish KiCad base image to GHCR

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Commit before the `git push` in Step 4; the `sed` edit is part of this commit.)

---

## Phase self-review

- Spec coverage: §2 (KiCad 11 master from source, pinned commit, headless
  `api-server`), §4 (base image `copperchat-kicad-base:<kicad-commit>`, built
  only when the pinned commit changes, cached; GHCR; users never build KiCad),
  §11 (`SUPPORTED_COMMITS` feeds the CI matrix over supported base tags),
  Global Constraints (pin in `docker/kicad-base/KICAD_COMMIT`, GPL-3.0, no
  hand-edited KiCad files — the smoke copies the installed demo).
- New cross-phase names (Produces): `scripts/kicad-base-smoke.sh <image>`,
  `docker/kicad-base/SUPPORTED_COMMITS`, `docker/kicad-base/LIBS_COMMITS`,
  image paths `/opt/venv` (kipy), `/opt/copperchat/smoke_ping.py`,
  `/usr/share/kicad/demos`, user `copperchat` uid 1000, tag `:<first 10 chars>`.
- Deviations: the index Repository Layout lists only `Dockerfile, KICAD_COMMIT`
  under `docker/kicad-base/`; this phase adds `LIBS_COMMITS`,
  `SUPPORTED_COMMITS`, `smoke_ping.py`, `.dockerignore`. The owner's local
  build uses `KICAD_SCRIPTING_WXPYTHON=ON`; the image uses OFF as instructed
  (no GUI scripting needed headless). Runtime package names (t64 suffixes,
  OCCT 7.8, libgit2 1.9) are trixie's best-known names and are guarded by the
  `ldd` check.
