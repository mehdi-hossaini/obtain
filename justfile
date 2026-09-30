# Run these recipes inside nix develop or after direnv allow.
set positional-arguments

default:
    @just --list

# Run the source CLI; defaults to a read-only help smoke check.
run *args="--help":
    python3 obtain.py "$@"

test:
    python3 -m unittest discover -s tests -q

lint:
    ruff check .
    actionlint .github/workflows/check.yml
    shellcheck tests/live/verify-store.sh

# Check Python import resolution and the editor's analysis configuration.
analyze:
    pyright

format-check:
    ruff format --check .
    nixfmt --check *.nix tests/vm/*.nix tests/live/*.nix
    just --fmt --check --unstable

fmt:
    ruff format .
    nixfmt *.nix tests/vm/*.nix tests/live/*.nix
    just --fmt --unstable

check: lint format-check analyze test

build:
    nix build path:. --no-update-lock-file

check-unit:
    nix build path:.#checks.x86_64-linux.unit --no-update-lock-file --no-link -L

# Requires an x86_64 Linux builder with KVM.
check-vm:
    nix build path:.#checks.x86_64-linux.vm --no-update-lock-file --out-link result-vm -L

# Attach a DAP client to localhost:5678 before the CLI starts.
debug *args="--help":
    python3 -Xfrozen_modules=off -m debugpy --listen 127.0.0.1:5678 --wait-for-client obtain.py "$@"
