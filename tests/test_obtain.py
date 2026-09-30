import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location(
    "obtain", Path(__file__).resolve().parents[1] / "obtain.py"
)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)


def candidate(version="v1", asset_id=1):
    return dict(
        repository="owner/app",
        version=version,
        release_id=1,
        asset_id=asset_id,
        asset_name="app-x86_64.AppImage",
        asset_updated_at="2026-01-01",
        url=f"https://github.com/owner/app/releases/download/{version}/app-x86_64.AppImage",
        release_url=f"https://github.com/owner/app/releases/tag/{version}",
        digest=None,
        size=42,
        system=o.SYSTEM,
    )


def record(version="v1", asset_id=1):
    return dict(
        candidate(version, asset_id),
        name="app",
        hash="sha256-" + "A" * 43 + "=",
        nixpkgs=o.default_pin(),
    )


def source():
    return dict(repository="owner/app", asset=None, prereleases=False, pinned=False)


def asset(name, **kwargs):
    return dict(
        name=name,
        id=1,
        browser_download_url="https://github.com/owner/app/releases/download/v1/"
        + name,
        **kwargs,
    )


class SelectionTests(unittest.TestCase):
    def test_architecture_and_signature_filter(self):
        good = asset("app-x86_64.AppImage")
        self.assertEqual(
            o.select_asset(
                [asset("app-arm64.AppImage"), asset("app-x86_64.AppImage.zsync"), good]
            ),
            good,
        )

    def test_ambiguous_never_guesses(self):
        with self.assertRaisesRegex(o.Error, "found 2"):
            o.select_asset([asset("one-x64.AppImage"), asset("two-x64.AppImage")])

    def test_sole_unlabelled_appimage_is_selected(self):
        a = asset("App.AppImage")
        self.assertEqual(o.select_asset([a]), a)
        self.assertEqual(o.select_asset([a], "*.AppImage"), a)

    def test_pattern_cannot_override_wrong_arch(self):
        with self.assertRaises(o.Error):
            o.select_asset([asset("app-aarch64.AppImage")], "*")

    def test_unfinished_asset_rejected(self):
        with self.assertRaises(o.Error):
            o.select_asset([asset("app-x64.AppImage", state="new")])

    def test_invalid_repo_and_name(self):
        for url in [
            "http://github.com/a/b",
            "https://github.com.evil/a/b",
            "https://github.com/a/b/releases",
            "https://github.com/a/b?x=y",
            "https://github.com/a/..",
        ]:
            with self.subTest(url=url), self.assertRaises(o.Error):
                o.repository(url)
        for name in ["../a", "a/b", "-flag", "${oops}", "a\n"]:
            with self.subTest(name=name), self.assertRaises(o.Error):
                o.name_check(name)

    def test_normalizes_git_suffix(self):
        self.assertEqual(o.repository("https://github.com/owner/app.git/"), "owner/app")

    def test_download_must_belong_to_repo(self):
        with self.assertRaises(o.Error):
            o.asset_url("https://evil.test/app.AppImage", "owner/app")

    def test_replaced_asset_same_tag_is_update(self):
        self.assertFalse(o.same_release(candidate(), candidate(asset_id=2)))

    def test_hash_mismatch_rejected(self):
        c = candidate()
        c["digest"] = "sha256:" + "00" * 32
        with (
            patch.object(o, "nix", return_value=json.dumps({"hash": "sha256-bad"})),
            self.assertRaisesRegex(o.Error, "hash does not match"),
        ):
            o.lock_release("app", c)

    def test_expected_digest_passed_to_nix(self):
        c = candidate()
        c["digest"] = "sha256:" + "00" * 32
        with patch.object(
            o, "nix", return_value=json.dumps({"hash": record()["hash"]})
        ) as nix:
            o.lock_release("app", c)
        self.assertIn("--expected-hash", nix.call_args.args)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                f"XDG_{part}_HOME": str(self.root / part.lower())
                for part in ["DATA", "CONFIG", "CACHE"]
            },
        )
        self.env.start()
        self.store = o.Store()
        self.store.data.mkdir(parents=True)
        self.store.sources = {"app": source()}
        self.store.locks = {"app": record()}
        self.store.save()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def installed(self, value):
        path = self.store.profile("app") / "share/obtain/manifest.json"
        o.atomic_json(path, value)

    def test_mismatched_source_and_lock_stop_before_profile_changes(self):
        mismatched = dict(source(), repository="other/app")
        o.atomic_json(
            self.store.config / "sources.json",
            {"schema": 1, "apps": {"app": mismatched}},
        )
        with self.assertRaisesRegex(o.Error, "Source and lock disagree"):
            with o.Store().session():
                pass
        with (
            patch.object(o, "nix") as nix,
            self.assertRaisesRegex(o.Error, "Source and lock disagree"),
        ):
            self.store.install("app", mismatched, record())
        nix.assert_not_called()
        self.assertFalse((self.store.data / "pending.json").exists())

    def test_failed_build_preserves_state_and_launchers(self):
        self.installed(record())
        self.store.links("app")
        before = (self.store.config / "lock.json").read_bytes()
        with (
            patch.object(o, "nix", side_effect=o.Error("build failed")),
            self.assertRaises(o.Error),
        ):
            self.store.install("app", source(), record("v2"))
        self.assertEqual(before, (self.store.config / "lock.json").read_bytes())
        self.assertEqual(self.store.installed("app"), record())
        self.assertFalse((self.store.data / "pending.json").exists())

    def test_failed_first_build_creates_no_launcher(self):
        with (
            patch.object(o, "nix", side_effect=o.Error("build failed")),
            self.assertRaises(o.Error),
        ):
            self.store.install("app", source(), record())
        self.assertFalse((self.store.data / "bin/app").is_symlink())

    def test_interrupt_after_switch_recovers_lock(self):
        self.installed(record())
        self.store.journal("install", "app", source(), record("v2"))
        self.installed(record("v2"))
        self.store.recover()
        self.assertEqual(self.store.locks["app"], record("v2"))
        self.assertTrue((self.store.data / "bin/app").is_symlink())
        self.assertFalse((self.store.data / "pending.json").exists())

    def test_interrupt_before_switch_keeps_previous(self):
        self.installed(record())
        self.store.journal("install", "app", source(), record("v2"))
        self.store.recover()
        self.assertEqual(self.store.locks["app"], record())

    def test_partial_config_write_recovers(self):
        self.store.journal("save", "app", source(), record("v2"))
        o.atomic_json(
            self.store.config / "sources.json", {"schema": 1, "apps": {"app": source()}}
        )
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks["app"], record("v2"))

    def test_rollback_reconciles_actual_version_and_pins(self):
        self.installed(record("v2"))
        self.store.locks["app"] = record("v2")
        self.store.previous_generation = lambda name: (1, record())
        with patch.object(
            o, "run", side_effect=lambda *a, **kw: self.installed(record())
        ):
            self.store.rollback("app")
        self.assertEqual(self.store.locks["app"], record())
        self.assertTrue(self.store.sources["app"]["pinned"])

    def test_failed_rollback_does_not_pin(self):
        self.installed(record())
        self.store.previous_generation = lambda name: (1, record("v0"))
        with (
            patch.object(o, "run", side_effect=o.Error("no previous generation")),
            self.assertRaises(o.Error),
        ):
            self.store.rollback("app")
        self.assertFalse(self.store.sources["app"]["pinned"])

    def test_rollback_skips_removed_generations(self):
        profiles = self.store.profile("app").parent
        profiles.mkdir(parents=True, exist_ok=True)
        for number, version in [(1, "v1"), (2, None), (3, "v2")]:
            target = self.root / f"generation-{number}"
            target.mkdir()
            if version:
                o.atomic_json(target / "share/obtain/manifest.json", record(version))
            (profiles / f"app-{number}-link").symlink_to(target)
        self.store.profile("app").symlink_to("app-3-link")
        self.assertEqual(self.store.previous_generation("app"), (1, record()))

    def test_rollback_never_uses_a_different_repository(self):
        profiles = self.store.profile("app").parent
        profiles.mkdir(parents=True, exist_ok=True)
        other = dict(record("v0"), repository="other/repo")
        for number, value in [(1, other), (2, record())]:
            target = self.root / f"generation-{number}"
            o.atomic_json(target / "share/obtain/manifest.json", value)
            (profiles / f"app-{number}-link").symlink_to(target)
        self.store.profile("app").symlink_to("app-2-link")
        with self.assertRaisesRegex(o.Error, "No previous retained"):
            self.store.previous_generation("app")

    def test_remove_uninstalled_source(self):
        self.store.remove("app")
        self.assertEqual(self.store.sources, {})
        self.assertEqual(self.store.locks, {})

    def test_failed_remove_keeps_tracking(self):
        self.installed(record())
        with (
            patch.object(o, "run", side_effect=o.Error("failure")),
            self.assertRaises(o.Error),
        ):
            self.store.remove("app")
        self.assertIn("app", self.store.sources)

    def test_refuses_launcher_collision_before_build(self):
        path = self.store.applications / "obtain-app.desktop"
        path.parent.mkdir(parents=True)
        path.write_text("user content")
        with patch.object(o, "nix") as nix, self.assertRaisesRegex(o.Error, "Refusing"):
            self.store.install("app", source(), record())
        nix.assert_not_called()
        self.assertEqual(path.read_text(), "user content")

    def test_concurrent_commands_rejected(self):
        with self.store.session(), self.assertRaisesRegex(o.Error, "Another Obtain"):
            with o.Store().session():
                pass

    def test_check_does_not_download_or_install(self):
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(github, "release", return_value=candidate("v2")),
            patch.object(o, "nix") as nix,
        ):
            o.dispatch(o.parser().parse_args(["check"]), self.store, github)
        nix.assert_not_called()
        self.assertEqual(self.store.locks["app"], record())

    def test_pin_skips_update(self):
        self.store.sources["app"]["pinned"] = True
        github = o.GitHub(self.root / "cache")
        with patch.object(github, "release") as release:
            o.dispatch(o.parser().parse_args(["update"]), self.store, github)
        release.assert_not_called()

    def test_track_only_update_stays_uninstalled(self):
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(github, "release", return_value=candidate("v2")),
            patch.object(o, "lock_release", return_value=record("v2")),
            patch.object(self.store, "install") as install,
        ):
            o.dispatch(o.parser().parse_args(["update"]), self.store, github)
        install.assert_not_called()
        self.assertEqual(self.store.locks["app"], record("v2"))

    def test_check_reports_partial_failure(self):
        self.store.sources["other"] = source()
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(
                github, "release", side_effect=[o.Error("offline"), candidate()]
            ) as release,
            self.assertRaises(o.Error),
        ):
            o.dispatch(o.parser().parse_args(["check"]), self.store, github)
        self.assertEqual(release.call_count, 2)


