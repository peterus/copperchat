# syntax=docker/dockerfile:1.7
# CopperChat KiCad base image: KiCad 11 (master) built from source at a pinned
# commit, plus the stock symbol/footprint libraries and kipy, running headless
# as the non-root user `copperchat` (uid 1000).
#
# Build args (the workflow passes them from the pin files in this directory):
#   KICAD_COMMIT  40-char SHA from KICAD_COMMIT (required)
#   KIPY_REV      40-char kicad-python SHA from KIPY_COMMIT (required)
#   RECIPE        hash of docker/kicad-base/* (label only; the workflow compares it
#                 with the published image to decide whether to rebuild)
ARG DEBIAN=debian:trixie

# ---------------------------------------------------------------------------
# deps: toolchain and -dev packages for the KiCad build (never reaches runtime)
FROM ${DEBIAN} AS deps
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build ccache git ca-certificates pkg-config gettext python3 \
      libwxgtk3.2-dev libwxgtk-webview3.2-dev libgtk-3-dev libx11-dev \
      libglew-dev libglm-dev libcurl4-openssl-dev libcairo2-dev libpixman-1-dev \
      libboost-dev libboost-locale-dev \
      libocct-data-exchange-dev libocct-visualization-dev libocct-foundation-dev \
      libocct-modeling-data-dev libocct-modeling-algorithms-dev libocct-ocaf-dev \
      libngspice0-dev libharfbuzz-dev libfontconfig-dev libfreetype-dev \
      libgit2-dev libsecret-1-dev libzstd-dev zlib1g-dev libssl-dev unixodbc-dev \
      libprotobuf-dev protobuf-compiler libnng-dev libpoppler-glib-dev libspnav-dev \
      shared-mime-info

# ---------------------------------------------------------------------------
# build: KiCad at KICAD_COMMIT, installed into /out. Clone, build and cleanup
# share one RUN so the cached layer holds only the install tree.
FROM deps AS build
ARG KICAD_COMMIT
ARG KICAD_REPO=https://gitlab.com/kicad/code/kicad.git
ENV CCACHE_DIR=/ccache CCACHE_MAXSIZE=5G CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime
RUN --mount=type=cache,target=/ccache \
    test -n "${KICAD_COMMIT}" || { echo "KICAD_COMMIT build-arg required" >&2; exit 1; }; \
    set -eux; \
    git init -q /src/kicad; \
    git -C /src/kicad remote add origin "${KICAD_REPO}"; \
    git -C /src/kicad fetch -q --depth 1 origin "${KICAD_COMMIT}"; \
    git -C /src/kicad checkout -q --detach FETCH_HEAD; \
    test "$(git -C /src/kicad rev-parse HEAD)" = "${KICAD_COMMIT}"; \
    # A shallow clone has no tags, so `git describe` falls back to "10.99.0-unknown";
    # the extra suffix keeps the commit visible in `kicad-cli version`.
    cmake -S /src/kicad -B /src/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr \
      -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
      -DKICAD_BUILD_I18N=OFF \
      -DKICAD_BUILD_QA_TESTS=OFF \
      -DKICAD_SPICE_QA=OFF \
      -DKICAD_UPDATE_CHECK=OFF \
      -DKICAD_USE_SENTRY=OFF \
      -DKICAD_WAYLAND=OFF \
      -DKICAD_INSTALL_DEMOS=ON \
      -DKICAD_VERSION_EXTRA="g$(echo "${KICAD_COMMIT}" | cut -c1-10)"; \
    cmake --build /src/build; \
    DESTDIR=/out cmake --install /src/build; \
    ccache --show-stats; \
    rm -rf /src; \
    # Runtime packages = owners of every shared library the installed ELF files
    # link against (KiCad's own libraries are under /out and resolve to "not found").
    find /out/usr/bin /out/usr/lib -type f -print0 | xargs -0 ldd 2>/dev/null | awk '$2 == "=>" && $3 ~ /^\// {print $3}' \
      | sort -u | xargs -r realpath | sort -u | xargs -r dpkg -S | cut -d: -f1 | sort -u > /out/runtime-packages.txt; \
    cat /out/runtime-packages.txt

