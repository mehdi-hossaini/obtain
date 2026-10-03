# Obtain usage reference

See the [README](../README.md) for installation and development commands.

## Discover packages

Start with just the repository URL:

```sh
obtain add https://github.com/openai/codex
```

Obtain prefers files named for the repository over companion tools, then an
AppImage when available, otherwise a supported archive or standalone binary.
Within that format selection it prefers a matching `APP-package-…` runtime
bundle over its bare executable archive. When several files remain plausible, it offers a numbered
choice in a terminal. Archives are checked for Linux executables automatically;
you only choose a program when its identity is ambiguous. Downloaded programs are
not executed during discovery.

When a repository has no supported Linux release, Obtain reports that result.
Use Nix directly for packages published only as flakes. Network errors and
invalid downloads are reported without switching install types.

The selected type and filename selection rule are saved for updates. Automatically
detected archive programs are rediscovered when the release changes, so a new
bundle layout can be installed. Explicit `--program` paths remain fixed.
When GitHub reports that a repository has moved, `add` resolves its current
public name through GitHub's repository ID. New locks use that canonical name.
Existing tracked apps are not silently migrated.
Automatic file selection follows versioned filenames. A file chosen from a menu
saves its variant with the release's version removed when recognizable; platform,
architecture, runtime, and format markers remain fixed. No match or multiple
matches stop an update. Tags without a recognizable numeric version keep an
exact filename. Scripts never prompt: an ambiguous
selection fails with the available choices and an explicit flag to use.
`--type`, `--asset`, and `--program` are available as overrides.
`--program` or `--strip-components` without a type implies an archive.

To explore without installing:

```sh
obtain inspect https://github.com/ilysenko/codex-desktop-linux
```

Inspection lists AppImages and Linux archive/binary candidates from the latest
stable GitHub release. It fetches release metadata without building or
installing the app, reading tracked installation state, or acquiring its lock.
GitHub and network errors are reported rather than being
treated as evidence that an application is unsupported.

`inspect --json` reports eligible filenames and the automatic selection using
the same selection rules as `add`. `list --json` reports each source, locked
manifest, and installed manifest. `check --json` reports each app's update status,
pin policy, versions, and any errors, with unfinished names retained for retry.
These commands write a single JSON document to stdout; progress and diagnostics
go to stderr. Failed checks still exit nonzero and include partial results.

## Nix flakes and earlier Obtain installs

Obtain manages release files. Install an upstream Nix flake with Nix itself,
for example:

```sh
nix --extra-experimental-features 'nix-command flakes' profile add github:OWNER/REPO#PACKAGE
```

You can also add it to your NixOS or Home Manager configuration.

Earlier Obtain versions could track native flakes. Existing flake records remain
readable with `obtain list` and `obtain info NAME`; `obtain remove NAME` removes
their Obtain launcher and tracking record while retaining old Nix generations
and application data. Other commands report guidance to move those records to
Nix. A flake record does not stop a batch from processing
other release apps, but `obtain check` and `obtain update` exit nonzero while
legacy flake entries remain. To migrate, save the source and pinned revision
shown by `obtain info NAME`, install the package through Nix or declare it in
your configuration, and verify the Nix-provided executable. If both copies
share a command name, check the Nix profile or store path directly so PATH does
not select Obtain's launcher. Then run `obtain remove NAME`. Remove any
`programs.obtain.checkInterval` setting from older Home Manager configurations;
the module no longer creates an update timer. Export directories created by
older Obtain versions keep their copied recipe and can still be used directly
by Nix; the current CLI does not create new exports.

## AppImages

AppImages use a generic compatibility wrapper. SquashFS and ELF64 DwarFS images
are extracted with trusted tools without executing the downloaded runtime.
The runtime includes WebKitGTK 4.1, libsoup 3, and ICU for applications that expect
those libraries from the host. Some applications still need specific libraries
or launch flags.

## Linux archives and standalone binaries

Usually `obtain add URL` detects the release file and executable. Use explicit
options to override its choices. These are command templates; replace the repository, filenames, and
program path with the release's actual values:

```sh
obtain add https://github.com/OWNER/REPO --type archive \
  --asset '*linux*x86_64*.tar.gz' --program bundle/bin/tool
obtain add https://github.com/OWNER/REPO --type binary \
  --asset 'tool-linux-amd64' --name tool
```

