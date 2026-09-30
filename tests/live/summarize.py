"""Render an auditable Markdown summary alongside a live VM report."""

import argparse
import collections
import copy
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path, help="Directory containing report.json")
args = parser.parse_args()
report = json.loads((args.directory / "report.json").read_text())
rows = copy.deepcopy(report["results"])
by_index = {r["index"]: r for r in rows}
followup_paths = list(args.directory.glob("reprobes-*.json"))
if not followup_paths and (args.directory / "reprobes.json").exists():
    followup_paths = [args.directory / "reprobes.json"]
followups = sorted(
    (json.loads(p.read_text()) for p in followup_paths), key=lambda p: p["started"]
)
for followup in followups:
    for result in followup["results"]:
        row = by_index[result["index"]]
        row.setdefault("original_status", row["status"])
        row["status"] = result["status"]
        row["installed_verified"] = result.get("installed_verified", False)
        row["followup_started"] = followup["started"]
        row.pop("runtime", None)
        row["stages"].pop("installation", None)
        row["stages"].pop("removal", None)
        if "runtime" in result:
            row["runtime"] = result["runtime"]
        if "installation" in result:
            row["stages"]["installation"] = result["installation"]
            row["evidence_directory"] = str(Path(result["installation"]["log"]).parent)
            removal_log = Path(row["evidence_directory"]) / "remove.log"
            if (args.directory / removal_log).exists():
                row["stages"]["removal"] = {
                    "log": str(removal_log),
                    "verified": result.get("removal_verified", False),
                }
        if "error" in result:
            row["followup_error"] = result["error"]
        else:
            row.pop("followup_error", None)
        row["removal_verified"] = result.get("removal_verified", False)
boundary_path = args.directory / "boundary" / "report.json"
payload_path = args.directory / "payload" / "report.json"
extra_rows = []
by_repository = {r["repository"]: r for r in rows}
for prefix, followup_path in (
    ("boundary", boundary_path),
    ("payload", payload_path),
):
    if not followup_path.exists():
        continue
    cohort_report = json.loads(followup_path.read_text())
    for original in cohort_report["results"]:
        result = copy.deepcopy(original)
        for stage in result.get("stages", {}).values():
            if "log" in stage:
                stage["log"] = prefix + "/" + stage["log"]
        if "log" in result.get("runtime", {}):
            result["runtime"]["log"] = prefix + "/" + result["runtime"]["log"]
        result["evidence_directory"] = prefix + "/" + result["name"]
        if result["repository"] in by_repository:
            row = by_repository[result["repository"]]
            index = row["index"]
            prior = row.get("original_status", row["status"])
            row.update(result)
            row.update(
                index=index,
                original_status=prior,
                followup_started=cohort_report["started"],
            )
        else:
            result["index"] = len(rows) + len(extra_rows) + 1
            extra_rows.append(result)
            by_repository[result["repository"]] = result
for row in rows + extra_rows:
    selection = row.get("stages", {}).get("selection", {})
    if "log" not in selection:
        continue
    path = args.directory / selection["log"]
    text = path.read_text(errors="replace") if path.exists() else ""
    if row["status"] in (
        "asset_selection_required",
        "unsupported_or_missing_release",
    ) and ("Assets: none" in text or "Eligible assets: none" in text):
        row["observed_status"] = row["status"]
        row["status"] = (
            "no_appimage_in_selected_release"
            if row["type"] == "appimage"
            else "no_compatible_asset"
        )
    if row["status"] == "selection_failed" and "redirected this repository" in text:
        row["observed_status"] = row["status"]
        row["status"] = "canonical_url_required"