# ---------------------------------------------------------------------------
# libs: kicad-symbols / kicad-footprints at the pins in LIBS_COMMITS (no 3D models)
FROM ${DEBIAN} AS libs
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates cmake make python3
COPY LIBS_COMMITS /tmp/LIBS_COMMITS
RUN set -eu; . /tmp/LIBS_COMMITS; \
    for spec in "kicad-symbols:${KICAD_SYMBOLS_COMMIT}" "kicad-footprints:${KICAD_FOOTPRINTS_COMMIT}"; do \
      name="${spec%%:*}"; sha="${spec##*:}"; \
      git init -q "/src/$name"; \
      git -C "/src/$name" remote add origin "https://gitlab.com/kicad/libraries/$name.git"; \
      git -C "/src/$name" fetch -q --depth 1 origin "$sha"; \
      git -C "/src/$name" checkout -q --detach FETCH_HEAD; \
      cmake -S "/src/$name" -B "/build/$name" -DCMAKE_INSTALL_PREFIX=/usr; \
      cmake --build "/build/$name"; \
      DESTDIR=/out cmake --install "/build/$name" >/dev/null; \
    done; \
    test -f /out/usr/share/kicad/template/sym-lib-table; \
    test -f /out/usr/share/kicad/template/fp-lib-table

# ---------------------------------------------------------------------------
# kipy: kicad-python at KIPY_REV in /opt/venv. Shallow KiCad submodule for the
# protos (docs/spikes/1c-api-server.md: a full submodule clone costs minutes and GBs).
FROM ${DEBIAN} AS kipy
ARG KIPY_REV
ARG KIPY_REPO=https://gitlab.com/kicad/code/kicad-python.git
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-venv
RUN test -n "${KIPY_REV}" || { echo "KIPY_REV build-arg required" >&2; exit 1; }; \
    set -eux; \
    git init -q /src/kicad-python; \
    git -C /src/kicad-python remote add origin "${KIPY_REPO}"; \
    git -C /src/kicad-python fetch -q --depth 1 origin "${KIPY_REV}"; \
    git -C /src/kicad-python checkout -q --detach FETCH_HEAD; \
    git -C /src/kicad-python submodule update --init --depth 1; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir /src/kicad-python; \
    /opt/venv/bin/python -c "import kipy.schematic, kipy.board_jobs"

# ---------------------------------------------------------------------------
# runtime: no compilers, no -dev packages
FROM ${DEBIAN} AS runtime
ARG KICAD_COMMIT
ARG KIPY_REV
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PATH=/opt/venv/bin:$PATH \
    KICAD_CONFIG_HOME=/home/copperchat/.config/kicad
COPY --from=build /out/runtime-packages.txt /tmp/runtime-packages.txt
# git stays installed (plan X12); libngspice0 is dlopen()ed, so ldd cannot see it.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update \
 && xargs -a /tmp/runtime-packages.txt apt-get install -y --no-install-recommends \
      ca-certificates python3 git libngspice0 shared-mime-info \
 && rm /tmp/runtime-packages.txt
COPY --from=build /out/usr /usr
COPY --from=libs /out/usr /usr
COPY --from=kipy /opt/venv /opt/venv
RUN ldconfig \
 && missing="$(find /usr/bin -name 'kicad*' -type f; find /usr/lib -type f \( -name '*.kiface' -o -path '*kicad*' -name '*.so*' \))" \
 && missing="$(echo "$missing" | xargs ldd 2>/dev/null | grep 'not found' || true)" \
 && { [ -z "$missing" ] || { echo "unresolved libraries: $missing" >&2; exit 1; }; } \
 && groupadd --gid 1000 copperchat \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash copperchat
COPY smoke_ping.py /opt/copperchat/smoke_ping.py
USER copperchat
WORKDIR /home/copperchat
# Global library tables: byte copies of the stock tables shipped by the library
# repos (what KiCad's first-run dialog does), placed in the versioned config dir.
RUN ver="$(kicad-cli version | grep -oE '^[0-9]+\.[0-9]+')" \
 && mkdir -p "${KICAD_CONFIG_HOME}/${ver}" \
 && cp /usr/share/kicad/template/sym-lib-table /usr/share/kicad/template/fp-lib-table "${KICAD_CONFIG_HOME}/${ver}/"
ARG RECIPE=local
LABEL org.opencontainers.image.source="https://github.com/peterus/copperchat" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.revision="${KICAD_COMMIT}" \
      io.copperchat.kipy-revision="${KIPY_REV}" \
      io.copperchat.recipe="${RECIPE}"
CMD ["kicad-cli", "version", "--format", "about"]
