"""Q7: external change, kill -9, SIGSTOP hang, startup, parallel servers, perms, locks."""

import os
import shutil
import signal
import stat
import time

from harness import Recorder, Server, fresh_fixture, main_guard, require_kicad


def locks(proj) -> list[str]:
    return sorted(n for n in os.listdir(proj) if n.endswith(".lck"))


def main() -> int:
    require_kicad()
    from kipy.errors import ApiError

    rec = Recorder("q07_lifecycle")
    starts, opens = [], []
    for _ in range(3):
        proj = fresh_fixture("ecc83")
        before = set(os.listdir(proj))
        with Server(proj) as srv:
            starts.append(round(srv.startup_s, 2))
            opens.append({k: round(v, 2) for k, v in srv.open_s.items()})
            created = sorted(set(os.listdir(proj)) - before)
            rec.note("files_created_in_project", created)
            rec.check("lock_files_created", any(n.endswith(".lck") for n in created), created)
            mode = stat.S_IMODE(srv.socket.stat().st_mode)
            rec.note("socket_mode", oct(mode))
            rec.note("socket_dir_mode", oct(stat.S_IMODE(srv.socket.parent.stat().st_mode)))
            rec.check("socket_not_world_writable", not mode & 0o002, oct(mode))
        rec.note("locks_after_sigterm", locks(proj))
        rec.note("socket_after_sigterm", srv.socket.exists())
    rec.note("startup_s", starts)
    rec.note("open_document_s", opens)
    rec.check("startup_under_15s", max(starts) < 15, starts)

    # A second server on a project that is already open (lock files present)
    proj = fresh_fixture("ecc83")
    with Server(proj) as a:
        try:
            with Server(proj) as b:
                n = len(b.client().get_schematic().get_symbols())
                rec.note("second_server_same_project", f"opened anyway ({n} symbols)")
        except Exception as e:  # noqa: BLE001
            rec.note("second_server_same_project", f"refused: {type(e).__name__}: {e}"[:400])
        rec.note("locks_with_two_servers", locks(proj))
        a.client().ping()

    # external change: replace the sch on disk with another KiCad-written schematic
    proj = fresh_fixture("ecc83")
    with Server(proj) as srv:
        sch = srv.client().get_schematic()
        n0 = len(sch.get_symbols())
        f = next(proj.glob("*.kicad_sch"))
        other = fresh_fixture("complex_hierarchy")
        shutil.copy2(next(other.glob("complex_hierarchy.kicad_sch")), f)  # whole-file replace
        time.sleep(2)
        n1 = len(sch.get_symbols())
        rec.note("auto_reload_on_external_change", n1 != n0)
        try:
            t0 = time.monotonic()
            sch.revert()
            dt = time.monotonic() - t0
            n2 = len(sch.get_symbols())
            rec.check("revert_reloads_from_disk", n2 != n0, f"{n0} -> {n1} -> {n2} ({dt:.2f}s)")
        except ApiError as e:
            rec.check("revert_reloads_from_disk", False, e)
        try:
            board = srv.client().get_board()
            board.revert()
            rec.note("board_revert", f"ok, {len(board.get_footprints())} footprints")
        except Exception as e:  # noqa: BLE001
            rec.note("board_revert", f"{type(e).__name__}: {e}")

    # kill -9: detection time + restart time
    proj = fresh_fixture("ecc83")
    srv = Server(proj).__enter__()
    k = srv.client(timeout_ms=2000)
    srv.proc.send_signal(signal.SIGKILL)
    srv.proc.wait(10)
    t0 = time.monotonic()
    try:
        k.ping()
        rec.check("kill9_detected", False, "ping succeeded after SIGKILL")
    except Exception as e:  # noqa: BLE001
        rec.check(
            "kill9_detected", True, f"{type(e).__name__} after {time.monotonic() - t0:.2f}s: {e}"
        )
    rec.note("stale_socket_left", srv.socket.exists())
    rec.note("locks_after_kill9", locks(proj))
    t1 = time.monotonic()
    try:
        srv2 = Server(proj, socket=srv.socket).__enter__()
        n = len(srv2.client().get_schematic().get_symbols())
        rec.check("restart_same_socket", n > 0, f"{time.monotonic() - t1:.2f}s incl. open, {n} sym")
        srv2.stop()
    except Exception as e:  # noqa: BLE001
        rec.check("restart_same_socket", False, f"{type(e).__name__}: {e}"[:400])

    # SIGSTOP hang: does a call time out (not block forever)?
    # (a) client already connected (the KicadSession case): recv_timeout applies.
    srv = Server(fresh_fixture("ecc83")).__enter__()
    k = srv.client(timeout_ms=2000)
    k.ping()
    os.kill(srv.proc.pid, signal.SIGSTOP)
    t0 = time.monotonic()
    try:
        k.ping()
        rec.check("hang_detected", False, "ping returned while stopped")
    except Exception as e:  # noqa: BLE001
        dt = time.monotonic() - t0
        rec.check("hang_detected", dt < 5, f"{type(e).__name__} after {dt:.2f}s: {e}")
    # (b) fresh, not yet connected client: kipy dials lazily with block_on_dial=True,
    # which is not bounded by timeout_ms.
    k_new = srv.client(timeout_ms=2000)
    t0 = time.monotonic()
    try:
        k_new.ping()
        rec.note("hang_fresh_client_dial", "ping returned while stopped")
    except Exception as e:  # noqa: BLE001
        rec.note(
            "hang_fresh_client_dial", f"{type(e).__name__} after {time.monotonic() - t0:.2f}s: {e}"
        )
    os.kill(srv.proc.pid, signal.SIGCONT)
    time.sleep(2)
    for label, client in (("same_client", k), ("new_client", srv.client(timeout_ms=2000))):
        try:
            client.ping()
            rec.note(f"recovers_after_sigcont_{label}", True)
        except Exception as e:  # noqa: BLE001
            rec.note(f"recovers_after_sigcont_{label}", f"no: {type(e).__name__}: {e}")
    srv.stop()

    # two servers in parallel on different projects/sockets
    with Server(fresh_fixture("ecc83")) as a, Server(fresh_fixture("complex_hierarchy")) as b:
        ka, kb = a.client(), b.client()
        na, nb = len(ka.get_schematic().get_symbols()), len(kb.get_schematic().get_symbols())
        rec.check("two_parallel_servers", na > 0 and nb > 0 and na != nb, f"{na} vs {nb}")
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
