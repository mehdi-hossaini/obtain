{
  description = "Obtain: a minimal GitHub release application manager";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      packages.${system} = {
        default = import ./package.nix { inherit pkgs; };
        live-vm = import ./tests/live { inherit pkgs; };
      };
      apps.${system}.default = {
        type = "app";
        meta.description = "Track and install GitHub release applications";
        program = "${self.packages.${system}.default}/bin/obtain";
      };
      homeManagerModules.default = import ./home-module.nix;
      checks.${system} = {
        unit = import ./checks.nix { inherit pkgs; };
        vm = import ./tests/vm { inherit pkgs home-manager; };
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          (pkgs.python3.withPackages (ps: [ ps.debugpy ]))
          pkgs.ruff
          pkgs.pyright
          pkgs.nixd
          pkgs.nixfmt
          pkgs.actionlint
          pkgs.shellcheck
          pkgs.nix
          pkgs.git
          pkgs.just
        ];
      };
    };
}
