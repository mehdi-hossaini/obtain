{ pkgs }:
let
  pin = (builtins.fromJSON (builtins.readFile ../../flake.lock)).nodes.nixpkgs;
  mkImage =
    version:
    pkgs.runCommand "fixture-${version}.AppImage"
      {
        nativeBuildInputs = [
          pkgs.squashfsTools
          pkgs.dwarfs
          pkgs.python3
          pkgs.stdenv.cc
        ];
      }
      ''
        mkdir AppDir
        cat > AppDir/AppRun <<'SCRIPT'
        #!/bin/sh
        "$(dirname "$0")/runtime-probe" || exit 1
        printf 'appimage ${version} %s\n' "$*"
        SCRIPT
        chmod +x AppDir/AppRun
        cat > runtime-probe.c <<'C'
        #include <dlfcn.h>
        #include <stdio.h>
        int main(void) {
          const char *libs[] = {
            "/usr/lib/libicuuc.so",
            "/usr/lib/libicui18n.so",
            "/usr/lib/libwebkit2gtk-4.1.so.0",
            "/usr/lib/libjavascriptcoregtk-4.1.so.0",
            "/usr/lib/libsoup-3.0.so.0"
          };
          for (size_t i = 0; i < sizeof(libs) / sizeof(libs[0]); ++i) {
            void *handle = dlopen(libs[i], RTLD_NOW);
            if (!handle) { fprintf(stderr, "%s\n", dlerror()); return 1; }
            dlclose(handle);
          }
          return 0;
        }
        C
        cc runtime-probe.c -ldl -o AppDir/runtime-probe
        ${pkgs.lib.optionalString (
          version == "missing-apprun"
        ) "rm AppDir/AppRun; echo fixture > AppDir/README"}
        ${pkgs.lib.optionalString (version == "nonexec-apprun") "chmod 644 AppDir/AppRun"}
        ${pkgs.lib.optionalString (version == "directory-apprun") "rm AppDir/AppRun; mkdir AppDir/AppRun"}
        ${
          if version == "2" then
            "mkdwarfs -i AppDir -o filesystem -l 1 -N 1 -L 64m --no-progress"
          else
            "env -u SOURCE_DATE_EPOCH mksquashfs AppDir filesystem -noappend -all-root -processors 1 -mkfs-time 1 -all-time 1"
        }
        python3 - ${pkgs.coreutils}/bin/true filesystem "$out" <<'PY'
        import struct, sys
        from pathlib import Path
        elf = bytearray(Path(sys.argv[1]).read_bytes())
        end = struct.unpack_from('<Q', elf, 40)[0] + struct.unpack_from('<H', elf, 58)[0] * struct.unpack_from('<H', elf, 60)[0]
        elf[8:11] = b'AI\x02'
        Path(sys.argv[3]).write_bytes(elf[:end] + Path(sys.argv[2]).read_bytes())
        PY
      '';
  images = map mkImage [
    "1"
    "2"
  ];
  warmImage = pkgs.appimageTools.wrapType2 {
    extraPkgs = p: [
      p.keybinder3
      p.libnotify
      p.libepoxy
      p.libarchive
      p.libva
      p.lcms2
      p.libxv
      p.lz4
      p.webkitgtk_4_1
      p.libsoup_3
      p.icu
    ];
    pname = "fixture-warmup";
    version = "1";
    src = builtins.head images;
  };
  # This private Nixpkgs helper is a build dependency, absent from the runtime
  # closure. Recreate its pinned definition to avoid a Rust bootstrap offline.
  fhsRootBuilder = pkgs.rustPlatform.buildRustPackage {
    name = "fhs-rootfs-bulder";
    src = pkgs.path + "/pkgs/build-support/build-fhsenv-bubblewrap/rootfs-builder";
    cargoLock.lockFile =
      pkgs.path + "/pkgs/build-support/build-fhsenv-bubblewrap/rootfs-builder/Cargo.lock";
    doCheck = false;
  };
  binaries =
    pkgs.runCommand "obtain-elf-fixtures"
      {
        nativeBuildInputs = [
          pkgs.stdenv.cc
          pkgs.patchelf
          pkgs.python3
          pkgs.pkg-config
        ];
        buildInputs = [
          pkgs.zlib
          pkgs.glib
          pkgs.alsa-lib
          pkgs.libGL
          pkgs.gtk3
        ];
      }
      ''
        mkdir -p "$out"
        for version in 1 2; do
          cat > program.c <<EOF
          #include <stdio.h>
          #include <zlib.h>
          #include <glib.h>
          #include <alsa/asoundlib.h>
          #include <dlfcn.h>
          #include <stdlib.h>
          #include <string.h>
          #include <unistd.h>
          #include <GL/gl.h>
          #ifdef FIXTURE_BUNDLE
          #include <gtk/gtk.h>
          extern int bundle_check(void);
          #endif
          int main(void) {
            /* A real DT_NEEDED dependency as well as the dlopen checks below. */
            (void)glGetError();
            #ifdef FIXTURE_BUNDLE
            if (gtk_get_major_version() != 3) return 1;
            if (bundle_check() != 42) return 1;
            #endif
            char *helper = getenv("OBTAIN_RAW_HELPER");
            if (helper) {
              helper = strdup(helper);
              unsetenv("OBTAIN_RAW_HELPER");
              execl(helper, helper, NULL);
              perror("unpatched helper");
              return 1;
            }
            const char *libraries[] = { "libvulkan.so.1", "libEGL.so.1", "libwayland-client.so.0", "libXcursor.so.1", "libxkbcommon.so.0", "libfontconfig.so.1" };
            for (unsigned int i = 0; i < sizeof(libraries) / sizeof(libraries[0]); ++i) {
              void *library = dlopen(libraries[i], RTLD_NOW);
              if (!library) { fprintf(stderr, "%s\n", dlerror()); return 1; }
              dlclose(library);
            }
            char *version = g_strdup(zlibVersion());
            printf("payload $version zlib %s alsa %s\n", version, snd_asoundlib_version());
            g_free(version);
            return 0;
          }
        EOF
          cc program.c -lGL -lz -ldl $(pkg-config --cflags --libs glib-2.0 alsa) -o "$out/binary-$version"
          patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 --remove-rpath "$out/binary-$version"
          if patchelf --print-needed "$out/binary-$version" | grep -q libgtk; then
            echo "Core helper fixture must not link GTK" >&2
            exit 1
          fi
          echo 'int bundle_check(void) { return 42; }' > library.c
          cc -shared -fPIC -Wl,-soname,libobtain-fixture.so.1 library.c -o "$out/libobtain-fixture.so.1.0"
          cc -DFIXTURE_BUNDLE program.c "$out/libobtain-fixture.so.1.0" -lGL -lz -ldl $(pkg-config --cflags --libs glib-2.0 alsa gtk+-3.0) -o "$out/bundled-$version"
          patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 --remove-rpath "$out/bundled-$version"
        done
        python3 - "$out" <<'PYCODE'
        import io, stat, sys, tarfile, zipfile
        from pathlib import Path
        root = Path(sys.argv[1])
        for version in (1, 2):
            binary = root / f"bundled-{version}"
            library = root / "libobtain-fixture.so.1.0"
            with tarfile.open(root / f"archive-{version}.tar.gz", "w:gz") as archive:
                archive.add(binary, arcname="bundle/bin/app")
                archive.add(library, arcname="bundle/lib/libobtain-fixture.so.1.0")
                link = tarfile.TarInfo("bundle/lib/libobtain-fixture.so.1")
                link.type = tarfile.SYMTYPE
                link.linkname = library.name
                archive.addfile(link)
            with zipfile.ZipFile(root / f"zip-{version}.zip", "w") as archive:
                archive.write(binary, "bundle/bin/app")
                archive.write(library, "bundle/lib/libobtain-fixture.so.1.0")
                link = zipfile.ZipInfo("bundle/lib/libobtain-fixture.so.1")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, library.name)
        for variant in ("traversal", "link", "script", "foreign"):
            with tarfile.open(root / f"{variant}.tar.gz", "w:gz") as archive:
                item = tarfile.TarInfo("../escaped" if variant == "traversal" else "bundle/bin/app")
                data = b"#!/bin/sh\necho no\n"
                if variant == "link":
                    item.type = tarfile.SYMTYPE
                    item.linkname = "/etc/passwd"
                    data = b""
                elif variant == "foreign":
                    data = bytearray((root / "binary-1").read_bytes())
                    data[18:20] = (183).to_bytes(2, "little")
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        PYCODE
      '';
  archives = pkgs.runCommand "obtain-vm-fixtures" { } ''
    mkdir -p $out
    cp -r ${binaries} $out/payloads
    cp ${builtins.elemAt images 0} $out/image-1.AppImage
    cp ${builtins.elemAt images 1} $out/image-2.AppImage
    cp ${mkImage "missing-apprun"} $out/missing-apprun.AppImage
    cp ${mkImage "nonexec-apprun"} $out/nonexec-apprun.AppImage
    cp ${mkImage "directory-apprun"} $out/directory-apprun.AppImage
    tar --hard-dereference -czf $out/nixpkgs.tar.gz --mtime=@${toString pin.locked.lastModified} --transform='flags=r;s,^,source/,' -C ${pkgs.path} .
  '';
