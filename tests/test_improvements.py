"""Regressions for findings from live installs and adversarial release bundles."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from test_obtain import o, asset, candidate, record, source
import test_obtain as support

spec = importlib.util.spec_from_file_location(
    "payload", Path(__file__).resolve().parents[1] / "payload.py"
)
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


def elf(machine=62):
    data = bytearray(64)
    data[:8] = b"\x7fELF\x02\x01\x01\x00"
    struct.pack_into("<HH", data, 16, 2, machine)
    struct.pack_into("<Q", data, 24, 0x400000)
    return data


class AssetTests(unittest.TestCase):
    def test_debug_and_foreign_assets_do_not_create_ambiguity(self):
        good = asset("app-linux-amd64.tar.gz")
        self.assertEqual(
            o.select_asset(
                [
                    good,
                    asset("app-linux-amd64-debug.tar.gz"),
                    asset("app-darwin-amd64.tar.gz"),
                    asset("app-linux-arm64.tar.gz"),
                ],
                kind="archive",
            ),
            good,
        )

    def test_generic_appimage_fallback_does_not_beat_explicit_arch(self):
        good = asset("app-x86_64.AppImage")
        self.assertEqual(o.select_asset([good, asset("app.AppImage")]), good)

    def test_binary_rejects_other_formats_and_checksums(self):
        good = asset("app-linux-x86_64")
        self.assertEqual(
            o.select_asset(
                [
                    good,
                    asset(good["name"] + ".sha256"),
                    asset(good["name"] + ".zip"),
                    asset("app.exe"),
                ],
                kind="binary",
            ),
            good,
        )

    def test_unlabelled_archive_requires_explicit_selection(self):
        a = asset("app.tar.gz")
        with self.assertRaises(o.Error):
            o.select_asset([a], kind="archive")
        self.assertEqual(o.select_asset([a], "*.tar.gz", "archive"), a)

    def test_noninteractive_ambiguity_never_reads_stdin(self):
        gh = o.GitHub(Path("/unused"))
        with (
            patch.object(
                gh,
                "release",
                side_effect=o.AssetChoice(
                    "ambiguous", [asset("a.AppImage"), asset("b.AppImage")]
                ),
            ),
            patch.object(o.sys.stdin, "isatty", return_value=False),
            patch.object(o.sys.stdin, "readline") as read,
        ):
            with self.assertRaises(o.AssetChoice):
                o.choose_asset(gh, source())
            read.assert_not_called()

    def test_interactive_choice_preserves_literal_variant_characters(self):
        src = source()
        choices = [asset("a.AppImage"), asset("b[1].AppImage")]
        gh = o.GitHub(Path("/unused"))
        with (
            patch.object(
                gh,
                "release",
                side_effect=[o.AssetChoice("ambiguous", choices), candidate()],
            ),
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline", return_value="2\n"),
        ):
            o.choose_asset(gh, src)
        self.assertIsNone(src["asset"])
        self.assertEqual(src["asset_variant"], "b[1].AppImage")

    def test_archive_program_rejects_traversal_and_shell_syntax(self):
        for value in ("../app", "/app", "a/../../b", "a b", "$(id)", None):
            with self.subTest(value=value), self.assertRaises(o.Error):
                o.program_path(value)
        self.assertEqual(o.program_path("app-v1/bin/tool"), "app-v1/bin/tool")


class RateTests(unittest.TestCase):
    def test_rate_limit_shows_reset_and_forbidden_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            with patch.object(
                gh.opener,
                "open",
                side_effect=urllib.error.HTTPError(
                    "url",
                    403,
                    "limit",
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790614800"},
                    None,
                ),
            ):
                with self.assertRaisesRegex(o.RateLimited, "2026-09-28T"):
                    gh.get("/test")
            with patch.object(
                gh.opener,
                "open",
                side_effect=urllib.error.HTTPError("url", 403, "denied", {}, None),
            ):
                with self.assertRaisesRegex(o.Error, "denied access") as caught:
                    gh.get("/test")
                self.assertNotIsInstance(caught.exception, o.RateLimited)


class BatchTests(unittest.TestCase):
    # Reuse fixture methods, not the entire parent test suite.
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown
    installed = support.StateTests.installed

    def test_retry_only_unfinished_after_rate_limit(self):
        self.store.sources.update(b=source(), c=source())
        gh = o.GitHub(self.root / "cache")
        with patch.object(
            gh, "release", side_effect=[candidate(), o.RateLimited("limit")]
        ) as fetch:
            with self.assertRaisesRegex(o.Error, "--retry-failed"):
                o.dispatch(o.parser().parse_args(["check"]), self.store, gh)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            o.load_json(self.store.data / "check-batch.json")["unfinished"], ["b", "c"]
        )
        with patch.object(gh, "release", return_value=candidate()) as fetch:
            o.dispatch(
                o.parser().parse_args(["check", "--retry-failed"]), self.store, gh
            )
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            o.load_json(self.store.data / "check-batch.json")["unfinished"], []
        )

    def test_doctor_detects_missing_executable_without_launching(self):
        self.installed(record())
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(o.subprocess, "Popen") as launch,
        ):
            with self.assertRaises(o.Error):
                o.doctor(o.parser().parse_args(["doctor", "app", "--json"]), self.store)
            launch.assert_not_called()
        self.assertIn(
            {
                "check": "executable",
                "status": "failed",
                "detail": str(self.store.profile("app") / "bin/app"),
            },
            json.loads(output.getvalue())["checks"],
        )

    def test_doctor_launch_captures_library_error(self):
        self.installed(record())
        self.store.links("app")
        executable = self.store.profile("app") / "bin/app"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            f'#!{sys.executable}\nimport sys\nprint("error while loading shared libraries: missing.so", file=sys.stderr)\nsys.exit(127)\n'
        )
        executable.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(o.Error):
            o.doctor(
                o.parser().parse_args(["doctor", "app", "--launch-test"]), self.store
            )
        self.assertIn("missing.so", (self.store.cache / "doctor-app.log").read_text())

    def test_failed_selector_override_survives_retry(self):
        gh = o.GitHub(self.root / "cache")
        args = o.parser().parse_args(["update", "app", "--asset", "*linux*"])
        with patch.object(gh, "release", side_effect=o.RateLimited("limit")):
            with self.assertRaises(o.Error):
                o.dispatch(args, self.store, gh)
        self.assertIsNone(self.store.sources["app"]["asset"])
        with patch.object(gh, "release", return_value=candidate()) as fetch:
            o.dispatch(
                o.parser().parse_args(["update", "--retry-failed"]), self.store, gh
            )
        self.assertEqual(fetch.call_args.args[0]["asset"], "*linux*")
        self.assertEqual(self.store.sources["app"]["asset"], "*linux*")

    def test_asset_selector_updates_are_saved_only_on_success(self):
        gh = o.GitHub(self.root / "cache")
        with (
            patch.object(gh, "release", return_value=candidate()),
            patch.object(o, "lock_release", return_value=record()),
        ):
            o.dispatch(
                o.parser().parse_args(["update", "app", "--asset", "*x64*"]),
                self.store,
                gh,
            )
        self.assertEqual(self.store.sources["app"]["asset"], "*x64*")


class PayloadTests(unittest.TestCase):
    def test_tar_and_zip_accept_regular_elf_and_preserve_data(self):
        for kind in ("tar", "tar.gz", "tar.bz2", "tar.xz", "zip"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "download"
                if kind != "zip":
                    compression = {
                        "tar": "w",
                        "tar.gz": "w:gz",
                        "tar.bz2": "w:bz2",
                        "tar.xz": "w:xz",
                    }[kind]
                    with tarfile.open(archive, compression) as out:
                        info = tarfile.TarInfo("bundle/bin/app")
                        info.size = len(elf())
                        out.addfile(info, io.BytesIO(elf()))
                else:
                    with zipfile.ZipFile(archive, "w") as out:
                        out.writestr("bundle/bin/app", elf())
                        out.writestr("bundle/data.txt", "data")
                payload.prepare("archive", archive, root / "out", "bundle/bin/app")
                self.assertTrue(os.access(root / "out/bundle/bin/app", os.X_OK))

    def test_strip_versioned_root_and_reject_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "download"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("tool-v1/bin/app", elf())
            payload.prepare("archive", archive, root / "out", "bin/app", 1)
            self.assertTrue((root / "out/bin/app").is_file())
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("a/app", elf())
                out.writestr("b/app", elf())
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                payload.prepare("archive", archive, root / "collision", "app", 1)

    def test_tar_rejects_traversal_links_devices_and_duplicates(self):
        for variant in ("traversal", "symlink", "hardlink", "device", "duplicate"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "download"
                with tarfile.open(archive, "w") as out:
                    item = tarfile.TarInfo(
                        "../escaped" if variant == "traversal" else "app"
                    )
                    if variant in ("symlink", "hardlink", "device"):
                        item.type = {
                            "symlink": tarfile.SYMTYPE,
                            "hardlink": tarfile.LNKTYPE,
                            "device": tarfile.CHRTYPE,
                        }[variant]
                        item.linkname = "/etc/passwd"
                    out.addfile(item)
                    if variant == "duplicate":
                        out.addfile(item)
                with self.assertRaises(ValueError):
                    payload.prepare("archive", archive, root / "out", "app")
                self.assertFalse((root / "escaped").exists())

    def test_zip_rejects_symlink_and_traversal(self):
        for name in ("../escaped", "link"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "download"
                with zipfile.ZipFile(archive, "w") as out:
                    info = zipfile.ZipInfo(name)
                    if name == "link":
                        info.external_attr = 0o120777 << 16
                    out.writestr(info, "target")
                with self.assertRaises(ValueError):
                    payload.prepare("archive", archive, root / "out", "app")

    def test_expansion_budget_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "download"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
                out.writestr("big", b"0" * 1000)
            with (
                patch.object(payload, "MAX_BYTES", 100),
                self.assertRaisesRegex(ValueError, "limit"),
            ):
                payload.prepare("archive", archive, root / "out", "big")

    def test_binary_rejects_scripts_foreign_and_truncated_elf(self):
        for data in (b"#!/bin/sh\necho hello", elf(183), b"\x7fELF"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download"
                source.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "ELF"):
                    payload.prepare("binary", source, root / "out", "program")

    def test_shared_library_without_entry_point_is_not_a_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.so"
            data = elf()
            struct.pack_into("<H", data, 16, 3)
            struct.pack_into("<Q", data, 24, 0)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "entry point"):
                payload.validate_elf(path)

    def test_standalone_elf_is_made_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            source.write_bytes(elf())
            payload.prepare("binary", source, root / "out", "program")
            self.assertTrue(os.access(root / "out/program", os.X_OK))


class DiagnosticsTests(unittest.TestCase):
    def test_missing_library_error_is_actionable(self):
        self.assertIn(
            "needs libraries",
            o.diagnose("auto-patchelf could not satisfy dependency libexample.so"),
        )

    def test_real_failed_process_retains_diagnostic_output(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
        ):
            with self.assertRaisesRegex(o.Error, "See the command output"):
                o.run(
                    [
                        sys.executable,
                        "-c",
                        'import sys; print("fixture command failed", file=sys.stderr); sys.exit(3)',
                    ]
                )
            self.assertIn(
                "fixture command failed",
                (Path(tmp) / "obtain/last-command.log").read_text(),
            )
