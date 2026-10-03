"""Desktop metadata is data: it cannot execute discovery hooks or escape bundles."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import desktop


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "bundle"
        self.root.mkdir()
        self.output = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, kind="archive", program="bin/app", name="app"):
        desktop.install(
            self.root,
            self.output,
            name,
            f"/nix/store/fixture/bin/{name}",
            kind,
            program,
        )
        return (self.output / f"share/applications/obtain-{name}.desktop").read_text()

    def entry(self, extra=""):
        (self.root / "app.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Friendly App\n" + extra
        )

    def test_metadata_icon_and_file_arguments_are_preserved(self):
        self.entry(
            'Exec=app --open "two words" %U\nIcon=app\nTerminal=false\nName[fr]=Application\nMimeType=text/plain;\nStartupWMClass=FriendlyApp\n'
        )
        (self.root / "app.png").write_bytes(b"fixture icon")
        result = self.build()
        for value in (
            "Name=Friendly App",
            "Name[fr]=Application",
            "Terminal=false",
            'Exec=/nix/store/fixture/bin/app --open "two words" %U',
            "MimeType=text/plain;",
            "StartupWMClass=FriendlyApp",
            "DBusActivatable=false",
        ):
            self.assertIn(value, result)
        icon = self.output / "share/icons/obtain-app.png"
        self.assertIn(f"Icon={icon}", result)
        self.assertEqual(icon.read_bytes(), b"fixture icon")

    def test_terminal_default_for_cli_and_appimage(self):
        self.assertIn("Terminal=true", self.build("binary"))
        self.assertIn("Terminal=false", self.build("appimage"))
        self.entry("Exec=app\n")
        self.assertIn("Terminal=false", self.build())

    def test_terminal_application_is_preserved(self):
        self.entry("Exec=app %F\nTerminal=true\n")
        self.assertIn("Terminal=true", self.build())

    def test_ambiguous_or_malformed_entries_use_fallback(self):
        self.entry("Exec=env EVIL=1 app %U\n")
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.build()
        self.assertIn("Name=app\n", result)
        self.assertNotIn("EVIL", result)
        (self.root / "app.desktop").rename(self.root / "one.desktop")
        (self.root / "two.desktop").write_text("invalid")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("Name=app\n", self.build())

    def test_outside_icon_and_desktop_links_are_never_read(self):
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(b"outside")
        (self.root / "app.png").symlink_to(outside)
        self.entry("Exec=app\nIcon=app\n")
        self.assertIn("Icon=application-x-executable", self.build())
        (self.root / "app.desktop").unlink()
        (self.root / "app.desktop").symlink_to(outside)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("Name=app\n", self.build())

    def test_oversized_desktop_metadata_is_bounded(self):
        self.entry("Exec=app\nComment=" + "x" * desktop.MAX_DESKTOP_BYTES)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("Name=app\n", self.build())

    def test_unrelated_exec_does_not_import_metadata_or_arguments(self):
        self.entry(
            "Exec=helper --listen %U\nTerminal=false\nMimeType=text/plain;\nIcon=app\n"
        )
        (self.root / "app.png").write_bytes(b"fixture icon")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = self.build()
        self.assertIn("does not match the selected program", errors.getvalue())
        self.assertIn("Name=app\n", result)
        self.assertIn("Terminal=true", result)
        self.assertIn("Exec=/nix/store/fixture/bin/app\n", result)
        self.assertNotIn("--listen", result)
        self.assertNotIn("MimeType=", result)
        self.assertFalse((self.output / "share/icons").exists())

    def test_selected_program_can_have_a_different_obtain_name(self):
        self.entry("Exec=/opt/vendor/app --open %U\n")
        result = self.build(name="custom")
        self.assertIn("Name=Friendly App", result)
        self.assertIn("Exec=/nix/store/fixture/bin/custom --open %U", result)

    def test_relative_exec_path_does_not_select_another_same_named_program(self):
        (self.root / "bin").mkdir()
        (self.root / "other").mkdir()
        (self.root / "bin/app").write_text("selected")
        (self.root / "other/app").write_text("unrelated")
        self.entry("Exec=other/app --wrong\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotIn("--wrong", self.build())
        self.entry("Exec=bin/app --right\n")
        self.assertIn(" --right", self.build())

    def test_appimage_primary_entry_applies_to_its_apprun_wrapper(self):
        self.entry("Exec=upstream-app --open %U\nTerminal=false\n")
        (self.root / "app.desktop").rename(self.root / "org.vendor.Upstream.desktop")
        nested = self.root / "usr/share/applications"
        nested.mkdir(parents=True)
        (nested / "helper.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Helper\nExec=helper --listen\n"
        )
        result = self.build(kind="appimage", program="AppRun", name="custom")
        self.assertIn("Name=Friendly App", result)
        self.assertIn("Exec=/nix/store/fixture/bin/custom --open %U", result)
        self.assertNotIn("--listen", result)

    def test_appimage_nested_entry_without_primary_is_ignored(self):
        self.entry("Exec=helper --listen\n")
        nested = self.root / "usr/share/applications"
        nested.mkdir(parents=True)
        (self.root / "app.desktop").rename(nested / "app.desktop")
        result = self.build(kind="appimage", program="AppRun")
        self.assertIn("Name=app\n", result)
        self.assertIn("Terminal=false", result)
        self.assertNotIn("--listen", result)

    def test_appimage_multiple_primary_entries_are_ambiguous(self):
        self.entry("Exec=app --first\n")
        (self.root / "other.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Other\nExec=other --second\n"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.build(kind="appimage", program="AppRun")
        self.assertIn("Name=app\n", result)
        self.assertNotIn("--first", result)
        self.assertNotIn("--second", result)

    def test_appimage_primary_symlink_imports_metadata_once(self):
        self.entry("Exec=upstream-app --open %U\n")
        nested = self.root / "usr/share/applications"
        nested.mkdir(parents=True)
        (self.root / "app.desktop").rename(nested / "org.vendor.Upstream.desktop")
        (self.root / "org.vendor.Upstream.desktop").symlink_to(
            "usr/share/applications/org.vendor.Upstream.desktop"
        )
        result = self.build(kind="appimage", program="AppRun", name="custom")
        self.assertIn("Name=Friendly App", result)
        self.assertIn("Exec=/nix/store/fixture/bin/custom --open %U", result)

    def test_exec_encoded_separators_and_quotes_survive_replacement(self):
        for command, arguments in (
            (r"app\s--open %U", ["--open", "%U"]),
            (
                r'"/opt/a\\"b/app"\s--open\s"say \\"hi\\""',
                ["--open", 'say "hi"'],
            ),
        ):
            with self.subTest(command=command):
                self.entry(f"Exec={command}\n")
                result = self.build()
                value = next(
                    line[5:] for line in result.splitlines() if line.startswith("Exec=")
                )
                self.assertEqual(
                    desktop.parse_exec(value),
                    ["/nix/store/fixture/bin/app", *arguments],
                )

    def test_exec_validation_uses_desktop_quoting_and_field_rules(self):
        for command, suffix in (
            ('"/opt/Friendly App/app" --open %F', " --open %F"),
            ('app "two words" %% %u', ' "two words" %% %u'),
            (r'app "price \\$5"', r' "price \\$5"'),
            ("app %i %c %k", " %i %c %k"),
            (r"app\s--open %U", r"\s--open %U"),
            (r'"/opt/a\\"b/app" --open %U', " --open %U"),
        ):
            self.assertEqual(desktop.exec_suffix(command), suffix)
        for command in (
            "app %F %u",
            "app --files=%F",
            'app "%U"',
            "app %x",
            "app %",
            'app "unclosed',
            "app 'shell quotes'",
            "app; other",
            "env X=1 app",
            'app "unescaped $value"',
            'app "partial"quote',
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                desktop.exec_suffix(command)

    def test_parse_exec_decodes_quoting_and_retains_field_codes(self):
        self.assertEqual(
            desktop.parse_exec(
                r'app "quote: \\"hello\\"" "price \\$5" "path C:\\\\apps" %% %U'
            ),
            ["app", 'quote: "hello"', "price $5", "path C:\\apps", "%%", "%U"],
        )
        self.assertEqual(desktop.parse_exec('app ""'), ["app", ""])
        for command in ("app\\", r"app\q--open", r"app\t--open"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                desktop.parse_exec(command)
