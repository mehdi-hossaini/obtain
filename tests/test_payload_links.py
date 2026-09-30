"""Real-bundle file links, and attacks that must never become filesystem writes."""

import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_improvements import payload, elf
from test_obtain import o, asset


def bundle(path, kind, entries):
    if kind == "tar":
        with tarfile.open(path, "w:xz") as archive:
            for name, contents, symbolic in entries:
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                if symbolic:
                    info.type = tarfile.SYMTYPE
                    info.linkname = contents
                    archive.addfile(info)
                else:
                    info.size = len(contents)
                    archive.addfile(info, io.BytesIO(contents))
    else:
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents, symbolic in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (
                    (stat.S_IFLNK if symbolic else stat.S_IFREG) | 0o755
                ) << 16
                archive.writestr(info, contents)


class LinkTests(unittest.TestCase):
    def test_internal_file_links_and_program_alias_survive_stripping(self):
        entries = [
            ("bundle/lib/libprobe.so", "libprobe.so.1", True),
            ("bundle/lib/libprobe.so.1", "libprobe.so.1.0", True),
            ("bundle/lib/libprobe.so.1.0", b"library", False),
            ("bundle/bin/library", "../lib/libprobe.so", True),
            ("bundle/app", "bin/app", True),
            ("bundle/bin/app", elf(), False),
        ]
        for kind in ("tar", "zip"):
            for reverse in (False, True):
                with (
                    self.subTest(kind=kind, reverse=reverse),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    source = root / "download"
                    bundle(source, kind, entries[::-1] if reverse else entries)
                    payload.prepare("archive", source, root / "out", "app", 1)
                    self.assertTrue((root / "out/app").is_symlink())
                    self.assertEqual((root / "out/app").read_bytes(), elf())
                    self.assertEqual(
                        (root / "out/bin/library").read_bytes(), b"library"
                    )

    def test_external_dangling_and_directory_links_rejected(self):
        for target in (
            "/etc/passwd",
            "../../outside",
            "missing",
            "dir",
            "\\outside",
            "",
            "missing/../dir/data",
            "dir/data/../data",
            "dir/data/.",
            "dir/data/",
        ):
            for kind in ("tar", "zip"):
                with (
                    self.subTest(target=target, kind=kind),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    source = root / "download"
                    bundle(
                        source,
                        kind,
                        [("dir/data", b"safe", False), ("link", target, True)],
                    )
                    with self.assertRaises(ValueError):
                        payload.extract(source, root / "out")
                    self.assertFalse((root / "out/link").is_symlink())

    def test_cycle_and_long_chain_rejected_in_either_archive_order(self):
        chains = [
            [("a", "b", True), ("b", "a", True)],
            [(f"link{i}", f"link{i + 1}", True) for i in range(40)]
            + [("link40", b"end", False)],
        ]
        for entries in chains:
            for reverse in (False, True):
                with (
                    self.subTest(reverse=reverse, length=len(entries)),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    bundle(
                        root / "download", "tar", entries[::-1] if reverse else entries
                    )
                    with self.assertRaisesRegex(ValueError, "cycle|depth"):
                        payload.extract(root / "download", root / "out")
                    self.assertFalse(
                        any(p.is_symlink() for p in (root / "out").rglob("*"))
                    )

    def test_no_file_write_can_traverse_an_archive_link(self):
        entries = [
            ("pivot", "real", True),
            ("pivot/escaped", b"bad", False),
            ("real", b"safe", False),
        ]
        for kind in ("tar", "zip"):
            for reverse in (False, True):
                with (
                    self.subTest(kind=kind, reverse=reverse),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    bundle(
                        root / "download", kind, entries[::-1] if reverse else entries
                    )
                    with self.assertRaises(ValueError):
                        payload.extract(root / "download", root / "out")
                    self.assertFalse((root / "escaped").exists())
                    self.assertFalse((root / "out/pivot").is_symlink())

    def test_preexisting_destination_symlinks_are_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside").mkdir()
            (root / "out").mkdir()
            (root / "out/pivot").symlink_to(root / "outside", target_is_directory=True)
            bundle(root / "download", "tar", [("pivot/escaped", b"bad", False)])
            with self.assertRaisesRegex(ValueError, "empty"):
                payload.extract(root / "download", root / "out")
            self.assertFalse((root / "outside/escaped").exists())
            with self.assertRaisesRegex(ValueError, "symbolic"):
                payload.extract(root / "download", root / "out/pivot")

    def test_links_count_toward_archive_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(
                root / "download",
                "tar",
                [("a", "file", True), ("b", "file", True), ("file", b"ok", False)],
            )
            with (
                patch.object(payload, "MAX_FILES", 2),
                self.assertRaisesRegex(ValueError, "limit"),
            ):
                payload.extract(root / "download", root / "out")

    def test_tar_link_target_limit_counts_encoded_bytes_in_both_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            bundle(source, "tar", [("app", elf(), False), ("alias", "é" * 2049, True)])
            for operation in (
                lambda: payload.discover_programs(source),
                lambda: payload.extract(source, root / "out"),
            ):
                with self.assertRaisesRegex(ValueError, "Unsafe archive link target"):
                    operation()

    def test_txz_is_an_archive_and_never_a_raw_binary(self):
        candidate = asset("shotcut-linux-x86_64-26.9.27.txz")
        self.assertEqual(o.select_asset([candidate], kind="archive"), candidate)
        self.assertEqual(o.asset_candidates([candidate], kind="binary"), [])
        source = asset("shotcut-src-26.9.27.txz")
        self.assertEqual(
            o.select_asset([candidate, source], "*.txz", "archive"), candidate
        )


if __name__ == "__main__":
    unittest.main()
