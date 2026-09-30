"""Real-process checks for bounded startup diagnostics."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest.mock import patch

import test_obtain as support


class DoctorReliabilityTests(unittest.TestCase):
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown
    installed = support.StateTests.installed

    def executable(self, text):
        self.installed(support.record())
        path = self.store.profile("app") / "bin/app"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        desktop = self.store.profile("app") / "share/applications/obtain-app.desktop"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text("[Desktop Entry]\nName=App\nType=Application\nExec=app\n")
        self.store.links("app")
        return path

    def report(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(support.o.Error):
            support.o.doctor(
                support.o.parser().parse_args(
                    ["doctor", "app", "--launch-test", "--json"]
                ),
                self.store,
            )
        return json.loads(stdout.getvalue())

    def test_corrupt_installed_manifest_produces_failed_json_report(self):
        self.executable(f"#!{sys.executable}\n")
        manifest = self.store.profile("app") / "share/obtain/manifest.json"
        for contents in ("{invalid json", "[]", "{}"):
            with self.subTest(contents=contents):
                manifest.write_text(contents)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    status = support.o.main(["doctor", "app", "--json"])
                self.assertEqual(status, 1)
                report = json.loads(stdout.getvalue())
                installed = next(
                    c for c in report["checks"] if c["check"] == "installed"
                )
                self.assertEqual(installed["status"], "failed")
                self.assertIn("manifest", installed["detail"])
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_failed_log_write_preserves_probe_failure_and_json_report(self):
        self.executable(
            f"#!{sys.executable}\n"
            "import sys\n"
            "print('startup diagnostic', file=sys.stderr)\n"
            "sys.exit(7)\n"
        )
        with patch.object(
            Path, "write_text", side_effect=PermissionError("log denied")
        ):
            report = self.report()
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks["diagnostic_log"]["status"], "warning")
        self.assertEqual(checks["startup"]["status"], "failed")
        self.assertIn("Exit: 7", checks["startup"]["detail"])
        self.assertIn("startup diagnostic", checks["startup"]["detail"])

    def test_unavailable_log_directory_does_not_prevent_successful_probe(self):
        self.executable(f"#!{sys.executable}\n")
        self.store.cache.parent.mkdir(parents=True, exist_ok=True)
        self.store.cache.write_text("unrelated file")
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            patch.object(support.o.shutil, "which", return_value="/available/nix"),
        ):
            support.o.doctor(
                support.o.parser().parse_args(
                    ["doctor", "app", "--launch-test", "--json"]
                ),
                self.store,
            )
        report = json.loads(stdout.getvalue())
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks["diagnostic_log"]["status"], "warning")
        self.assertEqual(checks["startup"]["status"], "ok")
        self.assertEqual(self.store.cache.read_text(), "unrelated file")

    def test_invalid_interpreter_is_reported_as_failed_startup(self):
        self.executable("#!/definitely/no/such/interpreter\n")
        report = self.report()
        startup = next(c for c in report["checks"] if c["check"] == "startup")
        self.assertEqual(startup["status"], "failed")
        self.assertIn("Launch failed", startup["detail"])
        self.assertIn(
            "Could not launch", (self.store.cache / "doctor-app.log").read_text()
        )

    def test_exec_format_error_is_reported_as_failed_startup(self):
        self.executable("not an executable format\n")
        report = self.report()
        startup = next(c for c in report["checks"] if c["check"] == "startup")
        self.assertEqual(startup["status"], "failed")
        self.assertIn("Launch failed", startup["detail"])

    def test_closed_output_still_waits_for_exit_code(self):
        self.executable(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(.05)\n"
            "sys.exit(7)\n"
        )
        report = self.report()
        startup = next(c for c in report["checks"] if c["check"] == "startup")
        self.assertEqual(startup["status"], "failed")
        self.assertIn("Exit: 7", startup["detail"])

    def test_noisy_probe_retains_only_last_64_kib(self):
        self.executable(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "for _ in range(64): os.write(1, b'x' * 65536)\n"
            "os.write(2, b'END OF DIAGNOSTIC')\n"
            "sys.exit(7)\n"
        )
        with patch.object(
            support.o.tempfile,
            "TemporaryFile",
            side_effect=AssertionError("unbounded file"),
        ):
            report = self.report()
        startup = next(c for c in report["checks"] if c["check"] == "startup")
        self.assertEqual(startup["status"], "failed")
        log = (self.store.cache / "doctor-app.log").read_bytes()
        self.assertEqual(len(log), 65536)
        self.assertTrue(log.endswith(b"END OF DIAGNOSTIC"))

    @unittest.skipUnless(sys.platform == "linux", "checks child state through /proc")
    def test_five_second_probe_cleans_up_descendant(self):
        child_pid_file = self.root / "child.pid"
        self.executable(
            f"#!{sys.executable}\n"
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        output = io.StringIO()
        try:
            with (
                contextlib.redirect_stdout(output),
                patch.object(support.o.shutil, "which", return_value="/usr/bin/nix"),
                patch.dict(
                    os.environ,
                    {"PATH": f"{self.store.data / 'bin'}:{os.environ.get('PATH', '')}"},
                ),
            ):
                support.o.doctor(
                    support.o.parser().parse_args(
                        ["doctor", "app", "--launch-test", "--json"]
                    ),
                    self.store,
                )
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 4.8)
            self.assertLess(elapsed, 7)
            report = json.loads(output.getvalue())
            startup = next(c for c in report["checks"] if c["check"] == "startup")
            self.assertEqual(startup["status"], "ok")
            self.assertIn("still running after five seconds", startup["detail"])
            child_pid = int(child_pid_file.read_text())
            status = Path(f"/proc/{child_pid}/stat")
            deadline = time.monotonic() + 1
            while status.exists() and status.read_text().split()[2] != "Z":
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(
                not status.exists() or status.read_text().split()[2] == "Z",
                "probe descendant survived cleanup",
            )
        finally:
            if child_pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(child_pid_file.read_text()), signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
