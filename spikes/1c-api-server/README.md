# Spike 1C — kicad-cli api-server + kipy

Throwaway. Not run in CI.

1. `./fetch_nightly.sh` — downloads the latest KiCad nightly AppImage into `.kicad/`
   (`KICAD_NIGHTLY_NAME=kicad-nightly-10.99.0.5265.g0616634034-x86_64.AppImage.tar`
   reproduces the tested one while it is still on the server).
   `./fetch_kipy.sh` — checks out kicad-python at the pinned revision into
   `.kicad/kicad-python` (path source in `pyproject.toml`).
2. `./make_fixtures.sh` — copies ecc83-pp and complex_hierarchy from
   `~/build/kicad/demos` and upgrades them with `kicad-cli` (never hand-edit).
3. `uv sync && uv run python run_all.py`
4. Results: `results/*.json`, `results/summary.md`. Findings: `docs/spikes/1c-api-server.md`.

`KICAD_CLI` overrides the binary (e.g. a local build). `KICAD_API_SERVER_ARGS`
overrides the launch template (default `api-server --socket {socket} {project}`).
