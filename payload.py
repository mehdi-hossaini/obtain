"""Unpack release bundles without executing their contents (Nix build helper)."""

import _compression
import bz2
from contextlib import contextmanager, ExitStack
import gzip
import io
import lzma
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import sys
import tarfile
import zipfile
import zlib

MAX_BYTES = 4 * 1024**3
MAX_FILES = 100_000
MAX_METADATA = 16 * 1024**2
MAX_EXTENSION = 1024**2
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENTS = 128
MAX_LINK_DEPTH = 32
MAX_DECODER_BYTES = 128 * 1024**2


class TarLzmaDecoder:
    """Allow XZ stream padding while keeping errors in later streams fatal."""

    def __init__(self, memlimit):
        self.decoder = lzma.LZMADecompressor(lzma.FORMAT_AUTO, memlimit=memlimit)
        self.started = self.padding_only = False
        self.padding = 0

    @property
    def eof(self):
        return self.decoder.eof or (self.padding_only and not self.padding % 4)

    @property
    def unused_data(self):
        return self.decoder.unused_data

    @property
    def needs_input(self):
        return self.decoder.needs_input

    def decompress(self, data, max_length):
        if not self.started:
            trimmed = data.lstrip(b"\0")
            self.padding += len(data) - len(trimmed)
            if not trimmed:
                self.padding_only = True
                return b""
            if self.padding % 4:
                raise lzma.LZMAError("Invalid XZ stream padding")
            self.started, self.padding_only = True, False
            data = trimmed
        return self.decoder.decompress(data, max_length)


@contextmanager
def checked_tar(source):
    """Keep compressed tar reads incremental, including decoder dictionary memory."""
    with ExitStack() as stack:
        raw = stack.enter_context(Path(source).open("rb"))
        signature = raw.read(6)
        raw.seek(0)
        if signature.startswith((b"\xfd7zXZ", b"\x5d\x00\x00\x80")):
            stream = stack.enter_context(
                io.BufferedReader(
                    _compression.DecompressReader(
                        raw,
                        TarLzmaDecoder,
                        memlimit=MAX_DECODER_BYTES,
                    )
                )
            )
        elif signature.startswith(b"\x1f\x8b\x08"):
            stream = stack.enter_context(gzip.GzipFile(fileobj=raw))
        elif signature.startswith(b"BZh"):
            stream = stack.enter_context(bz2.BZ2File(raw))
        else:
            stream = raw
        try:
            with tarfile.open(
                fileobj=stream, mode="r|", tarinfo=limited_tarinfo()
            ) as archive:
                yield archive
        except (EOFError, lzma.LZMAError, zlib.error) as error:
            raise ValueError(
                "Archive compressed data is invalid or exceeds the decoder memory limit"
            ) from error


class CompressedMember:
    """Read only one member's compressed bytes from ZipFile's validated offset."""

    def __init__(self, stream, size):
        self.stream, self.remaining = stream, size

    def read(self, size):
        data = self.stream.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


class DeflateDecoder:
    """Adapt zlib's unconsumed tail to the incremental decompressor interface."""

    def __init__(self):
        self.decoder = zlib.decompressobj(-15)

    @property
    def eof(self):
        return self.decoder.eof

    @property
    def unused_data(self):
        return self.decoder.unused_data

    @property
    def needs_input(self):
        return not self.decoder.unconsumed_tail

    def decompress(self, data, max_length):
        return self.decoder.decompress(self.decoder.unconsumed_tail + data, max_length)


class ZipLzmaDecoder:
    def __init__(self, filters, raw, item):
        self.decoder = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filters])
        self.raw, self.item, self.count = raw, item, 0

    @property
    def eof(self):
        # ZIP-LZMA may omit its end marker when general-purpose flag 1 is clear.
        return self.decoder.eof or (
            not self.item.flag_bits & 2
            and not self.raw.remaining
            and self.decoder.needs_input
            and self.count == self.item.file_size
        )

    @property
    def unused_data(self):
        return self.decoder.unused_data

    @property
    def needs_input(self):
        return self.decoder.needs_input

    def decompress(self, data, max_length):
        output = self.decoder.decompress(data, max_length)
        self.count += len(output)
        return output


