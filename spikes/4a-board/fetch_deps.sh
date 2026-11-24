#!/usr/bin/env bash
# Spike-only dependencies into .deps/ (gitignored): Freerouting 2.5.0 JAR (checksum-verified) and a
# Java 25 runtime copied out of eclipse-temurin:25-jre (the kicad-base image has no Java; the native
# freerouting-cli 2.5.0 crashes with a GraalVM serialization error and then hangs, so the JAR is used).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .deps/freerouting
if [ ! -f .deps/freerouting/freerouting-2.5.0.jar ]; then
  gh release download v2.5.0 -R freerouting/freerouting -p 'freerouting-2.5.0.jar' -D .deps/freerouting
fi
test "$(stat -c %s .deps/freerouting/freerouting-2.5.0.jar)" -gt 50000000
if [ ! -x .deps/jre/bin/java ]; then
  docker pull -q eclipse-temurin:25-jre
  cid=$(docker create eclipse-temurin:25-jre)
  docker cp "$cid":/opt/java/openjdk .deps/jre
  docker rm "$cid" >/dev/null
fi
.deps/jre/bin/java -version 2>&1 | head -1
