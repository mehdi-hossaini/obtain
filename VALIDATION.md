# Validation

Run checks from the pinned development environment:

```sh
nix develop --no-update-lock-file
just check
just check-unit
just build
./result/bin/obtain --help
./result/bin/obtain --version
```

`just check` runs Python lint, formatting, type analysis, workflow and shell
validation, and unit tests. `just check-unit` runs the Python suite in a Nix
build sandbox using its declared source inputs. The build and CLI smoke checks
verify that the installed wrapper can load its bundled resources.

GitHub Actions runs these checks on pushes and pull requests. Its packaged CLI
smoke check uses disposable XDG directories and also checks an empty app list.
See the [workflow](.github/workflows/check.yml) for the exact steps and results
in the repository's Actions tab.

## Offline lifecycle checks

`just check-vm` requires an x86_64 Linux builder with KVM. It tests release
selection, payload handling, real Nix profile switching, rollback, interrupted
transaction recovery, launcher ownership, and reboot persistence using local
fixtures. See the [VM guide](tests/vm/README.md). The workflow runs this check
only when explicitly enabled in a manual run.

## Live compatibility checks

Networked evaluations are opt-in and use disposable VM state. The
[live evaluation guide](tests/live/README.md) describes the reusable cohorts,
commands, and evidence outputs. Keep generated reports, logs, screenshots, and
VM disks under ignored `artifacts/`.

A passing unit or offline VM check does not establish compatibility with every
upstream application. Live findings apply to the specific locked releases and
workflows tested; opening a window does not verify application functionality.
Rollback restores the executable and lock, not mutable application data.
