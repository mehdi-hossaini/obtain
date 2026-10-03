# NixOS VM CLI evaluations

Run from the Obtain repository on x86_64 Linux with working `/dev/kvm`:

```sh
nix build path:.#checks.x86_64-linux.unit --no-link -L
nix build path:.#checks.x86_64-linux.vm --out-link result-vm -L
cat result-vm/report.json
cat result-vm/reboot.json
```

The VM uses 2 virtual CPUs, 2 GiB RAM, and a 16 GiB sparse disk. Its writable
Nix store preserves installed builds across the reboot test. The guest setup
does not activate anything on the host.

## What runs

The packaged CLI runs as an ordinary user in a booted NixOS VM. The real Nix
daemon builds and switches per-app profiles. Two small AppImage release fixtures
exercise extraction, launchers, updates, and rollback. Archive, ZIP, and
standalone ELF fixtures exercise payload installation and dynamic loader repair.
The real Home Manager module installs Obtain for the reboot scenario.

A guest-only HTTPS service supplies GitHub release metadata and assets, including
version changes, ETags, malformed responses, and download failures. The VM trusts
its test certificate only inside the guest. No real GitHub token or private key
is used. Crash tests pause the real `nix-env` profile switch with a PATH shim;
they run the packaged CLI source and leave the CLI and Nix implementation intact.

## Scenario matrix

| Group | Evaluations |
| --- | --- |
| CLI and selection | Help, empty state, argument validation, inspection, ambiguous assets, architecture filtering, prereleases, unsupported releases |
| Release lifecycle | Install, launch, desktop entry, check, pin, update, rollback, remove, re-add, track-only install |
| Payload lifecycle | tar.gz, ZIP, standalone ELF, bundled libraries, inherited loader environment, doctor, update, rollback, removal; bundled desktop metadata and icons; shared-library graphics loading and bundled SONAME precedence in direct/FHS modes |
| Runtime lifecycle | Direct executable launch, smaller closure, refresh to FHS without changing the release, rollback of runtime and policy |
| Integrity | SHA-256 mismatch, malformed AppImages, hostile archives, foreign ELF, malformed API records |
| Network | HTTP 503, rate limit, 404, redirect, malformed JSON, ETag/304 cache and corrupt-cache recovery |
| State and concurrency | Duplicate tracking, launcher collision, malformed state, file locking, interrupted remove |
| Recovery | Journal replay before/after profile switch, invalid journal retention, read-only and full filesystems |
| Signals | SIGINT during metadata fetch, SIGKILL and SIGTERM around profile switches, child-held lock after parent death |
| Batch | One failed repository does not block another; rate-limit retry skips completed apps |
| Scope boundary | Flake-only repository is rejected; unsupported backend names fail before network access |
| Reboot | Saved state, executable, and rollback generations survive guest reboot |

The 40 Python scenario groups have separate XDG state. The reboot case uses the
Home Manager user's default paths. Signal tests cover specific durable
boundaries; they do not cover every possible power-loss timing. The disk-full
tests exhaust a 96 KiB guest tmpfs.

## Results

A successful run contains `report.json`, `reboot.json`, `commands.log`, and
`requests.jsonl`. A failure makes the Nix build fail; use `nix log` and
`--keep-failed` to inspect the guest evidence.

The fast unit suite covers additional selectors and legacy flake state
validation. A passing VM run does not establish compatibility with every
upstream app, graphical rendering, ARM, live GitHub outages, or arbitrary
physical power loss.