Archives support `.tar`, `.tar.gz`, `.tgz`, `.tar.xz`, `.txz`, `.tar.bz2`, and `.zip`.
`--program`, when supplied, is the exact relative path inside the archive. For versioned top-level
directories, use `--strip-components 1 --program bin/tool` (or `--program tool`
when the executable is directly below that directory). The stripping policy and
explicit program path are pinned and reused across updates. The whole bundle is retained so adjacent data
files remain available. Standalone binaries get the local command name directly.

Both backends require an x86_64 Linux ELF executable. Nix patches the loader and
shared-library paths using glibc, GCC runtime libraries, zlib, OpenSSL, libxcrypt,
ncurses, GLib, ALSA, and D-Bus. OpenGL, Vulkan, Wayland, X11, keyboard, and font library paths are also added
to bundled executables and helpers for libraries loaded at runtime. The wrapper
provides X11 locale data when `XLOCALEDIR` is unset.
Archives and binaries run in an app-local FHS compatibility environment, so
helpers downloaded by an application after installation can use a standard Linux
loader and these libraries too. This does not enable `nix-ld` or change the host
configuration. As with AppImages, running them requires working user namespaces
and bubblewrap. This environment is for compatibility, not isolation from user data.
Application-managed helper downloads remain outside Obtain's release lock.
For tools that do not need such helpers, `add --runtime direct` uses the patched
executable directly, omitting the FHS environment and its supporting programs
and locale archive. The payload's shared-library paths remain patched. This is
an explicit compatibility choice; Obtain does not infer it from a static main
executable. Direct mode does not need bubblewrap or user namespaces. To change
an existing archive or binary, run `obtain refresh-runtime NAME --runtime direct`
or restore the default with `--runtime fhs`. AppImages require the FHS runtime.
Missing dependencies fail the build instead of producing a known-broken profile.
These generic dependencies do not cover every application. Bundles needing
additional libraries should use an upstream Nix package or a dedicated recipe.
The ELF dependency resolver also searches common desktop libraries including
GTK 3, NSS, WebKitGTK 4.1, Cairo, and Pango. Only referenced desktop libraries
enter the patched bundle's runtime closure; they are not all added to every
archive's FHS environment.
Graphics libraries also provide fallback search paths for libraries loaded with
`dlopen`, in both executables and shared libraries. Bundled libraries take
precedence over these fallbacks.

Extraction supports relative symbolic links that resolve to regular files inside
the extracted bundle, including shared-library aliases. Links are validated
before any are created. It rejects escaping, absolute, dangling, cyclic, or
directory links, hardlinks, sparse files, device files, duplicate entries,
and expanded contents over 4 GiB or 100,000 members, including skipped directory
entries. ZIP directories and TAR extension metadata are limited to 16 MiB before
parser allocation; individual TAR extension headers are limited to 1 MiB.
LZMA/XZ decoder memory and ZIP-LZMA dictionaries are limited to 128 MiB. ZIP members
are decoded incrementally and their actual sizes and checksums are validated.
Member paths are limited to 4,096 encoded bytes and 128 components. Discovery and
extraction share validation and allow at most 100,000 implicit directories.
Executable discovery streams archive contents without extracting resource files
to temporary storage. Link chains are limited to 32 hops. No installer scripts,
package hooks or source builds are executed by these backends. Debian/RPM packages, containers,
Windows/macOS installers and script executables remain unsupported.

## Diagnose an installation

```sh
obtain doctor app
obtain doctor app --json
obtain doctor app --launch-test
```

By default, doctor checks the profile, executable, managed launchers, lock, PATH,
and graphics-session availability without starting the app. Environment warnings
alone do not fail the command. Missing or inconsistent installation files do.

`--launch-test` starts the installed application with no arguments for up to five
seconds, captures runtime and missing-library errors, then kills its process group.
The probe retains only the last 64 KiB of output in memory, and launch errors are
reported in the same text or JSON report. The app can perform its normal startup
actions during this probe. A successful probe does not certify its full
functionality. Logs live in
`$XDG_CACHE_HOME/obtain/doctor-NAME.log` (default `~/.cache/obtain`). Failed external
commands also retain a bounded diagnostic tail in `last-command.log`, with an
explanation for missing dependencies, hash mismatches, full disks, or build
failures. Command progress streams immediately, including output without
a trailing newline; interrupting a command also terminates its child process group.
No automatic system repair is performed.

