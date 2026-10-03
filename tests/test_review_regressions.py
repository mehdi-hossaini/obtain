"""Observable regressions for asset selection and transaction preflight."""

import contextlib
import http.client
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_obtain as support

o = support.o


class SelectionRegressionTests(unittest.TestCase):
    def test_unlabelled_main_beats_architecture_labelled_companion_and_updates(self):
        for kind, names in (
            ("archive", ["app-linux.tar.gz", "app-helper-linux-x64.tar.gz"]),
            ("appimage", ["app.AppImage", "app-helper-x64.AppImage"]),
            ("binary", ["app-linux", "app-helper-linux-x64"]),
        ):
            with self.subTest(kind=kind):
                github = o.GitHub(Path("/unused"))
                source = dict(support.source(), kind="auto")
                with patch.object(
                    github,
                    "release_assets",
                    return_value=(
                        {"id": 1, "tag_name": "v1"},
                        [support.asset(n) for n in names],
                    ),
                ):
                    selected = o.discover_release(source, github)
                    self.assertEqual(selected["kind"], kind)
                    self.assertEqual(selected["asset_name"], names[0])
                    self.assertEqual(github.release(source)["asset_name"], names[0])
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        o.inspect_repository(
                            o.parser().parse_args(
                                ["inspect", "https://github.com/owner/app", "--json"]
                            ),
                            github,
                        )
                    report = json.loads(stdout.getvalue())
                    self.assertIn(names[0], report["assets"][kind])
                    self.assertEqual(
                        report["automatic_selection"]["asset_name"], names[0]
                    )

    def test_architecture_preference_still_applies_within_main_family(self):
        source = dict(support.source(), kind="auto")
        choices, main = o.release_choices(
            source,
            [
                support.asset("app.AppImage"),
                support.asset("app-x64.AppImage"),
                support.asset("app-arm64.AppImage"),
            ],
        )
        self.assertTrue(main)
        self.assertEqual([asset["name"] for _, asset in choices], ["app-x64.AppImage"])

    def test_release_version_does_not_replace_matching_runtime_version(self):
        old_name = "app-linux-glibc-2.17-v2.17.tar.gz"
        source = dict(
            support.source(),
            kind="archive",
            program="app",
            asset_variant=o.asset_variant(old_name, "v2.17"),
        )
        self.assertEqual(
            source["asset_variant"], "app-linux-glibc-2.17-{version}.tar.gz"
        )
        github = o.GitHub(Path("/unused"))
        with patch.object(
            github,
            "release_assets",
            return_value=(
                {"id": 2, "tag_name": "v2.18"},
                [
                    support.asset(n)
                    for n in (
                        "app-linux-glibc-2.17-v2.18.tar.gz",
                        "app-linux-glibc-2.18-v2.18.tar.gz",
                    )
                ],
            ),
        ):
            self.assertEqual(
                github.release(source)["asset_name"],
                "app-linux-glibc-2.17-v2.18.tar.gz",
            )

    def test_ambiguous_or_runtime_only_numbers_remain_literal(self):
        for filename, version in (
            ("app-2.17-linux-2.17.tar.gz", "v2.17"),
            ("app-linux-glibc-2.17.tar.gz", "v2.17"),
            ("app-linux-musl-2.17.tar.gz", "v2.17"),
            ("app-linux-glibc-2.17.tar.gz", "v17"),
            ("app-linux-musl-1.2.3.tar.gz", "v3"),
            ("app-linux-x86_64.tar.gz", "v64"),
            ("app-linux-2.17.tar.gz", "v17"),
        ):
            with self.subTest(filename=filename, version=version):
                self.assertEqual(o.asset_variant(filename, version), filename)
                labelled = filename.replace(".tar.gz", f"-{version}.tar.gz")
                if "app-2.17-linux-2.17" not in filename:
                    self.assertEqual(
                        o.asset_variant(labelled, version),
                        filename.replace(".tar.gz", "-{version}.tar.gz"),
                    )


