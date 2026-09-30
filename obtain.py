#!/usr/bin/env python3
"""Track and install public GitHub Linux releases with Nix; Python stdlib only."""

from __future__ import annotations

import argparse
import base64
import codecs
import contextlib
import contextvars
import io
from collections import deque
from datetime import datetime, timezone
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import payload

ROOT = Path(__file__).resolve().parent
SYSTEM = "x86_64-linux"
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+\Z")
IDENTITY = (
    "version",
    "release_id",
    "asset_id",
    "asset_updated_at",
    "url",
    "digest",
    "size",
)
MAX_GITHUB_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_GITHUB_CACHE_BYTES = 2 * MAX_GITHUB_RESPONSE_BYTES
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024


class Error(Exception):
    pass


class RateLimited(Error):
    pass


class AssetChoice(Error):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


class NotFound(Error):
    pass


class RepositoryMoved(Error):
    def __init__(self, repository_id):
        super().__init__(
            "GitHub redirected this repository. Add its current canonical URL."
        )
        self.repository_id = repository_id


# A subprocess doing profile work must keep transaction exclusion even if the
# Python parent is killed before it can reconcile the journal.
_store_lock_fd = contextvars.ContextVar("obtain_store_lock_fd", default=None)


def backend(value):
    # Existing flake records remain readable so users can inspect and remove them.
    kind = value.get("kind", "appimage")
    if kind not in ("appimage", "flake", "archive", "binary"):
        raise Error(f"Unsupported package type: {kind}")
    return kind


def legacy_flake_error(name, record):
    """Explain how to move a previously managed flake to Nix itself."""
    installable = (
        f"{record['flake_url']}#packages.{record['system']}."
        f"{json.dumps(record['package'])}"
    )
    return Error(
        f"{name} is a legacy Nix flake entry. Install it directly with "
        "nix --extra-experimental-features 'nix-command flakes' "
        f"profile add {repr(installable)}, then run 'obtain remove {name}' "
        "to stop tracking it. Obtain now manages GitHub release files only."
    )


def tracked_name_error(name, store):
    if backend(store.sources[name]) == "flake":
        return Error(
            f"{name} is a legacy Nix flake entry. Run 'obtain remove {name}' "
            "before reusing its name, or choose --name NEW_NAME."
        )
    return Error(f"{name} is already tracked; use 'obtain update {name}'.")


def executable_check(value):
    # Legacy flake lock validation only.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", value):
        raise Error(
            "Executable names must be a simple filename, without paths or spaces."
        )
    return value


def package_check(value):
    # Legacy flake lock validation only.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", value):
        raise Error("Invalid legacy flake package attribute; use a simple name.")
    return value


def clean(value):
    return "".join(c for c in str(value) if c.isprintable())


def name_check(value):
    if not NAME.fullmatch(value):
        raise Error(
            "App names must start with a lowercase letter and contain only a-z, 0-9, and hyphens (max 63 characters)."
        )
    return value


def repository(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme != "https" or p.netloc.lower() != "github.com" or p.query or p.fragment:
        raise Error("Use a public repository URL: https://github.com/owner/repo")
    repo = p.path.strip("/").removesuffix(".git")
    if not REPO.fullmatch(repo) or any(x in (".", "..") for x in repo.split("/")):
        raise Error("Use a repository URL, not a release or file URL.")
    return repo


def moved_repository_id(path, location):
    """Accept only GitHub's same-origin redirect to the same API endpoint by ID."""
    if (
        not isinstance(location, str)
        or location != location.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in location)
    ):
        return None
    try:
        requested = urllib.parse.urlsplit("https://api.github.com" + path)
        redirected = urllib.parse.urlsplit(location)
    except ValueError:
        return None
    parts = requested.path.split("/", 4)
    if len(parts) < 4 or parts[:2] != ["", "repos"]:
        return None
    repo = "/".join(parts[2:4])
    if not REPO.fullmatch(repo):
        return None
    suffix = requested.path[len("/repos/" + repo) :]
    if (
        redirected.scheme != "https"
        or redirected.netloc.lower() != "api.github.com"
        or redirected.query != requested.query
        or redirected.fragment
    ):
        return None
    match = re.fullmatch(r"/repositories/([1-9][0-9]{0,18})(/.*)?", redirected.path)
    if not match or (match[2] or "") != suffix:
        return None
    repository_id = int(match[1])
    return repository_id if repository_id <= 2**63 - 1 else None


def asset_url(url, repo):
    if not isinstance(url, str):
        raise Error("Invalid GitHub asset download URL.")
    p = urllib.parse.urlsplit(url)
    if (
        p.scheme != "https"
        or p.netloc.lower() != "github.com"
        or not p.path.lower().startswith(f"/{repo}/releases/download/".lower())
        or p.query
        or p.fragment
    ):
        raise Error(
            "The release asset does not have a GitHub download URL for this repository."
        )
    return url


def cache_directory():
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "obtain"


def load_json(path, default=None, max_bytes=None):
    try:
        if max_bytes is None:
            return json.loads(path.read_text())
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("JSON document exceeds its size limit")
        return json.loads(data)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as e:
        raise Error(f"Invalid JSON in {path}: {e}") from e


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".obtain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_json(path, value):
    """Append one durable record; callers hold the Store's exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    if created:
        sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_batch(path):
    """Read a snapshot plus completion records, including an interrupted batch."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        report, offset = json.JSONDecoder().raw_decode(text)
        if not isinstance(report, dict) or not isinstance(
            report.get("unfinished"), list
        ):
            raise ValueError("invalid batch header")
        unfinished = dict.fromkeys(report["unfinished"])
        # atomic_json ends the header with a newline. An incomplete final append
        # has not been acknowledged and must be retried after a crash.
        for line in text[offset:].splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            if not line.strip():
                continue
            name = json.loads(line)
            if not isinstance(name, str):
                raise ValueError("invalid batch completion")
            unfinished.pop(name, None)
        report["unfinished"] = list(unfinished)
        return report
    except (ValueError, TypeError) as e:
        raise Error(f"Invalid saved batch report: {path}") from e


def diagnose(output):
    text = output.lower()
    if ("autopatchelf" in text or "auto-patchelf" in text) and (
        "missing" in text or "could not" in text
    ):
        return "The downloaded program needs libraries absent from its Nix wrapper. This bundle needs a package-specific recipe."
    if "no space left" in text:
        return "Disk space is exhausted. Free space, then retry; pending profile changes are recovered on the next command that accesses tracked apps."
    if "hash mismatch" in text:
        return "The download differs from its pinned checksum. Review the upstream release before updating the lock."
    if "could not compile" in text or "builder for" in text:
        return "The package build failed. Review the build output; an upstream source or packaging failure may need a different revision."
    return "See the command output for details."


