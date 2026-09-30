# Live GitHub release compatibility evaluation

## Second ten-app challenge cohort

`new-challenge-repositories.json` exercises Arduino IDE, Joplin, ImHex, LMMS,
MeshLab, ONLYOFFICE Desktop Editors, OpenRA, Stellarium, Tiled, and RustDesk.
It uses one-step automatic `obtain add` for every app, local input fixtures for
six, bounded GUI observations, and additional CLI operations where available.
Set `OBTAIN_LIVE_HARD=1`,
`OBTAIN_LIVE_COHORT=/absolute/path/new-challenge-repositories.json`, and a
unique `OBTAIN_LIVE_RUN` label. Keep the VM working directory and results on a
persistent filesystem so interrupted attempts can be inspected and resumed.
Tiled's first window took about 76 seconds in this VM, so its probe allows
165 seconds. An alive GUI process with no observed window by the deadline is
reported as `gui_observation_timeout`; an exited process is `launch_failed`.

## Ten harder applications

`harder-repositories.json` adds Bambu Studio, OrcaSlicer, MuseScore, Shotcut,
Audacity, Godot, VSCodium, Zen, darktable, and OpenSCAD. Use it with
`OBTAIN_LIVE_HARD=1` and `OBTAIN_LIVE_COHORT` as described below.
The fixtures exercise mesh export, score PDF export, timeline/project saving,
audio import, game rendering, editor saving, browser JavaScript, photo import,
and solid geometry rendering. `additional_probes` records bounded CLI operations
after GUI cleanup; `files_base64` supplies binary media fixtures, and
`probe_seconds` controls each application's observation window.
GUI timing and first-run dialogs can require follow-up probes. A missing output
alone does not establish an Obtain defect; inspect the screenshot and raw logs.

## Five demanding applications

`hard-repositories.json` covers AppFlowy (Flutter), FreeCAD (Qt/OpenGL and a saved
CAD document), Zed (a desktop archive with helper executables), Neovim (bundled
syntax runtime and a saved edit), and Helix (a real terminal editing session).
The default cohort exercises automatic release selection. No accounts are used.
Zed's functional probe sets `ZED_ALLOW_EMULATED_GPU=1` only in the test process,
acknowledges trust for its generated local project, and records keyboard actions
that edit and save a file. The installed wrapper does not set this GPU override.
It also checks that the application-downloaded Rust language server remains
running, which catches helper-loader failures missed by an editor-only startup.

Build before starting the VM, and keep its working directory outside the
Git flake source or under an ignored directory such as `artifacts/`. The Git
flake reference excludes ignored runtime artifacts such as VM sockets, which
a `path:.` reference would try to copy into the Nix store.
For example:

```sh
nix build .#live-vm --out-link result-live-vm
mkdir -p "$HOME/obtain-test-state" "$HOME/obtain-test-results"
OBTAIN_LIVE_HARD=1 OBTAIN_LIVE_RUN=baseline \
  XDG_RUNTIME_DIR="$HOME/obtain-test-state" \
  ./result-live-vm/bin/nixos-test-driver --keep-machine-state \
  --output_directory "$HOME/obtain-test-results"
```

Keep this state directory between follow-ups to reuse downloaded dependencies.
Use a new `OBTAIN_LIVE_RUN` label for each attempt so original results survive.
`OBTAIN_LIVE_COHORT=/absolute/path/cohort.json` can supply a follow-up cohort:
set `direct_add: true` to test one-step `obtain add` and `type: "archive"` to
exercise editor archives as well as their default AppImages. Ordinary automatic
cases omit `--type`, `--asset`, and `--program` from the actual command.
A follow-up may contain a subset of the five apps; its requested count is
recorded explicitly rather than being counted as another complete five-app run.
To investigate runtime failures after API throttling, a follow-up can provide
`locked_source` and `locked_record` captured from a previous `obtain info` output.
The harness restores those records with a fresh local name and calls
`obtain install`, keeping asset identity, hashes, and the Nixpkgs pin unchanged.
It labels this as `restore_lock` and skips metadata checks explicitly. It is
installation/runtime evidence, not another successful discovery attempt.

Results include every command's output and exit status, exact asset locks,
manifest/launcher checks, doctor/check results, per-second window observations,
screenshots, edited files, and Helix's timed `terminal.cast` recording. Functional
assertions are recorded separately from visible-window startup. Application
files named by `capture_files` and Obtain's saved state/diagnostic tail are also
retained after failed attempts. Each run has its own continuously updated
`shared-xchg/hard-RUN` directory and an exported `hard-RUN/RUN` directory.

## Release compatibility cohort

