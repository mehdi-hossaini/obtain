import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "appimage", Path(__file__).resolve().parents[1] / "appimage.py"
)
appimage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appimage)


class AppImageTests(unittest.TestCase):
    def test_dwarfs_requires_matching_appimage_and_elf_boundary(self):
        image = bytearray(256)
        image[:6] = b"\x7fELF\x02\x01"
        image[8:11] = b"AI\x02"
        struct.pack_into("<Q", image, 40, 64)
        struct.pack_into("<HH", image, 58, 64, 1)
        image[128:134] = b"DWARFS"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "image"
            source.write_bytes(image)
            self.assertEqual(appimage.dwarfs_offset(source), 128)
            for offset, data in (
                (128, b"hsqs00"),
                (8, b"bad"),
                (40, struct.pack("<Q", 4096)),
                (58, struct.pack("<HH", 0, 0)),
                (4, b"\x01"),
            ):
                modified = image.copy()
                modified[offset : offset + len(data)] = data
                source.write_bytes(modified)
                self.assertIsNone(appimage.dwarfs_offset(source))
            source.write_bytes(image[:32])
            self.assertIsNone(appimage.dwarfs_offset(source))