class ZipMember:
    """Bound actual output before ZipExtFile can truncate it to its declared size."""

    def __init__(self, stream, item):
        self.stream, self.item = stream, item
        self.count = self.crc = 0

    def read(self, size):
        amount = min(size, 1024 * 1024, self.item.file_size - self.count + 1)
        data = self.stream.read(amount)
        self.count += len(data)
        if self.count > self.item.file_size:
            raise ValueError("Archive member exceeds its declared size")
        self.crc = zlib.crc32(data, self.crc)
        if len(data) < amount:
            if self.count != self.item.file_size:
                raise ValueError("Truncated archive member")
            if self.crc != self.item.CRC:
                raise zipfile.BadZipFile(f"Bad CRC-32 for file {self.item.filename!r}")
        return data


@contextmanager
def checked_zip_member(archive, item):
    # ZipFile.open validates the local header, overlapping members, compression
    # method and encryption before we read its compressed input. ZipExtFile's
    # own BZIP2/LZMA readers allocate unbounded output and clip it to file_size.
    try:
        with archive.open(item) as member, ExitStack() as stack:
            raw = CompressedMember(member._fileobj, item.compress_size)
            if item.compress_type == zipfile.ZIP_STORED:
                stream = raw
            else:
                if item.compress_type == zipfile.ZIP_DEFLATED:
                    factory = DeflateDecoder
                elif item.compress_type == zipfile.ZIP_BZIP2:
                    factory = bz2.BZ2Decompressor
                elif item.compress_type == zipfile.ZIP_LZMA:
                    header = raw.read(4)
                    if len(header) != 4 or struct.unpack_from("<H", header, 2)[0] != 5:
                        raise ValueError("Invalid ZIP LZMA properties")
                    properties = raw.read(5)
                    if len(properties) != 5:
                        raise ValueError("Truncated ZIP LZMA properties")
                    filters = lzma._decode_filter_properties(
                        lzma.FILTER_LZMA1, properties
                    )
                    if filters["dict_size"] > MAX_DECODER_BYTES:
                        raise ValueError("Archive exceeds the decoder memory limit")

                    def factory():
                        return ZipLzmaDecoder(filters, raw, item)
                else:
                    raise ValueError("Unsupported ZIP compression method")
                stream = stack.enter_context(
                    io.BufferedReader(_compression.DecompressReader(raw, factory))
                )
            yield ZipMember(stream, item)
    except (EOFError, lzma.LZMAError, zlib.error) as error:
        raise ValueError(
            "Archive compressed data is invalid or exceeds the decoder memory limit"
        ) from error


def checked_zip(source):
    """Reject oversized or inconsistent directories before ZipFile allocates them."""
    source = Path(source)
    with source.open("rb") as stream:
        end = zipfile._EndRecData(stream)
        if end is None:
            raise zipfile.BadZipFile("File is not a zip file")
        size = end[zipfile._ECD_SIZE]
        entries = end[zipfile._ECD_ENTRIES_TOTAL]
        if (
            end[zipfile._ECD_DISK_NUMBER] != 0
            or end[zipfile._ECD_DISK_START] != 0
            or end[zipfile._ECD_ENTRIES_THIS_DISK] != entries
        ):
            raise zipfile.BadZipFile("Inconsistent central directory entry count")
        if size > MAX_METADATA or entries > MAX_FILES:
            raise ValueError("Archive metadata exceeds the limit")
        directory_end = end[zipfile._ECD_LOCATION]
        if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
            # Python 3.10 reports the classic EOCD location here; newer
            # versions report the ZIP64 EOCD location. Locate the actual
            # ZIP64 record before measuring the central directory.
            stream.seek(directory_end)
            if stream.read(4) != zipfile.stringEndArchive64:
                directory_end -= (
                    zipfile.sizeEndCentDir64Locator + zipfile.sizeEndCentDir64
                )
                if directory_end < 0:
                    raise zipfile.BadZipFile("Bad ZIP64 central directory offset")
                stream.seek(directory_end)
                if stream.read(4) != zipfile.stringEndArchive64:
                    raise zipfile.BadZipFile("Bad ZIP64 central directory offset")
        start = directory_end - size
        if start < 0:
            raise zipfile.BadZipFile("Bad offset for central directory")
        stream.seek(start)
        consumed = count = 0
        while consumed < size:
            header = stream.read(zipfile.sizeCentralDir)
            if len(header) != zipfile.sizeCentralDir:
                raise zipfile.BadZipFile("Truncated central directory")
            fields = struct.unpack(zipfile.structCentralDir, header)
            if fields[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
                raise zipfile.BadZipFile("Bad magic number for central directory")
            length = (
                zipfile.sizeCentralDir
                + fields[zipfile._CD_FILENAME_LENGTH]
                + fields[zipfile._CD_EXTRA_FIELD_LENGTH]
                + fields[zipfile._CD_COMMENT_LENGTH]
            )
            consumed += length
            count += 1
            if consumed > size or count > MAX_FILES:
                raise ValueError("Archive metadata exceeds the limit")
            stream.seek(length - zipfile.sizeCentralDir, 1)
        if count != entries:
            raise zipfile.BadZipFile("Inconsistent central directory entry count")
    return zipfile.ZipFile(source)


class LimitedTarInfo(tarfile.TarInfo):
    metadata_size = 0
    metadata_count = 0

    def _check_extension(self):
        cls = type(self)
        cls.metadata_size += self.size
        cls.metadata_count += 1
        if (
            self.size < 0
            or self.size > MAX_EXTENSION
            or cls.metadata_size > MAX_METADATA
            or cls.metadata_count > MAX_FILES
        ):
            raise ValueError("Archive metadata exceeds the limit")

    def _proc_pax(self, archive):
        self._check_extension()
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        self._check_extension()
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive):
        raise ValueError("Archive sparse files are unsupported")

    def _proc_gnusparse_00(self, *args):
        raise ValueError("Archive sparse files are unsupported")

    def _proc_gnusparse_01(self, *args):
        raise ValueError("Archive sparse files are unsupported")

    def _proc_gnusparse_10(self, *args):
        raise ValueError("Archive sparse files are unsupported")


