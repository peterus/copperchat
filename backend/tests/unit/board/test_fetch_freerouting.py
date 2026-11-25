"""scripts/fetch-freerouting.sh: checksum-pinned Freerouting JAR + Temurin JRE (P6a Task 1).

The script runs against a temp lock whose URLs are ``file://`` fixtures built in tmp_path.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "fetch-freerouting.sh"
REAL_LOCK = REPO / "docker" / "freerouting.lock"
JAVA = b"#!/bin/sh\necho 'openjdk version \"25.0.1\" 2025-10-21 LTS' >&2\n"
JAR_NAME = "freerouting-2.5.0.jar"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tarball(java: bytes = JAVA) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, mode in [
            ("jdk-25/bin/java", java, 0o755),
            ("jdk-25/lib/modules", b"modules" * 100, 0o644),
            ("jdk-25/legal/java.base/LICENSE", b"GPLv2 + CPE\n", 0o644),
            ("jdk-25/NOTICE", b"notice\n", 0o644),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class Fixture:
    tmp: Path
    dest: Path
    lock: Path
    jar_src: Path
    jre_src: Path
    jar: bytes
    jre: bytes

    def write_lock(self, *, jar_sha: str | None = None, jre_sha: str | None = None) -> None:
        self.lock.write_text(
            "FREEROUTING_VERSION=2.5.0\n"
            f"FREEROUTING_URL={self.jar_src.as_uri()}\n"
            f"FREEROUTING_SHA256={jar_sha or _sha(self.jar)}\n"
            f"TEMURIN_URL={self.jre_src.as_uri()}\n"
            f"TEMURIN_SHA256={jre_sha or _sha(self.jre)}\n"
        )

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "FREEROUTING_LOCK": str(self.lock)}
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.dest)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    src = tmp_path / "src"
    src.mkdir()
    jar = b"PK\x03\x04 fake freerouting jar " * 64
    jre = _tarball()
    f = Fixture(
        tmp=tmp_path,
        dest=tmp_path / "dest",
        lock=tmp_path / "freerouting.lock",
        jar_src=src / JAR_NAME,
        jre_src=src / "OpenJDK25U-jre_x64_linux_hotspot_25.0.1_8.tar.gz",
        jar=jar,
        jre=jre,
    )
    f.jar_src.write_bytes(jar)
    f.jre_src.write_bytes(jre)
    f.write_lock()
    return f


def _ok(r: subprocess.CompletedProcess[str]) -> None:
    assert r.returncode == 0, r.stderr


def test_fetch_ok(fx: Fixture) -> None:
    r = fx.run()
    _ok(r)
    java = fx.dest / "jre" / "bin" / "java"
    assert java.read_bytes() == JAVA
    assert os.access(java, os.X_OK)
    assert (fx.dest / JAR_NAME).read_bytes() == fx.jar
    assert (fx.dest / ".lock").read_bytes() == fx.lock.read_bytes()
    assert (fx.dest / ".manifest.sha256").is_file()
    assert not (fx.dest / ".dl.tmp").exists()
    assert (fx.dest / ".dl" / JAR_NAME).read_bytes() == fx.jar
    assert (fx.dest / ".dl" / fx.jre_src.name).read_bytes() == fx.jre
    assert subprocess.run(
        [str(java)], capture_output=True, text=True, check=True
    ).stderr.startswith('openjdk version "25')


def test_jar_hash_mismatch_refused(fx: Fixture) -> None:
    wrong = "0" * 64
    fx.write_lock(jar_sha=wrong)
    r = fx.run()
    assert r.returncode != 0
    assert "freerouting" in r.stderr
    assert wrong in r.stderr and _sha(fx.jar) in r.stderr
    assert not fx.dest.exists() or not any(fx.dest.iterdir())


def test_jre_hash_mismatch_refused(fx: Fixture) -> None:
    wrong = "f" * 64
    fx.write_lock(jre_sha=wrong)
    r = fx.run()
    assert r.returncode != 0
    assert fx.jre_src.name in r.stderr
    assert wrong in r.stderr and _sha(fx.jre) in r.stderr
    assert not (fx.dest / ".manifest.sha256").exists()
    assert not (fx.dest / "jre").exists()
    assert not (fx.dest / ".dl" / fx.jre_src.name).exists()
    assert not (fx.dest / ".dl.tmp").exists()


def test_second_run_up_to_date(fx: Fixture) -> None:
    _ok(fx.run())
    jar = fx.dest / JAR_NAME
    before = jar.stat().st_mtime_ns
    r = fx.run()
    _ok(r)
    assert "up to date" in r.stdout
    assert jar.stat().st_mtime_ns == before


def test_lock_change_refetches(fx: Fixture) -> None:
    _ok(fx.run())
    fx.jar = b"PK\x03\x04 another freerouting build " * 64
    fx.jar_src.write_bytes(fx.jar)
    fx.write_lock()
    r = fx.run()
    _ok(r)
    assert "up to date" not in r.stdout
    assert (fx.dest / JAR_NAME).read_bytes() == fx.jar
    assert (fx.dest / ".lock").read_bytes() == fx.lock.read_bytes()


def test_tampered_cached_download_refused(fx: Fixture) -> None:
    _ok(fx.run())
    cached = fx.dest / ".dl" / JAR_NAME
    data = bytearray(cached.read_bytes())
    data[10] ^= 0xFF
    cached.write_bytes(bytes(data))
    r = fx.run()
    _ok(r)
    assert JAR_NAME in r.stderr
    assert _sha(bytes(data)) in r.stderr and _sha(fx.jar) in r.stderr
    assert _sha((fx.dest / JAR_NAME).read_bytes()) == _sha(fx.jar)
    assert cached.read_bytes() == fx.jar


def test_tampered_cache_and_bad_source_refused(fx: Fixture) -> None:
    _ok(fx.run())
    cached = fx.dest / ".dl" / JAR_NAME
    tampered = cached.read_bytes() + b"x"
    cached.write_bytes(tampered)
    other = b"not the pinned jar"
    fx.jar_src.write_bytes(other)
    r = fx.run()
    assert r.returncode != 0
    assert _sha(tampered) in r.stderr
    assert _sha(other) in r.stderr
    assert _sha(fx.jar) in r.stderr
    assert not (fx.dest / ".manifest.sha256").exists()
    assert not cached.exists()


def test_tampered_extracted_file_reinstalled(fx: Fixture) -> None:
    _ok(fx.run())
    java = fx.dest / "jre" / "bin" / "java"
    with java.open("ab") as fh:
        fh.write(b"\n")
    r = fx.run()
    _ok(r)
    assert "up to date" not in r.stdout
    assert java.read_bytes() == JAVA
    _ok(fx.run())


def test_extra_file_in_tree_reinstalled(fx: Fixture) -> None:
    _ok(fx.run())
    planted = fx.dest / "jre" / "lib" / "planted.so"
    planted.write_bytes(b"evil")
    r = fx.run()
    _ok(r)
    assert "up to date" not in r.stdout
    assert not planted.exists()


def test_unsafe_tar_member_refused(fx: Fixture) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("jdk-25/../../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    fx.jre = buf.getvalue()
    fx.jre_src.write_bytes(fx.jre)
    fx.write_lock()
    r = fx.run()
    assert r.returncode != 0
    assert not (fx.tmp / "escape").exists()
    assert not (fx.dest / ".manifest.sha256").exists()


def test_manifest_covers_tree(fx: Fixture) -> None:
    _ok(fx.run())
    lines = (fx.dest / ".manifest.sha256").read_text().splitlines()
    listed = {}
    for line in lines:
        digest, name = line.split("  ", 1)
        listed[name] = digest
    files = {
        p.relative_to(fx.dest).as_posix()
        for p in (fx.dest / "jre").rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    assert files == {"jre/bin/java", "jre/lib/modules", "jre/legal/java.base/LICENSE", "jre/NOTICE"}
    assert set(listed) == files | {JAR_NAME}
    for name, digest in listed.items():
        assert digest == _sha((fx.dest / name).read_bytes())


def test_lock_format() -> None:
    entries = [
        line
        for line in REAL_LOCK.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pairs = dict(line.split("=", 1) for line in entries)
    assert len(entries) == len(pairs)
    assert set(pairs) == {
        "FREEROUTING_VERSION",
        "FREEROUTING_URL",
        "FREEROUTING_SHA256",
        "TEMURIN_URL",
        "TEMURIN_SHA256",
    }
    assert pairs["FREEROUTING_VERSION"] == "2.5.0"
    for key in ("FREEROUTING_SHA256", "TEMURIN_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", pairs[key]), key
    for key in ("FREEROUTING_URL", "TEMURIN_URL"):
        assert pairs[key].startswith("https://"), key
    assert pairs["FREEROUTING_URL"].endswith("/v2.5.0/freerouting-2.5.0.jar")
    assert "adoptium/temurin25-binaries" in pairs["TEMURIN_URL"]
