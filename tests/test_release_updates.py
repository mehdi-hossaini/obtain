"""Release choices, reproducible runtime refresh, and composable metadata output."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_obtain as base
import test_auto_add as auto

o = base.o
release_assets = auto.release_assets


class VariantTests(unittest.TestCase):
    def test_explicit_menu_choice_without_linux_marker_survives_updates(self):
        github = o.GitHub(Path("/unused"))
        source = dict(base.source(), kind="archive", asset="*")
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(
                    [
                        "app-v1-x64.tar.gz",
                        "app-v1-x64-portable.tar.gz",
                    ]
                ),
            ),
            patch.object(o.sys.stdin, "isatty", return_value=True),
            patch.object(o.sys.stderr, "isatty", return_value=True),
            patch.object(o.sys.stdin, "readline", return_value="2\n"),
        ):
            o.choose_asset(github, source)
        with patch.object(
            github,
            "release_assets",
            return_value=(
                {"id": 2, "tag_name": "v2"},
                [
                    base.asset(name)
                    for name in (
                        "app-v2-x64.tar.gz",
                        "app-v2-x64-portable.tar.gz",
                        "app-v2-arm64-portable.tar.gz",
                    )
                ],
            ),
        ):
            self.assertEqual(
                github.release(source)["asset_name"], "app-v2-x64-portable.tar.gz"
            )

    def test_normalization_preserves_architecture_runtime_and_format(self):
        cases = [
            (
                "app-v1.2.3-linux-x86_64-musl.tar.gz",
                "v1.2.3",
                "app-{version}-linux-x86_64-musl.tar.gz",
            ),
            (
                "app_1.2.3_linux_x64_portable.AppImage",
                "rust-v1.2.3",
                "app_{version}_linux_x64_portable.AppImage",
            ),
            ("app-v12-linux-x64", "v1", "app-v12-linux-x64"),
            ("app-v1.2-linux-x64", "v1", "app-v1.2-linux-x64"),
            ("app[1]-linux-x64", "v1", "app[1]-linux-x64"),
            ("app-linux-x64", "continuous", "app-linux-x64"),
        ]
        for filename, version, expected in cases:
            with self.subTest(filename=filename):
                self.assertEqual(o.asset_variant(filename, version), expected)

    def test_menu_choice_tracks_version_but_never_changes_variant(self):
        github = o.GitHub(Path("/unused"))
        source = dict(base.source(), kind="auto")
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(
                    [
                        "app-v1-linux-x64.AppImage",
                        "app-v1-linux-x64-portable.AppImage",
                    ]
                ),
            ),
            patch.object(o, "choose_option", return_value=1),
        ):
            o.discover_release(source, github)
        source_after = dict(source)
        with patch.object(
            github,
            "release_assets",
            return_value=(
                {"id": 2, "tag_name": "v2"},
                [
                    base.asset(name)
                    for name in (
                        "app-v2-linux-x64.AppImage",
                        "app-v2-linux-x64-portable.AppImage",
                    )
                ],
            ),
        ):
            self.assertEqual(
                github.release(source)["asset_name"],
                "app-v2-linux-x64-portable.AppImage",
            )
        self.assertEqual(source, source_after)
        for names in (
            ["app-v2-linux-x64.AppImage"],
            ["app-v2-linux-x64-portable.AppImage", "app-2-linux-x64-portable.AppImage"],
        ):
            with (
                patch.object(
                    github,
                    "release_assets",
                    return_value=(
                        {"id": 2, "tag_name": "v2"},
                        [base.asset(name) for name in names],
                    ),
                ),
                self.assertRaises(o.AssetChoice),
            ):
                github.release(source)


class IdentityTests(unittest.TestCase):
    setUp = auto.DiscoveryTests.setUp
    discover = auto.DiscoveryTests.discover

    def test_main_archive_beats_helper_appimage(self):
        result = self.discover(
            ["app-helper-linux-x64.AppImage", "app-linux-x64.tar.gz"]
        )
        self.assertEqual(result["asset_name"], "app-linux-x64.tar.gz")

    def test_main_appimage_beats_main_runtime_archive(self):
        result = self.discover(["app-x64.AppImage", "app-package-linux-x64.tar.gz"])
        self.assertEqual(result["asset_name"], "app-x64.AppImage")


class RuntimeTests(unittest.TestCase):
    setUp = base.StateTests.setUp
    tearDown = base.StateTests.tearDown

    def test_rebuild_uses_saved_recipe_after_cli_changes(self):
        locked = self.store.pin_recipe(base.record())
        expected = (o.ROOT / "recipe.nix").read_text()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(o, "ROOT", Path("/no-current-cli")),
        ):
            build = self.store.build_recipe(locked, Path(tmp) / "saved")
            self.assertEqual(build.name, "build.nix")
            self.assertEqual((build.parent / "recipe.nix").read_text(), expected)
            self.assertTrue((build.parent / "desktop.py").is_file())

    def test_corrupt_recipe_stops_before_build_and_profile_changes(self):
        locked = self.store.pin_recipe(base.record())
        path = self.store.config / "recipes" / f"{locked['recipe_hash']}.json"
        files = o.load_json(path)
        files["recipe.nix"] = "altered"
        o.atomic_json(path, files)
        with (
            patch.object(o, "nix") as nix,
            self.assertRaisesRegex(o.Error, "missing or corrupt"),
        ):
            self.store.install("app", base.source(), locked)
        nix.assert_not_called()
        self.assertFalse((self.store.data / "pending.json").exists())

    def test_refresh_keeps_locked_release_and_pin_without_upstream_lookup(self):
        self.store.sources["app"]["pinned"] = True
        github = o.GitHub(self.root / "cache")
        updated_pin = "github:NixOS/nixpkgs/" + "b" * 40 + "?narHash=sha256-test"
        with (
            patch.object(github, "release") as upstream,
            patch.object(o, "default_pin", return_value=updated_pin),
            patch.object(self.store, "install") as install,
        ):
            o.dispatch(
                o.parser().parse_args(["refresh-runtime", "app"]), self.store, github
            )
        upstream.assert_not_called()
        install.assert_not_called()
        locked = self.store.locks["app"]
        for key in (*o.IDENTITY, "hash"):
            self.assertEqual(locked.get(key), base.record().get(key))
        self.assertEqual(locked["nixpkgs"], updated_pin)
        self.assertTrue(self.store.sources["app"]["pinned"])
        self.assertIn("recipe_hash", locked)

    def test_runtime_refresh_failure_preserves_source_and_lock(self):
        locked = dict(base.record(), kind="binary")
        source = dict(base.source(), kind="binary")
        self.store.sources["app"] = source
        self.store.locks["app"] = locked
        with (
            patch.object(self.store, "installed", return_value=locked),
            patch.object(self.store, "install", side_effect=o.Error("build failed")),
            self.assertRaisesRegex(o.Error, "build failed"),
        ):
            o.named_command(
                o.parser().parse_args(
                    ["refresh-runtime", "app", "--runtime", "direct"]
                ),
                self.store,
            )
        self.assertEqual(self.store.sources["app"], source)
        self.assertEqual(self.store.locks["app"], locked)

    def test_current_release_never_implicitly_refreshes_runtime(self):
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(github, "release", return_value=base.candidate()),
            patch.object(o, "default_pin") as pin,
            patch.object(self.store, "pin_recipe") as recipe,
        ):
            o.dispatch(o.parser().parse_args(["update", "app"]), self.store, github)
        pin.assert_not_called()
        recipe.assert_not_called()

    def test_rollback_restores_runtime_choice_for_future_updates(self):
        before = dict(base.record(), kind="binary", runtime="direct")
        current = dict(base.record(), kind="binary", runtime="fhs")
        self.store.sources["app"] = dict(base.source(), kind="binary", runtime="fhs")
        self.store.locks["app"] = current
        path = self.store.profile("app") / "share/obtain/manifest.json"
        o.atomic_json(path, current)
        with (
            patch.object(self.store, "previous_generation", return_value=(1, before)),
            patch.object(
                o,
                "run",
                side_effect=lambda *args, **kwargs: o.atomic_json(path, before),
            ),
        ):
            self.store.rollback("app")
        self.assertEqual(self.store.locks["app"], before)
        self.assertEqual(self.store.sources["app"]["runtime"], "direct")
        self.assertTrue(self.store.sources["app"]["pinned"])

    def test_source_cannot_silently_override_locked_runtime(self):
        with self.assertRaisesRegex(o.Error, "runtime disagree"):
            o.validate_source_lock(
                dict(base.source(), kind="binary", runtime="fhs"),
                dict(base.record(), kind="binary", runtime="direct"),
                "app",
            )

    def test_asset_override_preserves_legacy_automatic_program_discovery(self):
        self.store.sources["app"] = dict(
            base.source(),
            kind="archive",
            asset_family="app",
            program="old/app",
        )
        self.store.locks["app"] = dict(base.record(), kind="archive", program="old/app")
        github = o.GitHub(self.root / "cache")
        with patch.object(
            github,
            "release_assets",
            return_value=release_assets(
                [
                    "app-package-linux-x64.tar.gz",
                ]
            ),
        ):
            selected = github.release(
                {**self.store.sources["app"], "asset": "app-package*"}
            )
        self.assertIsNone(selected["program"])
        locked = dict(
            selected,
            name="app",
            program="bin/app",
            hash=base.record()["hash"],
            nixpkgs=o.default_pin(),
        )
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(
                    [
                        "app-package-linux-x64.tar.gz",
                    ]
                ),
            ),
            patch.object(o, "lock_release", return_value=locked) as lock,
        ):
            o.batch_item("update", "app", self.store, github, {"app": "app-package*"})
        self.assertIsNone(lock.call_args.args[1]["program"])
        self.assertTrue(self.store.sources["app"]["auto_program"])
        self.assertEqual(self.store.locks["app"]["program"], "bin/app")

    def test_direct_runtime_is_only_for_archives_and_binaries(self):
        self.store.sources = {}
        with self.assertRaisesRegex(o.Error, "archives and binaries"):
            o.add_source(
                o.parser().parse_args(
                    [
                        "add",
                        "https://github.com/owner/app",
                        "--type",
                        "appimage",
                        "--runtime",
                        "direct",
                    ]
                ),
                self.store,
            )
        source = o.add_source(
            o.parser().parse_args(
                [
                    "add",
                    "https://github.com/owner/app",
                    "--type",
                    "binary",
                    "--runtime",
                    "direct",
                ]
            ),
            self.store,
        )[1]
        self.assertEqual(source["runtime"], "direct")


class JsonTests(unittest.TestCase):
    setUp = base.StateTests.setUp
    tearDown = base.StateTests.tearDown

    def capture(self, argv, github):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                o.dispatch(o.parser().parse_args(argv), self.store, github)
            except o.Error:
                pass
        return json.loads(output.getvalue())

    def test_list_json_distinguishes_locked_and_installed(self):
        report = self.capture(["list", "--json"], o.GitHub(self.root / "cache"))
        self.assertEqual(report["apps"][0]["locked"]["version"], "v1")
        self.assertIsNone(report["apps"][0]["installed"])

    def test_check_json_retains_partial_success_and_rate_limit(self):
        self.store.sources["other"] = base.source()
        github = o.GitHub(self.root / "cache")
        with patch.object(
            github,
            "release",
            side_effect=[base.candidate("v2"), o.RateLimited("limit")],
        ):
            report = self.capture(["check", "--json"], github)
        self.assertEqual(report["apps"][0]["status"], "update-available")
        self.assertEqual(report["apps"][1]["status"], "failed")
        self.assertEqual(report["unfinished"], ["other"])

    def test_inspect_json_agrees_with_add_and_ignores_state(self):
        github = o.GitHub(self.root / "cache")
        output = io.StringIO()
        with (
            patch.object(
                github,
                "release_assets",
                return_value=release_assets(
                    [
                        "app-helper-x64.AppImage",
                        "app-linux-x64.tar.gz",
                    ]
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            o.inspect_repository(
                o.parser().parse_args(
                    ["inspect", "https://github.com/owner/app", "--json"]
                ),
                github,
            )
        report = json.loads(output.getvalue())
        self.assertEqual(
            report["automatic_selection"]["asset_name"], "app-linux-x64.tar.gz"
        )

    def test_lock_reports_local_hash_or_github_digest(self):
        for digest, method in (
            (None, "local-sha256"),
            ("sha256:" + "00" * 32, "github-digest"),
        ):
            with patch.object(
                o, "nix", return_value=json.dumps({"hash": base.record()["hash"]})
            ):
                locked = o.lock_release("app", dict(base.candidate(), digest=digest))
            self.assertEqual(
                locked["verification"], {"method": method, "hash": locked["hash"]}
            )
