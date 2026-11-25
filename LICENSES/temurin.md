# Eclipse Temurin JRE

The CopperChat app image ships the **Eclipse Temurin 25 JRE** (Linux x64, HotSpot) in
`/opt/freerouting/jre`, used only to run Freerouting (see `freerouting.md`).

- License: GNU General Public License, version 2, with the Classpath Exception
  (GPL-2.0-only WITH Classpath-exception-2.0), plus the third-party licenses of the OpenJDK
  components.
- The JRE's own license and notice files ship unmodified with it in the image:
  `/opt/freerouting/jre/legal/` (per module: `LICENSE`, `ASSEMBLY_EXCEPTION`,
  `ADDITIONAL_LICENSE_INFO` and third-party notices) and `/opt/freerouting/jre/NOTICE`.
- Upstream: <https://adoptium.net/>, binaries from
  <https://github.com/adoptium/temurin25-binaries/releases>; the exact tarball URL and its
  SHA-256 are pinned in `docker/freerouting.lock`. The JRE is unmodified.
- Corresponding source: the OpenJDK source at the revision recorded in
  `/opt/freerouting/jre/release` (`SOURCE=`), from <https://github.com/adoptium/jdk25u>.