A corrupt installed manifest is reported as a failed check, including in JSON
output. If a probe log cannot be saved, doctor reports a warning and retains the
startup result and diagnostic output in its report.

## Selection, tracking, and updates

Quote asset globs so your shell does not expand them. Automatic selection prefers
x86_64/amd64/x64 assets and accepts a sole unlabelled AppImage when no explicitly
labelled match exists. Debug, symbol, checksum and known foreign-platform assets
are excluded. Ambiguous `add` commands offer a numbered choice in an interactive
terminal; scripts fail with eligible filenames and never read stdin. Interactive
choices save a version-independent variant where the release version is
recognizable. Labelled glibc and musl versions remain literal; ambiguous numeric
tokens keep the exact filename. Older variant selectors are reconciled with the
locked asset before checking or updating. Earlier saved exact selectors remain exact. If a release changes
the selected variant or uses a different naming convention, set a
glob with `obtain update NAME --asset 'GLOB'`. This replaces the saved variant.
Known incompatible
architecture markers are rejected even with an explicit glob. Draft releases
are excluded. Stable selection uses GitHub's latest-release endpoint; this can
include a `continuous` release if its publisher marks it as stable.
Use `--prereleases` on `add` to opt into published prereleases.

`--name another-name` overrides the local command name. Names contain lowercase
letters, digits and hyphens and must begin with a letter.

To track a release without installing it:

```sh
obtain add https://github.com/owner/app --track-only
obtain install app
```

`--track-only` downloads the selected asset to compute or verify its hash but
does not build or install it. Subsequent `update` operations keep it uninstalled
until `install` is used. `install` uses the locked release, even if a newer
release exists upstream. `list` shows installed and locked versions separately.

App updates and compatibility-runtime updates are separate. To adopt the CLI's
current pinned Nixpkgs and packaging recipe while retaining the locked app
release and download hash, run:

```sh
obtain refresh-runtime app
```

This creates a new installation when the runtime changes, so rollback can restore
the previous runtime. Track-only apps stay uninstalled. An explicit refresh is
allowed for pinned apps and preserves their pin; ordinary updates still skip
them. `refresh-runtime` uses local locked data without querying a newer release.
Failed builds leave the working installation intact.

An update failure leaves the working profile intact. Updates to different apps
are independent; ordinary failures do not prevent checking the others. A GitHub
rate limit pauses the batch and saves unfinished app names. The command returns
a nonzero exit status if any app remains unfinished. Retry using
`obtain check --retry-failed` or `obtain update --retry-failed`; successful apps are
skipped. These checkpoints also survive an interrupted batch. Durable completion
records keep progress writes linear with the batch size. State changes are
journaled per app and compacted into the
configuration snapshot once per batch. An unreconciled state-write failure stops
the batch to preserve its pending operation. Pinned apps must be unpinned before
changing their asset selector.

The CLI reports GitHub's reset time or Retry-After delay when supplied. It does
not automatically wait, reauthenticate, or treat stale cached metadata as a fresh
upstream check. Rollback requires an older retained generation. Removing an app
stops tracking it and removes its managed launchers;
it retains old Nix generations and does not remove the app's own data.

Repeated requests for the same metadata are reused within a command, including
multiple tracked names for the same repository. Each new command revalidates
upstream metadata; the in-memory cache is limited to 32 responses and 2 MB. Metadata-only
checks skip per-app installed-manifest reads; interrupted operations are still
recovered before commands that access tracked state. Inspection runs independently.
Disk caching is optional: unreadable caches and failed cache writes do not
prevent fresh metadata queries or reuse within a command.

Use `GITHUB_TOKEN` if public API rate limits are a problem. Tokens are used only
for API metadata, never stored in configuration or forwarded to asset downloads.
Private repositories and authenticated assets are not supported.

## Home Manager

Add Obtain as an input to your configuration flake:

```nix
{
  inputs.obtain.url = "github:mehdi-hossaini/obtain";
}
```

