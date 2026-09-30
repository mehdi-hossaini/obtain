"""Run the public CLI against a fixed cohort inside the disposable live NixOS VM."""

import collections
import base64
import configparser
import datetime
import json
import os
from pathlib import Path
import pty
import re
import resource
import select
import shlex
import signal
import shutil
import struct
import subprocess
import sys
import termios
import time
import traceback
import fcntl

# Crash status and stderr are enough for these probes; do not write huge GUI core dumps.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

COHORT, OUTPUT = map(Path, sys.argv[1:3])
OUTPUT.mkdir(parents=True, exist_ok=True)
ROWS = json.loads(COHORT.read_text())
CASE_PREFIX = os.environ.get("OBTAIN_CASE_PREFIX") or "repo"
assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", CASE_PREFIX)
REPORT = {
    "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "requested": len(ROWS),
    "authentication": "none",
    "environment": "NixOS x86_64, unprivileged user, Xvfb/Openbox, software rendering",
    "limits": {
        "lock_seconds": 180,
        "install_seconds": 600,
        "gui_seconds": 25,
        "cli_seconds": 30,
        "pty_seconds": 35,
    },
    "results": [],
}
# Resume a stopped VM run without discarding evidence from completed cases.
if (OUTPUT / "report.json").exists():
    REPORT = json.loads((OUTPUT / "report.json").read_text())
    interrupted = [r for r in REPORT["results"] if r["status"] == "running"]
    REPORT.setdefault("interrupted_attempts", []).extend(interrupted)
    REPORT["results"] = [r for r in REPORT["results"] if r["status"] != "running"]
    for item in interrupted:
        old = OUTPUT / item["name"]
        if old.exists():
            old.rename(OUTPUT / f"{item['name']}-interrupted-{int(time.time())}")
BASE = dict(
    os.environ,
    DISPLAY=":0",
    XDG_RUNTIME_DIR="/run/user/1000",
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    LIBGL_ALWAYS_SOFTWARE="1",
    QT_QPA_PLATFORM="xcb",
)
for key in ("GITHUB_TOKEN", "GH_TOKEN"):
    BASE.pop(key, None)


