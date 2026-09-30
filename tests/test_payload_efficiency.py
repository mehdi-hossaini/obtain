"""Resource-bound regressions for payload archive extraction."""

import importlib.util
import io
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "payload", Path(__file__).resolve().parents[1] / "payload.py"
)
payload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(payload)


def elf():
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 16, 2)
    struct.pack_into("<H", header, 18, 62)
    struct.pack_into("<Q", header, 24, 0x1000)
    return bytes(header)


class PayloadEfficiencyTests(unittest.TestCase):
    def test_path_limits_apply_before_path_parts_are_allocated(self):
        for name in (
            "é" * 2050,
            "/".join(["a"] * 129),
            "app\0escaped",
        ):
            with self.subTest(size=len(name)):
                with (
                    patch.object(
                        payload, "PurePosixPath", side_effect=AssertionError("parsed")
                    ),
                    self.assertRaises(ValueError),
                ):
                    payload.member_path(name)

    def test_discovery_bounds_implicit_directory_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "download"
            with tarfile.open(source, "w") as archive:
                item = tarfile.TarInfo("a/b/c/d/app")
                item.size = 64
                archive.addfile(item, io.BytesIO(elf()))
            with patch.object(payload, "MAX_FILES", 3):
                with self.assertRaisesRegex(ValueError, "implicit-directory"):
                    payload.discover_programs(source)

    def test_directory_budget_applies_to_discovery_and_explicit_preparation(self):
        program = "a/b/c/app"
        for kind in ("tar", "zip"):
            for limit in (2, 3, 4):
                with (
                    self.subTest(kind=kind, limit=limit),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    source = root / "download"
                    if kind == "zip":
                        with zipfile.ZipFile(source, "w") as archive:
                            archive.writestr(program, elf())
                    else:
                        with tarfile.open(source, "w") as archive:
                            item = tarfile.TarInfo(program)
                            item.size = 64
                            archive.addfile(item, io.BytesIO(elf()))
                    with patch.object(payload, "MAX_FILES", limit):
                        if limit < 3:
                            with self.assertRaisesRegex(
                                ValueError, "implicit-directory"
                            ):
                                payload.discover_programs(source)
                            with self.assertRaisesRegex(
                                ValueError, "implicit-directory"
                            ):
                                payload.prepare(
                                    "archive", source, root / "out", program
                                )
                            self.assertEqual(list((root / "out").iterdir()), [])
                        else:
                            self.assertEqual(
                                payload.discover_programs(source), [program]
                            )
                            payload.prepare("archive", source, root / "out", program)
                            self.assertEqual(
                                (root / "out" / program).read_bytes(), elf()
                            )

    def test_long_pax_path_is_rejected_before_discovery_indexes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "download"
            with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                item = tarfile.TarInfo("a/" + "é" * 2050)
                item.size = 64
                archive.addfile(item, io.BytesIO(elf()))
            with patch.object(
                payload, "PurePosixPath", side_effect=AssertionError("parsed")
            ):
                with self.assertRaisesRegex(ValueError, "length or depth"):
                    payload.discover_programs(source)

    def test_zip_member_count_is_checked_before_zipfile_parses_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("one", b"")
                archive.writestr("two", b"")
            with (
                patch.object(payload, "MAX_FILES", 1),
                patch.object(
                    payload.zipfile, "ZipFile", side_effect=AssertionError("parsed")
                ),
                self.assertRaisesRegex(ValueError, "metadata"),
            ):
                payload.discover_programs(source)

    def test_zip_inconsistent_entry_count_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("one", b"")
                archive.writestr("two", b"")
            data = bytearray(source.read_bytes())
            eocd = data.rfind(b"PK\x05\x06")
            struct.pack_into("<HH", data, eocd + 8, 1, 1)
            source.write_bytes(data)
            with (
                patch.object(
                    payload.zipfile, "ZipFile", side_effect=AssertionError("parsed")
                ),
                self.assertRaisesRegex(zipfile.BadZipFile, "Inconsistent"),
            ):
                payload.extract(source, root / "out")

    def test_zip64_directory_remains_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 0):
                with zipfile.ZipFile(source, "w", allowZip64=True) as archive:
                    archive.writestr("app", elf())
            self.assertIn(zipfile.stringEndArchive64, source.read_bytes())
            self.assertEqual(payload.discover_programs(source), ["app"])

    def test_legacy_zip64_eocd_location_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "download"
            with patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 0):
                with zipfile.ZipFile(source, "w", allowZip64=True) as archive:
                    archive.writestr("app", elf())
            classic = source.read_bytes().rfind(zipfile.stringEndArchive)
            original = zipfile._EndRecData
            calls = 0

            def legacy_location(stream):
                nonlocal calls
                result = original(stream)
                calls += 1
                if calls == 1:
                    result[zipfile._ECD_LOCATION] = classic
                return result

            with patch.object(zipfile, "_EndRecData", side_effect=legacy_location):
                with payload.checked_zip(source) as archive:
                    self.assertEqual(archive.namelist(), ["app"])

    def test_tar_pax_extension_is_bounded_before_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                item = tarfile.TarInfo("app")
                item.size = 1
                item.pax_headers = {"comment": "x" * 200_000}
                archive.addfile(item, io.BytesIO(b"x"))
            with patch.object(payload, "MAX_EXTENSION", 1024):
                for action in (
                    lambda: payload.extract(source, root / "out"),
                    lambda: payload.discover_programs(source),
                ):
                    with self.assertRaisesRegex(ValueError, "metadata"):
                        action()

    def test_tar_sparse_map_is_rejected_before_unbounded_parser_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                item = tarfile.TarInfo("app")
                item.size = 1
                item.pax_headers = {
                    "GNU.sparse.major": "1",
                    "GNU.sparse.minor": "0",
                    "GNU.sparse.realsize": str(4 * 1024**3),
                }
                archive.addfile(item, io.BytesIO(b"x"))
            for action in (
                lambda: payload.extract(source, root / "out"),
                lambda: payload.discover_programs(source),
            ):
                with self.assertRaisesRegex(ValueError, "sparse"):
                    action()

    def test_discovery_streams_supported_formats_and_resolves_file_links(self):
        for mode in ("w", "w:gz", "w:bz2", "w:xz", "zip"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download"
                if mode == "zip":
                    with zipfile.ZipFile(
                        source, "w", compression=zipfile.ZIP_DEFLATED
                    ) as archive:
                        archive.writestr("bundle/bin/app", elf())
                        archive.writestr("bundle/resources/data", b"z" * 1024 * 1024)
                        link = zipfile.ZipInfo("bundle/app")
                        link.create_system = 3
                        link.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(link, "bin/app")
                else:
                    with tarfile.open(source, mode) as archive:
                        for name, data in (
                            ("bundle/bin/app", elf()),
                            ("bundle/resources/data", b"z" * 1024 * 1024),
                        ):
                            item = tarfile.TarInfo(name)
                            item.size = len(data)
                            archive.addfile(item, io.BytesIO(data))
                        link = tarfile.TarInfo("bundle/app")
                        link.type = tarfile.SYMTYPE
                        link.linkname = "bin/app"
                        archive.addfile(link)
                self.assertEqual(
                    payload.discover_programs(source, 1), ["app", "bin/app"]
                )
                self.assertEqual(sorted(root.iterdir()), [source])

    def test_discovery_rejects_unsafe_late_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with tarfile.open(source, "w") as archive:
                app = tarfile.TarInfo("app")
                app.size = 64
                archive.addfile(app, io.BytesIO(elf()))
                bad = tarfile.TarInfo("../escape")
                archive.addfile(bad)
            with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
                payload.discover_programs(source)

    def test_discovery_keeps_duplicate_size_and_link_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("first/app", elf())
                archive.writestr("second/app", elf())
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                payload.discover_programs(source, 1)
            with patch.object(payload, "MAX_BYTES", 100):
                with self.assertRaisesRegex(ValueError, "limit"):
                    payload.discover_programs(source)
            with tarfile.open(source, "w") as archive:
                item = tarfile.TarInfo("app")
                item.size = 64
                archive.addfile(item, io.BytesIO(elf()))
                link = tarfile.TarInfo("outside")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../escape"
                archive.addfile(link)
            with self.assertRaisesRegex(ValueError, "escapes"):
                payload.discover_programs(source)

    def test_tar_stream_does_not_cache_members(self):
        for mode in ("w", "w:gz", "w:bz2", "w:xz"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download"
                with tarfile.open(source, mode) as archive:
                    for index in range(256):
                        data = b"contents"
                        item = tarfile.TarInfo(f"bundle/file-{index}")
                        item.size = len(data)
                        archive.addfile(item, io.BytesIO(data))

                original_next = tarfile.TarFile.next
                cached = []

                def observe_next(archive):
                    member = original_next(archive)
                    cached.append(len(archive.members))
                    return member

                with patch.object(tarfile.TarFile, "next", observe_next):
                    payload.extract(source, root / "out")
                self.assertLessEqual(max(cached), 1)
                self.assertEqual(
                    (root / "out/bundle/file-255").read_bytes(), b"contents"
                )

    def test_repeated_tar_roots_count_toward_file_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with tarfile.open(source, "w") as archive:
                for _ in range(3):
                    item = tarfile.TarInfo("./")
                    item.type = tarfile.DIRTYPE
                    archive.addfile(item)
            with patch.object(payload, "MAX_FILES", 2):
                with self.assertRaisesRegex(ValueError, "limit"):
                    payload.extract(source, root / "out")

    def test_stripped_directories_count_toward_file_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("one/", b"")
                archive.writestr("two/", b"")
                archive.writestr("three/", b"")
            with patch.object(payload, "MAX_FILES", 2):
                with self.assertRaisesRegex(ValueError, "limit"):
                    payload.extract(source, root / "out", strip_components=1)

    def test_stripped_directories_count_toward_expanded_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("bundle/", b"hidden data")
            with patch.object(payload, "MAX_BYTES", 5):
                with self.assertRaisesRegex(ValueError, "limit"):
                    payload.extract(source, root / "out", strip_components=1)


if __name__ == "__main__":
    unittest.main()
