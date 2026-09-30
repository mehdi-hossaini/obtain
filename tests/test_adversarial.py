"""Adversarial metadata and atomic-state invariants, independent of upstream services."""

import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_obtain import o, candidate, source


class AdversarialTests(unittest.TestCase):
    def test_non_object_release_metadata_is_rejected(self):
        github = o.GitHub(Path("/unused"))
        for payload in (None, [], 7, "bad"):
            with (
                self.subTest(payload=payload),
                patch.object(github, "get", return_value=payload),
                self.assertRaises(o.Error),
            ):
                github.release_assets(source())

    def test_invalid_release_lists_are_rejected(self):
        github = o.GitHub(Path("/unused"))
        for payload in (None, {}, [None], [7], ["bad"]):
            with (
                self.subTest(payload=payload),
                patch.object(github, "get", return_value=payload),
                self.assertRaises(o.Error),
            ):
                github.release_assets(dict(source(), prereleases=True))

    def test_invalid_asset_metadata_is_rejected(self):
        for payload in (
            None,
            {},
            [None],
            [7],
            [{"name": None}],
            [{"name": 7}],
            [{"name": []}],
        ):
            with self.subTest(payload=payload), self.assertRaises(o.Error):
                o.select_asset(payload)

    def test_unsupported_formats_never_become_appimages(self):
        for suffix in (
            "deb",
            "rpm",
            "tar.gz",
            "tar.xz",
            "zip",
            "exe",
            "dmg",
            "apk",
            "nix",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(o.Error):
                o.select_asset([{"name": "program-x86_64." + suffix}], "*")

    def test_malformed_digest_is_rejected_before_nix_runs(self):
        for digest in ({"hash": "bad"}, ["bad"], 7):
            with (
                self.subTest(digest=digest),
                patch.object(o, "nix") as nix,
                self.assertRaises(o.Error),
            ):
                o.lock_release("app", dict(candidate(), digest=digest))
            nix.assert_not_called()

    def test_asset_urls_cannot_change_host_repository_or_protocol(self):
        bad = [
            None,
            42,
            "http://github.com/owner/app/releases/download/v1/app.AppImage",
            "https://github.com.evil.invalid/owner/app/releases/download/v1/app.AppImage",
            "https://github.com/other/app/releases/download/v1/app.AppImage",
            "file:///etc/passwd",
            "https://github.com/owner/app/releases/download/v1/app.AppImage?token=x",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(o.Error):
                o.asset_url(url, "owner/app")

    def test_failed_atomic_replace_preserves_previous_document_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            o.atomic_json(path, {"previous": True})
            before = path.read_bytes()
            with (
                patch.object(
                    o.os,
                    "replace",
                    side_effect=OSError(errno.ENOSPC, "No space left on device"),
                ),
                self.assertRaises(OSError),
            ):
                o.atomic_json(path, {"previous": False})
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))