class GitHubTests(unittest.TestCase):
    def test_stable_uses_latest_and_paginates_assets(self):
        gh = o.GitHub(Path("/unused"))
        responses = [
            dict(id=42, tag_name="v1", draft=False, prerelease=False),
            [asset(f"file-{i}.txt") for i in range(100)],
            [asset("app-x64.AppImage")],
        ]
        with patch.object(gh, "get", side_effect=responses) as get:
            self.assertEqual(gh.release(source())["asset_name"], "app-x64.AppImage")
        self.assertTrue(get.call_args_list[0].args[0].endswith("/latest"))
        self.assertTrue(get.call_args_list[-1].args[0].endswith("page=2"))

    def test_draft_and_prerelease_cannot_be_stable(self):
        gh = o.GitHub(Path("/unused"))
        for extra in [dict(draft=True), dict(prerelease=True)]:
            with (
                patch.object(gh, "get", return_value=dict(id=1, **extra)),
                self.assertRaises(o.Error),
            ):
                gh.release(source())

    def test_prerelease_opt_in(self):
        gh = o.GitHub(Path("/unused"))
        releases = [
            dict(id=1, tag_name="v1", published_at="2025-01-01"),
            dict(id=2, tag_name="v2-beta", published_at="2026-01-01", prerelease=True),
            dict(id=3, tag_name="draft", draft=True, published_at="2027-01-01"),
        ]
        with patch.object(
            gh, "get", side_effect=[releases, [asset("app-x64.AppImage")]]
        ):
            self.assertEqual(
                gh.release(dict(source(), prereleases=True))["version"], "v2-beta"
            )

    def test_cached_metadata_on_304(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            url = "https://api.github.com/repos/owner/app/releases/latest"
            cache = Path(tmp) / (o.hashlib.sha256(url.encode()).hexdigest() + ".json")
            o.atomic_json(cache, {"etag": "abc", "body": {"tag_name": "v1"}})
            with patch.object(
                gh.opener,
                "open",
                side_effect=urllib.error.HTTPError(url, 304, "cached", {}, None),
            ) as get:
                self.assertEqual(
                    gh.get("/repos/owner/app/releases/latest"), {"tag_name": "v1"}
                )
            self.assertEqual(get.call_args.args[0].get_header("If-none-match"), "abc")

    def test_rate_limit_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            with (
                patch.object(
                    gh.opener,
                    "open",
                    side_effect=urllib.error.HTTPError("url", 403, "limited", {}, None),
                ),
                self.assertRaisesRegex(o.Error, "GITHUB_TOKEN"),
            ):
                gh.get("/repos/owner/app/releases/latest")


if __name__ == "__main__":
    unittest.main()
