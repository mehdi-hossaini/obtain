#!/usr/bin/env bash
# Verify only this suite's downloaded AppImage outputs on the disposable VM disk.
# An interrupted QEMU process can leave a registered file with incomplete data.
set -euo pipefail
# Enumerate captured locks too: a registered output may be completely missing.
python - <<'PYTHON' > /tmp/obtain-test-asset-paths
import base64
import json
import subprocess
from pathlib import Path
for row in json.loads(Path("/home/alice/live-results/report.json").read_text())["results"]:
    if row["type"] != "appimage" or not row.get("lock"):
        continue
    digest = base64.b64decode(row["lock"]["hash"].removeprefix("sha256-")).hex()
    print(subprocess.check_output([
        "nix-store", "--print-fixed-path", "sha256", digest,
        row["name"] + ".AppImage"
    ], text=True).strip())
PYTHON
while IFS= read -r asset; do
  if test -f "$asset"; then sha256sum "$asset"; fi
  if ! test -f "$asset" || ! nix-store --verify-path "$asset"; then
    echo "Repairing interrupted test download: $asset"
    nix-store --repair-path "$asset" || python /tmp/repair-asset.py "$asset"
    nix-store --verify-path "$asset"
    sha256sum "$asset"
  fi
done < /tmp/obtain-test-asset-paths
