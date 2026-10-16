import sys
from pathlib import Path

from copperchat.kicad_session.launcher import KicadCliLauncher
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