in
{
  inherit archives;
  # Include runtime closures and the build tools needed for new
  # fixture wrappers. Final Obtain app profiles are built inside the guest.
  buildSupport = [
    warmImage
    fhsRootBuilder
    pkgs.glib.dev
    pkgs.jq.dev
    pkgs.stdenv
    pkgs.stdenvNoCC
    pkgs.bash
    pkgs.coreutils
    pkgs.lndir
    pkgs.makeWrapper
    pkgs.desktop-file-utils
    pkgs.gnugrep
    pkgs.gnused
    pkgs.findutils
    pkgs.path
    pkgs.autoPatchelfHook
    pkgs.python3
    pkgs.dwarfs
    pkgs.patchelf
    pkgs.stdenv.cc.cc.lib
    pkgs.zlib
    pkgs.zlib.dev
    pkgs.openssl
    pkgs.openssl.dev
    pkgs.libxcrypt
    pkgs.libxcrypt.man
    pkgs.ncurses
    pkgs.ncurses.dev
    pkgs.glib
    pkgs.alsa-lib
    pkgs.alsa-lib.dev
    pkgs.dbus
    pkgs.dbus.dev
    pkgs.libGL
    pkgs.vulkan-loader
    pkgs.wayland
    pkgs.libx11
    pkgs.libxcursor
    pkgs.libxext
    pkgs.libxfixes
    pkgs.libxi
    pkgs.libxinerama
    pkgs.libxrandr
    pkgs.libxrender
    pkgs.libxkbcommon
    pkgs.fontconfig
  ]
  ++ map pkgs.lib.getDev [
    pkgs.libGL
    pkgs.vulkan-loader
    pkgs.wayland
    pkgs.libx11
    pkgs.libxcursor
    pkgs.libxext
    pkgs.libxfixes
    pkgs.libxi
    pkgs.libxinerama
    pkgs.libxrandr
    pkgs.libxrender
    pkgs.libxkbcommon
    pkgs.fontconfig
    pkgs.gtk3
    pkgs.nss
    pkgs.nspr
    pkgs.atk
    pkgs.at-spi2-atk
    pkgs.at-spi2-core
    pkgs.cups
    pkgs.cairo
    pkgs.pango
    pkgs.gdk-pixbuf
    pkgs.libxcomposite
    pkgs.libxdamage
    pkgs.libgbm
    pkgs.libdrm
    pkgs.curl
    pkgs.webkitgtk_4_1
    pkgs.libsoup_3
    pkgs.libsecret
    pkgs.libxkbfile
  ]
  ++ warmImage.fhsenv.exportReferencesGraph.graph;
}
