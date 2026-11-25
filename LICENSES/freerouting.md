# Freerouting

The CopperChat app image ships **Freerouting 2.5.0**
(`/opt/freerouting/freerouting-2.5.0.jar`), the PCB autorouter by the Freerouting project.

- License: GNU General Public License, version 3 (GPL-3.0); the full text is the `LICENSE`
  file at the root of the CopperChat repository (CopperChat itself is GPL-3.0-only).
- Upstream: <https://github.com/freerouting/freerouting>
- Binary: <https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar>,
  SHA-256 pinned in `docker/freerouting.lock`.
- Corresponding source: the tag `v2.5.0` of the upstream repository,
  <https://github.com/freerouting/freerouting/tree/v2.5.0> (source archive:
  <https://github.com/freerouting/freerouting/archive/refs/tags/v2.5.0.tar.gz>).
- The JAR is distributed **unmodified**. CopperChat runs it as a **separate process**
  (`java -jar`, headless) and exchanges only Specctra DSN/SES files with it; it does not link
  against or include Freerouting code.
