# Phase 1B: Spike — KiCanvas with KiCad 11 files

**Goal:** Prove (or disprove) that KiCanvas, built from source at a pinned
commit, renders KiCad 11 (master) `.kicad_sch` and `.kicad_pcb` files produced
by the nightly `kicad-cli … upgrade`, and that a click on a symbol/footprint
yields an event carrying its reference. Record license, vendoring/pinning,
bundle size, theming and offline behaviour, and a go/no-go decision in
`docs/spikes/1b-kicanvas.md`.

**Depends on:** —

**Parallel with:** 1A, 1C

**Read first:**
- `docs/superpowers/plans/2026-10-15-foundation-plan.md` — Global Constraints
  (never hand-edit KiCad files; GPL-3.0-compatible deps), "Go/no-go gates" (1B).
- `docs/superpowers/specs/2026-10-15-foundation-design.md` — §3 (Viewer row),
  §4.2 (viewer, tap → `@REF`, themes), §6 (`render_view`), §11 (first plan steps).
- KiCanvas source facts (checked 2026-10-15 at `theacodes/kicanvas@b031159eb74aaa7eef2b026fd85d35bc05ff2095`,
  2026-04-28): `LICENSE.md` = MIT (+ ISC/CC0/OFL/Apache-2.0 bundled assets);
  `npm run build` → `build/kicanvas.js`; custom elements `kicanvas-embed`
  (attributes `src`, `controls` = `none|basic|full`, `controlslist`, `theme`,
  `zoom`, boolean `loaded`) and `kicanvas-source` (`src`); events in
  `src/viewers/base/events.ts`: `kicanvas:load`, `kicanvas:select`
  (`detail: {item, previous}`), `kicanvas:mousemove`. **`kicanvas:select` is
  dispatched on the `Viewer` object (a plain `EventTarget`), not on a DOM
  element**; `KCViewerAppElement` consumes it internally. Built-in themes:
  `witchhazel` (default) and `kicad`. The embed injects a Google Fonts
  stylesheet (Nunito, Material Symbols). The spike must confirm all of this
  at the pinned commit.

**PR title:** `foundation phase 1B: spike KiCanvas with KiCad 11 files`

**Files:**
- Create: `spikes/1b-kicanvas/.gitignore`
- Create: `spikes/1b-kicanvas/README.md`
- Create: `spikes/1b-kicanvas/KICANVAS_COMMIT`
- Create: `spikes/1b-kicanvas/fetch_kicanvas.sh`
- Create: `spikes/1b-kicanvas/check_vendor.sh`
- Create: `spikes/1b-kicanvas/fetch_kicad_nightly.sh`
- Create: `spikes/1b-kicanvas/prepare_files.sh`
- Create: `spikes/1b-kicanvas/verify_files.py`
- Create: `spikes/1b-kicanvas/package.json`, `spikes/1b-kicanvas/package-lock.json`
- Create: `spikes/1b-kicanvas/playwright.config.ts`
- Create: `spikes/1b-kicanvas/www/index.html`
- Create: `spikes/1b-kicanvas/tests/helpers.ts`
- Create: `spikes/1b-kicanvas/tests/render.spec.ts`
- Create: `spikes/1b-kicanvas/tests/events.spec.ts`
- Create: `docs/spikes/1b-kicanvas.md`

**Acceptance criteria:**
- KiCad 11 test files exist only as outputs of `kicad-cli sch upgrade` /
  `kicad-cli pcb upgrade` (nightly AppImage) run on copies of
  `~/build/kicad/demos/ecc83` and `~/build/kicad/demos/complex_hierarchy`;
  nothing is hand-edited; the generated files are gitignored (reproducible via
  `prepare_files.sh`).
- KiCanvas is built from source at the commit in `spikes/1b-kicanvas/KICANVAS_COMMIT`;
  `check_vendor.sh` asserts MIT license text, prints size, gzip size, sha256.
- Playwright tests assert, per question, PASS/FAIL with evidence appended to
  `spikes/1b-kicanvas/results/results.jsonl`, screenshots in `results/`:
  canvas non-empty for schematic and board; no page errors; a click on a known
  symbol (`U1`/`R1`–`R4`/`C1`/`C2`) and a known footprint emits a select event
  whose item resolves to that reference; DOM-level vs. viewer-level event
  delivery recorded separately; offline render; both themes.
- `docs/spikes/1b-kicanvas.md` has the fixed template and a Decision using
  the index gate ("1B go" / "1B no-go → Phase 6B own-viewer path").

**Verification commands:**
```bash
cd spikes/1b-kicanvas
./fetch_kicanvas.sh && ./check_vendor.sh
./fetch_kicad_nightly.sh && ./prepare_files.sh && python3 verify_files.py
npm ci && npx playwright install chromium
npx playwright test --reporter=list
```

**CI expectations:** CI does not run spikes. No new required checks. The PR
body includes `results/summary.md` and the two screenshots
`results/sch-witchhazel.png`, `results/pcb-witchhazel.png` (attached as files
in the PR, not committed).

> **Spike TDD adaptation.** The "failing test" is an assertion script or
> Playwright spec run before the artefact it checks exists (so it fails for the
> right reason); "implementation" is the fetch/build/prepare step; "verify" is
> the run whose PASS/FAIL is the recorded result — a FAIL on a question is a
> finding, not something to patch around inside KiCanvas.