def run(args, capture=True):
    # Keep progress visible while retaining a bounded diagnostic tail. Never run a shell.
    tail = deque(maxlen=200)
    try:
        lock_fd = _store_lock_fd.get()
        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(lock_fd,) if lock_fd is not None else (),
        ) as proc:
            output = bytearray()
            # Nix metadata is UTF-8 even when the caller's locale is not.
            encoding = "utf-8"
            readers = [proc.stderr]
            if capture:
                readers.append(proc.stdout)
            decoder = io.IncrementalNewlineDecoder(
                codecs.getincrementaldecoder(encoding)("replace"), translate=True
            )
            pending = ""

            def show(chunk):
                nonlocal pending
                if not chunk:
                    return
                print(chunk, end="", file=sys.stderr, flush=True)
                parts = chunk.split("\n")
                for part in parts[:-1]:
                    tail.append((pending + part + "\n")[-8192:])
                    pending = ""
                pending = (pending + parts[-1])[-8192:]

            try:
                # Drain both pipes together: either one can fill and block the child.
                # Descendants may keep a pipe open after the command exits.
                exited_at = None
                while readers:
                    if proc.poll() is not None and exited_at is None:
                        exited_at = time.monotonic()
                    wait = 0.1
                    if exited_at is not None:
                        wait = min(wait, max(0, exited_at + 0.5 - time.monotonic()))
                        if wait == 0:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(proc.pid, signal.SIGKILL)
                            break
                    readable, _, _ = select.select(readers, [], [], wait)
                    for stream in readable:
                        size = 4096 if stream is proc.stderr else 65536
                        raw = os.read(stream.fileno(), size)
                        if not raw:
                            readers.remove(stream)
                        elif stream is proc.stderr:
                            show(decoder.decode(raw))
                        else:
                            if len(output) + len(raw) > MAX_COMMAND_OUTPUT_BYTES:
                                raise Error(
                                    f"{args[0]} produced more than "
                                    f"{MAX_COMMAND_OUTPUT_BYTES} bytes of captured output."
                                )
                            output.extend(raw)
                show(decoder.decode(b"", final=True))
                if pending:
                    tail.append(pending)
                code = proc.wait()
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        if code:
            details = "".join(tail)
            if output:
                output_tail = output[-65536:].decode("utf-8", "replace")
                details += "\n[stdout tail]\n" + output_tail[-65536:]
            log = cache_directory() / "last-command.log"
            saved = ""
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(details)
                saved = f" Diagnostic tail: {log}"
            except OSError:
                pass
            raise Error(f"{args[0]} failed (exit {code}). {diagnose(details)}{saved}")
        return output.decode(encoding, "replace").strip() if capture else ""
    except FileNotFoundError as e:
        raise Error(f"Required command is missing: {args[0]}") from e


def nix(*args):
    return run(["nix", "--extra-experimental-features", "nix-command flakes", *args])


def default_pin():
    locked = load_json(ROOT / "flake.lock")["nodes"]["nixpkgs"]["locked"]
    return (
        "github:NixOS/nixpkgs/"
        + locked["rev"]
        + "?narHash="
        + urllib.parse.quote(locked["narHash"], safe="")
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # GitHub API metadata never needs to send a token to a redirected host.
        return None


class GitHub:
    def __init__(self, cache):
        self.cache = cache
        self.opener = urllib.request.build_opener(NoRedirect())
        self._memo = {}
        self._memo_bytes = 0

    def clear_memo(self):
        self._memo.clear()
        self._memo_bytes = 0

    def _remember(self, path, data):
        # Keep only successful JSON responses, and isolate them from callers that
        # might edit metadata while selecting an asset.
        encoded = json.dumps(data)
        size = len(encoded)
        if size > 2_000_000:
            return
        while self._memo and (
            len(self._memo) >= 32 or self._memo_bytes + size > 2_000_000
        ):
            oldest = next(iter(self._memo))
            self._memo_bytes -= len(self._memo.pop(oldest))
        self._memo[path] = encoded
        self._memo_bytes += size

    def get(self, path):
        if path in self._memo:
            return json.loads(self._memo[path])
        url = "https://api.github.com" + path
        cache_file = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        try:
            cached = load_json(cache_file, {}, MAX_GITHUB_CACHE_BYTES)
        except (Error, OSError):
            cached = {}
        if not isinstance(cached, dict):
            cached = {}
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "obtain-nixos/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if "body" in cached and isinstance(cached.get("etag"), str):
            headers["If-None-Match"] = cached["etag"]
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with self.opener.open(
                urllib.request.Request(url, headers=headers), timeout=30
            ) as response:
                # Release pages have at most 100 entries. Read one byte past the
                # cap so an upstream response cannot allocate unbounded memory.
                body = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
                if len(body) > MAX_GITHUB_RESPONSE_BYTES:
                    raise Error("GitHub API response exceeds the 8 MiB limit.")
                data = json.loads(body)
                self._remember(path, data)
                # Disk caching is optional; a fresh upstream result remains useful
                # even when the cache is unwritable or its filesystem is full.
                with contextlib.suppress(OSError):
                    atomic_json(
                        cache_file, {"etag": response.headers.get("ETag"), "body": data}
                    )
                return data
        except urllib.error.HTTPError as e:
            if e.code == 304 and "body" in cached:
                self._remember(path, cached["body"])
                return cached["body"]
            if e.code == 429 or (
                e.code == 403
                and (
                    e.headers.get("X-RateLimit-Remaining") == "0"
                    or e.headers.get("Retry-After")
                )
            ):
                reset = e.headers.get("X-RateLimit-Reset", "")
                retry = e.headers.get("Retry-After", "")
                when = "later"
                if reset.isdigit():
                    try:
                        when = (
                            "after "
                            + datetime.fromtimestamp(
                                int(reset), timezone.utc
                            ).isoformat()
                        )
                    except (ValueError, OverflowError, OSError):
                        pass
                elif retry.isdigit():
                    when = f"in {int(retry)} seconds"
                raise RateLimited(
                    f"GitHub API rate limit reached. Retry {when}; GITHUB_TOKEN is optional. Existing installations are unchanged."
                ) from e
            if e.code == 403:
                raise Error(
                    "GitHub denied access (HTTP 403). Check repository access or GITHUB_TOKEN permissions; no rate-limit reset was provided."
                ) from e
            if e.code == 404:
                raise NotFound(
                    "Repository, file, or release not found. Only public GitHub repositories are supported."
                ) from e
            if e.code == 301:
                repository_id = moved_repository_id(path, e.headers.get("Location"))
                if repository_id is not None:
                    raise RepositoryMoved(repository_id) from e
            if e.code in (301, 302, 307, 308):
                raise Error(
                    "GitHub redirected this repository. Add its current canonical URL."
                ) from e
            raise Error(f"GitHub API returned HTTP {e.code}.") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise Error(f"Could not read GitHub release metadata: {e}") from e

    def canonical_repository(self, repository_id):
        metadata = self.get(f"/repositories/{repository_id}")
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("id")) is not int
            or metadata["id"] != repository_id
            or not isinstance(metadata.get("full_name"), str)
        ):
            raise Error("Invalid moved GitHub repository identity.")
        try:
            canonical = repository("https://github.com/" + metadata["full_name"])
        except Error as e:
            raise Error("Invalid moved GitHub repository name.") from e
        if canonical != metadata["full_name"]:
            raise Error("Invalid moved GitHub repository name.")
        return canonical

    def release_assets(self, source):
        base = "/repos/" + source["repository"]
        if source.get("prereleases"):
            releases = []
            for page in range(1, 11):
                batch = self.get(f"{base}/releases?per_page=100&page={page}")
                if not isinstance(batch, list) or any(
                    not isinstance(r, dict) for r in batch
                ):
                    raise Error("Invalid GitHub release list metadata.")
                releases.extend(
                    r for r in batch if not r.get("draft") and r.get("published_at")
                )
                if releases or len(batch) < 100:
                    break
            if not releases:
                raise Error("No published releases found.")
            release = max(releases, key=lambda r: r["published_at"])
        else:
            release = self.get(base + "/releases/latest")
        if not isinstance(release, dict):
            raise Error("Invalid GitHub release metadata.")
        if release.get("draft") or (
            release.get("prerelease") and not source.get("prereleases")
        ):
            raise Error("No eligible release found.")
        assets = []
        release_id = int(release["id"])
        for page in range(1, 11):
            batch = self.get(
                f"{base}/releases/{release_id}/assets?per_page=100&page={page}"
            )
            if not isinstance(batch, list) or any(
                not isinstance(a, dict) for a in batch
            ):
                raise Error("Invalid GitHub asset list metadata.")
            assets.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise Error("Too many release assets to select safely.")
        return release, assets

    def release(self, source):
        if backend(source) == "flake":
            raise Error("Nix flake entries must be managed directly with Nix.")
        release, assets = self.release_assets(source)
        release_id = int(release["id"])
        if source.get("asset_family") and not source.get("asset"):
            assets = preferred_app_assets(
                asset_candidates(assets, kind=backend(source)), source["asset_family"]
            )
        selected = select_asset(assets, source.get("asset"), backend(source))
        return {
            "kind": backend(source),
            **(
                {
                    "program": (
                        None if automatic_program(source) else source.get("program")
                    ),
                    "strip_components": source.get("strip_components", 0),
                }
                if backend(source) == "archive"
                else {}
            ),
            "repository": source["repository"],
            "version": release["tag_name"],
            "release_id": release_id,
            "release_url": f"https://github.com/{source['repository']}/releases/tag/{urllib.parse.quote(release['tag_name'], safe='')}",
            "asset_id": selected["id"],
            "asset_name": selected["name"],
            "asset_updated_at": selected.get("updated_at"),
            "url": asset_url(selected["browser_download_url"], source["repository"]),
            "digest": selected.get("digest"),
            "size": selected.get("size"),
            "system": SYSTEM,
        }