def save():
    REPORT["counts"] = dict(collections.Counter(r["status"] for r in REPORT["results"]))
    REPORT["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tmp = OUTPUT / "report.tmp"
    tmp.write_text(json.dumps(REPORT, indent=2) + "\n")
    tmp.replace(OUTPUT / "report.json")


def command(args, env, log, timeout=60):
    started = time.monotonic()
    with log.open("w") as stream:
        stream.write("$ " + shlex.join(map(str, args)) + "\n")
        stream.flush()
        proc = subprocess.Popen(
            list(map(str, args)),
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            cwd=env.get("HOME"),
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        stream.write(f"\n[exit={proc.returncode}, timeout={timed_out}]\n")
    return {
        "exit": proc.returncode,
        "timeout": timed_out,
        "seconds": round(time.monotonic() - started, 2),
        "log": str(log.relative_to(OUTPUT)),
    }


def failure(result, log, stage):
    text = log.read_text(errors="replace")
    if result["timeout"]:
        return stage + "_timeout"
    if (
        "rate-limited" in text
        or "API rate limit exceeded" in text
        or "API rate limit reached" in text
    ):
        return "rate_limited"
    if (
        "Assets: none" in text
        or "No AppImage" in text
        or "No eligible" in text
        or "No matching" in text
        or "No supported Linux release file" in text
        or "No public repository or supported GitHub release found" in text
        or "not found. Only public" in text
    ):
        return "unsupported_or_missing_release"
    if "Could not choose an executable" in text:
        return "program_selection_required"
    if "Choose an asset" in text or "Use --asset" in text:
        return "asset_selection_required"
    return stage + "_failed"


def functional_status(startup_status, checks):
    if (
        startup_status in ("cli_startup_passed", "gui_startup_passed")
        and checks
        and not all(checks.values())
    ):
        return "functional_probe_failed"
    return startup_status


def post_install_status(startup_status, stages):
    if startup_status not in ("cli_startup_passed", "gui_startup_passed"):
        return startup_status
    for label in ("info", "doctor", "check"):
        stage = stages.get(label, {})
        if stage.get("skipped"):
            continue
        if stage.get("exit") != 0 or stage.get("timeout"):
            return label + "_failed"
    return startup_status


def gui_status(stable, exit_before_cleanup):
    if stable:
        return "gui_startup_passed"
    return "gui_observation_timeout" if exit_before_cleanup is None else "launch_failed"


def output_checks(log, expected, rejected):
    if not expected and not rejected:
        return {}
    output = log.read_text(errors="replace")
    checks = {"output": expected in output} if expected else {}
    for marker in rejected:
        checks["absent_output:" + marker] = marker not in output
    return checks


def reset_expected_outputs(home, directory, expected_files):
    """Keep outputs from an interrupted attempt, then require fresh results."""
    for filename in expected_files:
        target = home / filename
        if not (target.is_file() or target.is_symlink()):
            continue
        prior = directory / "prior-attempt-outputs" / filename
        prior.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            (prior.parent / (prior.name + ".symlink")).write_text(os.readlink(target))
        else:
            shutil.copyfile(target, prior)
        target.unlink()


def links_removed(*links):
    return all(not path.exists() and not path.is_symlink() for path in links)


def process_details(group_id):
    def proc_files(path):
        fields = {}
        for filename in ("status", "wchan", "syscall", "stack"):
            try:
                fields[filename] = (path / filename).read_text(errors="replace")[:16384]
            except OSError as error:
                fields[filename] = str(error)
        return fields

    details = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) != group_id:
                continue
        except (OSError, ProcessLookupError):
            continue
        fields = proc_files(entry)
        try:
            task_entries = list((entry / "task").iterdir())[:128]
            fd_entries = list((entry / "fd").iterdir())[:128]
        except OSError:
            task_entries, fd_entries = [], []
        fields["tasks"] = {
            task.name: proc_files(task) for task in task_entries if task.name.isdigit()
        }
        fds = {}
        for fd in fd_entries:
            try:
                fds[fd.name] = os.readlink(fd)
            except OSError as error:
                fds[fd.name] = str(error)
        fields["fds"] = fds
        details[entry.name] = fields
    return details


def windows():
    result = subprocess.run(
        ["xprop", "-root", "_NET_CLIENT_LIST"],
        env=BASE,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return set(re.findall(r"0x[0-9a-f]+", result.stdout)) - {"0x0"}


def gui_probe(
    executable, arguments, env, directory, actions=(), expected_processes=(), seconds=25
):
    before = windows()
    started = time.monotonic()
    observations = []
    pending = list(actions)
    performed = []
    with (directory / "launch.log").open("w") as log:
        log.write("$ " + shlex.join([executable, *arguments]) + "\n")
        log.flush()
        proc = subprocess.Popen(
            [executable, *arguments],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=env.get("HOME"),
        )
        try:
            while time.monotonic() - started < seconds:
                while pending and time.monotonic() - started >= pending[0]["seconds"]:
                    action = pending.pop(0)
                    outcome = command(
                        [
                            "xdotool",
                            *[
                                arg.replace("{home}", env["HOME"])
                                for arg in action["arguments"]
                            ],
                        ],
                        env,
                        directory / f"gui-action-{len(performed) + 1}.log",
                        timeout=5,
                    )
                    performed.append(dict(action, result=outcome))
                current = windows() - before
                visible = []
                for wid in sorted(current):
                    info = subprocess.run(
                        ["xwininfo", "-id", wid],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if "IsViewable" in info.stdout:
                        visible.append(wid)
                observations.append(
                    {
                        "seconds": round(time.monotonic() - started, 1),
                        "visible_windows": visible,
                        "exit": proc.poll(),
                        "window_details": {
                            wid: subprocess.run(
                                ["xprop", "-id", wid, "WM_CLASS", "_NET_WM_NAME"],
                                env=env,
                                capture_output=True,
                                text=True,
                                timeout=5,
                            ).stdout[:2000]
                            for wid in visible
                        },
                    }
                )
                if proc.poll() is not None and not visible:
                    break
                time.sleep(1)
            stable = len(observations) >= 5 and all(
                o["visible_windows"] for o in observations[-5:]
            )
            if observations and observations[-1]["visible_windows"]:
                subprocess.run(
                    [
                        "import",
                        "-display",
                        ":0",
                        "-window",
                        "root",
                        str(directory / "startup.png"),
                    ],
                    env=env,
                    stdout=log,
                    stderr=log,
                    timeout=15,
                )
            process_checks = {}
            if expected_processes:
                commands = []
                for entry in Path("/proc").iterdir():
                    if entry.name.isdigit():
                        try:
                            if entry.stat().st_uid == os.getuid():
                                commands.append(
                                    (entry / "cmdline")
                                    .read_bytes()
                                    .replace(b"\0", b" ")
                                    .decode(errors="replace")
                                )
                        except (OSError, ProcessLookupError):
                            pass
                for expected in expected_processes:
                    fragment = expected.replace("{home}", env["HOME"])
                    matches = [line for line in commands if fragment in line]
                    process_checks[expected] = bool(matches)
                    (directory / "processes.json").write_text(
                        json.dumps(commands, indent=2) + "\n"
                    )
            if not stable:
                (directory / "process-details.json").write_text(
                    json.dumps(process_details(proc.pid), indent=2) + "\n"
                )
                for label, args in (
                    ("x-tree", ["xwininfo", "-root", "-tree"]),
                    (
                        "x-client-list",
                        [
                            "xprop",
                            "-root",
                            "_NET_CLIENT_LIST",
                            "_NET_CLIENT_LIST_STACKING",
                        ],
                    ),
                    ("wmctrl", ["wmctrl", "-l"]),
                    (
                        "process-snapshot",
                        ["ps", "-eo", "pid,ppid,pgid,stat,wchan:25,args"],
                    ),
                ):
                    command(args, env, directory / (label + ".log"), timeout=10)
                subprocess.run(
                    [
                        "import",
                        "-display",
                        ":0",
                        "-window",
                        "root",
                        str(directory / "no-window.png"),
                    ],
                    env=env,
                    stdout=log,
                    stderr=log,
                    timeout=15,
                )
            return {
                "status": gui_status(stable, proc.poll()),
                "exit_before_cleanup": proc.poll(),
                "observations": observations,
                "actions": performed,
                "process_checks": process_checks,
                "log": str((directory / "launch.log").relative_to(OUTPUT)),
            }
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            # Close any remaining top-level windows from this app, never host windows.
            for wid in windows() - before:
                subprocess.run(
                    ["wmctrl", "-ic", wid], env=env, capture_output=True, timeout=5
                )


def pty_probe(executable, arguments, inputs, env, directory):
    """Record a real terminal session and bounded input, then reap its process group."""
    started = time.monotonic()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(env["HOME"])
        os.execvpe(
            str(executable),
            [str(executable), *arguments],
            dict(env, TERM="xterm-256color"),
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    status = None
    pending = list(inputs)
    next_input = started + 6
    raw = bytearray()
    with (directory / "terminal.cast").open("w") as cast:
        cast.write(
            json.dumps(
                {
                    "version": 2,
                    "width": 100,
                    "height": 30,
                    "command": shlex.join([str(executable), *arguments]),
                }
            )
            + "\n"
        )
        try:
            while time.monotonic() - started < 35:
                if pending and time.monotonic() >= next_input:
                    keys = pending.pop(0)
                    os.write(fd, keys.encode())
                    cast.write(
                        json.dumps([round(time.monotonic() - started, 3), "i", keys])
                        + "\n"
                    )
                    next_input = time.monotonic() + 0.5
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    raw.extend(chunk)
                    cast.write(
                        json.dumps(
                            [
                                round(time.monotonic() - started, 3),
                                "o",
                                chunk.decode(errors="replace"),
                            ]
                        )
                        + "\n"
                    )
                    cast.flush()
                    if b"\x1b[6n" in chunk:
                        os.write(fd, b"\x1b[1;1R")
                done, code = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = code
                    break
        finally:
            if status is None:
                done, code = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = code
                else:
                    os.killpg(pid, signal.SIGKILL)
                    _, status = os.waitpid(pid, 0)
            os.close(fd)
    exit_code = os.waitstatus_to_exitcode(status)
    (directory / "launch.log").write_bytes(raw)
    return {
        "status": "cli_startup_passed" if exit_code == 0 else "launch_failed",
        "exit": exit_code,
        "seconds": round(time.monotonic() - started, 2),
        "log": str((directory / "launch.log").relative_to(OUTPUT)),
        "recording": str((directory / "terminal.cast").relative_to(OUTPUT)),
    }


def test(index, spec):
    repo = spec["repository"]
    name = f"{CASE_PREFIX}-{index:03d}"
    directory = OUTPUT / name
    directory.mkdir(exist_ok=True)
    home = Path("/home/alice/live-apps") / name
    home.mkdir(parents=True, exist_ok=True)
    reset_expected_outputs(home, directory, spec.get("expected_files", {}))
    for filename, contents in spec.get("files", {}).items():
        target = home / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents.replace("{home}", str(home)))
    for filename, contents in spec.get("files_base64", {}).items():
        target = home / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(contents, validate=True))
    arguments = [arg.replace("{home}", str(home)) for arg in spec["arguments"]]
    env = dict(
        BASE,
        **spec.get("environment", {}),
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
    )
    (home / ".cache").mkdir(exist_ok=True)
    cache = Path("/home/alice/.cache/nix")
    cache.mkdir(parents=True, exist_ok=True)
    if not (home / ".cache/nix").exists():
        (home / ".cache/nix").symlink_to(cache, target_is_directory=True)
    if (home / ".config/obtain/sources.json").exists():
        command(["obtain", "remove", name], env, directory / "resume-cleanup.log")
    result = dict(spec, index=index, name=name, status="running", stages={})
    REPORT["results"].append(result)
    save()
    print(f"[{index:03d}/{len(ROWS)}] {repo}: selecting", flush=True)
    args = [
        "obtain",
        "add",
        f"https://github.com/{repo}",
        "--name",
        name,
    ]
    if spec["type"] != "auto":
        args += ["--type", spec["type"]]
    direct = spec.get("direct_add", False)
    if not direct:
        args.append("--track-only")
    for key in ("asset", "program"):
        if spec.get(key):
            args += ["--" + key, spec[key]]
    if spec.get("strip_components"):
        args += ["--strip-components", str(spec["strip_components"])]
    if spec.get("prereleases"):
        args.append("--prereleases")
    restored = spec.get("locked_record") is not None
    stage = "restore_lock" if restored else ("add" if direct else "selection")
    if restored:
        direct = False
        config = home / ".config/obtain"
        config.mkdir(parents=True, exist_ok=True)
        for filename, record in (
            ("sources.json", spec["locked_source"]),
            ("lock.json", dict(spec["locked_record"], name=name)),
        ):
            (config / filename).write_text(
                json.dumps({"schema": 1, "apps": {name: record}}, indent=2) + "\n"
            )
        (directory / "select.log").write_text(
            "Restored captured source and release lock; only the local app name changed. "
            "No GitHub release discovery was performed.\n"
        )
        selection = {"exit": 0, "timeout": False, "restored": True}
    else:
        selection = command(
            args, env, directory / "select.log", timeout=780 if direct else 180
        )
    result["stages"][stage] = selection
    if selection["exit"]:
        result["status"] = failure(selection, directory / "select.log", stage)
        save()
        return
    lock_path = home / ".config/obtain/lock.json"
    lock = json.loads(lock_path.read_text())["apps"][name]
    result["lock"] = lock
    save()
    print(f"[{index:03d}/{len(ROWS)}] {repo}: installing {lock['version']}", flush=True)
    if not direct:
        install = command(
            ["obtain", "install", name], env, directory / "install.log", timeout=600
        )
        result["stages"]["installation"] = install
        if install["exit"]:
            result["status"] = failure(install, directory / "install.log", "install")
            save()
            return
    data = home / ".local/share"
    profile = data / "obtain/profiles" / name
    launcher = data / "obtain/bin" / name
    manifest = json.loads((profile / "share/obtain/manifest.json").read_text())
    assert manifest == lock, "Installed manifest differs from CLI lock"
    assert launcher.is_symlink() and os.access(launcher, os.X_OK), (
        "Missing executable launcher"
    )
    desktop = data / "applications" / f"obtain-{name}.desktop"
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read(desktop)
    desktop_exec = shlex.split(parsed["Desktop Entry"]["Exec"])
    assert len(desktop_exec) == 1 and desktop_exec[0].startswith("/nix/store/"), (
        "Invalid desktop Exec"
    )
    assert os.access(desktop_exec[0], os.X_OK), "Desktop target not executable"
    result["installed_store_path"] = str(profile.resolve())
    result["installed_verified"] = True
    result["stages"]["info"] = command(
        ["obtain", "info", name], env, directory / "info.log"
    )
    result["stages"]["doctor"] = command(
        ["obtain", "doctor", name, "--json"], env, directory / "doctor.log"
    )
    if not restored:
        result["stages"]["check"] = command(
            ["obtain", "check", name], env, directory / "check.log"
        )
    else:
        result["stages"]["check"] = {
            "skipped": "Captured-lock runtime probe; no metadata requests"
        }
    print(f"[{index:03d}/{len(ROWS)}] {repo}: launching", flush=True)
    if spec["probe"] == "gui":
        result["runtime"] = gui_probe(
            desktop_exec[0],
            arguments,
            env,
            directory,
            spec.get("gui_actions", []),
            spec.get("expected_processes", []),
            spec.get("probe_seconds", 25),
        )
        result["status"] = result["runtime"]["status"]
    elif spec["probe"] == "pty":
        result["runtime"] = pty_probe(
            launcher, arguments, spec["input"], env, directory
        )
        result["status"] = result["runtime"]["status"]
    else:
        runtime = command(
            [launcher, *arguments],
            env,
            directory / "launch.log",
            timeout=spec.get("probe_seconds", 30),
        )
        result["runtime"] = runtime
        result["status"] = (
            "cli_startup_passed" if runtime["exit"] == 0 else "launch_failed"
        )
    checks = dict(result["runtime"].get("process_checks", {}))
    for probe in spec.get("additional_probes", []):
        label = probe["label"]
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", label)
        extra = command(
            [
                launcher,
                *[arg.replace("{home}", str(home)) for arg in probe["arguments"]],
            ],
            env,
            directory / (label + ".log"),
            timeout=probe.get("seconds", 60),
        )
        result.setdefault("probe_results", {})[label] = extra
        if probe.get("required", True):
            checks[label] = extra["exit"] == 0 and not extra["timeout"]
    checks.update(
        output_checks(
            directory / "launch.log",
            spec.get("expected_output"),
            spec.get("reject_output", []),
        )
    )
    for filename, expected in spec.get("expected_files", {}).items():
        target = home / filename
        checks[filename] = target.is_file() and target.stat().st_size > 0
        if checks[filename] and expected is not None:
            checks[filename] = expected in target.read_text(errors="replace")
        if target.is_file():
            destination = directory / "outputs" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, destination)
    result["functional_checks"] = checks
    result["status"] = functional_status(result["status"], checks)
    result["status"] = post_install_status(result["status"], result["stages"])
    capture_state(result)
    removal = command(["obtain", "remove", name], env, directory / "remove.log")
    result["stages"]["removal"] = removal
    result["removal_verified"] = removal["exit"] == 0 and links_removed(
        launcher, desktop
    )
    if not result["removal_verified"]:
        result["status"] = "removal_failed"
    save()


def capture_state(result):
    """Keep failed install state and app-owned logs alongside command output."""
    name = result.get("name", f"{CASE_PREFIX}-{result['index']:03d}")
    directory = OUTPUT / name
    home = Path("/home/alice/live-apps") / name
    paths = [
        ".config/obtain/sources.json",
        ".config/obtain/lock.json",
        ".cache/obtain/last-command.log",
        *result.get("capture_files", []),
    ]
    for filename in paths:
        source = home / filename
        if source.is_file():
            destination = directory / "state" / filename
            # Removal writes empty maps. Keep the state used for installation.
            if filename.startswith(".config/obtain/") and destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


if __name__ == "__main__":
    save()
    for number, row in enumerate(ROWS, 1):
        if any(r["index"] == number for r in REPORT["results"]):
            continue
        started = time.monotonic()
        try:
            test(number, row)
        except Exception:
            detail = traceback.format_exc()
            print(detail, flush=True)
            if REPORT["results"] and REPORT["results"][-1]["index"] == number:
                REPORT["results"][-1].update(status="harness_error", error=detail)
            else:
                REPORT["results"].append(
                    dict(row, index=number, status="harness_error", error=detail)
                )
        try:
            capture_state(REPORT["results"][-1])
        except OSError:
            REPORT["results"][-1].update(
                status="harness_error", capture_error=traceback.format_exc()
            )
        REPORT["results"][-1]["seconds"] = round(time.monotonic() - started, 2)
        save()
        print(
            f"[{number:03d}/{len(ROWS)}] {row['repository']}: {REPORT['results'][-1]['status']}",
            flush=True,
        )
    REPORT["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save()
    (OUTPUT / "done").write_text("complete\n")