---

### Task 1B.1: Build KiCanvas from source at a pinned commit; license and size

**Files:**
- Create: `spikes/1b-kicanvas/.gitignore`
- Create: `spikes/1b-kicanvas/README.md`
- Create: `spikes/1b-kicanvas/KICANVAS_COMMIT`
- Create: `spikes/1b-kicanvas/fetch_kicanvas.sh`
- Test: `spikes/1b-kicanvas/check_vendor.sh`

**Interfaces:**
- Consumes: `https://github.com/theacodes/kicanvas.git`.
- Produces: `spikes/1b-kicanvas/vendor/kicanvas.js`, `vendor/LICENSE.md`,
  `vendor/BUILD_INFO` (commit, node/npm versions, size, sha256). Phase 6B
  re-uses this recipe to vendor into `frontend/public/vendor/kicanvas/`.

- [ ] **Step 1: Write the failing test**

`spikes/1b-kicanvas/check_vendor.sh`:

```bash
#!/usr/bin/env bash
# Asserts the vendored KiCanvas bundle exists, is MIT licensed, and reports size.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V="$HERE/vendor"
fail() { echo "FAIL: $*"; exit 1; }
[ -s "$V/kicanvas.js" ] || fail "vendor/kicanvas.js missing"
[ -s "$V/LICENSE.md" ] || fail "vendor/LICENSE.md missing"
grep -q "available under the MIT License" "$V/LICENSE.md" || fail "LICENSE.md is not MIT"
grep -q "customElements.define(\"kicanvas-embed\"" "$V/kicanvas.js" \
  || grep -q 'kicanvas-embed' "$V/kicanvas.js" || fail "bundle does not define kicanvas-embed"
for ev in kicanvas:select kicanvas:load; do
  grep -q "$ev" "$V/kicanvas.js" || fail "event name $ev not in bundle"
done
if grep -q 'kicanvas:click' "$V/kicanvas.js"; then echo "NOTE: bundle mentions kicanvas:click"; \
  else echo "NOTE: no kicanvas:click event in bundle"; fi
size=$(stat -c %s "$V/kicanvas.js")
gz=$(gzip -9c "$V/kicanvas.js" | wc -c)
sha=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)
fonts=$(grep -o 'fonts.googleapis.com[^"`]*' "$V/kicanvas.js" | head -1 || true)
echo "PASS: vendor ok size=${size}B gzip=${gz}B sha256=${sha}"
echo "NOTE: external font URL in bundle: ${fonts:-none}"
```

`spikes/1b-kicanvas/.gitignore`:

```gitignore
node_modules/
.cache/
vendor/
files/
results/
test-results/
playwright-report/
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd spikes/1b-kicanvas && chmod +x check_vendor.sh && ./check_vendor.sh
```
Expected: `FAIL: vendor/kicanvas.js missing`, exit 1.

- [ ] **Step 3: Write minimal implementation**

`spikes/1b-kicanvas/KICANVAS_COMMIT`:

```text
b031159eb74aaa7eef2b026fd85d35bc05ff2095
```

(Before building, run `git ls-remote https://github.com/theacodes/kicanvas.git refs/heads/main`;
if `main` has moved, pin the newer sha in this file and record both in the doc.)

`spikes/1b-kicanvas/fetch_kicanvas.sh`:

```bash
#!/usr/bin/env bash
# Clone KiCanvas at the pinned commit, build it, copy bundle + license to vendor/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COMMIT="$(tr -d '[:space:]' < "$HERE/KICANVAS_COMMIT")"
SRC="$HERE/.cache/kicanvas"
if [ ! -d "$SRC/.git" ]; then
  git clone --filter=blob:none https://github.com/theacodes/kicanvas.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --detach "$COMMIT"
(cd "$SRC" && npm ci --no-audit --no-fund && npm run build)
mkdir -p "$HERE/vendor"
cp "$SRC/build/kicanvas.js" "$HERE/vendor/kicanvas.js"
cp "$SRC/LICENSE.md" "$HERE/vendor/LICENSE.md"
{
  echo "commit=$COMMIT"
  echo "commit_date=$(git -C "$SRC" show -s --format=%cI HEAD)"
  echo "node=$(node --version) npm=$(npm --version)"
  echo "size=$(stat -c %s "$HERE/vendor/kicanvas.js")"
  echo "sha256=$(sha256sum "$HERE/vendor/kicanvas.js" | cut -d' ' -f1)"
} > "$HERE/vendor/BUILD_INFO"
cat "$HERE/vendor/BUILD_INFO"
```

`spikes/1b-kicanvas/README.md`:

```markdown
# Spike 1B — KiCanvas with KiCad 11 files

Throwaway code. Results: `docs/spikes/1b-kicanvas.md`. CI never runs this.

    ./fetch_kicanvas.sh && ./check_vendor.sh          # build KiCanvas at KICANVAS_COMMIT
    ./fetch_kicad_nightly.sh                           # nightly AppImage → .cache/bin/kicad-cli
    ./prepare_files.sh && python3 verify_files.py      # copy demos, upgrade with kicad-cli
    npm ci && npx playwright install chromium
    npx playwright test --reporter=list                # results/ (jsonl, screenshots)

KiCad files under `files/` are produced only by `kicad-cli … upgrade`; never edit them.
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
chmod +x fetch_kicanvas.sh && ./fetch_kicanvas.sh && ./check_vendor.sh
```
Expected: `PASS: vendor ok size=…B gzip=…B sha256=…` plus the two NOTE lines.
If the build fails (Node version), record the error and retry with the Node
version from KiCanvas' `.nvmrc`/`package.json` `engines` (if any); record the
working version in the doc.

