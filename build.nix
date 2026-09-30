{
  manifestFile,
}:
let
  manifest = builtins.fromJSON (builtins.readFile manifestFile);
  source = builtins.getFlake manifest.nixpkgs;
  pkgs = import source.outPath { inherit (manifest) system; };
in
import ./recipe.nix {
  inherit pkgs manifest;
}
