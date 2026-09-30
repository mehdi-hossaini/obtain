"""Moved GitHub repository handling keeps API redirects and locks constrained."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import test_obtain as base

o = base.o


class RedirectValidationTests(unittest.TestCase):
    def test_only_same_origin_numeric_identity_and_unchanged_endpoint(self):
        request = "/repos/old-owner/old-app/releases/latest?per_page=100&page=2"
        valid = (
            "https://api.github.com/repositories/1418150/releases/latest"
            "?per_page=100&page=2"
        )
        self.assertEqual(o.moved_repository_id(request, valid), 1418150)
        for location in (
            valid.replace("https:", "http:"),
            valid.replace("api.github.com", "evil.example"),
            valid.replace("api.github.com", "api.github.com@evil.example"),
            valid.replace("api.github.com", "api.github.com:443"),
            valid.replace("/repositories/1418150", "/repositories/0"),
            valid.replace("/repositories/1418150", "/repositories/not-an-id"),
            valid.replace("/releases/latest", "/releases/other"),
            valid.replace("page=2", "page=3"),
            valid + "#fragment",
            "\n" + valid,
            valid + "\r",
            "https://[invalid",
        ):
            with self.subTest(location=location):
                self.assertIsNone(o.moved_repository_id(request, location))

    def test_github_get_signals_one_redirect_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            github = o.GitHub(Path(tmp))
            requests = []

            def redirect(request, timeout):
                requests.append(request.full_url)
                raise urllib.error.HTTPError(
                    request.full_url,
                    301,
                    "Moved Permanently",
                    {
                        "Location": "https://api.github.com/repositories/1418150/releases/latest"
                    },
                    None,
                )

            with patch.object(github.opener, "open", side_effect=redirect):
                with self.assertRaises(o.RepositoryMoved) as raised:
                    github.get("/repos/bjorn/tiled/releases/latest")
            self.assertEqual(raised.exception.repository_id, 1418150)
            self.assertEqual(
                requests, ["https://api.github.com/repos/bjorn/tiled/releases/latest"]
            )

    def test_malformed_redirect_stays_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            github = o.GitHub(Path(tmp))
            error = urllib.error.HTTPError(
                "https://api.github.com/repos/bjorn/tiled/releases/latest",
                301,
                "Moved Permanently",
                {
                    "Location": "https://evil.example/repositories/1418150/releases/latest"
                },
                None,
            )
            with (
                patch.object(github.opener, "open", side_effect=error),
                self.assertRaisesRegex(
                    o.Error, "Add its current canonical URL"
                ) as raised,
            ):
                github.get("/repos/bjorn/tiled/releases/latest")
            self.assertNotIsInstance(raised.exception, o.RepositoryMoved)

    def test_canonical_metadata_requires_matching_integer_id_and_valid_name(self):
        github = o.GitHub(Path("/unused"))
        with patch.object(
            github, "get", return_value={"id": 1418150, "full_name": "mapeditor/tiled"}
        ):
            self.assertEqual(github.canonical_repository(1418150), "mapeditor/tiled")
        for metadata in (
            {"id": True, "full_name": "mapeditor/tiled"},
            {"id": 7, "full_name": "mapeditor/tiled"},
            {"id": 1418150, "full_name": "evil/../tiled"},
            {"id": 1418150, "full_name": "mapeditor/tiled?ref=bad"},
            {"id": 1418150, "full_name": "/mapeditor/tiled"},
            {"id": 1418150, "full_name": "mapeditor/tiled.git"},
            {"id": 1418150, "full_name": 7},
        ):
            with (
                self.subTest(metadata=metadata),
                patch.object(github, "get", return_value=metadata),
                self.assertRaises(o.Error),
            ):
                github.canonical_repository(1418150)


class MovedAddTests(unittest.TestCase):
    setUp = base.StateTests.setUp
    tearDown = base.StateTests.tearDown

    def test_add_retries_once_and_saves_canonical_source_and_lock(self):
        self.store.sources = {}
        self.store.locks = {}
        # The old URL's default local name is occupied by a different app.
        self.store.sources["old-app"] = base.source()
        self.store.locks["old-app"] = dict(base.record(), name="old-app")
        self.store.save()
        github = o.GitHub(self.root / "cache")
        calls = []
        release = {"id": 12, "tag_name": "v1"}
        asset = {
            "id": 34,
            "name": "new-app-x86_64.AppImage",
            "browser_download_url": "https://github.com/new-owner/new-app/releases/download/v1/new-app-x86_64.AppImage",
        }

        def release_assets(source):
            calls.append(source["repository"])
            if source["repository"] == "old-owner/old-app":
                raise o.RepositoryMoved(1418150)
            return release, [asset]

        with (
            patch.object(github, "release_assets", side_effect=release_assets),
            patch.object(
                github,
                "get",
                return_value={"id": 1418150, "full_name": "new-owner/new-app"},
            ) as get,
            patch.object(
                o,
                "nix",
                return_value=json.dumps({"hash": "sha256-" + "A" * 43 + "="}),
            ),
        ):
            o.add_command(
                o.parser().parse_args(
                    ["add", "https://github.com/old-owner/old-app", "--track-only"]
                ),
                self.store,
                github,
            )
        self.assertEqual(
            calls, ["old-owner/old-app", "new-owner/new-app", "new-owner/new-app"]
        )
        get.assert_called_once_with("/repositories/1418150")
        self.assertEqual(
            self.store.sources["new-app"]["repository"], "new-owner/new-app"
        )
        self.assertEqual(self.store.locks["new-app"]["repository"], "new-owner/new-app")
        self.assertEqual(self.store.sources["old-app"]["repository"], "owner/app")
        o.validate_source_lock(
            self.store.sources["new-app"], self.store.locks["new-app"], "new-app"
        )

    def test_second_redirect_stops_without_saving_state(self):
        self.store.sources = {}
        self.store.locks = {}
        github = o.GitHub(self.root / "cache")
        with (
            patch.object(
                github, "release_assets", side_effect=o.RepositoryMoved(1418150)
            ) as releases,
            patch.object(
                github, "canonical_repository", return_value="mapeditor/tiled"
            ) as canonical,
            self.assertRaises(o.RepositoryMoved),
        ):
            o.add_command(
                o.parser().parse_args(
                    ["add", "https://github.com/bjorn/tiled", "--track-only"]
                ),
                self.store,
                github,
            )
        self.assertEqual(releases.call_count, 2)
        canonical.assert_called_once_with(1418150)
        self.assertEqual(self.store.sources, {})
        self.assertEqual(self.store.locks, {})


if __name__ == "__main__":
    unittest.main()
