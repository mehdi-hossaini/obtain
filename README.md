# Obtain

Install and maintain public GitHub release applications: AppImages, Linux
executable archives, and standalone binaries. Nix gives each application its
own profile, so updates and rollback affect that app alone.

Obtain supports **x86_64-linux**. It manages release files; use Nix directly
for native Nix flake packages. There is no background service or update schedule.

## Install and use

Requires Nix on PATH with `nix-command` and `flakes` enabled. If necessary, prefix
the Nix commands below with
`nix --extra-experimental-features 'nix-command flakes'`.

```sh
nix run github:mehdi-hossaini/obtain -- --help
nix profile add github:mehdi-hossaini/obtain
export PATH="${XDG_DATA_HOME:-$HOME/.local/share}/obtain/bin:$PATH"

obtain inspect https://github.com/openai/codex
obtain add https://github.com/openai/codex
obtain list
```

For a temporary session, use `nix shell github:mehdi-hossaini/obtain` instead of
installing the CLI. Obtain prefers AppImages, then supported executable archives
or binaries. Ambiguous choices prompt in a terminal and fail explicitly in
scripts. Downloads are hashed and locked before installation.

| Command | Purpose |
| --- | --- |
| `inspect URL` | List supported release files without installation or tracked-state access |
| `add URL` | Select, lock, and install a release; `--track-only` postpones installation |
| `list`, `info NAME` | Show tracked apps, installed versions, and exact locks |
| `check [NAME]` | Query upstream metadata without building or installing |
| `update [NAME]` | Update one app or all unpinned apps |
| `install NAME` | Install the saved lock |
| `pin NAME`, `unpin NAME` | Control whether an app participates in updates |
| `rollback NAME` | Restore the previous retained generation and pin it |
| `doctor NAME` | Diagnose the installation; `--json` provides a structured report |
| `remove NAME` | Remove tracking and managed launchers; retain app data and old generations |

Use `obtain COMMAND --help` for flags. The [usage reference](docs/usage.md)
explains selection overrides, archive limits, diagnostics, batch retry,
Home Manager integration, XDG paths, and migration from earlier flake entries.

Applications run with your user permissions. Compatibility wrappers are not a
security sandbox. Running installed applications requires working user
namespaces and bubblewrap; some applications need a dedicated dependency recipe.
Rollback restores the executable and lock, not mutable application data.

## Develop and verify

The checked-in lockfile pins the development tools. Python 3.10+ is required
when running source directly; the development shell supplies Python 3.13.

```sh
git clone https://github.com/mehdi-hossaini/obtain.git
cd obtain
nix develop --no-update-lock-file
just run --help
just check
just check-unit
just build
./result/bin/obtain --help
```

`just check` runs lint, formatting, editor analysis, workflow validation, and
the unit tests. `just check-unit` runs the packaged Nix test suite.
`just build` includes new source files and refuses implicit lockfile updates.
Use `just fmt` to format, `just test` for unit tests alone, and `just --list`
to see the recipes. Quoted CLI arguments are preserved: for example,
`just run add URL --asset '*.AppImage'`.

The shell provides Python, Ruff, Pyright, debugpy, nixd, nixfmt, actionlint,
ShellCheck, Nix, Git, and just. Launch your editor from the shell; use
`direnv allow` for automatic entry with nix-direnv. `just debug --help` waits
for a DAP client at `127.0.0.1:5678`.

GitHub Actions runs the same checks, packaged tests, build, and CLI smoke checks
on pushes and pull requests. Actions are pinned to commits and checkout
credentials are not retained. A manual workflow run can also enable the
offline VM check.

`just check-vm` requires an x86_64 Linux builder with KVM. It exercises real
profile switching, interruption recovery, and reboot persistence; see
the [VM evaluation guide](tests/vm/README.md). Networked compatibility experiments
are separate and opt-in: [live evaluation guide](tests/live/README.md).
[ENGINEERING.md](ENGINEERING.md) documents invariants and contribution boundaries;
[VALIDATION.md](VALIDATION.md) explains verification coverage and its limits.

## License

This repository is intentionally unlicensed.
