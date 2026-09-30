"""CLI evaluations, executed as an unprivileged user in the NixOS VM.

The upstream HTTP service is simulated. Crash-phase tests pause a wrapper around
the real nix-env command; they still use the packaged CLI and real Nix profiles.
"""

import errno
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path("/var/lib/obtain-evals")
URL = "https://github.com/eval/"
BASE_ENV = dict(
    os.environ,
    XDG_RUNTIME_DIR="/run/user/1000",
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
)


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


def control(**kwargs):
    write_json(ROOT / "control.json", kwargs)


def run(args, env, expected=0):
    result = subprocess.run(
        args,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=240,
    )
    output = result.stdout + result.stderr
    with (ROOT / "commands.log").open("a") as log:
        log.write(f"\n$ {shlex.join(args)}\nexit={result.returncode}\n{output}")
    if (expected == 0 and result.returncode != 0) or (
        expected != 0 and result.returncode == 0
    ):
        print(output, file=sys.stderr, flush=True)
        raise AssertionError(
            f"{shlex.join(args)}: expected {expected}, got {result.returncode}\n{output}"
        )
    if "Traceback (most recent call last)" in output:
        raise AssertionError("Unhandled exception:\n" + output)
    return output if expected else result.stdout


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=ROOT))
        self.env = dict(
            BASE_ENV,
            **{
                f"XDG_{kind}_HOME": str(self.root / kind.lower())
                for kind in ("CONFIG", "DATA", "CACHE")
            },
        )
        # Share only Nix's immutable download cache; Obtain state/cache remains
        # isolated per scenario. Avoid copying the whole Nixpkgs archive for each scenario.
        cache = self.root / "cache"
        cache.mkdir()
        (ROOT / "nix-cache").mkdir(exist_ok=True)
        (cache / "nix").symlink_to(ROOT / "nix-cache")
        self.config = self.root / "config/obtain"
        self.data = self.root / "data/obtain"
        control()

    def cli(self, *args, fail=False):
        return run(["obtain", *args], self.env, int(fail))

    def add(self, repo="base", name=None, *args):
        return self.cli("add", URL + repo, "--name", name or repo, *args)

    def info(self, name="base"):
        return json.loads(self.cli("info", name))

    def packaged_script(self):
        # Run the installed source with the VM Python so the crash-test shim is
        # first on PATH while exercising the packaged CLI and real Nix profiles.
        launcher = Path(shutil.which("obtain")).resolve()
        script = launcher.parent.parent / "lib/obtain/obtain.py"
        self.assertTrue(script.is_file(), script)
        return str(script)

    def app(self, name, expected, *args):
        self.assertEqual(
            run([str(self.data / "bin" / name), *args], self.env).strip(), expected
        )

    def requests(self):
        return [
            json.loads(line)
            for line in (ROOT / "requests.jsonl").read_text().splitlines()
        ]

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.config.glob("*.json")}

    def lifecycle(self, repo, prefix):
        self.add(repo)
        self.app(repo, prefix + " 1 hello world", "hello", "world")
        self.assertIn(repo, self.cli("list"))
        self.cli("rollback", repo, fail=True)
        desktop = self.root / f"data/applications/obtain-{repo}.desktop"
        executable = next(
            line[5:]
            for line in desktop.read_text().splitlines()
            if line.startswith("Exec=")
        )
        self.assertTrue(executable.startswith("/nix/store/"))
        self.assertEqual(run([executable], self.env).strip(), prefix + " 1")
        before = self.snapshot()
        control(versions={repo: 2})
        self.assertIn("→", self.cli("check", repo))
        self.assertEqual(before, self.snapshot())
        self.app(repo, prefix + " 1")
        self.cli("pin", repo)
        self.assertIn("pinned; skipped", self.cli("update", repo))
        self.app(repo, prefix + " 1")
        self.cli("unpin", repo)
        self.cli("update", repo)
        self.app(repo, prefix + " 2")
        self.cli("rollback", repo)
        self.app(repo, prefix + " 1")
        self.assertTrue(self.info(repo)["source"]["pinned"])
        self.cli("update", repo)
        self.app(repo, prefix + " 1")
        personal = self.root / "data/personal-file"
        personal.write_text("keep me")
        self.cli("remove", repo)
        self.assertFalse((self.data / "bin" / repo).is_symlink())
        self.assertFalse(desktop.is_symlink())
        self.assertTrue(list((self.data / "profiles").glob(repo + "-*-link")))
        self.assertEqual(personal.read_text(), "keep me")
        self.assertIn("No apps tracked", self.cli("list"))
        self.add(repo)
        self.cli("rollback", repo)
        self.app(repo, prefix + " 1")

    def test_01_help_empty_and_unknown_commands(self):
        self.assertIn("0.2.0", self.cli("--version"))
        self.assertIn("inspect", self.cli("--help"))
        self.assertIn("No apps tracked", self.cli("list"))
        self.cli("check")
        self.cli("update")
        self.cli("info", "missing", fail=True)
        self.cli("not-a-command", fail=True)

    def test_02_invalid_arguments_rejected_without_tracking(self):
        for args in [
            ("add", "http://github.com/eval/image"),
            ("add", URL + "image", "--name", "../bad"),
            ("add", URL + "image", "--type", "appimage", "--program", "x"),
            ("add", URL + "base", "--type", "flake"),
            ("add", URL + "base", "--type", "appimage", "--strip-components", "1"),
        ]:
            with self.subTest(args=args):
                self.cli(*args, fail=True)
        self.assertIn("No apps tracked", self.cli("list"))

    def test_03_inspection_without_installation(self):
        self.assertIn("Automatic x86_64 selection", self.cli("inspect", URL + "image"))
        self.assertIn("Automatic x86_64 selection", self.cli("inspect", URL + "base"))
        self.assertIn(
            "No public repository or published stable GitHub release",
            self.cli("inspect", URL + "native"),
        )
        self.assertIn("No apps tracked", self.cli("list"))
        self.assertFalse((self.data / "profiles").exists())

    def test_04_appimage_lifecycle_and_desktop_entry(self):
        self.lifecycle("image", "appimage")

    def test_05_second_appimage_lifecycle_and_desktop_entry(self):
        self.lifecycle("base", "appimage")

    def test_06_track_only_updates_and_install_locked_release(self):
        for repo, prefix in [("image", "appimage"), ("base", "appimage")]:
            with self.subTest(repo=repo):
                self.add(repo, None, "--track-only")
                self.assertIsNone(self.info(repo)["installed"])
                control(versions={repo: 2})
                self.cli("update", repo)
                self.assertIsNone(self.info(repo)["installed"])
                control(versions={repo: 1})
                self.cli("install", repo)
                self.app(repo, prefix + " 2")
                self.cli("remove", repo)
                control()

    def test_07_ambiguous_appimage_and_explicit_asset(self):
        self.cli("add", URL + "ambiguous-image", fail=True)
        self.add("ambiguous-image", "selected", "--asset", "one-*")
        self.app("selected", "appimage 1")

    def test_08_architecture_filter_and_unlabelled_override(self):
        self.cli("add", URL + "foreign", "--asset", "*", fail=True)
        self.add("unlabelled")
        self.app("unlabelled", "appimage 1")

    def test_09_prerelease_opt_in(self):
        self.add("prerelease", None, "--prereleases")
        self.app("prerelease", "appimage 2")

    def test_10_digest_mismatch_preserves_empty_state(self):
        self.cli("add", URL + "digest", fail=True)
        self.assertIn("No apps tracked", self.cli("list"))
        self.assertFalse((self.data / "bin/digest").exists())

    def test_15_failed_update_preserves_installed_version(self):
        self.add()
        before = self.snapshot()
        profile = (self.data / "profiles/base").resolve()
        control(versions={"base": 3})
        self.cli("update", "base", fail=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(profile, (self.data / "profiles/base").resolve())
        self.app("base", "appimage 1")

    def test_16_http_errors_preserve_working_installation(self):
        self.add()
        before = self.snapshot()
        errors = {
            "offline": "GitHub API returned HTTP 503",
            "rate-limit": "GitHub API rate limit reached",
            "missing": "not found",
            "redirect": "GitHub redirected this repository",
            "malformed": "Could not read GitHub release metadata",
        }
        for mode, error in errors.items():
            with self.subTest(mode=mode):
                control(modes={"base": mode})
                self.assertIn(error, self.cli("check", "base", fail=True))
                if mode == "missing":
                    self.assertIn(
                        "No public repository or published stable GitHub release",
                        self.cli("inspect", URL + "base"),
                    )
                else:
                    self.assertIn(error, self.cli("inspect", URL + "base", fail=True))
                self.assertEqual(before, self.snapshot())
                self.app("base", "appimage 1")

    def test_17_etag_cache_and_corrupt_cache_recovery(self):
        self.add("image", None, "--track-only")
        before = len(self.requests())
        self.cli("check", "image")
        checked = self.requests()[before:]
        self.assertTrue(
            any(
                r["status"] == 304 and "/repos/eval/image/releases/" in r["path"]
                for r in checked
            ),
            checked,
        )
        caches = list((self.root / "cache/obtain/github").glob("*.json"))
        self.assertTrue(caches, "Expected a populated GitHub HTTP cache")
        for cache in caches:
            cache.write_text("{broken")
        before = len(self.requests())
        self.assertIn("current", self.cli("check", "image"))
        refreshed = self.requests()[before:]
        self.assertTrue(
            any(
                r["status"] == 200 and "/repos/eval/image/releases/" in r["path"]
                for r in refreshed
            ),
            refreshed,
        )
        self.assertTrue(
            all(json.loads(cache.read_text()).get("body") for cache in caches)
        )

    def test_18_partial_batch_failure_still_updates_other_apps(self):
        self.add("image")
        self.add("base")
        control(versions={"image": 2}, modes={"base": "offline"})
        self.cli("update", fail=True)
        self.app("image", "appimage 2")
        self.app("base", "appimage 1")

    def test_19_duplicate_and_launcher_collision(self):
        self.add("base", None, "--track-only")
        before = self.snapshot()
        self.cli("add", URL + "base", fail=True)
        target = self.data / "bin/base"
        target.parent.mkdir(parents=True)
        target.write_text("user file")
        self.cli("install", "base", fail=True)
        self.assertEqual(target.read_text(), "user file")
        self.assertEqual(before, self.snapshot())

    def test_20_concurrent_commands_are_serialized(self):
        self.cli("list")
        with (self.data / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn("Another Obtain command", self.cli("list", fail=True))
        self.cli("list")

    def test_21_corrupt_state_and_lock_rejected(self):
        self.add("base", None, "--track-only")
        for filename, value in [
            ("sources.json", "{bad"),
            ("sources.json", '{"schema":99,"apps":{}}'),
            ("sources.json", '{"schema":1,"apps":{"base":null}}'),
            ("lock.json", '{"schema":1,"apps":{"base":[]}}'),
            ("lock.json", '{"schema":1,"apps":{"base":{}}}'),
        ]:
            path = self.config / filename
            original = path.read_bytes()
            path.write_text(value)
            self.cli("list", fail=True)
            path.write_bytes(original)
        self.assertIsNone(self.info()["installed"])

    def test_22_recovery_before_and_after_profile_switch(self):
        self.add()
        old = self.info()
        control(versions={"base": 2})
        self.cli("update", "base")
        new = self.info()
        for switched in (False, True):
            with self.subTest(switched=switched):
                record = new["installed"] if switched else old["installed"]
                generations = list((self.data / "profiles").glob("base-*-link"))
                target = next(
                    p.resolve()
                    for p in generations
                    if json.loads((p / "share/obtain/manifest.json").read_text())
                    == record
                )
                run(
                    [
                        "nix-env",
                        "--profile",
                        str(self.data / "profiles/base"),
                        "--set",
                        str(target),
                    ],
                    self.env,
                )
                write_json(
                    self.config / "lock.json",
                    {"schema": 1, "apps": {"base": old["locked"]}},
                )
                write_json(
                    self.data / "pending.json",
                    {
                        "operation": "install",
                        "name": "base",
                        "source": old["source"],
                        "record": new["locked"],
                        "previous": old["installed"],
                    },
                )
                self.cli("list")
                self.assertEqual(self.info()["locked"], record)
                self.assertFalse((self.data / "pending.json").exists())
                self.app("base", "appimage " + ("2" if switched else "1"))

    def test_23_recovery_after_interrupted_remove(self):
        self.add()
        old = self.info()
        write_json(
            self.data / "pending.json",
            {
                "operation": "remove",
                "name": "base",
                "source": None,
                "record": None,
                "previous": old["installed"],
            },
        )
        run(
            [
                "nix-env",
                "--profile",
                str(self.data / "profiles/base"),
                "--uninstall",
                "*",
            ],
            self.env,
        )
        self.assertIn("No apps tracked", self.cli("list"))
        self.assertFalse((self.data / "bin/base").is_symlink())

    def test_25_legacy_appimage_lock_needs_no_migration(self):
        self.add("image", None, "--track-only")
        path = self.config / "sources.json"
        value = json.loads(path.read_text())
        value["apps"]["image"].pop("kind")
        write_json(path, value)
        self.cli("install", "image")
        self.app("image", "appimage 1")
        self.cli("check", "image")

    def test_26_tokens_only_go_to_api_metadata(self):
        start = len(self.requests())
        self.env["GITHUB_TOKEN"] = "vm-fixture-not-a-real-token"
        self.add("image", "token-test", "--track-only")
        requests = self.requests()[start:]
        self.assertTrue(
            any(r["authorization"] for r in requests if r["path"].startswith("/repos/"))
        )
        downloads = [r for r in requests if "/releases/download/" in r["path"]]
        self.assertTrue(downloads)
        self.assertTrue(all(not r["authorization"] for r in downloads))
        self.assertNotIn("vm-fixture-not-a-real-token", str(self.snapshot()))

    def test_29_unsupported_release_formats_never_execute_or_download(self):
        before = len(self.requests())
        for pattern in (
            None,
            "*",
            "*.deb",
            "*.rpm",
            "*.tar.gz",
            "*.zip",
            "*.exe",
            "*.dmg",
        ):
            args = ["add", URL + "unsupported", "--type", "appimage"]
            if pattern:
                args += ["--asset", pattern]
            self.assertIn("Expected one x86_64 AppImage", self.cli(*args, fail=True))
        requests = self.requests()[before:]
        self.assertFalse(any("/releases/download/" in r["path"] for r in requests))
        self.assertIn("No apps tracked", self.cli("list"))

    def test_30_malformed_metadata_and_asset_urls_preserve_installed_apps(self):
        self.add()
        before = self.snapshot()
        errors = {
            "null": "Invalid GitHub release metadata",
            "scalar": "Invalid GitHub release metadata",
            "list": "Invalid GitHub release metadata",
            "asset-null": "Invalid GitHub asset list metadata",
            "asset-name-number": "Invalid GitHub asset metadata",
            "asset-url": "does not have a GitHub download URL",
            "asset-digest": "Invalid upstream asset digest",
        }
        for mode, error in errors.items():
            with self.subTest(mode=mode):
                control(modes={"image": mode})
                self.assertIn(error, self.cli("add", URL + "image", fail=True))
                self.assertEqual(before, self.snapshot())
                self.app("base", "appimage 1")

    def test_34_corrupt_recovery_journal_is_retained_without_mutation(self):
        self.add()
        before = self.snapshot()
        original = self.info()
        valid = {
            "operation": "save",
            "name": "base",
            "source": original["source"],
            "record": original["locked"],
            "previous": original["installed"],
        }
        bad = [
            None,
            [],
            {},
            {"operation": "unknown", "name": "base"},
            dict(valid, source=None),
            dict(valid, record=[]),
            dict(valid, previous=17),
            dict(valid, source=dict(original["source"], repository="eval/another")),
        ]
        path = self.data / "pending.json"
        for value in bad:
            with self.subTest(value=value):
                write_json(path, value)
                saved = path.read_bytes()
                self.cli("list", fail=True)
                self.assertEqual(saved, path.read_bytes())
                self.assertEqual(before, self.snapshot())
                self.app("base", "appimage 1")
                path.unlink()

    def test_35_read_only_configuration_fails_without_losing_state(self):
        self.add()
        before = self.snapshot()
        self.config.chmod(0o500)
        try:
            self.assertIn("Permission denied", self.cli("pin", "base", fail=True))
            self.assertEqual(before, self.snapshot())
        finally:
            self.config.chmod(0o700)
        self.assertFalse(self.info()["source"]["pinned"])
        self.app("base", "appimage 1")

    @staticmethod
    def fill_filesystem(directory):
        path = directory / "filler"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise
        finally:
            os.close(fd)
        return path

    def test_36_full_config_filesystem_recovers_after_profile_switch(self):
        self.add()
        volume = Path(tempfile.mkdtemp(dir=ROOT / "full"))
        shutil.copytree(self.config, volume / "obtain")
        self.config = volume / "obtain"
        self.env["XDG_CONFIG_HOME"] = str(volume)
        filler = self.fill_filesystem(volume)
        try:
            control(versions={"base": 2})
            self.assertIn("No space left", self.cli("update", "base", fail=True))
            # The per-app event is durable before batch snapshot compaction.
            # A full config filesystem must leave it available for replay.
            self.assertTrue((self.data / "state-events.jsonl").exists())
            self.app("base", "appimage 2")
        finally:
            filler.unlink()
        self.cli("list")
        self.assertEqual(
            self.info()["locked"]["version"], self.info()["installed"]["version"]
        )
        self.assertFalse((self.data / "pending.json").exists())
        self.assertFalse((self.data / "state-events.jsonl").exists())
        saved = json.loads((self.config / "lock.json").read_text())
        self.assertEqual(saved["apps"]["base"], self.info()["installed"])
        self.app("base", "appimage 2")

    def test_37_full_journal_filesystem_preserves_old_profile(self):
        volume = Path(tempfile.mkdtemp(dir=ROOT / "full"))
        self.env["XDG_DATA_HOME"] = str(volume)
        self.data = volume / "obtain"
        self.add()
        before = self.snapshot()
        filler = self.fill_filesystem(volume)
        try:
            control(versions={"base": 2})
            self.assertIn("No space left", self.cli("update", "base", fail=True))
            self.assertEqual(before, self.snapshot())
            self.app("base", "appimage 1")
        finally:
            filler.unlink()
        self.assertFalse((self.data / "pending.json").exists())
        self.app("base", "appimage 1")

    def test_38_sigint_during_release_check_preserves_state(self):
        for repo in ("base", "image"):
            self.add(repo)
            before = self.snapshot()
            control(modes={repo: "delay"}, versions={repo: 2})
            proc = subprocess.Popen(
                ["obtain", "update", repo],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.5)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 130, stdout + stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(before, self.snapshot())
            self.app(repo, "appimage 1")
            control()

    def interrupted_install(self, phase, terminate):
        """Stop an update at a known boundary around its real profile switch."""
        real_nix_env = shutil.which("nix-env")
        self.assertIsNotNone(real_nix_env)
        shim = self.root / f"shim-{phase}-{terminate.name.lower()}"
        shim.mkdir()
        marker = shim / "reached"
        wrapper = shim / "nix-env"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys, time\n"
            f"real = {real_nix_env!r}\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"phase = {phase!r}\n"
            "if '--set' not in sys.argv:\n"
            "    os.execv(real, [real, *sys.argv[1:]])\n"
            "if phase == 'post':\n"
            "    code = subprocess.run([real, *sys.argv[1:]]).returncode\n"
            "    if code:\n"
            "        sys.exit(code)\n"
            "marker.write_text(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        wrapper.chmod(0o755)
        env = dict(self.env, PATH=str(shim) + os.pathsep + self.env["PATH"])
        proc = subprocess.Popen(
            [sys.executable, self.packaged_script(), "update", "base"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        stopped = False
        try:
            while (
                not marker.exists()
                and proc.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(marker.exists(), f"Did not reach {phase}-switch barrier")
            pending = json.loads((self.data / "pending.json").read_text())
            self.assertEqual(pending["operation"], "install")
            if phase == "pre":
                self.app("base", "appimage 1")
            else:
                self.app("base", "appimage 2")
            if terminate == signal.SIGKILL:
                os.killpg(proc.pid, terminate)
                os.killpg(int(marker.read_text()), terminate)
            else:
                proc.send_signal(terminate)
            stopped = True
        finally:
            if proc.poll() is None and not stopped:
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                proc.communicate(timeout=5)
                self.fail("Profile command survived interruption")
            with (ROOT / "commands.log").open("a") as log:
                log.write(
                    f"\n{terminate.name} at {phase}-switch barrier:\n" + stdout + stderr
                )
        self.assertNotEqual(proc.returncode, 0, stdout + stderr)
        self.cli("list")
        info = self.info()
        expected = pending["previous"] if phase == "pre" else pending["record"]
        self.assertEqual(info["installed"], expected)
        self.assertEqual(info["locked"], expected)
        self.assertFalse((self.data / "pending.json").exists())
        self.app("base", "appimage " + ("1" if phase == "pre" else "2"))
        for link in (
            self.data / "bin/base",
            self.root / "data/applications/obtain-base.desktop",
        ):
            self.assertTrue(link.is_symlink() and link.exists(), link)

    def test_39_sigkill_before_and_after_profile_switch(self):
        self.add()
        control(versions={"base": 2})
        self.interrupted_install("pre", signal.SIGKILL)
        self.interrupted_install("post", signal.SIGKILL)

    def test_48_sigterm_before_and_after_profile_switch(self):
        self.add()
        control(versions={"base": 2})
        self.interrupted_install("pre", signal.SIGTERM)
        self.interrupted_install("post", signal.SIGTERM)

    def test_49_parent_sigkill_keeps_child_switch_exclusive(self):
        self.add()
        old = self.info()
        control(versions={"base": 2})
        real_nix_env = shutil.which("nix-env")
        self.assertIsNotNone(real_nix_env)
        shim = self.root / "orphan-shim"
        shim.mkdir()
        marker = shim / "ready"
        release = shim / "release"
        wrapper = shim / "nix-env"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys, time\n"
            f"real = {real_nix_env!r}\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"release = pathlib.Path({str(release)!r})\n"
            "if '--set' not in sys.argv:\n"
            "    os.execv(real, [real, *sys.argv[1:]])\n"
            "marker.write_text(str(os.getpid()))\n"
            "while not release.exists():\n"
            "    time.sleep(0.01)\n"
            "os.execv(real, [real, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        env = dict(self.env, PATH=str(shim) + os.pathsep + self.env["PATH"])
        proc = subprocess.Popen(
            [sys.executable, self.packaged_script(), "update", "base"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        try:
            while (
                not marker.exists()
                and proc.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "Did not reach child pre-switch barrier")
            pending = json.loads((self.data / "pending.json").read_text())
            self.assertEqual(pending["previous"], old["installed"])
            os.kill(proc.pid, signal.SIGKILL)
            self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL)
            self.assertIn("Another Obtain command", self.cli("list", fail=True))
            self.assertTrue((self.data / "pending.json").exists())
            self.app("base", "appimage 1")
        finally:
            release.touch()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                proc.communicate(timeout=5)
                self.fail("Surviving profile command did not finish after release")
            with (ROOT / "commands.log").open("a") as log:
                log.write("\nSIGKILL parent before child switch:\n" + stdout + stderr)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["obtain", "list"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                break
            self.assertIn("Another Obtain command", result.stderr)
            time.sleep(0.05)
        else:
            self.fail("Child never released the store lock after switching")
        self.assertIn("base", result.stdout)
        info = self.info()
        self.assertEqual(info["installed"], pending["record"])
        self.assertEqual(info["locked"], pending["record"])
        self.assertFalse((self.data / "pending.json").exists())
        self.app("base", "appimage 2")

    def test_41_unsupported_backend_options_fail_before_network_access(self):
        before = len(self.requests())
        for kind in ("deb", "rpm", "tar", "zip", "docker", "source"):
            self.assertIn(
                "invalid choice",
                self.cli("add", URL + "unsupported", "--type", kind, fail=True),
            )
        self.assertEqual(before, len(self.requests()))

    def test_42_valid_hash_does_not_make_a_broken_appimage_installable(self):
        self.add()
        before = self.snapshot()
        for repo in (
            "invalid-image",
            "truncated-image",
            "missing-apprun",
            "nonexec-apprun",
            "directory-apprun",
        ):
            with self.subTest(repo=repo):
                output = self.cli("add", URL + repo, "--name", "badbundle", fail=True)
                if repo in ("missing-apprun", "nonexec-apprun", "directory-apprun"):
                    self.assertIn("AppImage has no executable AppRun", output)
                self.assertEqual(before, self.snapshot())
                self.assertFalse((self.data / "bin" / "badbundle").exists())
                self.app("base", "appimage 1")

    def test_43_archive_zip_and_binary_lifecycle(self):
        for repo in ("archive", "zip", "binary"):
            with self.subTest(repo=repo):
                args = (
                    ["--type", "archive", "--strip-components", "1"]
                    if repo == "zip"
                    else []
                )
                self.cli("add", URL + repo, *args)
                self.assertRegex(
                    run([str(self.data / "bin" / repo)], self.env),
                    r"^payload 1 zlib \d",
                )
                # A helper fetched by the application after installation was
                # never patched by Obtain. It must inherit the app's loader
                # environment without requiring a global NixOS change.
                helper = self.root / "downloaded-helper"
                run(
                    [
                        "curl",
                        "--fail",
                        "--output",
                        str(helper),
                        URL + "binary/releases/download/v1/app-linux-x86_64",
                    ],
                    self.env,
                )
                helper.chmod(0o755)
                self.assertIn(
                    "NixOS cannot run dynamically linked executables",
                    run([str(helper)], self.env, expected=1),
                )
                self.assertRegex(
                    run(
                        [str(self.data / "bin" / repo)],
                        dict(self.env, OBTAIN_RAW_HELPER=str(helper)),
                    ),
                    r"^payload 1 zlib \d",
                )
                report = json.loads(self.cli("doctor", repo, "--json", "--launch-test"))
                self.assertFalse(any(c["status"] == "failed" for c in report["checks"]))
                control(versions={repo: 2})
                self.cli("update", repo)
                self.assertRegex(
                    run([str(self.data / "bin" / repo)], self.env),
                    r"^payload 2 zlib \d",
                )
                self.cli("rollback", repo)
                self.assertRegex(
                    run([str(self.data / "bin" / repo)], self.env),
                    r"^payload 1 zlib \d",
                )
                self.cli("remove", repo)
                self.assertFalse((self.data / "bin" / repo).exists())
                control()

    def test_44_malformed_archives_preserve_installed_app(self):
        self.add()
        before = self.snapshot()
        for repo in ("traversal", "link", "script", "foreign-payload"):
            with self.subTest(repo=repo):
                output = self.cli(
                    "add",
                    URL + repo,
                    "--type",
                    "archive",
                    "--program",
                    "bundle/bin/app",
                    "--name",
                    "badpayload",
                    fail=True,
                )
                self.assertIn("Obtain payload rejected", output)
                self.assertEqual(before, self.snapshot())
                self.assertFalse((self.data / "bin/badpayload").exists())
                self.app("base", "appimage 1")

    def test_45_batch_rate_limit_resume_skips_completed_apps(self):
        self.add("base", "a")
        self.add("image", "b")
        self.add("archive", "c")
        control(modes={"image": "rate-limit"})
        output = self.cli("check", fail=True)
        self.assertIn("in 60 seconds", output)
        self.assertEqual(
            json.loads((self.data / "check-batch.json").read_text())["unfinished"],
            ["b", "c"],
        )
        control()
        output = self.cli("check", "--retry-failed")
        self.assertNotIn("a:", output)
        self.assertEqual(
            json.loads((self.data / "check-batch.json").read_text())["unfinished"], []
        )

    def test_46_doctor_broken_launcher_and_archive_options(self):
        self.add()
        launcher = self.data / "bin/base"
        launcher.unlink()
        output = self.cli("doctor", "base", fail=True)
        self.assertIn("FAILED: launcher", output)
        before = len(self.requests())
        for args in (
            ("--type", "archive", "--program", "../app"),
            ("--type", "binary", "--program", "app"),
        ):
            self.cli("add", URL + "archive", *args, fail=True)
        self.assertEqual(before, len(self.requests()))

    def test_47_flake_only_repository_is_out_of_scope(self):
        self.cli("add", URL + "native", fail=True)
        self.assertIn("No apps tracked", self.cli("list"))


class JsonResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        failures = [
            (str(t), text)
            for t, text in self.failures + self.errors
            if str(t).startswith(str(test).split(" ")[0])
        ]
        self.rows.append(
            {
                "scenario": test.id(),
                "status": "failed" if failures else "passed",
                "seconds": round(time.monotonic() - self.started, 3),
                "failures": failures,
            }
        )
        super().stopTest(test)


def reboot(verify):
    env = dict(BASE_ENV)
    if not verify:
        control()
        run(
            [
                "obtain",
                "add",
                URL + "base",
                "--name",
                "persistent",
            ],
            env,
        )
        control(versions={"base": 2})
        run(["obtain", "update", "persistent"], env)
        write_json(
            ROOT / "reboot-before.json",
            json.loads(run(["obtain", "info", "persistent"], env)),
        )
    else:
        before = json.loads((ROOT / "reboot-before.json").read_text())
        after = json.loads(run(["obtain", "info", "persistent"], env))
        assert before == after, "State changed across reboot"
        exe = "/home/alice/.local/share/obtain/bin/persistent"
        assert run([exe], env).strip() == "appimage 2"
        run(["obtain", "rollback", "persistent"], env)
        assert run([exe], env).strip() == "appimage 1"
        run(["obtain", "remove", "persistent"], env)
        write_json(
            ROOT / "reboot.json",
            {
                "status": "passed",
                "checks": ["state", "executable", "rollback-history"],
            },
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        reboot(sys.argv[1] == "--verify-reboot")
    else:
        cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(Scenarios))
        # Exercise the newly added backends first for faster regression feedback.
        cases.sort(
            key=lambda case: (int(case._testMethodName.split("_")[1]) < 43, case.id())
        )
        result = unittest.TextTestRunner(verbosity=2, resultclass=JsonResult).run(
            unittest.TestSuite(cases)
        )
        write_json(
            ROOT / "report.json",
            {
                "passed": sum(r["status"] == "passed" for r in result.rows),
                "total": result.testsRun,
                "successful": result.wasSuccessful(),
                "scenarios": result.rows,
            },
        )
        sys.exit(0 if result.wasSuccessful() else 1)