def limited_tarinfo():
    # Keep extension budgets private to one archive, including concurrent reads.
    return type(
        "ArchiveTarInfo", (LimitedTarInfo,), {"metadata_size": 0, "metadata_count": 0}
    )


def member_path(value):
    # Apply filesystem-scale bounds before PurePosixPath allocates its parts.
    try:
        length = len(value.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError as error:
        raise ValueError("Unsafe archive path encoding") from error
    if length > MAX_PATH_BYTES or value.count("/") + 1 > MAX_PATH_COMPONENTS:
        raise ValueError("Archive path exceeds the length or depth limit")
    if "\0" in value:
        raise ValueError("Unsafe archive path contains NUL")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or not path.parts
        or len(path.parts) > MAX_PATH_COMPONENTS
    ):
        raise ValueError(f"Unsafe archive path: {value!r}")
    return path


def link_target(path, value):
    """Resolve a file link lexically inside the bundle, before any links exist."""
    try:
        length = len(value.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError as error:
        raise ValueError("Unsafe archive link encoding") from error
    if not value or length > MAX_PATH_BYTES or "\\" in value or "\0" in value:
        raise ValueError("Unsafe archive link target")
    target = PurePosixPath(value)
    if target.is_absolute():
        raise ValueError("Absolute archive link targets are unsupported")
    parts = []
    for part in (path.parent / target).parts:
        if part == "..":
            if not parts:
                raise ValueError("Archive link escapes the extracted bundle")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts)


