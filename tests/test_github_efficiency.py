"""GitHub metadata reuse within one command."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from test_obtain import asset, o, source
import test_obtain as support


class Response(io.BytesIO):
    def __init__(self, body, etag='"current"'):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag}


class GitHubEfficiencyTests(unittest.TestCase):
    def test_cache_write_failure_keeps_fresh_metadata_and_command_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            path = "/repos/owner/app/releases/latest"
            with (
                patch.object(o, "atomic_json", side_effect=OSError("cache disk full")),
                patch.object(
                    gh.opener,
                    "open",
                    side_effect=[
                        Response({"tag_name": "v1"}),
                        Response({"tag_name": "v2"}),
                    ],
                ) as open_api,
            ):
                self.assertEqual(gh.get(path), {"tag_name": "v1"})
                self.assertEqual(gh.get(path), {"tag_name": "v1"})
                self.assertEqual(open_api.call_count, 1)
                gh.clear_memo()
                self.assertEqual(gh.get(path), {"tag_name": "v2"})
                self.assertEqual(open_api.call_count, 2)

    def test_unreadable_cache_is_ignored_without_conditional_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            with (
                patch.object(
                    o, "load_json", side_effect=PermissionError("cache denied")
                ),
                patch.object(
                    gh.opener, "open", return_value=Response({"ok": True})
                ) as open_api,
            ):
                self.assertEqual(gh.get("/test"), {"ok": True})
            self.assertIsNone(open_api.call_args.args[0].get_header("If-none-match"))

    def test_cache_path_blocked_by_file_does_not_block_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.write_text("unrelated file")
            gh = o.GitHub(cache)
            with patch.object(gh.opener, "open", return_value=Response({"ok": True})):
                self.assertEqual(gh.get("/test"), {"ok": True})
                self.assertEqual(gh.get("/test"), {"ok": True})
            self.assertEqual(cache.read_text(), "unrelated file")

    def test_cache_etag_without_body_is_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            with (
                patch.object(o, "load_json", return_value={"etag": '"incomplete"'}),
                patch.object(
                    gh.opener, "open", return_value=Response({"ok": True})
                ) as open_api,
            ):
                self.assertEqual(gh.get("/test"), {"ok": True})
            self.assertIsNone(open_api.call_args.args[0].get_header("If-none-match"))

    def test_oversized_response_is_rejected_before_cache_or_memo(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            oversized = io.BytesIO(b" " * (o.MAX_GITHUB_RESPONSE_BYTES + 1))
            oversized.headers = {"ETag": '"oversized"'}
            with patch.object(gh.opener, "open", return_value=oversized):
                with self.assertRaisesRegex(o.Error, "exceeds the 8 MiB limit"):
                    gh.get("/repos/owner/app/releases/latest")
            self.assertEqual(list(Path(tmp).glob("*.json")), [])
            self.assertEqual(gh._memo, {})

    def test_oversized_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            path = "/repos/owner/app/releases/latest"
            url = "https://api.github.com" + path
            cache = Path(tmp) / (o.hashlib.sha256(url.encode()).hexdigest() + ".json")
            cache.write_bytes(b" " * (o.MAX_GITHUB_CACHE_BYTES + 1))
            with patch.object(
                gh.opener, "open", return_value=Response({"ok": True})
            ) as open_api:
                self.assertEqual(gh.get(path), {"ok": True})
            self.assertIsNone(open_api.call_args.args[0].get_header("If-none-match"))

    def test_success_is_reused_without_exposing_mutations_and_reset_revalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            path = "/repos/owner/app/releases/latest"
            with patch.object(
                gh.opener,
                "open",
                side_effect=[
                    Response({"tag_name": "v1"}),
                    Response({"tag_name": "v2"}),
                ],
            ) as open_api:
                first = gh.get(path)
                first["tag_name"] = "changed"
                self.assertEqual(gh.get(path)["tag_name"], "v1")
                self.assertEqual(open_api.call_count, 1)

                gh.clear_memo()
                self.assertEqual(gh.get(path)["tag_name"], "v2")
                self.assertEqual(open_api.call_count, 2)
                self.assertEqual(
                    open_api.call_args.args[0].get_header("If-none-match"),
                    '"current"',
                )

    def test_release_and_all_asset_pages_are_reused_for_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            releases = {"id": 42, "tag_name": "v1", "draft": False, "prerelease": False}
            pages = [asset(f"file-{i}.txt") for i in range(100)]
            last = asset("app-x64.AppImage")
            responses = [
                Response(releases),
                Response(pages),
                Response([last]),
                Response(releases),
                Response(pages),
                Response([last]),
            ]
            with patch.object(gh.opener, "open", side_effect=responses) as open_api:
                self.assertEqual(gh.release(source())["asset_name"], last["name"])
                self.assertEqual(gh.release(source())["asset_name"], last["name"])
                self.assertEqual(open_api.call_count, 3)

                gh.clear_memo()
                self.assertEqual(gh.release(source())["asset_name"], last["name"])
                self.assertEqual(open_api.call_count, 6)
                self.assertIn("page=2", open_api.call_args.args[0].full_url)

    def test_not_modified_response_is_reused_within_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            path = "/repos/owner/app/releases/latest"
            not_modified = urllib.error.HTTPError("url", 304, "unchanged", {}, None)
            with patch.object(
                gh.opener,
                "open",
                side_effect=[Response({"tag_name": "v1"}), not_modified],
            ) as open_api:
                gh.get(path)
                gh.clear_memo()
                self.assertEqual(gh.get(path), {"tag_name": "v1"})
                self.assertEqual(gh.get(path), {"tag_name": "v1"})
                self.assertEqual(open_api.call_count, 2)

    def test_cache_evicts_old_responses_and_skips_oversized_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            with patch.object(
                gh.opener, "open", side_effect=lambda *a, **kw: Response({"ok": True})
            ) as open_api:
                for index in range(33):
                    gh.get(f"/test/{index}")
                gh.get("/test/32")
                self.assertEqual(open_api.call_count, 33)
                gh.get("/test/0")
                self.assertEqual(open_api.call_count, 34)
            gh.clear_memo()
            with patch.object(
                gh.opener,
                "open",
                side_effect=lambda *a, **kw: Response({"body": "x" * 1_100_000}),
            ) as open_api:
                gh.get("/large/1")
                gh.get("/large/2")
                gh.get("/large/1")
                self.assertEqual(open_api.call_count, 3)
            gh.clear_memo()
            with patch.object(
                gh.opener,
                "open",
                side_effect=lambda *a, **kw: Response({"body": "x" * 2_000_000}),
            ) as open_api:
                gh.get("/oversized")
                gh.get("/oversized")
                self.assertEqual(open_api.call_count, 2)

    def test_rate_limit_is_not_memoized(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = o.GitHub(Path(tmp))
            path = "/repos/owner/app/releases/latest"
            failure = urllib.error.HTTPError(
                "https://api.github.com" + path,
                429,
                "limited",
                {"Retry-After": "1"},
                None,
            )
            with patch.object(
                gh.opener, "open", side_effect=[failure, Response({"tag_name": "v1"})]
            ) as open_api:
                with self.assertRaises(o.RateLimited):
                    gh.get(path)
                self.assertEqual(gh.get(path)["tag_name"], "v1")
                self.assertEqual(open_api.call_count, 2)


class CommandReuseTests(unittest.TestCase):
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown

    def test_batch_aliases_reuse_metadata_but_next_command_revalidates(self):
        self.store.sources["alias"] = source()
        gh = o.GitHub(self.root / "cache")
        release = {"id": 42, "tag_name": "v1", "draft": False, "prerelease": False}
        responses = [
            Response(release),
            Response([asset("app-x64.AppImage")]),
            Response(release),
            Response([asset("app-x64.AppImage")]),
        ]
        args = o.parser().parse_args(["check"])
        with (
            patch.object(gh.opener, "open", side_effect=responses) as open_api,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(args, self.store, gh)
            self.assertEqual(open_api.call_count, 2)
            o.dispatch(args, self.store, gh)
            self.assertEqual(open_api.call_count, 4)
            self.assertEqual(
                open_api.call_args.args[0].get_header("If-none-match"),
                '"current"',
            )


if __name__ == "__main__":
    unittest.main()
