{ pkgs }:
pkgs.stdenvNoCC.mkDerivation {
  pname = "obtain";
  version = "0.2.0";
  src = pkgs.lib.fileset.toSource {
    root = ./.;
    fileset = pkgs.lib.fileset.unions [
      ./obtain.py
      ./recipe.nix
      ./payload.py
      ./appimage.py
      ./desktop.py
      ./build.nix
      ./flake.lock
    ];
  };
  nativeBuildInputs = [ pkgs.makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/obtain" "$out/bin"
    cp obtain.py payload.py appimage.py desktop.py recipe.nix build.nix flake.lock "$out/lib/obtain/"
    makeWrapper ${pkgs.python3}/bin/python3 "$out/bin/obtain" \
      --add-flags "$out/lib/obtain/obtain.py"
    runHook postInstall
  '';
  meta = {
    description = "Minimal GitHub release application manager";
    mainProgram = "obtain";
    platforms = [ "x86_64-linux" ];
  };
}
