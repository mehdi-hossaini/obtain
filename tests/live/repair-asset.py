"""Repair one suite-created guest store file from its recorded upstream SHA-256."""

import base64
import hashlib
import json
from pathlib import Path
import re
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

asset = Path(sys.argv[1])
assert asset.parent == Path("/nix/store")
match = re.fullmatch(r"[a-z0-9]{32}-(repo-[0-9]{3})\.AppImage", asset.name)
assert match, "Not a suite-created AppImage store file"
row = next(
    r
    for r in json.loads(Path("/home/alice/live-results/report.json").read_text())[
        "results"
    ]
    if r.get("name") == match[1]
)
lock = row["lock"]
expected = base64.b64decode(lock["hash"].removeprefix("sha256-")).hex()
url = lock["url"]
assert url.startswith(
    "https://github.com/" + lock["repository"] + "/releases/download/"
)
name = match[1] + ".AppImage"
target = subprocess.check_output(
    ["nix-store", "--print-fixed-path", "sha256", expected, name], text=True
).strip()
assert target == str(asset), "Recorded digest does not identify this store path"
with tempfile.TemporaryDirectory(prefix="obtain-repair-") as tmp:
    file = Path(tmp) / name
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as response, file.open("wb") as output:
        while block := response.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    assert digest.hexdigest() == expected, (
        "Fresh upstream download differs from locked digest"
    )
    print("Verified fresh upstream SHA-256:", expected, flush=True)
    # Import under a different name so the corrupted registered path cannot be reused.
    verified = file.with_name("verified-obtain-repair.AppImage")
    file.rename(verified)
    source = subprocess.check_output(
        ["nix-store", "--add-fixed", "sha256", str(verified)], text=True
    ).strip()
    shell = os.path.realpath(shutil.which("bash"))
    copy = str(Path(os.path.realpath(shutil.which("cp"))).parent.parent)
    expression = (
        "let source = builtins.storePath " + json.dumps(source) + "; "
        "copy = builtins.storePath " + json.dumps(copy) + "; in builtins.derivation { "
        "name = " + json.dumps(name) + "; system = builtins.currentSystem; "
        "builder = builtins.storePath " + json.dumps(shell) + "; "
        'args = [ "-c" "${copy}/bin/cp ${source} $out" ]; '
        'outputHashMode = "flat"; outputHashAlgo = "sha256"; '
        "outputHash = " + json.dumps(expected) + "; }"
    )
    restored = subprocess.check_output(
        [
            "nix",
            "build",
            "--impure",
            "--repair",
            "--no-link",
            "--print-out-paths",
            "--expr",
            expression,
        ],
        text=True,
    ).strip()
    assert restored == target
