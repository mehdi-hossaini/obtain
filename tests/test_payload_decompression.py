"""Compressed release metadata cannot allocate beyond the decoder budget."""

import io
import lzma
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import payload
from test_payload_efficiency import elf


def zip_data(contents, method):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=method) as archive:
        archive.writestr("app", contents)
    return bytearray(output.getvalue())


def zip_offsets(data):
    filename, extra = struct.unpack_from("<HH", data, 26)
    return 30 + filename + extra, data.index(b"PK\x01\x02")


def tar_data(contents, extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo("app")
        item.size = len(contents)
        archive.addfile(item, io.BytesIO(contents))
        if extra is not None:
            item = tarfile.TarInfo("data")
            item.size = len(extra)
            archive.addfile(item, io.BytesIO(extra))
    return output.getvalue()


def oversized_xz(data):
    compressed = bytearray(lzma.compress(data))
    if compressed[14:16] != b"\x21\x01":
        raise AssertionError("Expected a single LZMA2 filter")
    header_length = (compressed[12] + 1) * 4
    compressed[16] = 40  # A 4 GiB dictionary, without allocating it here.
    struct.pack_into(
        "<I",
        compressed,
        12 + header_length - 4,
        zlib.crc32(compressed[12 : 12 + header_length - 4]),
    )
    return compressed


class PayloadDecompressionTests(unittest.TestCase):
    def verify_payload(self, data, contents, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download"
            source.write_bytes(data)
            self.assertEqual(payload.discover_programs(source), ["app"])
            payload.prepare("archive", source, root / "out", "app")
            self.assertEqual((root / "out/app").read_bytes(), contents)
            if extra is not None:
                self.assertEqual((root / "out/data").read_bytes(), extra)

    def test_supported_zip_compression_keeps_large_members_and_empty_files(self):
        contents = elf() + b"bounded output\0" * 160_000
        for method in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
            zipfile.ZIP_BZIP2,
            zipfile.ZIP_LZMA,
        ):
            with self.subTest(method=method):
                self.verify_payload(zip_data(contents, method), contents)
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = root / "download"
                    source.write_bytes(zip_data(b"", method))
                    self.assertEqual(payload.discover_programs(source), [])
                    payload.extract(source, root / "out")
                    self.assertEqual((root / "out/app").read_bytes(), b"")

    def test_actual_zip_output_cannot_hide_behind_declared_size_and_crc(self):
        contents = elf()
        for method in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
            zipfile.ZIP_BZIP2,
            zipfile.ZIP_LZMA,
        ):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download"
                data = zip_data(contents + b"\0" * (256 * 1024), method)
                _, central = zip_offsets(data)
                for offset in (14, central + 16):
                    struct.pack_into("<I", data, offset, zlib.crc32(contents))
                for offset in (22, central + 24):
                    struct.pack_into("<I", data, offset, len(contents))
                source.write_bytes(data)
                with patch.object(payload, "MAX_BYTES", len(contents)):
                    with self.assertRaisesRegex(ValueError, "declared size"):
                        payload.discover_programs(source)
                    with self.assertRaisesRegex(ValueError, "declared size"):
                        payload.extract(source, root / "out")

    def test_zip_crc_is_checked_with_bounded_decompression(self):
        for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download"
                data = zip_data(elf(), method)
                _, central = zip_offsets(data)
                struct.pack_into("<I", data, 14, 0)
                struct.pack_into("<I", data, central + 16, 0)
                source.write_bytes(data)
                for operation in (
                    lambda: payload.discover_programs(source),
                    lambda: payload.extract(source, root / "out"),
                ):
                    with self.assertRaisesRegex(zipfile.BadZipFile, "CRC"):
                        operation()

    def test_zip_lzma_without_required_end_marker_remains_supported(self):
        contents = elf()
        data = zip_data(contents, zipfile.ZIP_LZMA)
        _, central = zip_offsets(data)
        # An LZMA member may use its declared size instead of an end marker.
        for flag_offset in (6, central + 8):
            flags = struct.unpack_from("<H", data, flag_offset)[0]
            struct.pack_into("<H", data, flag_offset, flags & ~2)
        for size_offset in (18, central + 20):
            size = struct.unpack_from("<I", data, size_offset)[0]
            struct.pack_into("<I", data, size_offset, size - 1)
        self.verify_payload(data, contents)

    def test_xz_concatenated_streams_remain_supported(self):
        contents = elf()
        extra = b"second member"
        data = tar_data(contents, extra)
        for padding in (0, 4, 8196):
            with self.subTest(padding=padding):
                self.verify_payload(
                    lzma.compress(data[:1024])
                    + b"\0" * padding
                    + lzma.compress(data[1024:])
                    + b"\0" * padding,
                    contents,
                    extra,
                )

    def test_legacy_lzma_alone_tar_remains_supported(self):
        contents = elf()
        self.verify_payload(
            lzma.compress(tar_data(contents), format=lzma.FORMAT_ALONE), contents
        )

    def test_excessive_decoder_dictionaries_are_rejected_before_allocation(self):
        xz = oversized_xz(tar_data(elf()))
        data = tar_data(elf(), b"second member")
        later_xz = lzma.compress(data[:1024]) + oversized_xz(data[1024:])
        zipped = zip_data(elf(), zipfile.ZIP_LZMA)
        start, _ = zip_offsets(zipped)
        struct.pack_into("<I", zipped, start + 5, 0xFFFFFFFF)
        alone = lzma.compress(tar_data(elf()), format=lzma.FORMAT_ALONE)
        child = """
import resource, sys
from pathlib import Path
import payload
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
payload.MAX_DECODER_BYTES = int(sys.argv[3])
source = Path(sys.argv[1])
try:
    if sys.argv[2] == 'discovery':
        payload.discover_programs(source)
    else:
        payload.extract(source, source.parent / 'out')
except ValueError as error:
    assert 'decoder memory limit' in str(error), str(error)
    print('rejected within memory budget')
else:
    raise AssertionError('oversized dictionary accepted')
"""
        for kind, data, budget in (
            ("xz", xz, payload.MAX_DECODER_BYTES),
            ("later-xz-stream", later_xz, payload.MAX_DECODER_BYTES),
            ("zip-lzma", zipped, payload.MAX_DECODER_BYTES),
            # Its recognizable 8 MiB dictionary must honor the same limit.
            ("lzma-alone", alone, 1024**2),
        ):
            for operation in ("discovery", "extraction"):
                with (
                    self.subTest(kind=kind, operation=operation),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    source = Path(tmp) / "download"
                    source.write_bytes(data)
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            child,
                            str(source),
                            operation,
                            str(budget),
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("rejected within memory budget", result.stdout)


if __name__ == "__main__":
    unittest.main()
