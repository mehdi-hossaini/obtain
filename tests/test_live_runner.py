"""Regressions for the live cohort's outcome and resume bookkeeping."""

import os
from pathlib import Path
import resource
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch


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
