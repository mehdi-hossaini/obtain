"""Build a desktop launcher from trusted extraction output, without running it."""

import configparser
import os
from pathlib import Path
import re
import shutil
import sys

MAX_DESKTOP_BYTES = 64 * 1024
MAX_ICON_BYTES = 4 * 1024 * 1024
MAX_FILES = 100_000
RESERVED = "\t\n\r'\\><~|&;$*?#()`"


def _parse_exec(value):
    """Parse Exec and map the first argument boundary back to its encoded value."""
    # Desktop values are unescaped before command-line quoting is processed.
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    decoded = []
    offsets = [0]
    position = 0
    while position < len(value):
        char = value[position]
        position += 1
        if char == "\\":
            if position >= len(value) or value[position] not in escapes:
                raise ValueError("Unsupported Exec escape")
            char = escapes[value[position]]
            position += 1
        decoded.append(char)
        offsets.append(position)
    decoded = "".join(decoded)
    tokens = []
    position = 0
    first_end = 0
    file_codes = 0
    while position < len(decoded):
        if decoded[position] == " ":
            position += 1
            continue
        quoted = decoded[position] == '"'
        if quoted:
            position += 1
        token = ""
        closed = not quoted
        while position < len(decoded):
            char = decoded[position]
            if quoted and char == '"':
                position += 1
                closed = True
                break
            if not quoted and char == " ":
                break
            if quoted and char == "\\":
                position += 1
                if position >= len(decoded) or decoded[position] not in '"`$\\':
                    raise ValueError("Invalid quoted Exec escape")
                token += decoded[position]
            elif (not quoted and (char in RESERVED or char == '"')) or (
                quoted and char in "$`"
            ):
                raise ValueError("Invalid Exec quoting")
            else:
                token += char
            position += 1
        if not closed or (position < len(decoded) and decoded[position] != " "):
            raise ValueError("Unterminated or partial Exec quote")
        codes = []
        offset = 0
        while offset < len(token):
            if token[offset] == "%":
                if offset + 1 == len(token) or token[offset + 1] not in "%fFuUick":
                    raise ValueError("Unsupported Exec field code")
                codes.append(token[offset + 1])
                offset += 1
            offset += 1
        if quoted and any(code != "%" for code in codes):
            raise ValueError("Quoted Exec field code")
        if any(code in "FUi" for code in codes) and token not in ("%F", "%U", "%i"):
            raise ValueError("Exec field code must be a separate argument")
        file_codes += sum(code in "fFuU" for code in codes)
        tokens.append(token)
        if len(tokens) == 1:
            first_end = offsets[position]
    if (
        not tokens
        or not tokens[0]
        or "=" in tokens[0]
        or "%" in tokens[0]
        or file_codes > 1
    ):
        raise ValueError("Invalid desktop command")
    return tokens, first_end


def parse_exec(value):
    """Decode and validate Exec arguments, retaining field codes including %%.

    Field-code expansion belongs to the launcher; this parser only removes
    desktop value escapes and command-line quotes.
    """
    return _parse_exec(value)[0]


def exec_suffix(value):
    """Validate desktop Exec quoting and retain its original encoded arguments."""
    tokens, first_end = _parse_exec(value)
    if Path(tokens[0]).name in ("env", "sh", "bash", "python", "python3"):
        raise ValueError(
            "Desktop command uses an unsupported interpreter or environment wrapper"
        )
    return value[first_end:]


def inside(path, root):
    resolved = path.resolve()
    return resolved.is_relative_to(root.resolve()) and resolved.is_file()


def bundle_files(root):
    files = []
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = sorted(
            d for d in subdirs if not (Path(directory) / d).is_symlink()
        )
        for name in sorted(names):
            files.append(Path(directory) / name)
            if len(files) > MAX_FILES:
                raise ValueError("Desktop discovery exceeds the file limit")
    return files