assessment = {
    "original_report": "report.json",
    "followup_reports": [p.name for p in followup_paths],
    "results": rows,
    "additional_results": extra_rows,
    "additional_counts": dict(collections.Counter(r["status"] for r in extra_rows)),
    "boundary_followup_report": "boundary/report.json"
    if boundary_path.exists()
    else None,
    "payload_followup_report": "payload/report.json" if payload_path.exists() else None,
    "counts": dict(collections.Counter(r["status"] for r in rows)),
}
(args.directory / "assessment.json").write_text(json.dumps(assessment, indent=2) + "\n")
counts = collections.Counter(r["status"] for r in rows)
installed = sum(bool(r.get("installed_verified")) for r in rows)
passed = sum(r["status"] in ("gui_startup_passed", "cli_startup_passed") for r in rows)
lines = [
    f"# Obtain: {report['requested']}-repository live VM evaluation",
    "",
    f"Initial sweep started: {report['started']}. Initial sweep finished: {report.get('finished', 'in progress')}.",
    "",
    f"Recorded **{len(rows)}/{report['requested']} repositories**. "
    f"Verified **{installed} installations** and **{passed} startup probes**.",
    "",
    f"Additional repositories outside the original cohort: **{len(extra_rows)}**; "
    f"verified installations: **{sum(bool(r.get('installed_verified')) for r in extra_rows)}**; "
    f"startup passes: **{sum(r['status'] in ('gui_startup_passed', 'cli_startup_passed') for r in extra_rows)}**.",
    "",
    f"Across the original cohort and additional repositories: **{len(rows) + len(extra_rows)} unique repositories**, "
    f"**{installed + sum(bool(r.get('installed_verified')) for r in extra_rows)} verified installations**, "
    f"**{passed + sum(r['status'] in ('gui_startup_passed', 'cli_startup_passed') for r in extra_rows)} startup passes**.",
    "",
    f"Environment: {report['environment']}. GitHub authentication: {report['authentication']}.",
    "",
    "| Outcome | Repositories |",
    "| --- | ---: |",
    *[
        f"| {status.replace('_', ' ')} | {count} |"
        for status, count in sorted(counts.items())
    ],
    "",
    "The table uses the latest exact-lock follow-up when available. Original command "
    "outcomes remain in `report.json`; follow-ups and category clarification are recorded "
    "in [assessment.json](assessment.json). Reprobes restore captured CLI state and do "
    "not re-run API discovery. Extra repositories are listed after the original "
    "cohort. Boundary retries "
    "repeat normal release selection for Kaneo and ripgrep. Payload follow-ups use "
    "the archive/binary backends; the original observations are preserved.",
    "",
    "A GUI startup pass means a visible window survived the observation period. "
    "It does not certify rendering correctness, login, document operations, hardware, "
    "audio, networking, or long-term stability. Timeouts and rate limits are inconclusive.",
    "",
    "| # | Repository | Type | Resolved version | Install verified | Outcome | Evidence |",
    "| ---: | --- | --- | --- | --- | --- | --- |",
]
for row in rows + extra_rows:
    name = row.get("evidence_directory", row.get("name", f"repo-{row['index']:03d}"))
    stages = row.get("stages", {})
    links = []
    for key, label in (
        ("selection", "select"),
        ("installation", "install"),
        ("removal", "remove"),
    ):
        if key in stages:
            links.append(f"[{label}]({stages[key]['log']})")
    runtime = row.get("runtime", {})
    if "log" in runtime:
        links.append(f"[launch]({runtime['log']})")
    if (args.directory / name / "startup.png").exists():
        links.append(f"[screenshot]({name}/startup.png)")
    repo = row["repository"]
    version = str(row.get("lock", {}).get("version", "—")).replace("|", "\\|")
    lines.append(
        f"| {row['index']} | [{repo}](https://github.com/{repo}) | {row['type']} | "
        f"{version} | {'yes' if row.get('installed_verified') else 'no'} | "
        f"{row['status'].replace('_', ' ')} | {' · '.join(links)} |"
    )
lines += [
    "",
    "Raw observations, immutable locks, timings, and exact command exits: [report.json](report.json).",
    "",
]
(args.directory / "SUMMARY.md").write_text("\n".join(lines))
print(
    f"Recorded {len(rows)}; installed {installed}; startup passed {passed}; {dict(counts)}"
)