class ArchiveIndex:
    """Validate one bundle's namespace and budgets for discovery or extraction."""

    def __init__(self, strip_components):
        if type(strip_components) is not int or not 0 <= strip_components <= 16:
            raise ValueError("--strip-components must be between 0 and 16")
        self.strip_components = strip_components
        self.seen, self.files, self.directories = set(), set(), set()
        self.links = {}
        self.total = self.count = 0

    def account(self, size):
        self.count += 1
        self.total += size
        if self.count > MAX_FILES or self.total > MAX_BYTES or size < 0:
            raise ValueError("Archive exceeds the file-count or expanded-size limit")

    def target(self, name, size, directory):
        self.account(size)
        original = member_path(name)
        if len(original.parts) <= self.strip_components:
            if directory:
                return None
            raise ValueError("--strip-components removes the entire file path")
        relative = PurePosixPath(*original.parts[self.strip_components :])
        if relative in self.seen:
            raise ValueError(f"Duplicate archive member: {name!r}")
        self.seen.add(relative)
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                break
            if parent in self.files:
                raise ValueError("Archive file blocks a directory")
            self.directories.add(parent)
            if len(self.directories) > MAX_FILES:
                raise ValueError("Archive exceeds the implicit-directory limit")
        if directory:
            if relative in self.files:
                raise ValueError("Archive directory collides with a file")
            self.directories.add(relative)
            if len(self.directories) > MAX_FILES:
                raise ValueError("Archive exceeds the implicit-directory limit")
        elif relative in self.directories:
            raise ValueError("Archive file collides with a directory")
        return relative

    def link(self, path, value):
        self.links[path] = (link_target(path, value), value)

    def resolve_links(self):
        resolved = {}
        for name, (_, value) in self.links.items():
            if name in self.files or name in self.directories:
                raise ValueError("Archive link collides with a file or directory")
            # Check each traversed directory before resolving '..': lexical
            # normalization alone would accept file/../target or missing/../target.
            parent = name.parent
            for component in value.split("/")[:-1]:
                if component == "..":
                    parent = parent.parent
                elif component != ".":
                    parent = parent / component
                if parent not in self.directories and parent != PurePosixPath("."):
                    raise ValueError("Archive link traverses a non-directory")
            chain, visiting, current = [], set(), name
            while current in self.links and current not in resolved:
                if current in visiting or len(chain) >= MAX_LINK_DEPTH:
                    raise ValueError("Archive link cycle or excessive link depth")
                chain.append(current)
                visiting.add(current)
                current = self.links[current][0]
            current, depth = resolved.get(current, (current, 0))
            if current not in self.files:
                raise ValueError(
                    "Archive link must resolve to a regular file in the bundle"
                )
            for entry in reversed(chain):
                depth += 1
                if depth > MAX_LINK_DEPTH:
                    raise ValueError("Archive link cycle or excessive link depth")
                resolved[entry] = (current, depth)
        return resolved