This is an opt-in network test, separate from the deterministic VM check. It
attempts the fixed cohort in `repositories.json` through the packaged Obtain CLI
inside a disposable NixOS VM. It includes 90 AppImage candidates and 2 compatibility boundaries
(Kaneo and ripgrep). Failed candidates are retained.
The list draws on upstream repositories and the [AppImage catalog](https://appimage.github.io/feed.json);
catalog membership does not guarantee that the latest release remains compatible.

On an x86_64 Linux host with KVM:

```sh
nix build .#live-vm --out-link result-live-vm -L
mkdir -p /tmp/obtain-live-work /tmp/obtain-live-results
cd /tmp/obtain-live-work
XDG_RUNTIME_DIR=/tmp/obtain-live-work /path/to/obtain/result-live-vm/bin/nixos-test-driver \
  --output_directory /tmp/obtain-live-results
```

Use a fresh working directory for each run. The guest uses 2 CPUs, 3 GiB RAM and a
128 GiB **sparse** disk. Downloads and builds consume real disk space. Apps and
profiles live in the guest; host user profiles and desktop settings are untouched.
The driver stops the VM when finished. Its eight-hour deadline bounds the run.
This run intentionally supplies no GitHub API token. GitHub API throttling is
reported as `rate_limited`; it is not a compatibility verdict or a success.

For each repository the runner:

1. Executes `obtain add URL --name repo-NNN --type TYPE --track-only`.
2. Executes `obtain install repo-NNN` if locking succeeded.
3. Compares the installed manifest with the saved lock, checks the profile,
   executable symlink, absolute desktop target, and `obtain info`.
4. Runs a CLI help/version probe, or starts the desktop target in Xvfb/Openbox
   with software rendering. A GUI pass requires a visible new window for at
   least the last five observations of a 25-second probe. It saves a screenshot
   and the application's output.
5. Removes installed apps through Obtain and verifies launcher removal.

Each repository has isolated HOME and XDG state. The guest shares only immutable
Nix downloads among cases. Limits are 180 seconds for selection/download and 600
seconds for installation. Timeouts are inconclusive, not proof of incompatibility.
Unmodified default selection is tested unless an explicit override appears in the
cohort. Ambiguous assets, prerelease-only projects, redirected repositories and
unlabelled architectures may need user choices; the runner does not silently
change the chosen source or disable an application's sandbox.

`live/report.json` contains per-stage exit codes, timings, resolved releases,
immutable locks, installation checks, launch outcomes, and cleanup checks. The
`live/repo-NNN/` directories contain raw command logs and GUI screenshots. A
completed driver means all 92 cases were recorded, **not** that all succeeded.
A partial report is continuously written under the driver's `shared-xchg/live`
directory, with progress in `progress.log`.

A startup probe does not establish full application functionality. Hardware,
GPU acceleration, audio, authentication, opening/saving documents, external
services and long-running stability need app-specific tests on a real desktop.

## Exact-lock follow-up checks

To repeat installation and launch checks for the already resolved real assets,
reuse the preserved VM disk. The runner restores each app's previously captured
CLI source and lock state and calls `obtain install`. It does not perform new
GitHub API discovery. Original observations remain in `report.json`.

```sh
OBTAIN_LIVE_REPROBE=1 XDG_RUNTIME_DIR=/tmp/obtain-live-work \
  /path/to/obtain/result-live-vm/bin/nixos-test-driver \
  --keep-machine-state --output_directory /tmp/obtain-live-results
```

Optionally set `OBTAIN_REPROBE_ONLY=AppFlowy-IO/AppFlowy,keepassxreboot/keepassxc`
to narrow a follow-up. Each new pass saves a timestamped report and command logs.
The guest verifies its test asset store paths before reprobes and repairs an
incomplete download if an interrupted VM left one behind. This affects only
suite-created AppImage outputs inside the disposable VM.

The current VM enables Mesa; `graphics.txt` records its actual OpenGL renderer.
The initial 2026-09-28 pass lacked Mesa and Audacity failed to initialize OpenGL;
its recorded follow-up distinguishes that environment failure from CLI behavior.
The initial pass also encountered a KeePassXC extraction failure after a test-driver
interruption; follow-up integrity checks retain the diagnosis and any repair evidence.

## Summarizing results

Place optional boundary and archive/binary follow-up reports under `boundary/`
and `payload/` in the collected results directory, then run:

```sh
python tests/live/summarize.py /path/to/collected-results
```

The summary preserves the original outcomes and later evidence separately.
Core dumps are disabled in the live VM to prevent failing GUI apps from filling
the test disk. Command output, exits, and failure reports remain available.

## Non-AppImage boundary retries

The original cohort includes Kaneo and ripgrep to test real repositories that
may provide server code or raw binary archives. To retry those two selections
in fresh isolated state (for example after an API quota reset):

```sh
OBTAIN_LIVE_BOUNDARIES=1 XDG_RUNTIME_DIR=/tmp/obtain-live-work \
  /path/to/obtain/result-live-vm/bin/nixos-test-driver \
  --keep-machine-state --output_directory /tmp/obtain-live-results
```

Results are exported to `boundary-live/boundary-results`. Put them in the
collected report's `boundary/` subdirectory before running `summarize.py`.
These are compatibility checks; clean rejection does not add support for a
server deployment or an arbitrary archive.

## Release archive and binary follow-up

The `payload-repositories.json` cohort exercises ripgrep and fd release archives,
jq's standalone Linux ELF binary, and Kaneo as an unsupported-release boundary.
Run after the controlled VM has stopped:

```sh
nix build .#live-vm --out-link result-live-vm
mkdir -p /tmp/obtain-payload-vm /tmp/obtain-payload-results
OBTAIN_LIVE_PAYLOADS=1 XDG_RUNTIME_DIR=/tmp/obtain-payload-vm \
  result-live-vm/bin/nixos-test-driver --keep-machine-state \
  --output_directory /tmp/obtain-payload-results
```

The harness passes pinned extraction policy (`--strip-components`) and program
paths through the public CLI. It verifies installed manifests, executable and
desktop launchers, actual startup, and removal. The VM requests shutdown on both
normal exit and termination; interrupted cases remain resumable from their report.
No GitHub authentication is supplied.
