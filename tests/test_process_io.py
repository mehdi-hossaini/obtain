"""Real-process regressions for command output and interruption."""

import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_obtain import o


class NotifiedStderr(io.StringIO):
    def __init__(self):
        super().__init__()
        self.progress = threading.Event()

    def write(self, value):
        written = super().write(value)
        if "progress é" in self.getvalue():
            self.progress.set()
        return written


class ProcessIOTests(unittest.TestCase):
    @staticmethod
    def wait_for(path, process, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("runner exited before child became ready")
            time.sleep(0.02)
        if not path.exists():
            raise AssertionError("child did not become ready")

    def test_progress_without_newline_is_visible_before_exit(self):
        stderr = NotifiedStderr()
        result = []
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "release"
            script = (
                "import os, sys, time; from pathlib import Path\n"
                "os.write(2, b'progress '); os.write(2, b'\\xc3')\n"
                "time.sleep(.05); os.write(2, b'\\xa9')\n"
                "deadline = time.monotonic() + 10\n"
                "while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:\n"
                "    time.sleep(.01)\n"
                "os.write(1, b'captured\\n')\n"
            )

            def call():
                result.append(o.run([sys.executable, "-c", script, str(release)]))

            with patch.object(o.sys, "stderr", stderr):
                worker = threading.Thread(target=call, daemon=True)
                worker.start()
                try:
                    self.assertTrue(
                        stderr.progress.wait(5), "progress waited for newline or exit"
                    )
                    self.assertTrue(worker.is_alive(), "child should still be running")
                finally:
                    release.touch()
                    worker.join(12)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["captured"])
        self.assertEqual(stderr.getvalue(), "progress é")

    def test_long_unterminated_line_keeps_bounded_diagnostic_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import os, sys; "
                "os.write(2, b'x' * 2000000 + b'no space left'); sys.exit(7)"
            )
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
                patch.object(o.sys, "stderr", io.StringIO()),
            ):
                with self.assertRaisesRegex(o.Error, "Disk space is exhausted"):
                    o.run([sys.executable, "-c", script])
            details = (Path(tmp) / "obtain/last-command.log").read_text()
            self.assertEqual(len(details), 8192)
            self.assertTrue(details.endswith("no space left"))

    def test_diagnostic_tail_keeps_last_200_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import sys; "
                "sys.stderr.writelines(f'L{i:03d}\\n' for i in range(205)); "
                "sys.exit(7)"
            )
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
                patch.object(o.sys, "stderr", io.StringIO()),
            ):
                with self.assertRaises(o.Error):
                    o.run([sys.executable, "-c", script])
            lines = (Path(tmp) / "obtain/last-command.log").read_text().splitlines()
            self.assertEqual(len(lines), 200)
            self.assertEqual((lines[0], lines[-1]), ("L005", "L204"))

    def test_capture_false_inherits_stdout(self):
        runner = (
            "import obtain, sys; "
            "result = obtain.run([sys.executable, '-c', "
            "'import sys; sys.stdout.write(\"inherited\\\\n\")'], capture=False); "
            "assert result == ''"
        )
        completed = subprocess.run(
            [sys.executable, "-c", runner],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertEqual(completed.stdout, "inherited\n")

    def test_stdout_only_failure_is_in_bounded_diagnostic_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import os, sys; "
                "os.write(1, b'x' * 100000 + b' no space left'); sys.exit(7)"
            )
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
                patch.object(o.sys, "stderr", io.StringIO()),
            ):
                with self.assertRaisesRegex(o.Error, "Disk space is exhausted"):
                    o.run([sys.executable, "-c", script])
            details = (Path(tmp) / "obtain/last-command.log").read_text()
            self.assertIn("[stdout tail]", details)
            self.assertTrue(details.endswith(" no space left"))
            self.assertLessEqual(len(details), 65536 + 20)

    def test_captured_output_limit_stops_noisy_child(self):
        script = "import os, time; os.write(1, b'x' * 8192); time.sleep(10)"
        started = time.monotonic()
        with patch.object(o, "MAX_COMMAND_OUTPUT_BYTES", 4096):
            with self.assertRaisesRegex(o.Error, "more than 4096 bytes"):
                o.run([sys.executable, "-c", script])
        self.assertLess(time.monotonic() - started, 5)

    def test_interleaved_stdout_and_stderr_are_both_drained(self):
        script = (
            "import os; "
            "[(os.write(1, b'a' * 8192), os.write(2, b'progress\\n')) "
            "for _ in range(20)]"
        )
        with patch.object(o.sys, "stderr", io.StringIO()) as progress:
            result = o.run([sys.executable, "-c", script])
        self.assertEqual(len(result), 20 * 8192)
        self.assertEqual(progress.getvalue(), "progress\n" * 20)

    def test_captured_metadata_uses_utf8_independent_of_locale(self):
        script = 'import os; os.write(1, b\'{"name":"\\xc3\\xa9"}\')'
        with patch("locale.getpreferredencoding", return_value="latin-1"):
            result = o.run([sys.executable, "-c", script])
        self.assertEqual(result, '{"name":"é"}')

    def test_stdout_tail_handles_split_multibyte_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import os, sys; os.write(1, b'\\xc3\\xa9' + b'x' * 65535); sys.exit(7)"
            )
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
                patch.object(o.sys, "stderr", io.StringIO()),
            ):
                with self.assertRaisesRegex(o.Error, r"failed \(exit 7\)"):
                    o.run([sys.executable, "-c", script])
            details = (Path(tmp) / "obtain/last-command.log").read_text()
            self.assertIn("[stdout tail]", details)
            self.assertTrue(details.endswith("x" * 65535))

    def test_exited_child_does_not_wait_for_descendant_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "descendant.pid"
            script = (
                "import subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(p.pid))"
            )
            started = time.monotonic()
            try:
                o.run([sys.executable, "-c", script, str(pid_file)])
                self.assertLess(time.monotonic() - started, 2)
            finally:
                if pid_file.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_sigkill_parent_keeps_session_locked_until_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready, release = root / "ready", root / "release"
            child = (
                "import pathlib, sys, time\n"
                "pathlib.Path(sys.argv[1]).touch()\n"
                "deadline = time.monotonic() + 10\n"
                "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline:\n"
                "    time.sleep(.02)\n"
            )
            runner = (
                "import obtain, sys\n"
                "with obtain.Store().session():\n"
                "    obtain.run([sys.executable, '-c', sys.argv[3], sys.argv[1], sys.argv[2]])\n"
            )
            env = {
                **os.environ,
                "XDG_CONFIG_HOME": tmp,
                "XDG_DATA_HOME": tmp,
                "XDG_CACHE_HOME": tmp,
            }
            process = subprocess.Popen(
                [sys.executable, "-c", runner, str(ready), str(release), child],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                self.wait_for(ready, process)
                process.kill()
                process.wait(timeout=5)
                with patch.dict(os.environ, env):
                    with self.assertRaisesRegex(o.Error, "Another Obtain command"):
                        with o.Store().session():
                            pass
                release.touch()
                deadline = time.monotonic() + 5
                with patch.dict(os.environ, env):
                    while time.monotonic() < deadline:
                        try:
                            with o.Store().session():
                                break
                        except o.Error as error:
                            self.assertIn("Another Obtain command", str(error))
                            time.sleep(0.02)
                    else:
                        self.fail("child retained the store lock after exit")
            finally:
                release.touch()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_sigterm_main_cleans_child_before_next_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            child = (
                "import pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
            )
            runner = (
                "import obtain, sys; "
                "obtain.dispatch = lambda args, store, github: "
                "obtain.run([sys.executable, '-c', sys.argv[2], sys.argv[1]]); "
                "sys.exit(obtain.main(['list']))"
            )
            env = {
                **os.environ,
                "XDG_CONFIG_HOME": tmp,
                "XDG_DATA_HOME": tmp,
                "XDG_CACHE_HOME": tmp,
            }
            process = subprocess.Popen(
                [sys.executable, "-c", runner, str(ready), child],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                self.wait_for(ready, process)
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=8)
                self.assertEqual(process.returncode, 130)
                with patch.dict(os.environ, env):
                    with o.Store().session():
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    @unittest.skipUnless(sys.platform == "linux", "uses /proc to distinguish zombies")
    def test_interrupt_cleans_up_descendant_holding_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "descendant.pid"
            child = (
                "import os, signal, subprocess, sys, time; "
                "grandchild = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                "open(sys.argv[1], 'w').write(str(grandchild.pid)); "
                "time.sleep(30)"
            )
            runner = (
                "import obtain, sys; "
                "obtain.run([sys.executable, '-c', sys.argv[2], sys.argv[1]])"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", runner, str(pid_file), child],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            descendant = None
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    self.assertIsNone(
                        process.poll(), "runner exited before starting child"
                    )
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists(), "descendant did not start")
                descendant = int(pid_file.read_text())
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=8)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    status = Path(f"/proc/{descendant}/stat")
                    if not status.exists() or status.read_text().split()[2] == "Z":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("descendant survived interruption")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                if descendant is not None:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
