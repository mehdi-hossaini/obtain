"""Reinstall previously locked real assets and repeat launch checks without API discovery.

Run on the preserved live VM disk after a release cohort pass. The original
report stays intact. Restored state is copied from the earlier CLI's own output.
"""

import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import traceback

spec = importlib.util.spec_from_file_location("harness", "/tmp/live-run.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
root = Path(sys.argv[2])
original = json.loads((root / "report.json").read_text())
report = {
    "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "purpose": "Exact-lock reinstall and startup with Mesa software graphics and updated wrapper",
    "cli": os.path.realpath(shutil.which("obtain")),
    "results": [],
}
run_label = "reprobes-" + datetime.datetime.now(datetime.timezone.utc).strftime(
    "%Y%m%d-%H%M%S"
)
only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()
for row in original["results"]:
    if only and row["repository"] not in only:
        continue
    if not row.get("lock"):
        continue
    name = row["name"]
    directory = root / run_label / name
    directory.mkdir(parents=True, exist_ok=True)
    home = Path("/home/alice/live-apps") / name
    env = dict(
        h.BASE,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
    )
    result = {
        "index": row["index"],
        "repository": row["repository"],
        "version": row["lock"]["version"],
        "status": "running",
    }
    report["results"].append(result)
    print(f"Reprobe {row['repository']}", flush=True)
    try:
        # Reconcile any interrupted prior removal before restoring captured state.
        recovery = h.command(["obtain", "list"], env, directory / "recovery.log")
        assert recovery["exit"] == 0, "Existing CLI state must recover before a reprobe"
        state = home / ".config/obtain"
        state.mkdir(parents=True, exist_ok=True)
        prior_info = root / name / "info.log"
        source = None
        if prior_info.exists():
            try:
                text = prior_info.read_text()
                info = json.JSONDecoder().raw_decode(text[text.index("{") :])[0]
                source = info["source"]
                result["source_provenance"] = "original CLI info"
            except (ValueError, KeyError):
                pass
        if source is None and (state / "sources.json").exists():
            source = json.loads((state / "sources.json").read_text())["apps"].get(name)
            if source is not None:
                result["source_provenance"] = "retained CLI state"
        if source is None:
            # A failed initial install may have no info output; a successful
            # later removal empties its state. Reconstruct the exact original
            # add arguments, while retaining the original immutable CLI lock.
            assert row["type"] == "appimage", (
                "Fallback only supports the original AppImage cohort"
            )
            source = {
                "kind": "appimage",
                "repository": row["repository"],
                "asset": row.get("asset"),
                "prereleases": row.get("prereleases", False),
                "pinned": False,
            }
            result["source_provenance"] = "recorded original add arguments"
        for file, record in (("sources.json", source), ("lock.json", row["lock"])):
            (state / file).write_text(json.dumps({"schema": 1, "apps": {name: record}}))
        install = h.command(
            ["obtain", "install", name], env, directory / "install.log", timeout=600
        )
        result["installation"] = install
        if install["exit"]:
            result["status"] = "install_failed"
            # Preserve Nix's actual builder log to diagnose extraction failures.
            import re

            text = (directory / "install.log").read_text(errors="replace")
            matches = re.findall(r"/nix/store/[a-z0-9]+-[^\s']+-extracted.drv", text)
            if matches:
                h.command(["nix", "log", matches[0]], env, directory / "builder.log")
        else:
            data = home / ".local/share"
            profile = data / "obtain/profiles" / name
            installed = json.loads((profile / "share/obtain/manifest.json").read_text())
            assert installed == row["lock"]
            result["installed_verified"] = True
            executable = str(data / "obtain/bin" / name)
            if row["probe"] == "gui":
                result["runtime"] = h.gui_probe(
                    executable, row["arguments"], env, directory
                )
                result["status"] = result["runtime"]["status"]
            else:
                result["runtime"] = h.command(
                    [executable, *row["arguments"]],
                    env,
                    directory / "launch.log",
                    timeout=30,
                )
                result["status"] = (
                    "cli_startup_passed"
                    if result["runtime"]["exit"] == 0
                    else "launch_failed"
                )
            if (
                result["status"] == "launch_failed"
                and row["repository"] == "AppFlowy-IO/AppFlowy"
            ):
                diagnostic = directory / "diagnose-libraries.sh"
                diagnostic.write_text(
                    '#!/bin/sh\ncat "$APPDIR/AppRun"\n'
                    'export LD_LIBRARY_PATH="$APPDIR/lib:$APPDIR/usr/lib:${LD_LIBRARY_PATH:-}"\n'
                    'ldd "$APPDIR/AppFlowy"\n'
                )
                diagnostic.chmod(0o755)
                result["library_diagnostics"] = h.command(
                    [executable],
                    dict(env, APPIMAGE_DEBUG_EXEC=str(diagnostic)),
                    directory / "libraries.log",
                )
            removal = h.command(
                ["obtain", "remove", name], env, directory / "remove.log"
            )
            result["removal_verified"] = (
                removal["exit"] == 0 and not Path(executable).exists()
            )
    except Exception:
        result.update(status="harness_error", error=traceback.format_exc())
    (root / "reprobes.json").write_text(json.dumps(report, indent=2) + "\n")
    (root / (run_label + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(result["status"], flush=True)
report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(root / "reprobes.json").write_text(json.dumps(report, indent=2) + "\n")
(root / (run_label + ".json")).write_text(json.dumps(report, indent=2) + "\n")
# Signal completion even on harness errors; the driver exports the evidence and
# rejects the report instead of waiting for a sentinel that can never arrive.
(root / "reprobes-done").write_text("complete\n")