Pass the input to your Home Manager configuration (for example through
`extraSpecialArgs = { inherit inputs; };`), then import its module:

```nix
{ inputs, ... }:
{
  imports = [ inputs.obtain.homeManagerModules.default ];
  programs.obtain.enable = true;
}
```

The module installs the CLI and adds its app commands to the session PATH.
Re-login after activating it so terminals receive the updated PATH.
Desktop entries point directly to the installed Nix executable, so they do not
depend on PATH. When an extracted bundle has one applicable desktop entry,
Obtain preserves its display names, comments, categories, MIME types, startup
class, terminal setting, and validated launch arguments including file/URL
placeholders. It copies a matching bundled PNG, SVG, or XPM icon. Exec parsing
uses the [desktop entry quoting and field-code rules](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html).
Archive metadata must launch the selected executable. AppImages use their single
root desktop entry, including a root symlink to bundled metadata; nested helper
entries are ignored.
External icon paths, ambiguous entries, interpreter/environment launch wrappers,
and malformed metadata use a generic fallback. No bundled program runs during
launcher discovery. Desktop actions and D-Bus activation are not imported.
Without usable metadata, archives and binaries launch in a terminal; AppImages
use a graphical launcher.

The module installs no service or timer. Run `obtain check` when you want to
query upstream release metadata; `obtain update` installs selected updates.

## State and reproducibility

| Path | Contents |
| --- | --- |
| `$XDG_CONFIG_HOME/obtain/sources.json` | Repositories, release types and selectors, pins, and any earlier flake entries |
| `$XDG_CONFIG_HOME/obtain/lock.json` | Exact release hashes and programs, or earlier flake pins |
| `$XDG_CONFIG_HOME/obtain/recipes/` | Content-hashed packaging and extraction snapshots used by new locks |
| `$XDG_DATA_HOME/obtain/profiles/` | One Nix profile and retained generations per app |
| `$XDG_DATA_HOME/obtain/bin/` | App launchers pointing through those profiles |
| `$XDG_DATA_HOME/obtain/pending.json` | Recovery journal during a state change |
| `$XDG_DATA_HOME/obtain/state-events.jsonl` | Durable per-app changes until a batch snapshot is saved |
| `$XDG_DATA_HOME/applications/obtain-*.desktop` | Desktop launcher links |
| `$XDG_CACHE_HOME/obtain/` | Rebuildable GitHub metadata/ETag cache |

XDG defaults are `~/.config`, `~/.local/share`, and `~/.cache`. Commands serialize
state access. An interrupted operation is reconciled against the actual profile
on the next command that accesses tracked apps. `inspect` does not access this
state. SIGTERM stops and waits for the active external command;
if Obtain is killed abruptly, that command retains the Store lock until it exits,
preventing recovery from racing its profile switch. A successful build gets a
temporary GC root until its profile is switched; retained generations keep
previous installations available.

Builds consume locked data; they do not discover releases during Nix evaluation.
New locks record the recipe's content hash, and installation verifies and uses
the retained snapshot rather than the current CLI's recipe. Keep the recipes
directory with your locks when backing up or moving state. Missing or corrupt
snapshots fail before a build. Older locks retain their existing behavior of
using the current recipe, with a warning; `refresh-runtime` pins a snapshot for
them. Recipe snapshots are shared between apps with identical packaging inputs
and retained for rollback and old locks.

GitHub asset digests are checked when available. New locks expose verification
evidence in `info` and `list --json`: `github-digest` means the download matched
GitHub's published SHA-256 digest; `local-sha256` means Obtain computed and pinned
the download's hash without an upstream digest. `add` and `doctor` also report
that method. SHA-256 pinning detects changed
bytes but is not independent publisher-signature verification. Applications run
with your user permissions; the compatibility wrapper is not a security sandbox.
Rollback restores the executable and lock, not mutable application data.
Upstream deletion can prevent rebuilding a release after its store paths have
been garbage-collected.

If your system resets its root or home at boot, persist Obtain's configuration,
profiles, and launchers. With default XDG paths, these directories are:

```text
.config/obtain
.local/share/obtain
.local/share/applications
```

The cache is rebuildable. Obtain and its Home Manager module do not
change your system's persistence settings.
