"""Regressions for the live cohort's outcome and resume bookkeeping."""

import contextlib
import io
import json
import os
from pathlib import Path
import resource
import runpy
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import test_obtain as base


RUNNER = Path(__file__).resolve().parent / "live/run.py"


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cohort = self.root / "cohort.json"
        cohort.write_text("[]")
        with (
            patch.object(
                sys, "argv", [str(RUNNER), str(cohort), str(self.root / "out")]
            ),
            patch.object(resource, "setrlimit"),
        ):
            self.runner = runpy.run_path(str(RUNNER))

    def test_desktop_arguments_use_the_production_parser_and_expand_field_codes(self):
        command = self.runner["desktop_command"](
            {
                "Exec": '/nix/store/fixture/bin/app --open "two words" %U %% %c %k %i',
                "Name": r"Friendly\sApp",
                "Icon": "/nix/store/fixture/icon.png",
            },
            self.root / "app.desktop",
        )
        self.assertEqual(
            command,
            [
                "/nix/store/fixture/bin/app",
                "--open",
                "two words",
                "%",
                "Friendly App",
                str(self.root / "app.desktop"),
                "--icon",
                "/nix/store/fixture/icon.png",
            ],
        )

    def test_captured_lock_restores_the_original_recipe_into_a_fresh_home(self):
        cli = self.runner["cli"]
        home = self.root / "original-home"
        store = cli.Store()
        store.config = home / ".config/obtain"
        record = store.pin_recipe(base.record())
        source = base.source()
        cli.atomic_json(
            store.config / "sources.json", {"schema": 1, "apps": {"app": source}}
        )
        cli.atomic_json(
            store.config / "lock.json", {"schema": 1, "apps": {"app": record}}
        )
        row = {"index": 1, "name": "app", "lock": record}
        self.runner["capture_state"](row, home)
        snapshot = self.runner["captured_recipe"](
            self.root / "out", row, self.root / "absent-config"
        )
        fresh = self.root / "fresh-home/.config/obtain"
        self.runner["restore_locked_state"](fresh, "followup", source, record, snapshot)
        store.config = fresh
        build = store.build_recipe(
            dict(record, name="followup"), self.root / "recipe-build"
        )
        self.assertEqual(build.read_text(), snapshot["build.nix"])
        self.assertEqual(store.recipe_files(record["recipe_hash"]), snapshot)
        saved = json.loads((fresh / "lock.json").read_text())["apps"]["followup"]
        self.assertEqual(saved, dict(record, name="followup"))
        changed = dict(snapshot, **{"recipe.nix": "changed packaging"})
        with self.assertRaisesRegex(cli.Error, "does not match the locked hash"):
            self.runner["restore_locked_state"](
                fresh, "followup", source, record, changed
            )
        self.assertEqual(store.recipe_files(record["recipe_hash"]), snapshot)
        with self.assertRaisesRegex(ValueError, "locked_recipe"):
            self.runner["restore_locked_state"](
                self.root / "missing-config", "followup", source, record
            )

    def leader_with_descendant(self, backend, stay_alive=False):
        pidfile = self.root / f"{backend}.pid"
        child = (
            "import os,signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "signal.signal(signal.SIGHUP,signal.SIG_IGN)\n"
            f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        leader = self.root / f"{backend}.py"
        leader.write_text(
            "import signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
            f"while not Path({str(pidfile)!r}).exists(): time.sleep(0.01)\n"
            + ("time.sleep(30)\n" if stay_alive else "")
        )

        def cleanup():
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(cleanup)
        return leader, pidfile

    def assert_descendant_stopped(self, pidfile):
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = (
                    Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
                )
            except FileNotFoundError:
                return
            if state == "Z":
                return
            time.sleep(0.01)
        self.fail(f"Probe left descendant {pid} running")

    def test_successful_command_cleans_descendants_after_leader_exit(self):
        leader, pidfile = self.leader_with_descendant("command")
        result = self.runner["command"](
            [sys.executable, str(leader)],
            {"HOME": str(self.root)},
            self.root / "out/command.log",
        )
        self.assertEqual(result["exit"], 0)
        self.assert_descendant_stopped(pidfile)

    def test_timeout_cleans_descendants_and_cannot_pass_after_zero_exit(self):
        leader, pidfile = self.leader_with_descendant("timeout", stay_alive=True)
        result = self.runner["command"](
            [sys.executable, str(leader)],
            {"HOME": str(self.root)},
            self.root / "out/timeout.log",
            timeout=1,
        )
        self.assertEqual(result["exit"], 0)
        self.assertTrue(result["timeout"])
        self.assertEqual(self.runner["cli_status"](result), "launch_timeout")
        self.assert_descendant_stopped(pidfile)

    def test_zero_exit_selection_and_install_timeouts_stop_the_case(self):
        test = self.runner["test"]
        real_path = Path

        def local_path(value):
            if str(value).startswith("/home/alice/"):
                return self.root / str(value).removeprefix("/home/alice/")
            return real_path(value)

        for stage in ("selection", "install"):
            with self.subTest(stage=stage):
                calls = []

                def command(args, env, log, **kwargs):
                    calls.append(args[1])
                    log.write_text("Fake command completed with exit zero\n")
                    timed_out = stage == "selection" or args[1] == "install"
                    if not timed_out:
                        config = Path(env["XDG_CONFIG_HOME"]) / "obtain"
                        config.mkdir(parents=True, exist_ok=True)
                        (config / "lock.json").write_text(
                            json.dumps(
                                {
                                    "schema": 1,
                                    "apps": {
                                        "repo-001": dict(base.record(), name="repo-001")
                                    },
                                }
                            )
                        )
                    return {"exit": 0, "timeout": timed_out}

                with (
                    patch.dict(test.__globals__, command=command, Path=local_path),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    test(
                        1,
                        {
                            "repository": "owner/app",
                            "type": "auto",
                            "probe": "cli",
                            "arguments": [],
                        },
                    )
                row = self.runner["REPORT"]["results"][-1]
                self.assertEqual(row["status"], stage + "_timeout")
                self.assertEqual(
                    calls, ["add"] if stage == "selection" else ["add", "install"]
                )

    def test_gui_and_terminal_probes_clean_descendants_after_leader_exit(self):
        env = {"HOME": str(self.root)}
        for backend in ("gui", "pty"):
            with self.subTest(backend=backend):
                leader, pidfile = self.leader_with_descendant(backend)
                if backend == "pty":
                    result = self.runner["pty_probe"](
                        sys.executable, [str(leader)], [], env, self.root / "out"
                    )
                    self.assertEqual(result["exit"], 0)
                    self.assertFalse(result["timeout"])
                else:
                    probe = self.runner["gui_probe"]
                    with (
                        patch.dict(
                            probe.__globals__,
                            windows=lambda: set(),
                            command=lambda *a, **k: {},
                        ),
                        patch.object(
                            subprocess,
                            "run",
                            return_value=subprocess.CompletedProcess([], 0, "", ""),
                        ),
                    ):
                        probe(
                            sys.executable,
                            [str(leader)],
                            env,
                            self.root / "out",
                            seconds=2,
                        )
                self.assert_descendant_stopped(pidfile)

    def test_driver_rejects_final_error_when_done_races_with_shared_report_copy(self):
        source = (RUNNER.parent / "default.nix").read_text()
        start = source.index("        previous = -1")
        end = source.index("\n    finally:", start)
        code = compile(textwrap.dedent(source[start:end]), "live driver", "exec")
        root = self.root

        class Machine:
            shared_dir = root

            def succeed(self, command):
                if command.startswith("cat "):
                    return json.dumps(
                        {
                            "requested": 1,
                            "counts": {"harness_error": 1},
                            "results": [{"status": "harness_error"}],
                        }
                    )
                shared = self.shared_dir / "live"
                shared.mkdir(exist_ok=True)
                (shared / "report.json").write_text(
                    json.dumps(
                        {
                            "requested": 1,
                            "counts": {"running": 1},
                            "results": [{"status": "running"}],
                        }
                    )
                )

            def execute(self, command):
                return (0, "")

            def copy_from_machine(self, *args):
                pass

        with (
            self.assertRaisesRegex(AssertionError, "Harness errors invalidate"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(
                code,
                {
                    "machine": Machine(),
                    "guest_results": "/guest/results",
                    "shared_label": "live",
                    "result_label": "live",
                    "expected_count": 1,
                    "time": time,
                    "json": json,
                },
            )

    def test_failed_startup_is_not_hidden_by_missing_output(self):
        classify = self.runner["functional_status"]
        self.assertEqual(
            classify("launch_failed", {"render.png": False}), "launch_failed"
        )
        self.assertEqual(
            classify("cli_startup_passed", {"render.png": False}),
            "functional_probe_failed",
        )
        self.assertEqual(
            classify("gui_startup_passed", {"render.png": True}),
            "gui_startup_passed",
        )

    def test_failed_post_install_check_cannot_receive_startup_pass(self):
        classify = self.runner["post_install_status"]
        stages = {
            "info": {"exit": 0, "timeout": False},
            "doctor": {"exit": 1, "timeout": False},
            "check": {"exit": 0, "timeout": False},
        }
        self.assertEqual(classify("gui_startup_passed", stages), "doctor_failed")
        self.assertEqual(classify("launch_failed", stages), "launch_failed")
        stages["doctor"]["exit"] = 0
        stages["check"] = {"skipped": "Captured-lock probe"}
        self.assertEqual(classify("gui_startup_passed", stages), "gui_startup_passed")
        stages["info"]["timeout"] = True
        self.assertEqual(classify("cli_startup_passed", stages), "info_failed")

    def test_alive_gui_without_window_is_an_observation_timeout(self):
        status = self.runner["gui_status"]
        self.assertEqual(status(False, None), "gui_observation_timeout")
        self.assertEqual(status(False, 1), "launch_failed")
        self.assertEqual(status(True, None), "gui_startup_passed")

    def test_fatal_launch_log_rejects_stable_error_dialog(self):
        log = self.root / "launch.log"
        log.write_text("Process terminated. Couldn't find a valid ICU package.\n")
        checks = self.runner["output_checks"](
            log, None, ["Couldn't find a valid ICU package"]
        )
        self.assertFalse(checks["absent_output:Couldn't find a valid ICU package"])
        self.assertEqual(
            self.runner["functional_status"]("gui_startup_passed", checks),
            "functional_probe_failed",
        )

    def test_interrupted_attempt_outputs_are_preserved_and_reset(self):
        home = self.root / "home"
        directory = self.root / "case"
        output = home / "project/render.png"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"old render")
        link = home / "preview.png"
        link.symlink_to(output)
        link_target = os.readlink(link)

        self.runner["reset_expected_outputs"](
            home, directory, {"project/render.png": None, "preview.png": None}
        )

        self.assertFalse(output.exists())
        self.assertFalse(link.is_symlink())
        self.assertEqual(
            (directory / "prior-attempt-outputs/project/render.png").read_bytes(),
            b"old render",
        )
        self.assertEqual(
            (directory / "prior-attempt-outputs/preview.png.symlink").read_text(),
            link_target,
        )

    def test_missing_supported_release_is_classified_as_boundary(self):
        log = self.root / "select.log"
        log.write_text(
            "obtain: No supported Linux release file matches this repository and selection. "
            "Use 'obtain inspect URL' for details.\n"
        )
        self.assertEqual(
            self.runner["failure"]({"timeout": False}, log, "selection"),
            "unsupported_or_missing_release",
        )

    def test_dangling_launcher_is_not_verified_as_removed(self):
        launcher = self.root / "launcher"
        desktop = self.root / "desktop"
        launcher.symlink_to(self.root / "missing")
        self.assertFalse(self.runner["links_removed"](launcher, desktop))
        launcher.unlink()
        self.assertTrue(self.runner["links_removed"](launcher, desktop))


if __name__ == "__main__":
    unittest.main()
