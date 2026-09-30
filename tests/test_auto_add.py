"""Automatic install discovery, archive validation and persistent selections."""

import io
import json
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_obtain as base
from test_obtain import o, asset
from test_improvements import elf


def release_assets(names):
    return {"id": 1, "tag_name": "v1"}, [asset(name) for name in names]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.github = o.GitHub(Path("/unused"))
        self.source = dict(
            kind="auto",
            repository="owner/app",
            pinned=False,
            asset=None,
            prereleases=False,
        )

    def discover(self, names):
        release, files = release_assets(names)
        for item in files:
            item["browser_download_url"] = (
                f"https://github.com/{self.source['repository']}/releases/download/v1/"
                + item["name"]
            )
        with patch.object(self.github, "release_assets", return_value=(release, files)):
            return o.discover_release(self.source, self.github)

    def test_appimage_still_wins_over_archive(self):
        result = self.discover(["app-x64.AppImage", "app-linux-x64.tar.gz"])
        self.assertEqual(result["kind"], "appimage")

    def test_codex_main_archive_wins_over_companions_and_sidecars(self):
        self.source["repository"] = "owner/codex"
        names = [
            "codex-app-server-x86_64-unknown-linux-musl.tar.gz",
            "codex-npm-0.158.0.tgz",
            "codex-x86_64-unknown-linux-musl.sigstore",
            "codex-x86_64-unknown-linux-musl.zst",
            "codex-x86_64-unknown-linux-musl.tar.gz",
            "openai_codex_cli_bin-0.158.0-py3-none-manylinux_2_17_x86_64.whl",
        ]
        # Match the repository in download URLs as well as release filenames.
        files = [
            dict(
                asset(n),
                browser_download_url="https://github.com/owner/codex/releases/download/v1/"
                + n,
            )
            for n in names
        ]
        with patch.object(
            self.github,
            "release_assets",
            return_value=({"id": 1, "tag_name": "v1"}, files),
        ):
            result = o.discover_release(self.source, self.github)
        self.assertEqual(result["asset_name"], "codex-x86_64-unknown-linux-musl.tar.gz")
        self.assertEqual(result["kind"], "archive")
        self.assertEqual(self.source["asset_family"], "codex")

    def test_codex_release_package_is_selected_when_published(self):
        self.source["repository"] = "openai/codex"
        result = self.discover(
            [
                "codex-x86_64-unknown-linux-musl.tar.gz",
                "codex-app-server-package-x86_64-unknown-linux-musl.tar.gz",
                "codex-package-x86_64-unknown-linux-musl.tar.gz",
            ]
        )
        self.assertEqual(
            result["asset_name"], "codex-package-x86_64-unknown-linux-musl.tar.gz"
        )
        self.assertEqual(self.source["asset_family"], "codex")

    def test_updates_follow_versioned_names_without_switching_to_companions(self):
        self.discover(["app-v1-linux-x64.tar.gz", "app-helper-linux-x64.tar.gz"])
        with patch.object(
            self.github,
            "release_assets",
            return_value=release_assets(
                ["app-v2-linux-x64.tar.gz", "app-helper-linux-x64.tar.gz"]
            ),
        ):
            self.assertEqual(
                self.github.release(self.source)["asset_name"],
                "app-v2-linux-x64.tar.gz",
            )
        with (
            patch.object(
                self.github,
                "release_assets",
                return_value=release_assets(["app-helper-linux-x64.tar.gz"]),
            ),
            self.assertRaises(o.AssetChoice),
        ):
            self.github.release(self.source)
        self.source["asset"] = "app-helper*"
        with patch.object(
            self.github,
            "release_assets",
            return_value=release_assets(["app-helper-linux-x64.tar.gz"]),
        ):
            self.assertEqual(
                self.github.release(self.source)["asset_name"],
                "app-helper-linux-x64.tar.gz",
            )

    def test_full_package_beats_bare_archive_and_companion_package(self):
        result = self.discover(
            [
                "app-linux-x64.tar.gz",
                "app-package-linux-x64.tar.gz",
                "app-server-package-linux-x64.tar.gz",
                "app-package-linux-x64.tar.zst",
                "app-package-linux-x64.tar.gz.sigstore",
            ]
        )
        self.assertEqual(result["asset_name"], "app-package-linux-x64.tar.gz")
        self.assertEqual(self.source["asset_family"], "app")

    def test_explicit_bare_archive_remains_an_override(self):
        self.source["asset"] = "app-linux-x64.tar.gz"
        result = self.discover(["app-linux-x64.tar.gz", "app-package-linux-x64.tar.gz"])
        self.assertEqual(result["asset_name"], "app-linux-x64.tar.gz")

    def test_binary_selection_and_explicit_asset_override(self):
        result = self.discover(["app-linux-amd64", "app-linux-amd64.sigstore"])
        self.assertEqual(result["kind"], "binary")
        self.source["asset"] = "helper-linux-amd64.tar.gz"
        result = self.discover(["app-x64.AppImage", "helper-linux-amd64.tar.gz"])
        self.assertEqual(result["kind"], "archive")

    def test_noninteractive_ambiguity_does_not_read_stdin(self):
        with (
            patch.object(o.sys.stdin, "isatty", return_value=False),
            patch.object(o.sys.stdin, "readline") as read,
            self.assertRaisesRegex(o.Error, "--asset"),
        ):
            self.discover(["app-linux-x64.tar.gz", "app-linux-x64.zip"])
        read.assert_not_called()

    def test_interactive_choice_is_persisted_and_cancel_is_safe(self):
        self.source["asset"] = "*linux*"
        with (
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline", return_value="2\n"),
        ):
            result = self.discover(["one-linux-x64.tar.gz", "two[1]-linux-x64.tar.gz"])
        self.assertEqual(result["asset_name"], "two[1]-linux-x64.tar.gz")
        self.assertEqual(self.source["asset"], "two[[]1]-linux-x64.tar.gz")
        with (
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline", return_value="\n"),
            self.assertRaisesRegex(o.Error, "cancelled"),
        ):
            o.choose_option(["a", "b"], "a file", "hint")

    def test_missing_or_unsupported_release_does_not_change_source(self):
        for outcome in (o.NotFound("none"), release_assets(["app-windows-x64.exe"])):
            with (
                self.subTest(outcome=outcome),
                patch.object(self.github, "release_assets", side_effect=[outcome]),
                self.assertRaises(o.Error),
            ):
                o.discover_release(self.source, self.github)
            self.assertEqual(self.source["kind"], "auto")

    def test_network_errors_are_never_treated_as_missing_release(self):
        for error in (o.RateLimited("limit"), o.Error("offline")):
            with (
                patch.object(self.github, "release_assets", side_effect=error),
                self.assertRaises(type(error)),
            ):
                o.discover_release(self.source, self.github)

    def test_unmatched_selector_reports_error(self):
        self.source["asset"] = "missing*"
        with self.assertRaisesRegex(o.Error, "matches"):
            self.discover(["app-linux-x64.tar.gz"])


