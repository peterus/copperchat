# CopperChat

Design KiCad schematics and PCBs by talking to an AI agent — a self-hosted web app, powered by Claude.

Status: early design. See `docs/superpowers/specs/`.

License: GPL-3.0-only.

## Third-party software

- **KiCanvas** (MIT) — vendored in `frontend/public/vendor/kicanvas/` (see `LICENSE.md` and
  `BUILD_INFO` there), built from the pinned upstream commit with CopperChat's patches from
  `frontend/vendor-patches/kicanvas/` (KiCad 11 format support, touch gestures, host
  integration, no external font requests). Rebuild with `frontend/scripts/vendor-kicanvas.sh`.
