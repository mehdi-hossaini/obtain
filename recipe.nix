{
  pkgs,
  manifest,
}:
let
  kind = manifest.kind or "appimage";
  runtime = manifest.runtime or "fhs";
  imageVersion = builtins.substring 0 16 (builtins.hashString "sha256" manifest.version);
  payloadLibraries = [
    pkgs.stdenv.cc.cc.lib
    pkgs.zlib
    pkgs.openssl
    pkgs.libxcrypt
    pkgs.ncurses
    pkgs.glib
    pkgs.alsa-lib
    pkgs.dbus
  ];
  graphicsLibraries = [
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
  ];
  # Search these for directly linked desktop dependencies. Unlike the core
  # FHS runtime, unreferenced desktop libraries do not enter the app's closure.
  desktopLibraries = with pkgs; [
    gtk3
    nss
    nspr
    atk
    at-spi2-atk
    at-spi2-core
    cups
    cairo
    pango
    gdk-pixbuf
    libxcomposite
    libxdamage
    libgbm
    libdrm
    curl
    webkitgtk_4_1
    libsoup_3
    libsecret
    libxkbfile
  ];
  imageSource = pkgs.fetchurl {
    inherit (manifest) url hash;
    name = "${manifest.name}.AppImage";
  };
  extractedImage =
    pkgs.runCommand "${manifest.name}-${imageVersion}-extracted"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.dwarfs
          pkgs.appimageTools.appimage-exec
        ];
        strictDeps = true;
      }
      ''
        python3 ${./appimage.py} ${imageSource} "$out"
        if ! test -f "$out/AppRun" || ! test -x "$out/AppRun"; then
          echo "AppImage has no executable AppRun entry point." >&2
          exit 1
        fi
      '';
  payload = pkgs.stdenvNoCC.mkDerivation {
    pname = "obtain-payload-${manifest.name}";
    version = imageVersion;
    src = pkgs.fetchurl {
      inherit (manifest) url hash;
      name = "${manifest.name}.download";
    };
    nativeBuildInputs = [
      pkgs.python3
      pkgs.autoPatchelfHook
      pkgs.makeWrapper
    ];
    # Desktop archives can link these directly even when shipping their own
    # X11 libraries (for example Zed). Only referenced libraries enter RPATH.
    buildInputs = payloadLibraries ++ graphicsLibraries ++ desktopLibraries;
    # dlopen() dependencies do not appear in ELF's DT_NEEDED list. Append fallback
    # paths to executables and shared libraries, after resolved bundle dependencies.
    # runtimeDependencies would prepend them only to executables, overriding a
    # bundled library with the same SONAME and missing shared-library dlopen calls.
    appendRunpaths = map (package: "${pkgs.lib.getLib package}/lib") graphicsLibraries;
    dontUnpack = true;
    dontConfigure = true;
    dontBuild = true;
    dontStrip = true;
    installPhase = ''
      runHook preInstall
      python3 ${./payload.py} ${pkgs.lib.escapeShellArg kind} "$src" "$out/lib/obtain-payload" ${
        pkgs.lib.escapeShellArg (manifest.program or "program")
      } ${toString (manifest.strip_components or 0)}
      mkdir -p "$out/bin"
      makeWrapper "$out/lib/obtain-payload/${
        if kind == "binary" then "program" else manifest.program
      }" "$out/bin/${manifest.name}" \
        --set-default XLOCALEDIR "${pkgs.libx11}/share/X11/locale"
      runHook postInstall
    '';
  };
  payloadRuntime = pkgs.buildFHSEnv {
    pname = manifest.name;
    version = imageVersion;
    runScript = "${payload}/bin/${manifest.name}";
    # Applications may download new helper executables after installation.
    # Give their child processes a Linux loader too, without modifying the host
    # or patching mutable downloads outside the immutable Nix build.
    targetPkgs = _: payloadLibraries ++ graphicsLibraries ++ [ pkgs.libx11 ];
  };
  wrapped =
    if
      builtins.elem kind [
        "archive"
        "binary"
      ]
    then
      (if runtime == "direct" then payload else payloadRuntime)
    else
      pkgs.appimageTools.wrapAppImage {
        # Desktop libraries used by Flutter AppImages such as AppFlowy.
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
        pname = manifest.name;
        version = imageVersion;
        src = extractedImage;
      };
  desktop =
    pkgs.runCommand "obtain-${manifest.name}-desktop" { nativeBuildInputs = [ pkgs.python3 ]; }
      ''
        python3 ${./desktop.py} ${
          if kind == "appimage" then extractedImage else "${payload}/lib/obtain-payload"
        } \
          "$out" ${pkgs.lib.escapeShellArg manifest.name} \
          ${pkgs.lib.escapeShellArg "${wrapped}/bin/${manifest.name}"} \
          ${pkgs.lib.escapeShellArg kind} ${pkgs.lib.escapeShellArg (manifest.program or "AppRun")}
      '';
in
assert builtins.elem kind [
  "appimage"
  "archive"
  "binary"
];
assert
  builtins.elem runtime [
    "fhs"
    "direct"
  ]
  && (kind != "appimage" || runtime == "fhs");
pkgs.symlinkJoin {
  name = "obtain-${manifest.name}-${
    builtins.substring 0 16 (builtins.hashString "sha256" (builtins.toJSON manifest))
  }";
  paths = [
    wrapped
    desktop
  ];
  postBuild = ''
    mkdir -p "$out/share/obtain"
    cp ${pkgs.writeText "manifest.json" (builtins.toJSON manifest)} "$out/share/obtain/manifest.json"
  '';
  meta.mainProgram = manifest.name;
}
