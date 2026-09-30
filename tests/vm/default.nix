{ pkgs, home-manager }:
let
  fixtures = import ./fixtures.nix { inherit pkgs; };
  obtain = import ../../package.nix { inherit pkgs; };
  certificates = pkgs.runCommand "obtain-vm-only-tls" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    mkdir -p $out
    openssl req -x509 -newkey rsa:2048 -nodes -days 36500 \
      -keyout $out/key.pem -out $out/cert.pem -subj '/CN=Obtain VM test CA' \
      -addext 'subjectAltName=DNS:github.com,DNS:api.github.com,DNS:codeload.github.com'
  '';
  python = pkgs.python3;
in
pkgs.testers.runNixOSTest {
  name = "obtain-cli-evals";
  nodes.machine = { ... }: {
    imports = [ home-manager.nixosModules.home-manager ];
    virtualisation = {
      memorySize = 2048;
      diskSize = 16384;
      writableStoreUseTmpfs = false;
      cores = 2;
      additionalPaths = fixtures.buildSupport;
      fileSystems."/var/lib/obtain-evals/full" = {
        device = "tmpfs";
        fsType = "tmpfs";
        options = [
          "size=96k"
          "mode=0777"
        ];
      };

    };
    nix.settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      substituters = pkgs.lib.mkForce [ ];
      connect-timeout = 2;
      download-attempts = 1;
    };
    nix.registry = pkgs.lib.mkForce { };
    networking.hosts."127.0.0.1" = [
      "github.com"
      "api.github.com"
      "codeload.github.com"
    ];
    security.pki.certificateFiles = [ "${certificates}/cert.pem" ];
    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      linger = true;
    };
    home-manager = {
      useGlobalPkgs = true;
      useUserPackages = true;
      users.alice = {
        imports = [ ../../home-module.nix ];
        home.stateVersion = "26.05";
        programs.bash.enable = true;
        programs.obtain.enable = true;
      };
    };
    environment.systemPackages = [
      obtain
      python
      pkgs.curl
      pkgs.jq
    ];
    systemd = {
      tmpfiles.rules = [ "d /var/lib/obtain-evals 0770 alice users -" ];
      services.fixture-github = {
        wantedBy = [ "multi-user.target" ];
        after = [ "systemd-tmpfiles-setup.service" ];
        preStart = ''
          test -e /var/lib/obtain-evals/control.json || echo '{}' > /var/lib/obtain-evals/control.json
          chown alice:users /var/lib/obtain-evals/control.json
          touch /var/lib/obtain-evals/requests.jsonl
          chown alice:users /var/lib/obtain-evals/requests.jsonl
        '';
        serviceConfig.ExecStart = "${python}/bin/python ${./server.py} ${fixtures.archives} ${certificates}/cert.pem ${certificates}/key.pem";
      };
    };
    system.stateVersion = "26.05";
  };
  testScript = ''
    import json

    machine.start()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("home-manager-alice.service")
    machine.wait_for_unit("user@1000.service")
    machine.wait_for_open_port(443)
    machine.succeed("curl --fail https://api.github.com/health")
    machine.succeed("mountpoint /var/lib/obtain-evals/full && df -B1 /var/lib/obtain-evals/full")
    machine.succeed("install -m 644 ${./scenarios.py} /tmp/scenarios.py")
    status, output = machine.execute("su - alice -c 'python /tmp/scenarios.py'", timeout=1800)
    print(output)
    machine.copy_from_machine("/var/lib/obtain-evals/report.json")
    machine.copy_from_machine("/var/lib/obtain-evals/commands.log")
    machine.copy_from_machine("/var/lib/obtain-evals/requests.jsonl")
    assert status == 0, "CLI scenario suite failed; see report.json and commands.log"

    with subtest("installed app and rollback history survive reboot"):
        machine.succeed("su - alice -c 'python /tmp/scenarios.py --prepare-reboot'")
        machine.shutdown()
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.wait_for_unit("user@1000.service")
        machine.wait_for_open_port(443)
        machine.succeed("su - alice -c 'python /tmp/scenarios.py --verify-reboot'")
        machine.copy_from_machine("/var/lib/obtain-evals/reboot.json")
        report = json.loads(machine.succeed("cat /var/lib/obtain-evals/report.json"))
        print(f"Passed {report['passed']} CLI scenarios plus reboot persistence.")
  '';
}
