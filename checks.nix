{ pkgs }:
pkgs.runCommand "obtain-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
    src = pkgs.lib.fileset.toSource {
      root = ./.;
      fileset = pkgs.lib.fileset.unions [
        ./obtain.py
        ./flake.lock
        ./recipe.nix
        ./payload.py
        ./appimage.py
        ./desktop.py
        ./build.nix
        ./tests/live/default.nix
        (pkgs.lib.fileset.fileFilter (file: file.hasExt "py") ./tests)
      ];
    };
  }
  ''
    cp -r "$src" source
    chmod -R u+w source
    cd source
    python3 -m unittest discover -s tests -v
    touch "$out"
  ''
