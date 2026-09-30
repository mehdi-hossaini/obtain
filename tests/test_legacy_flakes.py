"""Old flake records remain readable and removable after release-only migration."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import test_obtain as base
from test_obtain import o, candidate, record, source


def flake_record(revision="a" * 40):
    nar_hash = "sha256-" + "A" * 43 + "="
    return {
        "kind": "flake",
        "repository": "owner/app",
        "ref": "main",
        "revision": revision,
        "version": revision[:12],
        "package": "default",
        "requested_program": None,
        "system": o.SYSTEM,
        "release_url": f"https://github.com/owner/app/commit/{revision}",
        "name": "app",
        "nar_hash": nar_hash,
        "flake_url": f"github:owner/app/{revision}?narHash={o.urllib.parse.quote(nar_hash, safe='')}",
        "program": "actual-program",
        "package_version": "1.0",
        "nixpkgs": o.default_pin(),
        "inputs": {"nodes": {}, "version": 7},
    }


def flake_source():
    return {
        "kind": "flake",
        "repository": "owner/app",
        "ref": "main",
        "package": "default",
        "program": None,
        "pinned": False,
    }


class LegacyFlakeTests(unittest.TestCase):
    setUp = base.StateTests.setUp
    tearDown = base.StateTests.tearDown
    installed = base.StateTests.installed

    def legacy(self):
        self.store.sources = {"app": flake_source()}
        self.store.locks = {"app": flake_record()}
        self.store.save()

    def test_saved_record_validates_but_tampering_does_not(self):
        o.validate_lock(flake_record(), "app")
        self.assertEqual(o.backend(flake_record()), "flake")
        for field, value in [
            ("revision", "main"),
            ("nar_hash", "bad"),
            ("flake_url", "github:evil/repo/main"),
            ("program", "../evil"),
            ("package", "x/y"),
        ]:
            with self.subTest(field=field), self.assertRaises(o.Error):
                o.validate_lock(dict(flake_record(), **{field: value}), "app")

    def test_saved_legacy_state_does_not_block_release_commands(self):
        self.legacy()
        self.store.sources["release"] = source()
        self.store.locks["release"] = dict(record(), name="release")
        self.store.save()
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks["app"], flake_record())
            self.assertEqual(fresh.locks["release"]["version"], "v1")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                o.dispatch(
                    o.parser().parse_args(["list"]),
                    fresh,
                    o.GitHub(self.root / "cache"),
                )
            self.assertIn("app", out.getvalue())
            self.assertIn("release", out.getvalue())

    def test_info_and_remove_track_only_legacy_record(self):
        self.legacy()
        gh = o.GitHub(self.root / "cache")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            o.dispatch(o.parser().parse_args(["info", "app"]), self.store, gh)
        self.assertEqual(json.loads(out.getvalue())["locked"], flake_record())
        with contextlib.redirect_stdout(io.StringIO()):
            o.dispatch(o.parser().parse_args(["remove", "app"]), self.store, gh)
        self.assertEqual(self.store.sources, {})
        self.assertEqual(self.store.locks, {})

    def test_remove_installed_legacy_profile_uses_existing_uninstall_path(self):
        self.legacy()
        self.installed(flake_record())
        manifest = self.store.profile("app") / "share/obtain/manifest.json"

        def uninstall(*args, **kwargs):
            manifest.unlink()

        with (
            patch.object(o, "run", side_effect=uninstall) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(
                o.parser().parse_args(["remove", "app"]),
                self.store,
                o.GitHub(self.root / "cache"),
            )
        self.assertEqual(
            run.call_args.args[0][:3],
            ["nix-env", "--profile", str(self.store.profile("app"))],
        )
        self.assertEqual(self.store.sources, {})
        self.assertEqual(self.store.locks, {})
        self.assertFalse((self.store.data / "pending.json").exists())

    def test_legacy_install_check_update_guide_direct_nix_use(self):
        self.legacy()
        gh = o.GitHub(self.root / "cache")
        for argv in (["install", "app"], ["check", "app"], ["update", "app"]):
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                patch.object(gh, "release") as fetch,
                patch.object(o, "nix") as nix,
                contextlib.redirect_stderr(stderr),
                self.assertRaises(o.Error) as error,
            ):
                o.dispatch(o.parser().parse_args(argv), self.store, gh)
            self.assertIn(
                "nix --extra-experimental-features",
                str(error.exception) + stderr.getvalue(),
            )
            fetch.assert_not_called()
            nix.assert_not_called()
        self.assertEqual(self.store.locks["app"], flake_record())

    def test_batch_keeps_processing_release_apps_after_legacy_error(self):
        self.legacy()
        self.store.sources["release"] = source()
        self.store.locks["release"] = dict(record(), name="release")
        gh = o.GitHub(self.root / "cache")
        with (
            patch.object(gh, "release", return_value=candidate()) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(o.Error),
        ):
            o.dispatch(o.parser().parse_args(["check"]), self.store, gh)
        fetch.assert_called_once()
        self.assertEqual(
            o.load_batch(self.store.data / "check-batch.json")["unfinished"], ["app"]
        )

    def test_legacy_save_journal_is_recoverable(self):
        self.legacy()
        new = flake_record("b" * 40)
        self.store.journal("save", "app", flake_source(), new)
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks["app"], new)


if __name__ == "__main__":
    unittest.main()
