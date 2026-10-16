import sys
from pathlib import Path

from copperchat.kicad_session.launcher import (
    KICAD_SETTINGS_VERSION,
    KicadCliLauncher,
    kicad_cli_env,
    seed_library_tables,
)
from copperchat.projects import discover_projects


async def test_launcher_builds_command_line_and_logs(tmp_path: Path) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "print(' '.join(sys.argv[1:]), file=sys.stderr)\n"
        "print('env', os.environ['LANG'], os.environ['LC_ALL'], "
        "os.environ['KICAD_CONFIG_HOME'], file=sys.stderr)\n"
    )
    fake.chmod(0o755)
    proj_dir = tmp_path / "projects" / "demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "demo.kicad_pro").touch()  # filename marker only
    project = discover_projects(tmp_path / "projects")[0]
    config = tmp_path / "data" / "kicad-config"
    launcher = KicadCliLauncher(str(fake), tmp_path / "logs", config)
    proc = await launcher.start(project, Path("/tmp/copperchat/x.sock"))
    assert await proc.wait() == 0
    log = (tmp_path / "logs" / f"api-server-{project.id}.log").read_text()
    # Spike 1C: `kicad-cli api-server --socket SOCKET PROJECT.kicad_pro`
    assert f"api-server --socket /tmp/copperchat/x.sock {project.pro_file}" in log
    assert f"env C C {config}" in log
    assert config.is_dir()


def _fake_kicad_install(tmp_path: Path) -> Path:
    """bin/kicad-cli + share/kicad/template/{sym,fp}-lib-table, like the base image."""
    exe = tmp_path / "kicad" / "bin" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!{sys.executable}\n")
    exe.chmod(0o755)
    template = tmp_path / "kicad" / "share" / "kicad" / "template"
    template.mkdir(parents=True)
    (template / "sym-lib-table").write_text("(sym_lib_table)\n")
    (template / "fp-lib-table").write_text("(fp_lib_table)\n")
    return exe


def test_kicad_cli_env_seeds_library_tables(tmp_path: Path) -> None:
    exe = _fake_kicad_install(tmp_path)
    config = tmp_path / "data" / "kicad-config"
    env = kicad_cli_env(str(exe), config)
    assert env["LANG"] == env["LC_ALL"] == "C"
    assert env["KICAD_CONFIG_HOME"] == str(config)
    version_dir = config / KICAD_SETTINGS_VERSION
    assert (version_dir / "sym-lib-table").read_text() == "(sym_lib_table)\n"
    assert (version_dir / "fp-lib-table").read_text() == "(fp_lib_table)\n"


def test_seeding_keeps_existing_tables(tmp_path: Path) -> None:
    exe = _fake_kicad_install(tmp_path)
    config = tmp_path / "cfg"
    (config / KICAD_SETTINGS_VERSION).mkdir(parents=True)
    (config / KICAD_SETTINGS_VERSION / "fp-lib-table").write_text("custom\n")
    seed_library_tables(config, str(exe))
    assert (config / KICAD_SETTINGS_VERSION / "fp-lib-table").read_text() == "custom\n"
    assert (config / KICAD_SETTINGS_VERSION / "sym-lib-table").is_file()


def test_seeding_without_kicad_install_is_a_no_op(tmp_path: Path) -> None:
    seed_library_tables(tmp_path / "cfg", str(tmp_path / "missing-kicad-cli"))
    assert not (tmp_path / "cfg").exists()


async def test_launcher_seeds_config_home(tmp_path: Path) -> None:
    exe = _fake_kicad_install(tmp_path)
    proj_dir = tmp_path / "projects" / "demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "demo.kicad_pro").touch()  # filename marker only
    project = discover_projects(tmp_path / "projects")[0]
    config = tmp_path / "data" / "kicad-config"
    proc = await KicadCliLauncher(str(exe), tmp_path / "logs", config).start(
        project, Path("/tmp/copperchat/x.sock")
    )
    assert await proc.wait() == 0
    assert (config / KICAD_SETTINGS_VERSION / "sym-lib-table").is_file()