def read_entry(path, root):
    if not inside(path, root):
        raise ValueError("Desktop file escapes the bundle")
    with path.open("rb") as stream:
        data = stream.read(MAX_DESKTOP_BYTES + 1)
    if len(data) > MAX_DESKTOP_BYTES:
        raise ValueError("Desktop entry exceeds the size limit")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(data.decode("utf-8"))
    entry = dict(parser["Desktop Entry"])
    if entry.get("Type") != "Application" or not entry.get("Name"):
        raise ValueError("Not an application desktop entry")
    if any(any(ord(char) < 32 for char in value) for value in entry.values()):
        raise ValueError(
            "Desktop entry contains control characters or multiline values"
        )
    return entry


def exec_applies(command, root, program):
    """Match upstream Exec to the selected executable, allowing installed paths."""
    command = Path(command)
    selected = root / program
    if command.name != Path(program).name:
        return False
    # A relative path that exists in the bundle identifies a concrete program,
    # rather than an upstream installation location such as /opt/vendor/app.
    if not command.is_absolute() and len(command.parts) > 1:
        bundled = root / command
        if bundled.exists():
            return inside(bundled, root) and bundled.resolve() == selected.resolve()
    return True


def install(root, output, name, executable, kind, program):
    root, output = Path(root), Path(output)
    files = bundle_files(root)
    entries = [path for path in files if path.suffix == ".desktop"]
    if kind == "appimage":
        # AppDir's single root desktop entry describes the complete image;
        # AppRun may wrap a differently named executable. Nested entries can
        # describe helpers and cannot stand in for the declared primary entry.
        entries = [path for path in entries if path.parent == root]
    else:
        identities = {name.lower(), Path(program).name.lower()}
        matching = [path for path in entries if path.stem.lower() in identities]
        entries = matching or entries
    entry = {}
    suffix = ""
    if len(entries) == 1:
        try:
            entry = read_entry(entries[0], root)
            suffix = exec_suffix(entry.get("Exec", ""))
            if kind != "appimage" and not exec_applies(
                parse_exec(entry["Exec"])[0], root, program
            ):
                raise ValueError("Desktop command does not match the selected program")
        except (ValueError, KeyError, configparser.Error, OSError) as error:
            print(f"obtain: desktop metadata ignored: {error}", file=sys.stderr)
            entry = {}
            suffix = ""
    elif entries:
        print(
            "obtain: multiple desktop entries; using a generic launcher",
            file=sys.stderr,
        )
    values = {
        "Type": "Application",
        "Name": name,
        "Comment": "Managed by Obtain",
        "Icon": "application-x-executable",
        "Categories": "Utility;",
        "Terminal": "false" if entry or kind == "appimage" else "true",
    }
    for key, value in entry.items():
        if re.fullmatch(
            r"(?:Name|Comment|Keywords)(?:\[[A-Za-z0-9_.@-]+\])?", key
        ) or key in (
            "Categories",
            "MimeType",
            "StartupWMClass",
        ):
            values[key] = value
    if entry.get("Terminal") in ("true", "false"):
        values["Terminal"] = entry["Terminal"]
    icon = entry.get("Icon", "")
    if icon and not Path(icon).is_absolute() and ".." not in Path(icon).parts:
        icons = [
            path
            for path in files
            if (path.stem == icon or path.relative_to(root).as_posix() == icon)
            and path.suffix.lower() in (".png", ".svg", ".xpm")
            and inside(path, root)
            and path.stat().st_size <= MAX_ICON_BYTES
        ]
        if icons:
            # Prefer the largest available icon; path ordering breaks ties.
            selected = max(icons, key=lambda path: (path.stat().st_size, str(path)))
            target = output / "share/icons" / f"obtain-{name}{selected.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(selected, target)
            values["Icon"] = str(target)
    # These paths are supplied by Nix, never by upstream metadata.
    values["Exec"] = executable + suffix
    values["TryExec"] = executable
    values["DBusActivatable"] = "false"
    destination = output / "share/applications" / f"obtain-{name}.desktop"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "[Desktop Entry]\n"
        + "".join(f"{key}={value}\n" for key, value in values.items())
    )


if __name__ == "__main__":
    install(*sys.argv[1:])
