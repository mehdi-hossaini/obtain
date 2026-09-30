{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.obtain;
in
{
  options.programs.obtain = {
    enable = lib.mkEnableOption "Obtain GitHub release application manager";
    package = lib.mkOption {
      type = lib.types.package;
      default = import ./package.nix { inherit pkgs; };
      description = "Obtain package to install.";
    };
  };
  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];
    # Managed commands are linked under the user's data directory.
    home.sessionPath = [ "${config.xdg.dataHome}/obtain/bin" ];
  };
}