def extract(source, destination, strip_components=0):
    """Extract files and validated internal file links; never traverse a link."""
    index = ArchiveIndex(strip_components)
    if destination.is_symlink():
        raise ValueError("Archive destination must not be a symbolic link")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Archive destination must be empty")

    def target(name, size, directory):
        relative = index.target(name, size, directory)
        if relative is None:
            return None
        path = destination / relative
        if directory:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def copy(stream, path, size, executable):
        # Bound actual writes too, including malformed compressed metadata.
        remaining = size
        with path.open("xb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Truncated archive member")
                output.write(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ValueError("Archive member exceeds its declared size")
        path.chmod(0o755 if executable else 0o644)
        index.files.add(PurePosixPath(path.relative_to(destination).as_posix()))

    def link(path, value):
        # Delay every link until all file writes and validation are complete.
        # This makes archive order irrelevant to traversal safety.
        relative = PurePosixPath(path.relative_to(destination).as_posix())
        index.link(relative, value)

    if zipfile.is_zipfile(source):
        with checked_zip(source) as archive:
            for item in archive.infolist():
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (
                    0,
                    stat.S_IFREG,
                    stat.S_IFDIR,
                    stat.S_IFLNK,
                ):
                    raise ValueError("Archive special files are unsupported")
                symbolic = stat.S_ISLNK(mode)
                if symbolic and (item.is_dir() or item.file_size > 4096):
                    raise ValueError("Invalid archive link")
                path = target(item.filename, item.file_size, item.is_dir())
                if symbolic:
                    with checked_zip_member(archive, item) as stream:
                        link(path, stream.read(4097).decode("utf-8"))
                elif not item.is_dir():
                    with checked_zip_member(archive, item) as stream:
                        copy(stream, path, item.file_size, mode & 0o111)
    else:
        # Read sequentially and release cached TarInfo entries as we go.
        with checked_tar(source) as archive:
            while (item := archive.next()) is not None:
                archive.members.clear()
                if not item.isdir() and not item.isfile() and not item.issym():
                    raise ValueError(
                        "Archive hard links and special files are unsupported"
                    )
                # GNU tar commonly includes a harmless root directory entry.
                if item.isdir() and item.name in (".", "./"):
                    index.account(item.size)
                    continue
                path = target(item.name, item.size, item.isdir())
                if item.issym():
                    if item.size:
                        raise ValueError(
                            "Archive symbolic link has unexpected contents"
                        )
                    link(path, item.linkname)
                elif item.isfile():
                    with archive.extractfile(item) as stream:
                        copy(stream, path, item.size, item.mode & 0o111)

    index.resolve_links()
    for name, (_, value) in index.links.items():
        (destination / name).symlink_to(value)


def validate_elf_header(header):
    if (
        len(header) < 64
        or header[:4] != b"\x7fELF"
        or header[4:6] != b"\x02\x01"
        or struct.unpack_from("<H", header, 18)[0] != 62
        or header[7] not in (0, 3)
    ):
        raise ValueError(
            "Selected program must be an x86_64 Linux ELF binary; scripts and other architectures are unsupported"
        )
    if (
        struct.unpack_from("<H", header, 16)[0] not in (2, 3)
        or struct.unpack_from("<Q", header, 24)[0] == 0
    ):
        raise ValueError(
            "Selected ELF has no executable entry point (shared libraries are not programs)"
        )


def validate_elf(path):
    with path.open("rb") as stream:
        validate_elf_header(stream.read(64))


def discover_programs(source, strip_components=0):
    """Return safe ELF paths while reading archive members without writing files."""
    index = ArchiveIndex(strip_components)
    programs = set()

    def read_file(stream, size):
        remaining = size
        header = bytearray()
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("Truncated archive member")
            if len(header) < 64:
                header.extend(chunk[: 64 - len(header)])
            remaining -= len(chunk)
        if stream.read(1):
            raise ValueError("Archive member exceeds its declared size")
        return bytes(header)

    def record_file(path, stream, size):
        header = read_file(stream, size)
        index.files.add(path)
        try:
            validate_elf_header(header)
            programs.add(path)
        except ValueError:
            pass

    if zipfile.is_zipfile(source):
        with checked_zip(source) as archive:
            for item in archive.infolist():
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (
                    0,
                    stat.S_IFREG,
                    stat.S_IFDIR,
                    stat.S_IFLNK,
                ):
                    raise ValueError("Archive special files are unsupported")
                symbolic = stat.S_ISLNK(mode)
                if symbolic and (item.is_dir() or item.file_size > 4096):
                    raise ValueError("Invalid archive link")
                path = index.target(item.filename, item.file_size, item.is_dir())
                if symbolic:
                    with checked_zip_member(archive, item) as stream:
                        index.link(path, stream.read(4097).decode("utf-8"))
                elif not item.is_dir():
                    with checked_zip_member(archive, item) as stream:
                        record_file(path, stream, item.file_size)
    else:
        with checked_tar(source) as archive:
            while (item := archive.next()) is not None:
                archive.members.clear()
                if not item.isdir() and not item.isfile() and not item.issym():
                    raise ValueError(
                        "Archive hard links and special files are unsupported"
                    )
                if item.isdir() and item.name in (".", "./"):
                    index.account(item.size)
                    continue
                path = index.target(item.name, item.size, item.isdir())
                if item.issym():
                    if item.size:
                        raise ValueError(
                            "Archive symbolic link has unexpected contents"
                        )
                    index.link(path, item.linkname)
                elif item.isfile():
                    with archive.extractfile(item) as stream:
                        record_file(path, stream, item.size)

    for name, (current, _) in index.resolve_links().items():
        if current in programs:
            programs.add(name)
    return sorted(path.as_posix() for path in programs)


def prepare(kind, source, destination, program, strip_components=0):
    if type(strip_components) is not int or not 0 <= strip_components <= 16:
        raise ValueError("--strip-components must be between 0 and 16")
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "archive":
        extract(source, destination, strip_components)
        selected = destination / member_path(program)
    elif kind == "binary":
        selected = destination / "program"
        shutil.copyfile(source, selected)
    else:
        raise ValueError("Unsupported payload type")
    if not selected.is_file():
        raise ValueError(
            "Selected --program is missing or not a regular file in the archive"
        )
    validate_elf(selected)
    selected.chmod(0o755)


if __name__ == "__main__":
    try:
        prepare(
            sys.argv[1],
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            sys.argv[4],
            int(sys.argv[5]) if len(sys.argv) > 5 else 0,
        )
    except (
        ValueError,
        OSError,
        RuntimeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        print(f"Obtain payload rejected: {error}", file=sys.stderr)
        sys.exit(1)
