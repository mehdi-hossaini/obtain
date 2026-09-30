"""Extract supported AppImage filesystems without executing the downloaded runtime."""

from pathlib import Path
import struct
import subprocess
import sys


def dwarfs_offset(source):
    """Recognize DwarFS appended after an ELF64 section table (uruntime layout)."""
    with source.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            return None
        if header[8:11] != b"AI\x02":
            return None
        table = struct.unpack_from("<Q", header, 40)[0]
        size, count = struct.unpack_from("<HH", header, 58)
        if not table or size < 64 or not count:
            return None
        offset = table + size * count
        if offset < 64 or offset + 8 > source.stat().st_size:
            return None
        stream.seek(offset)
        return offset if stream.read(6) == b"DWARFS" else None


def extract(source, destination):
    offset = dwarfs_offset(source)
    if offset is None:
        subprocess.run(
            ["appimage-exec.sh", "-x", str(destination), str(source)], check=True
        )
    else:
        print(f"Extracting DwarFS AppImage at offset {offset}", flush=True)
        destination.mkdir()
        subprocess.run(
            [
                "dwarfsextract",
                "-i",
                str(source),
                "-O",
                str(offset),
                "-o",
                str(destination),
            ],
            check=True,
        )


if __name__ == "__main__":
    extract(Path(sys.argv[1]), Path(sys.argv[2]))