ARCHIVES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".txz", ".tar", ".zip")


def program_path(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*", value)
        or any(p in (".", "..") for p in value.split("/"))
    ):
        raise Error(
            "Archive --program must be a relative file path without '..', spaces or shell syntax."
        )
    return value


def asset_candidates(assets, pattern=None, kind="appimage"):
    if not isinstance(assets, list) or any(
        not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in assets
    ):
        raise Error("Invalid GitHub asset metadata.")
    candidates = []
    for asset in assets:
        name = asset["name"]
        lower = name.lower()
        if asset.get("state", "uploaded") != "uploaded":
            continue
        if re.search(
            r"(?i)(?:aarch64|arm64|armhf|armv[5-9]|i[3-6]86|riscv|ppc|s390|darwin|macos|windows|win32|win64)",
            name,
        ):
            continue
        if lower.endswith(
            (
                ".sig",
                ".asc",
                ".sigstore",
                ".sigstore.json",
                ".sha256",
                ".sha512",
                ".zsync",
            )
        ):
            continue
        if re.search(
            r"(?i)(?:^|[._-])(?:debug|symbols|checksums?|sha256|sha512|src|source|sources)(?:[._-]|$)",
            name,
        ):
            continue
        if kind == "appimage" and not lower.endswith(".appimage"):
            continue
        if kind == "archive" and not lower.endswith(ARCHIVES):
            continue
        if kind == "binary" and (
            lower.endswith(
                ARCHIVES
                + (
                    ".appimage",
                    ".deb",
                    ".rpm",
                    ".exe",
                    ".dmg",
                    ".msi",
                    ".txt",
                    ".json",
                    ".sha256",
                    ".sha512",
                    ".sig",
                    ".asc",
                    ".zsync",
                    ".gz",
                    ".xz",
                    ".bz2",
                    ".zst",
                    ".7z",
                    ".whl",
                    ".nupkg",
                    ".apk",
                )
            )
            or "checksum" in lower
        ):
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        if kind != "appimage" and not pattern and not re.search(r"(?i)linux", name):
            continue
        candidates.append(asset)
    if not pattern:
        explicit = [
            a
            for a in candidates
            if re.search(r"(?i)(?:x86[_-]64|amd64|x64)", a["name"])
        ]
        if explicit:
            return explicit
    return candidates


def select_asset(assets, pattern=None, kind="appimage"):
    candidates = asset_candidates(assets, pattern, kind)
    if len(candidates) != 1:
        choices = ", ".join(clean(a["name"]) for a in candidates) or "none"
        label = "AppImage" if kind == "appimage" else kind
        raise AssetChoice(
            f"Expected one x86_64 {label}, found {len(candidates)}. Use --asset 'glob' to select it explicitly. Eligible assets: {choices}",
            candidates,
        )
    return candidates[0]