class AutoAddTests(unittest.TestCase):
    setUp = base.StateTests.setUp
    tearDown = base.StateTests.tearDown

    def archive(self, files):
        path = self.root / "release.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
        return path

    def add(self, files, options=()):
        self.store.sources = {}
        self.store.locks = {}
        path = self.archive(files)
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(["app-linux-x64.tar.gz"]),
            ),
            patch.object(
                o,
                "nix",
                return_value=json.dumps(
                    {"hash": "sha256-" + "A" * 43 + "=", "storePath": str(path)}
                ),
            ),
            patch.object(self.store, "install") as install,
        ):
            o.dispatch(
                o.parser().parse_args(
                    ["add", "https://github.com/owner/app", "--track-only", *options]
                ),
                self.store,
                github,
            )
        install.assert_not_called()

    def test_archive_program_is_discovered_and_saved_for_updates(self):
        self.add(
            [
                ("bundle/bin/app", elf()),
                ("bundle/README", b"data"),
                ("bundle/helper", elf()),
            ]
        )
        self.assertEqual(self.store.sources["app"]["kind"], "archive")
        self.assertEqual(self.store.sources["app"]["program"], "bundle/bin/app")
        o.validate_lock(self.store.locks["app"], "app")
        github = o.GitHub(self.root / "cache")
        with patch.object(
            github,
            "release_assets",
            return_value=release_assets(["app-linux-x64.tar.gz"]),
        ):
            self.assertIsNone(github.release(self.store.sources["app"])["program"])
        self.assertTrue(self.store.sources["app"]["auto_program"])

    def test_update_repairs_legacy_automatic_install_with_new_package_layout(self):
        self.add([("app-linux-x64", elf())])
        source = self.store.sources["app"]
        source.pop("auto_program")  # Source written by the previous Obtain version.
        before = dict(self.store.locks["app"])
        path = self.archive([("bin/app", elf()), ("app-package.json", b"{}")])
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(
                    ["app-linux-x64.tar.gz", "app-package-linux-x64.tar.gz"]
                ),
            ),
            patch.object(
                o,
                "nix",
                return_value=json.dumps(
                    {"hash": before["hash"], "storePath": str(path)}
                ),
            ),
            patch.object(o.sys.stdin, "readline") as read,
        ):
            o.dispatch(o.parser().parse_args(["update", "app"]), self.store, github)
        read.assert_not_called()
        self.assertEqual(self.store.locks["app"]["program"], "bin/app")
        self.assertEqual(
            self.store.locks["app"]["asset_name"], "app-package-linux-x64.tar.gz"
        )
        self.assertEqual(self.store.sources["app"]["program"], "bin/app")
        self.assertTrue(self.store.sources["app"]["auto_program"])

    def test_explicit_program_is_preserved_on_future_candidates(self):
        self.add([("chosen", elf())], ["--type", "archive", "--program", "chosen"])
        github = o.GitHub(self.root / "cache")
        with patch.object(
            github,
            "release_assets",
            return_value=release_assets(["app-linux-x64.tar.gz"]),
        ):
            self.assertEqual(
                github.release(self.store.sources["app"])["program"], "chosen"
            )

    def test_update_ambiguity_preserves_previous_source_and_lock(self):
        self.add([("app", elf())])
        before_source = dict(self.store.sources["app"])
        before_lock = dict(self.store.locks["app"])
        path = self.archive([("one", elf()), ("two", elf())])
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(["app-package-linux-x64.tar.gz"]),
            ),
            patch.object(
                o,
                "nix",
                return_value=json.dumps(
                    {"hash": before_lock["hash"], "storePath": str(path)}
                ),
            ),
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline") as read,
            self.assertRaises(o.Error),
        ):
            o.dispatch(o.parser().parse_args(["update", "app"]), self.store, github)
        read.assert_not_called()
        self.assertEqual(self.store.sources["app"], before_source)
        self.assertEqual(self.store.locks["app"], before_lock)

    def test_explicit_archive_type_also_discovers_sole_elf(self):
        self.add([("unusual-program-name", elf())], ["--type", "archive"])
        self.assertEqual(self.store.locks["app"]["program"], "unusual-program-name")

    def test_ambiguous_programs_prompt_and_save_choice(self):
        with (
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline", return_value="2\n"),
        ):
            self.add([("a", elf()), ("b", elf())])
        self.assertEqual(self.store.locks["app"]["program"], "b")

    def test_scripts_and_noninteractive_ambiguity_do_not_save_state(self):
        for files in (
            [("app", b"#!/bin/sh\necho unsafe")],
            [("one", elf()), ("two", elf())],
        ):
            with (
                patch.object(o.sys.stdin, "isatty", return_value=False),
                patch.object(o.sys.stdin, "readline") as read,
                self.assertRaisesRegex(o.Error, "--program"),
            ):
                self.add(files)
            read.assert_not_called()
            self.assertEqual(self.store.sources, {})

    def test_traversal_is_rejected_before_program_selection(self):
        with self.assertRaisesRegex(o.Error, "Unsafe archive path"):
            self.add([("../app", elf())])
        self.assertEqual(self.store.sources, {})

    def test_strip_components_applies_before_detection(self):
        self.add([("bundle/bin/app", elf())], ["--strip-components", "1"])
        self.assertEqual(self.store.sources["app"]["program"], "bin/app")
        self.assertEqual(self.store.sources["app"]["strip_components"], 1)


if __name__ == "__main__":
    unittest.main()
