# Engineering approach

Obtain applies
[TigerStyle](https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/TIGER_STYLE.md)
in this Python/Nix CLI with safety, performance, and developer experience as
priorities, in that order. A
change should explain its failure mode, resource cost, and recovery behavior
before implementation.

## Boundaries and invariants

| Boundary | Invariant | Verification |
|---|---|---|
| GitHub metadata | HTTPS metadata stays on GitHub; a response is at most 8 MiB before JSON parsing. Oversized or unreadable caches are discarded; cache writes are optional. Truncated HTTP responses fail one batch item and allow the next app to be checked. | `test_adversarial.py`, `test_github_efficiency.py`, `test_review_regressions.py` |
| External commands | Captured output is at most 8 MiB; stdout and stderr are drained together so either pipe can make progress. Failed commands keep bounded diagnostic tails. | `test_process_io.py` |
| Release archive | Discovery and extraction share path, link, member, expanded-size, and implicit-directory validation before executing any bundled program. Decoder memory is bounded; ZIP output sizes and checksums are verified. | `test_payload_links.py`, `test_payload_efficiency.py`, `test_payload_decompression.py`, VM archive scenarios |
| ELF library lookup | Graphics fallback paths cover executable and shared-library `dlopen` calls while preserving bundled library precedence. | VM graphics fallback scenario |
| Locked release | Repository, architecture, hash, and executable must match the saved lock before an install or rollback. | `test_obtain.py`, VM scenarios |
| Packaging inputs | New locks retain content-hashed recipe/helper snapshots; rebuilds verify those inputs. Runtime refresh keeps upstream release identity and pins. | `test_release_updates.py` |
| Desktop integration | Extracted metadata is bounded; launch arguments follow desktop quoting rules, archive entries match the selected program, and AppImages use their primary root entry. Icons stay inside the bundle and discovery executes no downloaded program. | `test_desktop.py` |
| Tracked state | Source and lock names, repositories, and package types must agree after recovery and before installation. | `test_obtain.py`, `test_batch_efficiency.py` |
| Earlier flake records | Existing flake sources and locks remain readable and removable; release app batches continue while flake items report migration guidance. | `test_legacy_flakes.py` |
| Profile switch | The durable journal records intent before `nix-env` changes the profile. Its previous manifest is validated before writing. Recovery reads the actual installed manifest before reconciling state. | `test_batch_efficiency.py`, `test_process_io.py`, `test_review_regressions.py`, VM interruption scenarios |
| Managed launchers | Existing files and links owned by others are never replaced; install, rollback, and removal check ownership before profile changes. | `test_obtain.py`, `test_review_regressions.py`, VM ownership scenarios |
| Inspection | Upstream metadata inspection neither loads nor recovers tracked state and does not acquire the Store lock. | `test_cli.py` |
| Diagnostics | Corrupt installed manifests produce failed checks; optional log failures preserve probe results and JSON reports. | `test_doctor_reliability.py` |

The network response cap is deliberately larger than a GitHub page of 100
assets. Archive limits are in `payload.py`; they account for both declared and
actual extracted bytes. When changing a limit, test the value immediately
below and above it.

## Control flow and cost

`main()` routes stateless inspection; `dispatch()` routes commands that access
tracked state. `add_source()` validates options before metadata
lookup or state changes. Batch setup selects retry targets; `batch_item()`
handles one app; `batch_command()` owns durable progress and error handling.
Keep external commands and state transitions explicit in orchestration and
`Store` methods. Keep selection and validation helpers free of side effects.

Obtain owns public GitHub release files: AppImages, Linux executable archives,
and standalone binaries. Nix handles native flakes directly. The Home Manager
module installs the CLI and PATH entry only; it creates no service or timer.
Maintain a readable removal path for flake state written by older versions so
that release apps remain usable during migration.

The expensive operations are network requests, Nix builds, and disk writes.
GitHub responses are reused within one command and revalidated for the next.
Batch updates append one durable completion per app and compact the full state
once. Keep those costs proportional to the number of apps. The scaling
regression is in `test_batch_efficiency.py`.

Captured external commands return JSON or small metadata, so exceeding the
8 MiB output limit is an error. Commands that stream output to the terminal
use uncaptured stdout. This keeps metadata parsing bounded without buffering
arbitrary subprocess output in memory or temporary storage.

## Verification

Enter `nix develop --no-update-lock-file` and run `just check` for source,
workflow, and shell checks. `just check-unit` tests the packaged source;
`just build` builds the CLI. `just check-vm` exercises real profile switching
and reboot recovery and requires KVM. Live compatibility evaluations remain
opt-in and do not run in ordinary CI.

For a change, include its failure mode, relevant regression coverage, and checks
actually run. Preserve compatibility paths and unrelated work. Keep generated
results under ignored `artifacts/`; use a Git flake reference for VM builds when
results contain sockets that `path:.` cannot copy.

The GitHub workflow reuses these recipes with read-only contents permission,
full commit pins, and no persisted checkout credentials, following
[GitHub's workflow security guidance](https://docs.github.com/en/actions/reference/security/secure-use).