def choose_asset(github, source):
    # Called only for add; unattended check/update never consumes stdin.
    try:
        return github.release(source)
    except AssetChoice as e:
        if not sys.stdin.isatty() or not sys.stderr.isatty() or not e.candidates:
            raise
        for index, asset in enumerate(e.candidates, 1):
            print(f"  {index}. {clean(asset['name'])}", file=sys.stderr)
        print(
            "Choose an asset number (Enter cancels): ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        choice = sys.stdin.readline().strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(e.candidates):
            raise Error("Asset selection cancelled; nothing was installed.") from e
        filename = e.candidates[int(choice) - 1]["name"]
        # Exact choice for this install; never silently broaden an update selector.
        source["asset"] = (
            filename.replace("[", "[[]").replace("?", "[?]").replace("*", "[*]")
        )
        print(
            "Saved an exact asset selector. If a future release renames it, use obtain update NAME --asset 'GLOB'.",
            file=sys.stderr,
        )
        return github.release(source)


def choose_option(options, label, hint, interactive=True):
    if len(options) == 1:
        return 0
    choices = ", ".join(map(clean, options)) or "none"
    if (
        not options
        or not interactive
        or not sys.stdin.isatty()
        or not sys.stderr.isatty()
    ):
        raise Error(f"Could not choose {label}. Choices: {choices}. {hint}")
    print(f"Choose {label}:", file=sys.stderr)
    for index, option in enumerate(options, 1):
        print(f"  {index}. {clean(option)}", file=sys.stderr)
    print("Number (Enter cancels): ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        raise Error("Selection cancelled; nothing was installed.")
    return int(answer) - 1


def asset_stem(filename):
    for suffix in (".appimage", *ARCHIVES):
        if filename.lower().endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def matches_app(filename, app):
    # Match the app itself followed by platform/version markers, not companion
    # tools such as codex-app-server or codex-npm in the same release.
    stem = asset_stem(filename).lower()
    return bool(
        re.fullmatch(
            re.escape(app.lower())
            + r"(?:[._-](?:v?\d[0-9a-z.]*|linux|x86[_-]64|amd64|x64|unknown|musl|gnu|static|portable))*",
            stem,
        )
    )


def preferred_app_assets(assets, app):
    # A full runtime package can accompany a single-executable archive. Exclude
    # companion packages such as app-server from the main application's match.
    packaged = [a for a in assets if matches_app(a["name"], app + "-package")]
    return packaged or [a for a in assets if matches_app(a["name"], app)]


def automatic_program(source):
    # Earlier automatic installs saved asset_family but not auto_program. They
    # never accepted an explicit --program, so rediscovering on update is safe.
    return source.get("auto_program", bool(source.get("asset_family")))


def discover_release(source, github):
    print("Looking for a Linux release…", flush=True)
    try:
        _, assets = github.release_assets(source)
    except NotFound as e:
        raise Error(
            "No public repository or supported GitHub release found. "
            "Use 'obtain inspect URL' for "
            "details, or use Nix directly for an upstream flake."
        ) from e
    choices = [
        (kind, asset)
        for kind in ("appimage", "archive", "binary")
        for asset in asset_candidates(assets, source.get("asset"), kind)
    ]
    if choices:
        main = []
        if not source.get("asset"):
            images = [item for item in choices if item[0] == "appimage"]
            choices = images or choices
            preferred = preferred_app_assets(
                [asset for _, asset in choices], source["repository"].split("/")[1]
            )
            main = [item for item in choices if item[1] in preferred]
            choices = main or choices
        index = choose_option(
            [asset["name"] for _, asset in choices],
            "a release file",
            "Use --asset 'FILENAME' to choose one.",
        )
        kind, asset = choices[index]
        source["kind"] = kind
        # Preserve automatic selection across versioned filenames, but restrict
        # a main-program match so updates cannot switch to a companion tool.
        if not source.get("asset") and len(choices) == 1 and main:
            source["asset_family"] = source["repository"].split("/")[1]
        elif len(choices) > 1:
            source["asset"] = (
                asset["name"]
                .replace("[", "[[]")
                .replace("?", "[?]")
                .replace("*", "[*]")
            )
        return github.release(source)
    raise Error(
        "No supported Linux release file matches this repository and selection. "
        "Use 'obtain inspect URL' for details, or use Nix directly for an "
        "upstream flake."
    )


def archive_program(path, candidate, interactive):
    print("Finding the executable inside the archive…", flush=True)
    try:
        programs = []
        for p in payload.discover_programs(path, candidate.get("strip_components", 0)):
            try:
                programs.append(program_path(p))
            except Error:
                continue
    except (
        OSError,
        ValueError,
        RuntimeError,
        payload.tarfile.TarError,
        payload.zipfile.BadZipFile,
    ) as e:
        raise Error(f"Cannot inspect release archive: {e}") from e
    app = candidate["repository"].split("/")[1]
    main = [p for p in programs if matches_app(Path(p).name, app)]
    programs = main or programs
    index = choose_option(
        programs, "an executable", "Use --program PATH to choose one.", interactive
    )
    return programs[index]


def same_release(a, b):
    if backend(a) == "flake" or backend(b) == "flake":
        raise Error("Nix flake entries must be managed directly with Nix.")
    if backend(a) != backend(b):
        return False
    return all(a.get(k) == b.get(k) for k in IDENTITY)


def lock_release(name, candidate, interactive=False):
    if backend(candidate) == "flake":
        raise Error("Nix flake entries must be managed directly with Nix.")
    suffix = ".AppImage" if backend(candidate) == "appimage" else ".download"
    args = ["store", "prefetch-file", "--json", "--name", f"{name}{suffix}"]
    digest = candidate.get("digest")
    if digest and not isinstance(digest, str):
        raise Error("Invalid upstream asset digest.")
    if digest:
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise Error("Unsupported upstream asset digest; expected SHA-256.")
        expected = "sha256-" + base64.b64encode(bytes.fromhex(digest[7:])).decode()
        args += ["--expected-hash", expected]
    result = json.loads(nix(*args, candidate["url"]))
    if digest and result["hash"] != expected:
        raise Error("Downloaded asset hash does not match GitHub's digest.")
    record = {
        **candidate,
        "name": name,
        "hash": result["hash"],
        "nixpkgs": default_pin(),
    }
    if backend(candidate) == "archive" and not candidate.get("program"):
        record["program"] = archive_program(
            Path(result["storePath"]), candidate, interactive
        )
    return record


def inspect_repository(args, github):
    repo = repository(args.url)
    source = {"repository": repo}
    print(f"Repository: {repo}")
    try:
        release, assets = github.release_assets(source)
        print(f"Release: {clean(release['tag_name'])}")
        for kind in ("appimage", "archive", "binary"):
            eligible = asset_candidates(assets, kind=kind)
            label = {
                "appimage": "AppImages",
                "archive": "Archives",
                "binary": "Binaries",
            }[kind]
            print(
                f"{label}: " + (", ".join(clean(a["name"]) for a in eligible) or "none")
            )
        choices = [
            (kind, asset)
            for kind in ("appimage", "archive", "binary")
            for asset in asset_candidates(assets, kind=kind)
        ]
        images = [item for item in choices if item[0] == "appimage"]
        choices = images or choices
        preferred = preferred_app_assets(
            [asset for _, asset in choices], repo.split("/")[1]
        )
        choices = [item for item in choices if item[1] in preferred] or choices
        if len(choices) == 1:
            print("Automatic x86_64 selection: " + clean(choices[0][1]["name"]))
        elif choices:
            print("Choose a release file with --asset FILENAME.")
    except NotFound:
        print("No public repository or published stable GitHub release found.")


def validate_lock(record, name):
    if record.get("name") != name or record.get("system") != SYSTEM:
        raise Error(f"Invalid name or platform in lock for {name}.")
    if not re.fullmatch(
        r"github:NixOS/nixpkgs/[0-9a-f]{40}\?narHash=.+", record.get("nixpkgs", "")
    ):
        raise Error(f"Missing pinned Nixpkgs reference for {name}.")
    repo = repository("https://github.com/" + record["repository"])
    if backend(record) == "flake":
        revision, nar_hash = record.get("revision", ""), record.get("nar_hash", "")
        if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(
            r"sha256-[A-Za-z0-9+/]{43}=", nar_hash
        ):
            raise Error(f"Invalid pinned flake revision or hash for {name}.")
        expected = (
            f"github:{repo}/{revision}?narHash={urllib.parse.quote(nar_hash, safe='')}"
        )
        if record.get("flake_url") != expected:
            raise Error(
                f"Flake URL does not match the pinned repository, revision and hash for {name}."
            )
        package_check(record["package"])
        executable_check(record["program"])
    else:
        if not re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", record.get("hash", "")):
            raise Error(f"Invalid SHA-256 lock for {name}.")
        asset_url(record["url"], repo)
        if backend(record) == "archive":
            program_path(record.get("program"))
            stripped = record.get("strip_components", 0)
            if type(stripped) is not int or not 0 <= stripped <= 16:
                raise Error("Invalid archive strip-components value in lock.")


def validate_source_lock(source, record, name):
    if not isinstance(source, dict) or not isinstance(record, dict):
        raise Error(f"Invalid source or lock for {name}.")
    validate_lock(record, name)
    repo = repository("https://github.com/" + source["repository"])
    if record["repository"] != repo or backend(source) != backend(record):
        raise Error(f"Source and lock disagree for {name}.")


class Store:
    def __init__(self):
        home = Path.home()
        self.config = (
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "obtain"
        )
        self.data = (
            Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "obtain"
        )
        self.cache = cache_directory()
        self.applications = self.data.parent / "applications"
        self.sources = {}
        self.locks = {}
        self._batch_saves = False
        self._state_dirty = False

    @contextlib.contextmanager
    def session(self):
        self.data.mkdir(parents=True, exist_ok=True)
        with (self.data / ".lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise Error(
                    "Another Obtain command is running; retry when it finishes."
                ) from e
            self.sources = self.read_document(self.config / "sources.json")
            self.locks = self.read_document(self.config / "lock.json")
            self.replay_state()
            for name, source in self.sources.items():
                name_check(name)
                backend(source)
                repository("https://github.com/" + source["repository"])
            for name, record in self.locks.items():
                name_check(name)
                validate_lock(record, name)
            # Reconcile an interrupted profile switch from the actual profile.
            self.recover()
            if self.sources.keys() != self.locks.keys():
                raise Error("Tracked sources and locks disagree.")
            for name, source in self.sources.items():
                validate_source_lock(source, self.locks[name], name)
            if self._state_dirty:
                self.save()
            token = _store_lock_fd.set(f.fileno())
            try:
                yield self
            finally:
                _store_lock_fd.reset(token)

    @staticmethod
    def read_document(path):
        doc = load_json(path, {"schema": 1, "apps": {}})
        if (
            not isinstance(doc, dict)
            or doc.get("schema") != 1
            or not isinstance(doc.get("apps"), dict)
            or any(not isinstance(value, dict) for value in doc["apps"].values())
        ):
            raise Error(f"Unsupported state format in {path}.")
        return doc["apps"]

    def save(self):
        # A journal protects operations that need both files to change together.
        atomic_json(self.config / "sources.json", {"schema": 1, "apps": self.sources})
        atomic_json(self.config / "lock.json", {"schema": 1, "apps": self.locks})
        events = self.data / "state-events.jsonl"
        if events.exists():
            events.unlink()
            sync_directory(events.parent)
        self._state_dirty = False

    def replay_state(self):
        """Overlay durable per-app changes if a batch died before compaction."""
        path = self.data / "state-events.jsonl"
        try:
            stream = path.open("r+b")
        except FileNotFoundError:
            return
        with stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # The operation's pending journal remains until its complete
                    # state event is durable, so recovery can replay this tail.
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                try:
                    event = json.loads(line)
                    name = name_check(event["name"])
                    source, record = event["source"], event["record"]
                    if source is None and record is None:
                        self.sources.pop(name, None)
                        self.locks.pop(name, None)
                    else:
                        if not isinstance(source, dict) or not isinstance(record, dict):
                            raise ValueError("invalid records")
                        validate_source_lock(source, record, name)
                        self.sources[name] = source
                        self.locks[name] = record
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    raise Error(f"Invalid state event in {path}") from e
        self._state_dirty = True

    @contextlib.contextmanager
    def batch_saves(self):
        """Journal individual app changes, then write the full snapshot once."""
        self._batch_saves = True
        try:
            yield
        finally:
            self._batch_saves = False
            if self._state_dirty:
                self.save()

    def profile(self, name):
        return self.data / "profiles" / name

    def installed(self, name):
        return load_json(self.profile(name) / "share/obtain/manifest.json")

    def targets(self, name):
        if name and name not in self.sources:
            raise Error(f"Unknown app: {name}")
        return [name] if name else sorted(self.sources)

    def links(self, name, remove=False, verify_only=False):
        targets = {
            self.data / "bin" / name: self.profile(name) / "bin" / name,
            self.applications / f"obtain-{name}.desktop": self.profile(name)
            / "share/applications"
            / f"obtain-{name}.desktop",
        }
        for link, target in targets.items():
            if link.is_symlink() and os.readlink(link) == str(target):
                if remove and not verify_only:
                    link.unlink()
                continue
            if link.exists() or link.is_symlink():
                raise Error(
                    f"Refusing to overwrite a file not managed by Obtain: {link}"
                )
            if not remove and not verify_only:
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)

    def journal(self, operation, name, source=None, record=None):
        atomic_json(
            self.data / "pending.json",
            {
                "operation": operation,
                "name": name,
                "source": source,
                "record": record,
                "previous": self.installed(name),
            },
        )

    def recover(self):
        path = self.data / "pending.json"
        pending = load_json(path)
        if pending is None and not path.exists():
            return
        if (
            not isinstance(pending, dict)
            or not {"operation", "name", "source", "record", "previous"}.issubset(
                pending
            )
            or pending.get("operation")
            not in (
                "save",
                "install",
                "rollback",
                "remove",
            )
        ):
            raise Error("Invalid recovery journal; state was left unchanged.")
        name = name_check(pending["name"])
        op = pending["operation"]
        if op != "remove":
            source, record = pending.get("source"), pending.get("record")
            if not isinstance(source, dict) or not isinstance(record, dict):
                raise Error(
                    "Invalid recovery journal records; state was left unchanged."
                )
            validate_source_lock(source, record, name)
        previous = pending.get("previous")
        if previous is not None:
            if not isinstance(previous, dict):
                raise Error("Invalid previous package in recovery journal.")
            validate_lock(previous, name)
        current = self.installed(name)
        if op == "save":
            self.sources[name] = pending["source"]
            self.locks[name] = pending["record"]
        elif op == "remove" and current is None:
            self.links(name, remove=True)
            self.sources.pop(name, None)
            self.locks.pop(name, None)
        elif (
            op in ("install", "rollback")
            and current is not None
            and (
                (op == "install" and current == pending["record"])
                or (
                    op == "rollback"
                    and current == pending["record"]
                    and current != pending["previous"]
                )
            )
        ):
            self.sources[name] = pending["source"]
            self.locks[name] = current
            self.links(name)
        if self._batch_saves:
            append_json(
                self.data / "state-events.jsonl",
                {
                    "name": name,
                    "source": self.sources.get(name),
                    "record": self.locks.get(name),
                },
            )
            self._state_dirty = True
        else:
            self.save()
        path.unlink()
        sync_directory(path.parent)

    def save_record(self, name, source, record):
        validate_source_lock(source, record, name)
        if backend(record) == "flake":
            raise legacy_flake_error(name, record)
        self.journal("save", name, source, record)
        self.recover()

    def install(self, name, source, record):
        validate_source_lock(source, record, name)
        if backend(record) == "flake":
            raise legacy_flake_error(name, record)
        if platform.machine() != "x86_64" or sys.platform != "linux":
            raise Error("This first version only installs on x86_64 Linux.")
        # Check ownership before building without changing launchers.
        self.links(name, verify_only=True)
        self.profile(name).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="obtain-") as tmp:
            manifest = Path(tmp) / "manifest.json"
            atomic_json(manifest, record)
            print(f"Building {name} {clean(record['version'])}…", flush=True)
            output = json.loads(
                nix(
                    "build",
                    "--impure",
                    "--out-link",
                    str(Path(tmp) / "result"),
                    "--json",
                    "--file",
                    str(ROOT / "build.nix"),
                    "--argstr",
                    "manifestFile",
                    str(manifest),
                )
            )
            store_path = output[0]["outputs"]["out"]
            if load_json(Path(store_path) / "share/obtain/manifest.json") != record:
                raise Error(
                    "Built package metadata does not match the selected release."
                )
            self.journal("install", name, source, record)
            try:
                run(
                    [
                        "nix-env",
                        "--profile",
                        str(self.profile(name)),
                        "--set",
                        store_path,
                    ],
                    capture=False,
                )
            finally:
                self.recover()
        if self.installed(name) != record:
            raise Error("Profile switch did not install the expected release.")
        print(f"Installed {name} {clean(record['version'])}")

    def previous_generation(self, name):
        current = self.installed(name)
        if not current:
            raise Error(f"{name} is not installed.")
        pattern = re.compile(re.escape(name) + r"-(\d+)-link\Z")
        active = pattern.fullmatch(self.profile(name).readlink().name)
        if not active:
            raise Error(
                "Unrecognized profile layout; cannot select a rollback generation."
            )
        candidates = []
        for path in self.profile(name).parent.glob(f"{name}-*-link"):
            match = pattern.fullmatch(path.name)
            if not match or int(match[1]) >= int(active[1]):
                continue
            previous = load_json(path / "share/obtain/manifest.json")
            if (
                previous
                and previous != current
                and previous.get("repository") == current["repository"]
                and backend(previous) == backend(current)
                and previous.get("package") == current.get("package")
            ):
                validate_lock(previous, name)
                candidates.append((int(match[1]), previous))
        if not candidates:
            raise Error(f"No previous retained installation of {name} is available.")
        return max(candidates, key=lambda item: item[0])

    def rollback(self, name):
        if backend(self.locks[name]) == "flake":
            raise legacy_flake_error(name, self.locks[name])
        generation, previous = self.previous_generation(name)
        source = {**self.sources[name], "pinned": True}
        self.journal("rollback", name, source, previous)
        try:
            run(
                [
                    "nix-env",
                    "--profile",
                    str(self.profile(name)),
                    "--switch-generation",
                    str(generation),
                ],
                capture=False,
            )
        finally:
            self.recover()
        if self.installed(name) != previous:
            raise Error("Profile rollback did not select the expected release.")
        print(
            f"Rolled back {name} to {clean(previous['version'])}; pinned until 'obtain unpin {name}'."
        )

    def remove(self, name):
        self.links(name, verify_only=True)
        self.journal("remove", name)
        try:
            if self.installed(name):
                run(
                    [
                        "nix-env",
                        "--profile",
                        str(self.profile(name)),
                        "--uninstall",
                        "*",
                    ],
                    capture=False,
                )
        finally:
            self.recover()
        print(
            f"Removed {name}; application data and old Nix generations were retained."
        )