- [ ] **Step 5: Commit**

```bash
git add spikes/1b-kicanvas/.gitignore spikes/1b-kicanvas/README.md spikes/1b-kicanvas/KICANVAS_COMMIT \
  spikes/1b-kicanvas/fetch_kicanvas.sh spikes/1b-kicanvas/check_vendor.sh
git commit -m "chore: spike 1B builds KiCanvas from a pinned commit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B.2: KiCad 11 test files via the nightly kicad-cli

**Files:**
- Create: `spikes/1b-kicanvas/fetch_kicad_nightly.sh`
- Create: `spikes/1b-kicanvas/prepare_files.sh`
- Test: `spikes/1b-kicanvas/verify_files.py`

**Interfaces:**
- Consumes: `https://downloads.kicad.org/kicad/linux/explore/nightlies`
  (links of the form `…/download/kicad-nightly-<ver>.g<sha10>-x86_64-lite.AppImage.tar`),
  `~/build/kicad/demos/{ecc83,complex_hierarchy}` (override with `KICAD_DEMOS`).
- Produces: `.cache/bin/kicad-cli` wrapper; `.cache/kicad-nightly/nightly.env`
  (`NIGHTLY_FILE`, `NIGHTLY_VERSION`, `NIGHTLY_SHORT`, `NIGHTLY_COMMIT`,
  `NIGHTLY_COMMIT_DATE`); `files/ecc83/*`, `files/complex_hierarchy/*` upgraded.

- [ ] **Step 1: Write the failing test**

`spikes/1b-kicanvas/verify_files.py`:

```python
"""Assert the files under files/ were upgraded by the nightly kicad-cli (header check only)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("KICAD_DEMOS", Path.home() / "build/kicad/demos"))
PAIRS = [
    ("ecc83/ecc83-pp.kicad_sch", "ecc83/ecc83-pp.kicad_sch"),
    ("ecc83/ecc83-pp.kicad_pcb", "ecc83/ecc83-pp.kicad_pcb"),
    ("complex_hierarchy/complex_hierarchy.kicad_sch", "complex_hierarchy/complex_hierarchy.kicad_sch"),
    ("complex_hierarchy/ampli_ht.kicad_sch", "complex_hierarchy/ampli_ht.kicad_sch"),
    ("complex_hierarchy/complex_hierarchy.kicad_pcb", "complex_hierarchy/complex_hierarchy.kicad_pcb"),
]


def header(path: Path) -> tuple[int, str]:
    head = path.read_text(encoding="utf-8")[:400]
    version = int(re.search(r"\(version (\d+)\)", head).group(1))
    gen = re.search(r'\(generator_version "([^"]+)"\)', head)
    return version, gen.group(1) if gen else ""


def main() -> int:
    failed = 0
    for out_rel, src_rel in PAIRS:
        out, src = HERE / "files" / out_rel, SRC / src_rel
        if not out.exists():
            print(f"FAIL: {out_rel} missing")
            failed += 1
            continue
        v_out, g_out = header(out)
        v_src, _ = header(src)
        ok = v_out > v_src and (g_out.startswith("10.99") or g_out.startswith("11"))
        print(f"{'PASS' if ok else 'FAIL'}: {out_rel} version {v_src} -> {v_out} generator_version={g_out}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 verify_files.py
```
Expected: `FAIL: ecc83/ecc83-pp.kicad_sch missing` (×5), exit 1.

- [ ] **Step 3: Write minimal implementation**

`spikes/1b-kicanvas/fetch_kicad_nightly.sh`:

