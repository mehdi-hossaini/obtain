"""Batch checks should avoid profiles and preserve durable progress."""

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from test_obtain import o, candidate, record, source
import test_obtain as support


class BatchEfficiencyTests(unittest.TestCase):
    setUp = support.StateTests.setUp
    tearDown = support.StateTests.tearDown

    def test_check_uses_metadata_even_with_broken_installed_manifests(self):
        self.store.sources["other"] = source()
        for name in ("app", "other"):
            path = self.store.profile(name) / "share/obtain/manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text("{broken json")
        gh = o.GitHub(self.root / "cache")
        with (
            patch.object(gh, "release", return_value=candidate()) as fetch,
            patch.object(
                self.store, "installed", side_effect=AssertionError("profile read")
            ) as read,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(o.parser().parse_args(["check"]), self.store, gh)
        self.assertEqual(fetch.call_count, 2)
        read.assert_not_called()
        self.assertEqual(
            o.load_json(self.store.data / "check-batch.json"),
            {"command": "check", "unfinished": [], "asset_overrides": {}},
        )

    def test_update_reads_profile_to_repair_outdated_install(self):
        gh = o.GitHub(self.root / "cache")
        old_install = record("v0")
        with (
            patch.object(gh, "release", return_value=candidate()),
            patch.object(self.store, "installed", return_value=old_install) as read,
            patch.object(self.store, "install") as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(o.parser().parse_args(["update", "app"]), self.store, gh)
        read.assert_called_once_with("app")
        install.assert_called_once_with(
            "app", self.store.sources["app"], self.store.locks["app"]
        )

    def test_checkpoints_keep_order_and_failed_names_after_later_success(self):
        self.store.sources.update({name: source() for name in ("b", "c", "d")})
        gh = o.GitHub(self.root / "cache")
        batch_path = self.store.data / "check-batch.json"
        snapshots = []
        results = iter([candidate(), o.Error("bad release"), candidate(), candidate()])

        def fetch_candidate(src):
            snapshots.append(o.load_batch(batch_path)["unfinished"])
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            patch.object(
                gh,
                "release",
                side_effect=fetch_candidate,
            ) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(o.Error, "Unfinished apps: b"),
        ):
            o.dispatch(o.parser().parse_args(["check"]), self.store, gh)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(
            snapshots,
            [["app", "b", "c", "d"], ["b", "c", "d"], ["b", "c", "d"], ["b", "d"]],
        )
        self.assertEqual(o.load_json(batch_path)["unfinished"], ["b"])
        with (
            patch.object(gh, "release", return_value=candidate()) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(
                o.parser().parse_args(["check", "--retry-failed"]), self.store, gh
            )
        fetch.assert_called_once()
        self.assertEqual(o.load_json(batch_path)["unfinished"], [])

    def test_progress_bytes_scale_linearly_even_for_pinned_apps(self):
        totals = []
        for count in (20, 200):
            self.store.sources = {
                f"app-{i:04d}": dict(source(), pinned=True) for i in range(count)
            }
            writes = []

            def count_write(path, value):
                writes.append(len(json.dumps(value)))

            with (
                patch.object(o, "atomic_json", side_effect=count_write) as snapshots,
                patch.object(o, "append_json", side_effect=count_write),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                o.dispatch(
                    o.parser().parse_args(["update"]),
                    self.store,
                    o.GitHub(self.root / "cache"),
                )
            self.assertEqual(snapshots.call_count, 2)
            totals.append(sum(writes))
        self.assertLess(totals[1], totals[0] * 11)

    def test_interrupted_progress_retries_only_unfinished_and_partial_tail(self):
        self.store.sources.update(b=source(), c=source())
        path = self.store.data / "check-batch.json"
        o.atomic_json(
            path,
            {
                "command": "check",
                "unfinished": ["app", "b", "c"],
                "asset_overrides": {},
            },
        )
        o.append_json(path, "app")
        with path.open("ab") as stream:
            stream.write(b'"b')
        gh = o.GitHub(self.root / "cache")
        with (
            patch.object(gh, "release", return_value=candidate()) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            o.dispatch(
                o.parser().parse_args(["check", "--retry-failed"]), self.store, gh
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(o.load_json(path)["unfinished"], [])

    def test_many_state_changes_write_full_snapshot_only_once(self):
        real_atomic = o.atomic_json
        full_writes = []

        def observe(path, value):
            if Path(path).name in ("sources.json", "lock.json"):
                full_writes.append(path)
            real_atomic(path, value)

        with (
            patch.object(o, "atomic_json", side_effect=observe),
            self.store.batch_saves(),
        ):
            for i in range(20):
                name = f"app-{i}"
                self.store.save_record(name, source(), dict(record(), name=name))
        self.assertEqual(len(full_writes), 2)
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks, self.store.locks)

    def test_state_events_recover_partial_snapshot_and_torn_tail(self):
        # Simulate failure after writing sources.json, before lock.json, during
        # end-of-batch compaction. Durable app events must restore both files.
        real_atomic = o.atomic_json

        def fail_lock(path, value):
            if Path(path).name == "lock.json":
                raise OSError("interrupted snapshot")
            real_atomic(path, value)

        with self.assertRaisesRegex(OSError, "interrupted snapshot"):
            with (
                patch.object(o, "atomic_json", side_effect=fail_lock),
                self.store.batch_saves(),
            ):
                self.store.save_record("app", dict(source(), pinned=True), record("v2"))
                self.store.save_record(
                    "other", source(), dict(record("v3"), name="other")
                )
        events = self.store.data / "state-events.jsonl"
        with events.open("ab") as stream:
            stream.write(b'{"name":')
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks["app"]["version"], "v2")
            self.assertTrue(fresh.sources["app"]["pinned"])
            self.assertEqual(fresh.locks["other"]["version"], "v3")
            fresh.save()
        self.assertFalse(events.exists())

    def test_unreconciled_state_failure_stops_before_next_app(self):
        self.store.sources["b"] = source()
        gh = o.GitHub(self.root / "cache")

        def fail_save(name, src, rec):
            self.store.journal("save", name, src, rec)
            raise OSError("disk failure")

        with (
            patch.object(gh, "release", return_value=candidate("v2")) as fetch,
            patch.object(o, "lock_release", return_value=record("v2")),
            patch.object(self.store, "save_record", side_effect=fail_save),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(o.Error, "Unfinished apps: app, b"),
        ):
            o.dispatch(o.parser().parse_args(["update"]), self.store, gh)
        self.assertEqual(fetch.call_count, 1)
        with o.Store().session() as fresh:
            self.assertEqual(fresh.locks["app"]["version"], "v2")

    def test_pinned_asset_override_explains_unpin_and_preserves_retry(self):
        self.store.sources["app"]["pinned"] = True
        gh = o.GitHub(self.root / "cache")
        stderr = io.StringIO()
        with (
            patch.object(gh, "release") as fetch,
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(o.Error, "Unfinished apps: app"),
        ):
            o.dispatch(
                o.parser().parse_args(["update", "app", "--asset", "new-*"]),
                self.store,
                gh,
            )
        fetch.assert_not_called()
        self.assertIn("obtain unpin app", stderr.getvalue())
        report = o.load_batch(self.store.data / "update-batch.json")
        self.assertEqual(report["asset_overrides"], {"app": "new-*"})
        self.assertEqual(report["unfinished"], ["app"])
        self.assertIsNone(self.store.sources["app"]["asset"])