def parser():
    p = argparse.ArgumentParser(
        description="Track and install public GitHub Linux release files."
    )
    p.add_argument("--version", action="version", version="obtain 0.2.0")
    sub = p.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser(
        "inspect",
        help="Show supported Linux release files",
    )
    inspect.add_argument("url")
    add = sub.add_parser("add", help="Track and install a GitHub release file")
    add.add_argument("url")
    add.add_argument("--name", type=name_check)
    add.add_argument(
        "--type",
        choices=["auto", "appimage", "archive", "binary"],
        default="auto",
        help="Release file type (default: auto)",
    )
    add.add_argument(
        "--program",
        help="Archive executable path (automatically detected when omitted)",
    )
    add.add_argument(
        "--strip-components",
        type=int,
        choices=range(17),
        default=0,
        metavar="N",
        help="Archive only: remove N leading directory components (0–16)",
    )
    add.add_argument("--asset", help="Case-sensitive filename glob (quote it)")
    add.add_argument("--prereleases", action="store_true")
    add.add_argument(
        "--track-only",
        action="store_true",
        help="Lock the release without installing",
    )
    sub.add_parser("list", help="Show installed and locked versions")
    check = sub.add_parser(
        "check", help="Check upstream metadata without building or installing"
    )
    check.add_argument("name", nargs="?", type=name_check)
    update = sub.add_parser(
        "update", help="Resolve and apply updates (pinned apps are skipped)"
    )
    update.add_argument("name", nargs="?", type=name_check)
    update.add_argument(
        "--asset", help="Replace the asset selector for one named release app"
    )
    for command in (check, update):
        command.add_argument(
            "--retry-failed",
            action="store_true",
            help="Retry only unfinished apps from this command's last batch",
        )
    doctor_parser = sub.add_parser(
        "doctor", help="Check an installed app and its environment without launching it"
    )
    doctor_parser.add_argument("name", type=name_check)
    doctor_parser.add_argument(
        "--json", action="store_true", help="Machine-readable diagnostic report"
    )
    doctor_parser.add_argument(
        "--launch-test",
        action="store_true",
        help="Launch the app for five seconds, capture runtime errors, then stop its process group",
    )
    for command, help_text in [
        ("install", "Install the locked release"),
        ("rollback", "Restore the previous installed generation and pin it"),
        ("remove", "Remove the app and stop tracking it"),
        ("pin", "Exclude an app from updates"),
        ("unpin", "Allow updates again"),
        ("info", "Show source, installed metadata, and lock"),
    ]:
        cmd = sub.add_parser(command, help=help_text)
        cmd.add_argument("name", type=name_check)
    return p