```bash
#!/usr/bin/env bash
# Download and extract the latest KiCad nightly (lite) AppImage; no install, no FUSE.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HERE/.cache/kicad-nightly"
INDEX="https://downloads.kicad.org/kicad/linux/explore/nightlies"
mkdir -p "$CACHE" "$HERE/.cache/bin"
url="${KICAD_APPIMAGE_URL:-$(curl -fsSL "$INDEX" \
  | grep -oE 'https://[^"]*/kicad-nightly-[^"]*-x86_64-lite\.AppImage\.tar' | sort -uV | tail -1)}"
[ -n "$url" ] || { echo "FAIL: no lite AppImage link at $INDEX"; exit 1; }
file="$(basename "$url")"
[ -f "$CACHE/$file" ] || curl -fL --retry 3 -o "$CACHE/$file" "$url"
rm -rf "$CACHE/x" && mkdir -p "$CACHE/x"
tar -xf "$CACHE/$file" -C "$CACHE/x"
appimage="$(find "$CACHE/x" -maxdepth 2 -name '*.AppImage' | head -1)"
chmod +x "$appimage"
(cd "$CACHE/x" && "$appimage" --appimage-extract >/dev/null)
root="$CACHE/x/squashfs-root"

# Mode A: run the binary directly with the AppImage's libraries.
cat > "$HERE/.cache/bin/kicad-cli" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$root/usr/lib:$root/usr/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$root/usr/bin/kicad-cli" "\$@"
EOF
chmod +x "$HERE/.cache/bin/kicad-cli"
if ! "$HERE/.cache/bin/kicad-cli" version >/dev/null 2>&1; then
  # Mode B: let AppRun set up the environment.
  printf '#!/usr/bin/env bash\nexec "%s/AppRun" kicad-cli "$@"\n' "$root" > "$HERE/.cache/bin/kicad-cli"
  "$HERE/.cache/bin/kicad-cli" version >/dev/null || { echo "FAIL: kicad-cli does not start"; exit 1; }
  echo "NOTE: using AppRun mode"
fi

version="$(echo "$file" | sed -E 's/^kicad-nightly-(.*)-x86_64-lite\.AppImage\.tar$/\1/')"
short="$(echo "$version" | sed -E 's/.*\.g([0-9a-f]+)$/\1/')"
read -r commit commit_date < <(curl -fsSL \
  "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/commits/$short" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["id"], d["committed_date"])')
cat > "$CACHE/nightly.env" <<EOF
NIGHTLY_FILE=$file
NIGHTLY_VERSION=$version
NIGHTLY_SHORT=$short
NIGHTLY_COMMIT=$commit
NIGHTLY_COMMIT_DATE=$commit_date
EOF
cat "$CACHE/nightly.env"
"$HERE/.cache/bin/kicad-cli" version
```

`spikes/1b-kicanvas/prepare_files.sh`:

```bash
#!/usr/bin/env bash
# Copy KiCad demo projects and upgrade them to the nightly (KiCad 11) format with kicad-cli.
# Never edit the resulting files by hand.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CLI="$HERE/.cache/bin/kicad-cli"
SRC="${KICAD_DEMOS:-$HOME/build/kicad/demos}"
DST="$HERE/files"
"$CLI" sch upgrade --help | tee "$HERE/.cache/sch-upgrade-help.txt" >/dev/null
force=""
grep -q -- '--force' "$HERE/.cache/sch-upgrade-help.txt" && force="--force"
rm -rf "$DST" && mkdir -p "$DST"
for demo in ecc83 complex_hierarchy; do
  cp -r "$SRC/$demo" "$DST/$demo"
  for f in "$DST/$demo"/*.kicad_sch; do "$CLI" sch upgrade $force "$f"; done
  for f in "$DST/$demo"/*.kicad_pcb; do "$CLI" pcb upgrade $force "$f"; done
done
ls -la "$DST"/*
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
chmod +x fetch_kicad_nightly.sh prepare_files.sh
./fetch_kicad_nightly.sh && ./prepare_files.sh && python3 verify_files.py
```
Expected: five `PASS:` lines showing `version <old> -> <new>` and
`generator_version=10.99…`. Record the nightly file name, version, full commit
and the new format version numbers in the doc (Setup).

- [ ] **Step 5: Commit**