class StateRegressionTests(unittest.TestCase):
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown
    installed = support.StateTests.installed

    def test_rollback_collision_stops_before_switch_and_preserves_session(self):
        self.installed(support.record("v2"))
        self.store.locks["app"] = support.record("v2")
        self.store.save()
        for collision in (
            self.store.data / "bin/app",
            self.store.applications / "obtain-app.desktop",
        ):
            with self.subTest(collision=collision):
                collision.parent.mkdir(parents=True, exist_ok=True)
                collision.write_text("owned by user")
                try:
                    with (
                        patch.object(o, "run") as switch,
                        patch.object(
                            self.store,
                            "previous_generation",
                            return_value=(1, support.record()),
                        ),
                    ):
                        with self.assertRaisesRegex(o.Error, "Refusing to overwrite"):
                            self.store.rollback("app")
                    switch.assert_not_called()
                    self.assertEqual(self.store.installed("app"), support.record("v2"))
                    self.assertEqual(collision.read_text(), "owned by user")
                    self.assertFalse((self.store.data / "pending.json").exists())
                    with o.Store().session() as fresh:
                        self.assertEqual(fresh.locks["app"], support.record("v2"))
                finally:
                    collision.unlink()

    def test_corrupt_installed_manifest_never_creates_invalid_pending_journal(self):
        for value in ({}, [], 42):
            with self.subTest(value=value):
                self.installed(value)
                before = (self.store.config / "lock.json").read_bytes()
                with patch.object(o, "run") as uninstall:
                    with self.assertRaises(o.Error):
                        self.store.remove("app")
                uninstall.assert_not_called()
                self.assertFalse((self.store.data / "pending.json").exists())
                self.assertEqual((self.store.config / "lock.json").read_bytes(), before)
                with o.Store().session():
                    pass
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    status = o.main(["doctor", "app", "--json"])
                self.assertEqual(status, 1)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["checks"][0]["status"], "failed")

    def test_old_variant_is_repaired_from_locked_asset_and_saved_on_update(self):
        old = dict(
            support.record("v2.17"),
            kind="archive",
            program="app",
            asset_name="app-linux-glibc-2.17-v2.17.tar.gz",
        )
        source = dict(
            support.source(),
            kind="archive",
            program="app",
            asset_variant="app-linux-glibc-{version}-{version}.tar.gz",
        )
        self.store.sources["app"] = source
        self.store.locks["app"] = old
        self.store.save()
        github = o.GitHub(Path("/unused"))
        with patch.object(
            github,
            "release_assets",
            return_value=(
                {"id": 1, "tag_name": "v2.17"},
                [support.asset(old["asset_name"])],
            ),
        ):
            # Match the preexisting lock identity so repair needs no download.
            candidate = github.release(
                dict(
                    source,
                    asset_variant=o.asset_variant(old["asset_name"], old["version"]),
                )
            )
            self.store.locks["app"] = dict(old, **candidate)
            o.batch_item("check", "app", self.store, github, {})
            self.assertEqual(self.store.sources["app"], source)
            with patch.object(o, "nix") as download:
                o.batch_item("update", "app", self.store, github, {})
            download.assert_not_called()
        self.assertEqual(
            self.store.sources["app"]["asset_variant"],
            "app-linux-glibc-2.17-{version}.tar.gz",
        )

    def test_truncated_chunked_response_is_reported_and_next_app_is_checked(self):
        class Socket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nA\r\n{}"
                )

        class Response(io.BytesIO):
            headers = {}

        response = http.client.HTTPResponse(Socket())
        response.begin()
        self.store.sources["other"] = support.source()
        self.store.locks["other"] = dict(support.record(), name="other")
        self.store.save()
        github = o.GitHub(self.store.cache / "github")
        responses = [
            response,
            Response(json.dumps({"id": 1, "tag_name": "v1"}).encode()),
            Response(json.dumps([support.asset("app-x64.AppImage")]).encode()),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(github.opener, "open", side_effect=responses),
            patch.object(o, "GitHub", return_value=github),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = o.main(["check", "--json"])
        self.assertEqual(status, 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(
            [app["status"] for app in report["apps"]], ["failed", "update-available"]
        )
        self.assertEqual(report["unfinished"], ["app"])
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
