"""Q1: kicad-cli version, api-server CLI syntax, KiCad commit."""

import re

from harness import Recorder, main_guard, require_kicad, run_cli


def main() -> int:
    require_kicad()
    rec = Recorder("q01_cli")
    v = run_cli("version", "--format", "about", timeout=60)
    rec.check("version_runs", v.returncode == 0, v.stdout[-400:] or v.stderr[-400:])
    m = re.search(r"10\.99\.\d+[-.\w]*", v.stdout)
    rec.check("is_master_10_99", m is not None, m.group(0) if m else v.stdout[:200])
    h = run_cli("api-server", "--help", timeout=60)
    text = h.stdout + h.stderr
    rec.check("api_server_help", h.returncode == 0 and "api-server" in text, text)
    rec.check("has_socket_option", "--socket" in text, "look for --socket in help")
    rec.note("api_server_help_full", text)
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