```bash
git add spikes/1b-kicanvas/fetch_kicad_nightly.sh spikes/1b-kicanvas/prepare_files.sh spikes/1b-kicanvas/verify_files.py
git commit -m "chore: spike 1B produces KiCad 11 files via nightly kicad-cli upgrade

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B.3: Render KiCad 11 schematic and board (Playwright)

Questions: **Q1** schematic renders, **Q2** board renders, **Q3** hierarchical
schematic renders, **Q7** renders with all non-localhost requests blocked
(self-hosted/offline; Google Fonts), **Q8** theming (`kicad` vs `witchhazel`,
background luminance — is there a light theme?).

**Files:**
- Create: `spikes/1b-kicanvas/package.json` (+ `package-lock.json` from `npm install`)
- Create: `spikes/1b-kicanvas/playwright.config.ts`
- Create: `spikes/1b-kicanvas/www/index.html`
- Create: `spikes/1b-kicanvas/tests/helpers.ts`
- Test: `spikes/1b-kicanvas/tests/render.spec.ts`

**Interfaces:**
- Consumes: `vendor/kicanvas.js` (1B.1), `files/**` (1B.2).
- Produces: test page contract used by both specs —
  `www/index.html?files=<comma-separated URLs>&theme=<name>&controls=<none|basic|full>`,
  globals `window.__embed`, `window.__events: {via, type, ref, kind}[]`,
  `window.__refOf(item)`, `window.__findViewer()`, `window.__attachViewer(): boolean`.

- [ ] **Step 1: Write the failing test**

`spikes/1b-kicanvas/package.json`:

```json
{
  "name": "spike-1b-kicanvas",
  "private": true,
  "type": "module",
  "license": "GPL-3.0-only",
  "scripts": { "test": "playwright test --reporter=list" },
  "devDependencies": {
    "@playwright/test": "^1.55.0",
    "@types/node": "^22.0.0",
    "pngjs": "^7.0.0",
    "@types/pngjs": "^6.0.5"
  }
}
```

`spikes/1b-kicanvas/playwright.config.ts`:

```ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8765",
    viewport: { width: 1400, height: 900 },
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  },
  webServer: {
    command: "python3 -m http.server 8765 --bind 127.0.0.1",
    url: "http://127.0.0.1:8765/www/index.html",
    reuseExistingServer: true,
  },
});
```

`spikes/1b-kicanvas/tests/helpers.ts`:

```ts
import { appendFileSync, mkdirSync } from "node:fs";
import { PNG } from "pngjs";
import type { Page } from "@playwright/test";

export const RESULTS = new URL("../results/", import.meta.url).pathname;
mkdirSync(RESULTS, { recursive: true });

export function record(question: string, check: string, ok: boolean, evidence: unknown): void {
  const row = { question, check, status: ok ? "PASS" : "FAIL", evidence };
  appendFileSync(`${RESULTS}results.jsonl`, JSON.stringify(row) + "\n");
  console.log(`${row.status}: ${question}/${check} :: ${JSON.stringify(evidence).slice(0, 300)}`);
}

export type Errors = { console: string[]; page: string[]; requests: string[] };

export async function openViewer(
  page: Page,
  files: string[],
  opts: { theme?: string; offline?: boolean } = {},
): Promise<Errors> {
  const errors: Errors = { console: [], page: [], requests: [] };
  page.on("console", (m) => m.type() === "error" && errors.console.push(m.text()));
  page.on("pageerror", (e) => errors.page.push(String(e)));
  page.on("request", (r) => errors.requests.push(r.url()));
  if (opts.offline) {
    await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => route.abort());
  }
  const q = new URLSearchParams({ files: files.join(","), controls: "none" });
  if (opts.theme) q.set("theme", opts.theme);
  await page.goto(`/www/index.html?${q}`);
  await page.waitForFunction(() => (window as any).__embed?.hasAttribute("loaded"), null, {
    timeout: 60_000,
  });
  await page.waitForTimeout(1500); // first paint after load
  return errors;
}

/** Screenshot the embed; return pixel stats. Background = most frequent colour. */
export async function canvasStats(page: Page, file: string) {
  const buf = await page.locator("kicanvas-embed").screenshot({ path: `${RESULTS}${file}` });
  const png = PNG.sync.read(buf);
  const counts = new Map<number, number>();
  for (let i = 0; i < png.data.length; i += 4) {
    const key = ((png.data[i] >> 3) << 10) | ((png.data[i + 1] >> 3) << 5) | (png.data[i + 2] >> 3);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const total = png.width * png.height;
  const [bgKey, bgCount] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  const bg = [(bgKey >> 10) << 3, ((bgKey >> 5) & 31) << 3, (bgKey & 31) << 3];
  const luminance = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255;
  return {
    distinctColours: counts.size,
    nonBackgroundFraction: 1 - bgCount / total,
    background: bg,
    luminance: Math.round(luminance * 100) / 100,
  };
}

export const ECC83_SCH = "/files/ecc83/ecc83-pp.kicad_sch";
export const ECC83_PCB = "/files/ecc83/ecc83-pp.kicad_pcb";
export const HIER = [
  "/files/complex_hierarchy/complex_hierarchy.kicad_sch",
  "/files/complex_hierarchy/ampli_ht.kicad_sch",
];
export const SCH_REFS = ["U1", "R1", "R2", "R3", "R4", "C1", "C2"];
export const PCB_REFS = ["U1", "R1", "R2", "R3", "R4", "C1", "C2",
  "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"];
```

`spikes/1b-kicanvas/tests/render.spec.ts`:

```ts
import { expect, test } from "@playwright/test";
import { ECC83_PCB, ECC83_SCH, HIER, canvasStats, openViewer, record } from "./helpers";

function rendered(s: { distinctColours: number; nonBackgroundFraction: number }): boolean {
  return s.distinctColours >= 4 && s.nonBackgroundFraction > 0.005;
}

for (const [q, name, files] of [
  ["Q1", "sch", [ECC83_SCH]],
  ["Q2", "pcb", [ECC83_PCB]],
  ["Q3", "hier", HIER],
] as const) {
  test(`${q} ${name} renders KiCad 11 file`, async ({ page }) => {
    const errors = await openViewer(page, [...files]);
    const stats = await canvasStats(page, `${name}-witchhazel.png`);
    const ok = rendered(stats);
    record(q, "canvas_non_empty", ok, stats);
    record(q, "no_page_errors", errors.page.length === 0 && errors.console.length === 0, errors);
    expect(ok).toBe(true);
    expect(errors.page).toEqual([]);
    expect(errors.console).toEqual([]);
  });
}

test("Q7 renders offline (non-localhost requests blocked)", async ({ page }) => {
  const errors = await openViewer(page, [ECC83_SCH], { offline: true });
  const stats = await canvasStats(page, "sch-offline.png");
  const external = errors.requests.filter((u) => !u.startsWith("http://127.0.0.1"));
  record("Q7", "external_requests_attempted", true, external);
  record("Q7", "canvas_non_empty_offline", rendered(stats), stats);
  expect(rendered(stats)).toBe(true);
});

test("Q8 themes", async ({ page }) => {
  const out: Record<string, unknown> = {};
  for (const theme of ["witchhazel", "kicad"]) {
    for (const [name, file] of [["sch", ECC83_SCH], ["pcb", ECC83_PCB]] as const) {
      await openViewer(page, [file], { theme });
      out[`${name}-${theme}`] = await canvasStats(page, `${name}-${theme}.png`);
    }
  }
  const lums = Object.values(out).map((s: any) => s.luminance);
  record("Q8", "theme_backgrounds", true, out);
  record("Q8", "light_theme_available", lums.some((l) => l > 0.5), lums);
  record("Q8", "themes_differ", new Set(lums).size > 1, lums);
  expect(Object.keys(out)).toHaveLength(4);
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npm install && npx playwright install chromium
npx playwright test tests/render.spec.ts --reporter=list
```
Expected: FAIL — `GET /www/index.html` 404 (page does not exist yet), so
`waitForFunction` times out.

- [ ] **Step 3: Write minimal implementation**

`spikes/1b-kicanvas/www/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>KiCanvas spike 1B</title>
    <script type="module" src="/vendor/kicanvas.js"></script>
    <style>
      html, body { margin: 0; height: 100%; }
      kicanvas-embed { display: block; width: 100vw; height: 100vh; }
    </style>
  </head>
  <body>
    <script type="module">
      const params = new URLSearchParams(location.search);
      const files = (params.get("files") || "").split(",").filter(Boolean);
      const el = document.createElement("kicanvas-embed");
      el.setAttribute("controls", params.get("controls") || "none");
      if (params.get("theme")) el.setAttribute("theme", params.get("theme"));
      for (const f of files) {
        const s = document.createElement("kicanvas-source");
        s.setAttribute("src", f);
        el.appendChild(s);
      }

      function refOf(item, depth = 0) {
        if (!item || depth > 4) return null;
        for (const k of ["reference", "ref"]) {
          if (typeof item[k] === "string" && item[k]) return item[k];
        }
        const props = item.properties;
        if (props && typeof props.get === "function") {
          const r = props.get("Reference");
          if (r) return r.text ?? r.value ?? String(r);
        }
        if (Array.isArray(props)) {
          const r = props.find((p) => p.name === "Reference");
          if (r) return r.text ?? r.value ?? null;
        }
        return refOf(item.parent, depth + 1);
      }

      window.__events = [];
      const log = (via) => (e) =>
        window.__events.push({
          via,
          type: e.type,
          ref: refOf(e.detail?.item),
          kind: e.detail?.item?.constructor?.name ?? null,
        });
      for (const type of ["kicanvas:select", "kicanvas:click", "kicanvas:load"]) {
        document.addEventListener(type, log("document"), true);
        window.addEventListener(type, log("window"), true);
        el.addEventListener(type, log("element"));
      }

      window.__findViewer = () => {
        const roots = [document];
        while (roots.length) {
          const root = roots.shift();
          for (const node of root.querySelectorAll("*")) {
            if (node.viewer && typeof node.viewer.addEventListener === "function") return node.viewer;
            if (node.shadowRoot) roots.push(node.shadowRoot);
          }
        }
        return null;
      };
      window.__attachViewer = () => {
        const v = window.__findViewer();
        if (!v) return false;
        v.addEventListener("kicanvas:select", log("viewer"));
        return true;
      };
      window.__refOf = refOf;
      window.__embed = el;
      document.body.appendChild(el);
    </script>
  </body>
</html>
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
npx playwright test tests/render.spec.ts --reporter=list
```
Expected: Q1/Q2/Q3/Q7/Q8 report `PASS`/`FAIL` lines with evidence; for a "go"
Q1 and Q2 must PASS. A parse failure of the KiCad 11 format shows up as a
page/console error and an empty canvas — record the exact error text (it
names the unknown token) in the doc. Do not patch KiCanvas in this spike.

- [ ] **Step 5: Commit**

```bash
git add spikes/1b-kicanvas/package.json spikes/1b-kicanvas/package-lock.json spikes/1b-kicanvas/playwright.config.ts \
  spikes/1b-kicanvas/www/index.html spikes/1b-kicanvas/tests/helpers.ts spikes/1b-kicanvas/tests/render.spec.ts
git commit -m "test: spike 1B KiCanvas render checks for KiCad 11 files

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B.4: Click / selection events carry the reference

Questions: **Q4** schematic — does a click on a symbol produce a select event
whose item resolves to its reference, (a) at DOM level (document/window/
element listeners) and (b) at viewer level (`kicanvas:select` on the `Viewer`
found through the shadow DOM)? **Q5** the same for board footprints. **Q6**
programmatic selection by reference (`viewer.select("U1")` — KiCanvas'
schematic viewer accepts a reference or uuid string) for later
"highlight REF" support.

Method: click a 28×18 grid over the embed, collect events, find a grid point
that selects a known reference, then click that point again on a fresh page
and assert that exactly that reference is reported.

**Files:**
- Test: `spikes/1b-kicanvas/tests/events.spec.ts`

**Interfaces:**
- Consumes: page contract from Task 1B.3.
- Produces: `results/results.jsonl` rows for Q4–Q6, including the channel
  (`document`/`window`/`element`/`viewer`), the item's constructor name and
  the exact property path that yields the reference — Phase 6B uses them.

- [ ] **Step 1: Write the failing test**

`spikes/1b-kicanvas/tests/events.spec.ts`:

```ts
import { expect, test, type Page } from "@playwright/test";
import { ECC83_PCB, ECC83_SCH, PCB_REFS, SCH_REFS, openViewer, record } from "./helpers";

type Ev = { via: string; type: string; ref: string | null; kind: string | null };

async function events(page: Page): Promise<Ev[]> {
  return page.evaluate(() => (window as any).__events as Ev[]);
}

async function gridScan(page: Page, known: string[]) {
  const box = (await page.locator("kicanvas-embed").boundingBox())!;
  const hits: { x: number; y: number; ref: string; via: string; kind: string | null }[] = [];
  for (let gy = 1; gy < 18; gy++) {
    for (let gx = 1; gx < 28; gx++) {
      const x = box.x + (box.width * gx) / 28;
      const y = box.y + (box.height * gy) / 18;
      const before = (await events(page)).length;
      await page.mouse.click(x, y);
      await page.waitForTimeout(25);
      for (const e of (await events(page)).slice(before)) {
        if (e.type === "kicanvas:select" && e.ref && known.includes(e.ref)) {
          hits.push({ x, y, ref: e.ref, via: e.via, kind: e.kind });
        }
      }
    }
  }
  return hits;
}

for (const [q, file, known] of [
  ["Q4", ECC83_SCH, SCH_REFS],
  ["Q5", ECC83_PCB, PCB_REFS],
] as const) {
  test(`${q} click emits select event with reference (${file})`, async ({ page }) => {
    await openViewer(page, [file]);
    const attached = await page.evaluate(() => (window as any).__attachViewer());
    record(q, "viewer_found_via_shadow_dom", attached, { attached });

    const hits = await gridScan(page, [...known]);
    const channels = [...new Set(hits.map((h) => h.via))];
    record(q, "dom_level_select_event", channels.some((c) => c !== "viewer"), channels);
    record(q, "viewer_level_select_event", channels.includes("viewer"), {
      channels,
      refs: [...new Set(hits.map((h) => h.ref))],
      kinds: [...new Set(hits.map((h) => h.kind))],
    });
    expect(hits.length).toBeGreaterThan(0);

    // Deterministic re-check: fresh page, one click at the first hit's position.
    const target = hits[0];
    await openViewer(page, [file]);
    await page.evaluate(() => (window as any).__attachViewer());
    await page.mouse.click(target.x, target.y);
    await page.waitForTimeout(100);
    const ev = (await events(page)).filter((e) => e.type === "kicanvas:select");
    const ok = ev.some((e) => e.ref === target.ref);
    record(q, "single_click_on_known_item_reports_reference", ok, { target, ev });
    expect(ok).toBe(true);
  });
}

test("Q6 programmatic select by reference (schematic)", async ({ page }) => {
  await openViewer(page, [ECC83_SCH]);
  await page.evaluate(() => (window as any).__attachViewer());
  const result = await page.evaluate(() => {
    const v = (window as any).__findViewer();
    v.select("U1");
    const sel = v.selected;
    return { hasSelected: !!sel, ref: (window as any).__refOf(sel?.context) };
  });
  const ev = (await events(page)).filter((e) => e.via === "viewer" && e.ref === "U1");
  record("Q6", "select_by_reference", result.ref === "U1" && ev.length > 0, { result, ev });
  expect(result.ref).toBe("U1");
});
```

- [ ] **Step 2: Run test to verify it fails**

Temporarily point the spec at a missing file to prove the assertions bite:

```bash
sed 's#/files/ecc83/ecc83-pp.kicad_sch#/files/missing.kicad_sch#' tests/helpers.ts > /tmp/h.ts && \
  cp tests/helpers.ts /tmp/helpers.orig.ts && cp /tmp/h.ts tests/helpers.ts && \
  npx playwright test tests/events.spec.ts -g Q4 --reporter=list; cp /tmp/helpers.orig.ts tests/helpers.ts
```
Expected: Q4 FAIL (timeout waiting for `loaded` or `hits.length` 0). The
original `helpers.ts` is restored afterwards (`git diff --exit-code tests/helpers.ts`).

- [ ] **Step 3: Write minimal implementation**

No page change is expected. If `__attachViewer()` returns `false`, inspect
the bundle (`grep -o 'get viewer()' vendor/kicanvas.js`, or in the browser
console walk `__embed.shadowRoot`) and adjust `window.__findViewer` in
`www/index.html` to the actual property path; record that path in the doc.

- [ ] **Step 4: Run tests to verify they pass**

```bash
rm -f results/results.jsonl && npx playwright test --reporter=list
```
Expected: Q4/Q5 `single_click_on_known_item_reports_reference` PASS for a go;
`dom_level_select_event` is expected FAIL at the pinned commit (the event
lives on the `Viewer`), `viewer_level_select_event` PASS. Then build
`results/summary.md`:

```bash
python3 - <<'EOF'
import json
rows=[json.loads(l) for l in open("results/results.jsonl")]
out=["| Question | Check | Status | Evidence |","|---|---|---|---|"]
for r in rows:
    ev=json.dumps(r["evidence"])[:160].replace("|","\\|")
    out.append(f"| {r['question']} | {r['check']} | {r['status']} | `{ev}` |")
open("results/summary.md","w").write("\n".join(out)+"\n"); print("\n".join(out))
EOF
```

- [ ] **Step 5: Commit**

```bash
git add spikes/1b-kicanvas/tests/events.spec.ts spikes/1b-kicanvas/www/index.html
git commit -m "test: spike 1B KiCanvas click and selection events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B.5: Spike result doc and decision

**Files:**
- Create: `docs/spikes/1b-kicanvas.md`

**Interfaces:**
- Consumes: `vendor/BUILD_INFO`, `check_vendor.sh` output,
  `.cache/kicad-nightly/nightly.env`, `verify_files.py` output, `results/summary.md`.
- Produces: the decision that gates Phase 6B, and the exact integration
  recipe 6B follows.

- [ ] **Step 1: Write the failing test**

```bash
test -f docs/spikes/1b-kicanvas.md && \
  [ "$(grep -cE '^## (Question\(s\)|Setup|Results|Decision|Follow-ups for later phases)$' docs/spikes/1b-kicanvas.md)" = 5 ] && \
  grep -qE '1B (go|no-go)' docs/spikes/1b-kicanvas.md && echo PASS || echo FAIL
```

- [ ] **Step 2: Run test to verify it fails**

Run the command above. Expected: `FAIL` (doc missing).

- [ ] **Step 3: Write minimal implementation**

Write `docs/spikes/1b-kicanvas.md` with exactly these sections, filled from
the evidence (quote numbers, error texts, event payload kinds):

```markdown
# Spike 1B — KiCanvas with KiCad 11 files

Status: done (YYYY-MM-DD). Code: `spikes/1b-kicanvas/`. CI does not run spikes.

## Question(s)
Q1 schematic renders · Q2 board renders · Q3 hierarchical schematic renders ·
Q4 click on symbol → event with reference (DOM vs viewer level) · Q5 same for
footprints · Q6 select by reference · Q7 offline render · Q8 themes ·
plus license, vendoring/pinning, bundle size.

## Setup
- KiCanvas commit <sha> (<date>), built with node <v>/npm <v>; bundle <size> B
  (<gzip> B gzip), sha256 <…>
- KiCad nightly <NIGHTLY_FILE>, commit <NIGHTLY_COMMIT>; demo files upgraded
  with `kicad-cli sch upgrade` / `pcb upgrade`; format versions <old> → <new>
- Chromium <version> via Playwright <version>, SwiftShader WebGL
- Commands: see spikes/1b-kicanvas/README.md

## Results
Table from results/summary.md, then per question: evidence (screenshots
attached to the PR, error texts, event channel, item constructor name and the
property path that yields the reference, external URLs requested, theme
background colours/luminance).

## Decision
- **1B go** — Q1, Q2 PASS and Q4, Q5 `single_click_on_known_item_reports_reference`
  PASS (any channel). Phase 6B uses KiCanvas.
- **1B no-go** — otherwise. Phase 6B implements the own-viewer fallback
  (server-side render + `/api/projects/{id}/hit-test`).
License: MIT (+ ISC/CC0/OFL/Apache-2.0 assets) — GPL-3.0-compatible; vendor
`LICENSE.md` next to the bundle.

## Follow-ups for later phases
- Phase 6B vendoring: build at commit <sha> with the recipe in
  spikes/1b-kicanvas/fetch_kicanvas.sh into `frontend/public/vendor/kicanvas/`
  (`kicanvas.js`, `LICENSE.md`, `BUILD_INFO`); load via `<script type="module">`.
- Event wiring: exact channel and code (viewer lookup path through shadow DOM,
  `viewer.addEventListener("kicanvas:select", …)`, reference extraction path);
  if only viewer-level works, either keep the lookup helper or carry a one-line
  patch that re-dispatches `kicanvas:select` on the `kicanvas-embed` element
  (record which is recommended and why).
- Programmatic select (`viewer.select(ref)`) result for highlight support.
- Theming: available theme names and which maps to CopperChat light/dark; if
  no light board theme exists, state it.
- Offline: whether Google Fonts are requested and whether rendering works
  without them (Phase 6B must self-host or strip the font link).
- Reload behaviour hint for `view-updated`: setting a new `src`/sources on the
  embed vs. re-creating the element (record which worked in the spike, if tried).
```

- [ ] **Step 4: Run tests to verify they pass**

Re-run the Step 1 command. Expected: `PASS`.

- [ ] **Step 5: Commit**

```bash
git add docs/spikes/1b-kicanvas.md
git commit -m "docs: record spike 1B results and decision

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Open the PR (`gh pr create --title "foundation phase 1B: spike KiCanvas with KiCad 11 files"`),
attach the screenshots and `results/summary.md`, report the decision to the
coordinator — it gates Phase 6B.

---

## Phase self-review

- Spec coverage: §3 Viewer row and fallback → Decision rules in Task 1B.5;
  §4.2 "tapping a component inserts `@REF`" → Task 1B.4 (Q4/Q5), "theming via
  CSS variables, GUI view follows theme" → Q8; §11 "KiCanvas rendering of
  KiCad 11 files including click events" → whole phase; §12 license → Task
  1B.1 `check_vendor.sh`; Global Constraint "never hand-edit KiCad files" →
  Task 1B.2 (files only via `kicad-cli … upgrade`, gitignored, regenerated).
- Known risk recorded up front from source reading: `kicanvas:select` is
  dispatched on the internal `Viewer` (`EventTarget`), not the DOM, at the
  pinned commit; the test distinguishes the channels so the decision stays
  mechanical. The assignment mentioned `kicanvas:click`; no such event exists
  in the source at the pinned commit — `check_vendor.sh` records whether the
  bundle mentions it.
- The nightly fetch script duplicates the one in Phase 1C on purpose (1B and
  1C run in parallel and spikes are throwaway).
- Review Focus: no Review Focus line is owned by this phase.
