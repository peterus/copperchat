# Changelog

All notable changes to CopperChat are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Container images:
`ghcr.io/peterus/copperchat:<version>`.

## [Unreleased]

## [0.1.0] - 2026-10-17

First public release: the **foundation** — chat with a read-only AI agent about your KiCad
projects, in the browser on desktop, iPad and iPhone.

### Added

- **Chat agent with read-only KiCad tools.** The agent inspects schematics and boards through
  KiCad 11's own API (`kicad-cli api-server` + kipy), runs ERC/DRC, renders annotated
  "agent views" and looks at them. It has no file, shell or web tools of its own. Message
  queue, Stop, resume after rate limits, usage display, image/PDF attachments.
- **Viewer.** Schematic and board viewer based on KiCanvas, patched for the KiCad 11 file
  format, touch gestures and host integration (sheet tree, layers, pinch zoom); tap a part to
  mention it in the chat (`@R12`).
- **History, undo and git.** Autosave after every turn, per-turn undo snapshots, detection of
  external changes, crash recovery; git status, diff summary and commit — only on request,
  never push or history rewriting.
- **Skills and part researcher.** Built-in, user and per-project skills; a separate, guarded
  part-researcher agent (`research_part`, public web only) that collects datasheets and
  pinout/application notes under `docs/parts/<MPN>/` in the project.
- **Uploads** of images and PDFs per project (the agent can read PDF pages).
- **Project picker** over the mounted projects folder (any depth); projects open in the
  KiCad GUI open read-only with a banner.
- **Installable app (PWA) and Web Push** when a run finishes, the agent needs you or a
  rate-limit window reopens.
- **Security.** App login always on (argon2, rate-limited), strict Content-Security-Policy,
  WebSocket origin check, push endpoints restricted to the browsers' push services.
- **Deployment.** Docker image `ghcr.io/peterus/copperchat` (KiCad 11 built from source,
  runs as uid 1000), `docker-compose.yml` with an optional Cloudflare Tunnel profile
  (`docker compose --profile tunnel up -d`), `/api/health` with app and KiCad version.

### Known limitations

- **No editing yet:** the agent cannot change schematics or boards (planned for the next
  sub-project, with plan-before-edit).
- One user per instance.
- No light theme for the board viewer.
- Web Push delivery is not tested in CI, and the app has not been tested on a real iPhone
  yet.
- The optional `cloudflared` service uses the unpinned `cloudflare/cloudflared:latest` image.
- The KiCanvas patches live in this repository (`frontend/vendor-patches/kicanvas/`), not in
  an upstream fork; the main JavaScript chunk is large (~590 kB).

[Unreleased]: https://github.com/peterus/copperchat/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/peterus/copperchat/releases/tag/v0.1.0