def _doctor_probe(executable):
    """Read a startup probe without retaining more than its last 64 KiB."""
    try:
        proc = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        return None, f"Could not launch {executable}: {e}\n", True

    tail = bytearray()
    deadline = time.monotonic() + 5

    def read_available():
        chunk = os.read(proc.stdout.fileno(), 65536)
        if chunk:
            tail.extend(chunk)
            del tail[:-65536]
        return bool(chunk)

    try:
        eof = False
        while time.monotonic() < deadline:
            timeout = (
                0 if proc.poll() is not None else min(0.1, deadline - time.monotonic())
            )
            if not select.select([proc.stdout], [], [], max(0, timeout))[0]:
                if proc.poll() is not None:
                    break
                continue
            if not read_available():
                eof = True
                break
        code = proc.poll()
        if eof and code is None:
            try:
                code = proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
    finally:
        # A launcher can exit while descendants still hold the output pipe.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        drain_deadline = time.monotonic() + 0.2
        while (
            time.monotonic() < drain_deadline
            and select.select([proc.stdout], [], [], 0)[0]
            and read_available()
        ):
            pass
        proc.stdout.close()
    return code, tail.decode("utf-8", errors="replace"), False


def doctor(args, store):
    name = store.targets(args.name)[0]
    if backend(store.sources[name]) == "flake":
        raise legacy_flake_error(name, store.locks[name])
    checks = []

    def check(label, ok, detail, warning=False):
        checks.append(
            {
                "check": label,
                "status": "ok" if ok else ("warning" if warning else "failed"),
                "detail": str(detail),
            }
        )

    try:
        installed = store.installed(name)
        if installed is not None:
            if not isinstance(installed, dict):
                raise Error("Installed manifest must be a JSON object.")
            validate_lock(installed, name)
    except (Error, OSError, ValueError, KeyError, TypeError) as e:
        installed = None
        check("installed", False, f"Could not read installed manifest: {e}")
    else:
        check(
            "installed",
            installed is not None,
            "Installed profile found" if installed else f"Run obtain install {name}",
        )
    if installed:
        executable = store.profile(name) / "bin" / name
        check(
            "executable",
            executable.is_file() and os.access(executable, os.X_OK),
            executable,
        )
        check(
            "lock",
            installed == store.locks.get(name),
            "Installed manifest compared with the tracked lock",
        )
        for label, link, target in (
            ("launcher", store.data / "bin" / name, executable),
            (
                "desktop",
                store.applications / f"obtain-{name}.desktop",
                store.profile(name) / "share/applications" / f"obtain-{name}.desktop",
            ),
        ):
            check(
                label,
                link.is_symlink()
                and os.readlink(link) == str(target)
                and link.exists(),
                link,
            )
    if args.launch_test and installed:
        executable = store.profile(name) / "bin" / name
        if executable.is_file() and os.access(executable, os.X_OK):
            log = store.cache / f"doctor-{name}.log"
            code, details, launch_error = _doctor_probe(executable)
            diagnostic = ""
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(details)
                diagnostic = f"; log: {log}"
            except OSError as e:
                check(
                    "diagnostic_log", False, f"Could not save {log}: {e}", warning=True
                )
                if details and not launch_error:
                    diagnostic = f"; output: {details.strip()}"
            missing = re.findall(
                r"(?:error while loading shared libraries:|cannot open shared object file[^\n]*|Library not loaded:)[^\n]*",
                details,
                re.IGNORECASE,
            )
            check(
                "runtime_libraries",
                not missing,
                "; ".join(missing)
                if missing
                else "No missing-library errors observed during the probe",
            )
            check(
                "startup",
                not launch_error and code in (0, None),
                (
                    f"Launch failed: {details.strip()}{diagnostic}"
                    if launch_error
                    else f"Exit: {code if code is not None else 'still running after five seconds'}{diagnostic}"
                ),
            )
    check("nix", shutil.which("nix") is not None, "Nix available on PATH")
    check(
        "path",
        str(store.data / "bin") in os.environ.get("PATH", "").split(os.pathsep),
        f"Add {store.data / 'bin'} to PATH",
        warning=True,
    )
    check(
        "display",
        bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        "A graphical session is required for GUI apps",
        warning=True,
    )
    check(
        "graphics",
        Path("/run/opengl-driver/lib").is_dir(),
        "NixOS hardware.graphics libraries",
        warning=True,
    )
    log = store.cache / "last-command.log"
    try:
        last_failure = log.exists()
    except OSError as e:
        last_failure = False
        check("last_failure", False, f"Could not inspect {log}: {e}", warning=True)
    if last_failure:
        checks.append(
            {
                "check": "last_failure",
                "status": "info",
                "detail": f"Most recent failed external command (may belong to another app): {log}",
            }
        )
    report = {
        "name": name,
        "checks": checks,
        "scope": (
            "Includes a five-second startup probe; full app functionality is unverified."
            if args.launch_test
            else "Static checks only. Use --launch-test to probe startup and capture runtime errors."
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in checks:
            print(f"{item['status'].upper()}: {item['check']}: {clean(item['detail'])}")
        print(report["scope"])
    if any(item["status"] == "failed" for item in checks):
        raise Error(f"Doctor found problems with {name}.")


def add_source(args, store):
    kind = args.type
    if kind not in ("auto", "appimage", "archive", "binary"):
        raise Error(
            "Obtain installs GitHub release files only; use Nix directly for flakes."
        )
    if kind == "auto" and (args.program or args.strip_components):
        kind = "archive"
    if kind in ("appimage", "binary") and args.program:
        raise Error("--program applies only to archive packages.")
    if args.strip_components and kind != "archive":
        raise Error("--strip-components requires --type archive.")
    if kind == "archive":
        if args.program:
            program_path(args.program)
    repo = repository(args.url)
    name = args.name or name_check(
        re.sub(r"[^a-z0-9-]", "-", repo.split("/")[1].lower())
    )
    if name in store.sources and (
        args.name or store.sources[name]["repository"] == repo
    ):
        raise tracked_name_error(name, store)
    source = {
        "kind": kind,
        "repository": repo,
        "pinned": False,
    }
    source.update(asset=args.asset, prereleases=args.prereleases)
    if kind == "archive":
        source["program"] = args.program
        source["strip_components"] = args.strip_components
    return name, source


def add_command(args, store, github):
    name, source = add_source(args, store)
    original_source = dict(source)

    def choose_candidate():
        return (
            discover_release(source, github)
            if source["kind"] == "auto"
            else choose_asset(github, source)
        )

    try:
        candidate = choose_candidate()
    except RepositoryMoved as moved:
        canonical = github.canonical_repository(moved.repository_id)
        source = {**original_source, "repository": canonical}
        if not args.name:
            name = name_check(
                re.sub(r"[^a-z0-9-]", "-", canonical.split("/")[1].lower())
            )
            if name in store.sources:
                raise tracked_name_error(name, store)
        print(f"Repository moved to https://github.com/{canonical}", flush=True)
        candidate = choose_candidate()
    if name in store.sources:
        raise tracked_name_error(name, store)
    print(f"Selected {clean(candidate['asset_name'])} ({clean(candidate['version'])})")
    print(candidate["release_url"])
    record = lock_release(name, candidate, interactive=True)
    if backend(source) == "archive":
        source.update(
            program=record["program"],
            strip_components=record.get("strip_components", 0),
            auto_program=args.program is None,
        )
        print(f"Program: {record['program']}")
    if args.track_only:
        store.save_record(name, source, record)
        print(f"Locked {name}; use 'obtain install {name}' to install.")
    else:
        store.install(name, source, record)


def list_command(store):
    if not store.sources:
        print("No apps tracked. Start with: obtain add https://github.com/owner/repo")
    else:
        print("APP\tTYPE\tINSTALLED\tLOCKED\tPOLICY")
        for name in sorted(store.sources):
            installed = store.installed(name)
            print(
                "\t".join(
                    [
                        name,
                        backend(store.sources[name]),
                        clean(installed["version"]) if installed else "—",
                        clean(store.locks.get(name, {}).get("version", "—")),
                        (
                            "legacy flake; migrate to Nix"
                            if backend(store.sources[name]) == "flake"
                            else (
                                "pinned"
                                if store.sources[name].get("pinned")
                                else "updates enabled"
                            )
                        ),
                    ]
                )
            )


def batch_targets(args, store, batch_path):
    cmd = args.command
    targets = store.targets(args.name)
    if cmd == "update" and args.asset and (not args.name or args.retry_failed):
        raise Error("update --asset requires one NAME and cannot use --retry-failed.")
    overrides = {args.name: args.asset} if cmd == "update" and args.asset else {}
    if args.retry_failed:
        if args.name:
            raise Error("Use either NAME or --retry-failed.")
        previous = load_batch(batch_path)
        if not isinstance(previous, dict) or not isinstance(
            previous.get("unfinished", []), list
        ):
            raise Error("Invalid saved batch report.")
        saved_overrides = previous.get("asset_overrides", {})
        if not isinstance(saved_overrides, dict) or any(
            not isinstance(value, str) for value in saved_overrides.values()
        ):
            raise Error("Invalid saved asset overrides in batch report.")
        overrides = saved_overrides if cmd == "update" else {}
        targets = list(
            dict.fromkeys(
                name
                for name in previous.get("unfinished", [])
                if isinstance(name, str) and name in store.sources
            )
        )
    return targets, overrides


def batch_item(cmd, name, store, github, overrides):
    source = dict(store.sources[name])
    if backend(source) == "flake":
        raise legacy_flake_error(name, store.locks[name])
    if name in overrides:
        source["asset"] = overrides[name]
    if source.get("pinned") and cmd == "update":
        if name in overrides:
            raise Error(
                f"Cannot change --asset while pinned. Run 'obtain unpin {name}', then retry the update."
            )
        print(f"{name}: pinned; skipped")
        return
    candidate = github.release(source)
    old = store.locks.get(name, {})
    if cmd == "update":
        current = store.installed(name)
    changed = not same_release(old, candidate)
    if changed:
        print(
            f"{name}: {clean(old.get('version', 'unlocked'))} → {clean(candidate['version'])}"
            + (" (pinned)" if source.get("pinned") else "")
        )
        print(candidate["release_url"])
    else:
        print(f"{name}: locked release is current ({clean(candidate['version'])})")
    if cmd == "update":
        record = lock_release(name, candidate) if changed else old
        if backend(source) == "archive" and automatic_program(source):
            source.update(program=record["program"], auto_program=True)
        if current:
            if record != current:
                store.install(name, source, record)
            elif source != store.sources[name]:
                store.save_record(name, source, record)
        elif changed or source != store.sources[name]:
            store.save_record(name, source, record)
            print(f"{name}: lock updated; not installed")


def batch_command(args, store, github):
    cmd = args.command
    batch_path = store.data / f"{cmd}-batch.json"
    targets, overrides = batch_targets(args, store, batch_path)
    failures = []
    unfinished = dict.fromkeys(targets)

    def checkpoint():
        atomic_json(
            batch_path,
            {
                "command": cmd,
                "unfinished": list(unfinished),
                "asset_overrides": {
                    name: value
                    for name, value in overrides.items()
                    if name in unfinished
                },
            },
        )

    checkpoint()
    try:
        with store.batch_saves():
            for name in targets:
                try:
                    batch_item(cmd, name, store, github, overrides)
                    append_json(batch_path, name)
                    unfinished.pop(name)
                except (Error, OSError, ValueError, KeyError, TypeError) as e:
                    failures.append(name)
                    print(f"{name}: {clean(e)}", file=sys.stderr)
                    if (store.data / "pending.json").exists():
                        # Never replace an unreconciled operation's journal
                        # with the next app's operation after a write failure.
                        break
                    if isinstance(e, RateLimited):
                        print(
                            "Batch paused at the API limit; remaining apps were saved.",
                            file=sys.stderr,
                        )
                        break
    finally:
        checkpoint()
    if failures:
        raise Error(
            "Unfinished apps: "
            + ", ".join(unfinished)
            + f". Retry with: obtain {cmd} --retry-failed"
        )


def named_command(args, store):
    cmd = args.command
    name = store.targets(args.name)[0]
    if backend(store.sources[name]) == "flake" and cmd not in ("info", "remove"):
        raise legacy_flake_error(name, store.locks[name])
    if cmd == "install":
        if name not in store.locks:
            raise Error(f"No locked release; run 'obtain update {name}' first.")
        store.install(name, store.sources[name], store.locks[name])
    elif cmd == "rollback":
        store.rollback(name)
    elif cmd == "remove":
        store.remove(name)
    elif cmd in ("pin", "unpin"):
        store.sources[name]["pinned"] = cmd == "pin"
        store.save()
        print(f"{name}: {'pinned' if cmd == 'pin' else 'updates enabled'}")
    elif cmd == "info":
        print(
            json.dumps(
                {
                    "source": store.sources[name],
                    "locked": store.locks.get(name),
                    "installed": store.installed(name),
                },
                indent=2,
            )
        )


def dispatch(args, store, github):
    github.clear_memo()
    cmd = args.command
    if cmd == "add":
        add_command(args, store, github)
    elif cmd == "list":
        list_command(store)
    elif cmd in ("check", "update"):
        batch_command(args, store, github)
    elif cmd == "doctor":
        doctor(args, store)
    else:
        named_command(args, store)


def main(argv=None):
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupt_on_term(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, interrupt_on_term)
        args = parser().parse_args(argv)
        if args.command == "inspect":
            inspect_repository(args, GitHub(cache_directory() / "github"))
        else:
            store = Store()
            with store.session():
                dispatch(args, store, GitHub(store.cache / "github"))
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError) as e:
        print("obtain: " + clean(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "obtain: interrupted; the next command that accesses tracked apps will reconcile any pending profile change.",
            file=sys.stderr,
        )
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(main())
