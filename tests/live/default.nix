{ pkgs }:
let
  obtain = import ../../package.nix { inherit pkgs; };
in
(pkgs.testers.runNixOSTest {
  name = "obtain-live-repositories";
  globalTimeout = 29000;
  nodes.machine = {
    virtualisation = {
      memorySize = 3072;
      cores = 2;
      diskSize = 131072;
      writableStoreUseTmpfs = false;
      restrictNetwork = false;
    };
    nix.settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      max-jobs = 2;
      cores = 2;
      connect-timeout = 15;
      download-attempts = 2;
    };
    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      linger = true;
    };
    environment.systemPackages = [
      obtain
      pkgs.python3
      pkgs.curl
      pkgs.jq
      pkgs.xorg.xorgserver
      pkgs.xorg.xwininfo
      pkgs.xorg.xprop
      pkgs.openbox
      pkgs.wmctrl
      pkgs.xdotool
      pkgs.mesa-demos
      pkgs.imagemagick
    ];
    fonts.packages = [ pkgs.dejavu_fonts ];
    services.dbus.enable = true;
    hardware.graphics.enable = true;
    systemd = {
      coredump.settings.Coredump = {
        Storage = "none";
        ProcessSizeMax = 0;
      };
      services.virtual-display = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          User = "alice";
          ExecStart = "${pkgs.xorg.xorgserver}/bin/Xvfb :0 -screen 0 1280x800x24 -nolisten tcp";
        };
      };
      services.window-manager = {
        wantedBy = [ "multi-user.target" ];
        after = [ "virtual-display.service" ];
        environment.DISPLAY = ":0";
        serviceConfig = {
          User = "alice";
          ExecStart = "${pkgs.openbox}/bin/openbox";
          Restart = "on-failure";
        };
      };
    };
    system.stateVersion = "26.05";
  };
  testScript = ''
    import json
    import os
    import shlex
    import time

    import signal

    def terminate_run(signum, frame):
        raise KeyboardInterrupt("Live evaluation interrupted; completed reports remain on disk")

    signal.signal(signal.SIGTERM, terminate_run)
    try:
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.wait_for_unit("user@1000.service")
        machine.wait_for_unit("window-manager.service")
        machine.succeed("curl --fail --max-time 30 https://github.com/robots.txt >/dev/null")
        boundary_only = os.environ.get("OBTAIN_LIVE_BOUNDARIES") == "1"
        payload_only = os.environ.get("OBTAIN_LIVE_PAYLOADS") == "1"
        hard_only = os.environ.get("OBTAIN_LIVE_HARD") == "1"
        if hard_only:
            import re
            run_label = os.environ.get("OBTAIN_LIVE_RUN", "baseline")
            assert re.fullmatch(r"[a-z][a-z0-9-]{0,24}", run_label)
            guest_results = "/home/alice/hard-results/" + run_label
            cohort = "${./hard-repositories.json}"
            expected_count = 5
            result_label = "hard-" + run_label
        elif payload_only:
            guest_results = "/home/alice/payload-results"
            cohort = "${./payload-repositories.json}"
            expected_count = 4
            result_label = "payload-live"
        elif boundary_only:
            guest_results = "/home/alice/boundary-results"
            cohort = "${./boundary-repositories.json}"
            expected_count = 2
            result_label = "boundary-live"
        else:
            guest_results = "/home/alice/live-results"
            cohort = "${./repositories.json}"
            expected_count = 92
            result_label = "live"
        shared_label = result_label if hard_only else "live"
        machine.succeed(f"install -d /tmp/shared/{shared_label}; install -d -o alice -g users {guest_results}")
        machine.succeed(f"cp {cohort} /tmp/repositories.json; cp ${./run.py} /tmp/live-run.py")
        if hard_only and os.environ.get("OBTAIN_LIVE_COHORT"):
            machine.copy_from_host(os.environ["OBTAIN_LIVE_COHORT"], "/tmp/repositories.json")
            expected_count = len(json.loads(machine.succeed("cat /tmp/repositories.json")))
        if hard_only:
            machine.succeed(f"su - alice -c 'DISPLAY=:0 LIBGL_ALWAYS_SOFTWARE=1 glxinfo -B' > {guest_results}/graphics.txt")
        if os.environ.get("OBTAIN_LIVE_REPROBE") != "1":
            prefix = "OBTAIN_CASE_PREFIX=payload " if payload_only else ("OBTAIN_CASE_PREFIX=boundary " if boundary_only else "")
            if hard_only:
                prefix = "OBTAIN_CASE_PREFIX=hard-" + run_label + " "
            command = f"{prefix}nohup python -u /tmp/live-run.py /tmp/repositories.json {guest_results} > {guest_results}/progress.log 2>&1 < /dev/null &"
            machine.succeed("su - alice -c " + shlex.quote(command))
        if os.environ.get("OBTAIN_LIVE_REPROBE") == "1":
            machine.succeed("su - alice -c 'DISPLAY=:0 LIBGL_ALWAYS_SOFTWARE=1 glxinfo -B' > /home/alice/live-results/graphics.txt")
            machine.succeed("cp ${./repair-asset.py} /tmp/repair-asset.py")
            integrity_status, integrity_output = machine.execute("bash ${./verify-store.sh} > /home/alice/live-results/store-integrity.txt 2>&1", timeout=300)
            print(machine.succeed("cat /home/alice/live-results/store-integrity.txt"))
            machine.copy_from_machine("/home/alice/live-results/store-integrity.txt")
            assert integrity_status == 0, "Cached asset integrity repair failed"
            machine.succeed("cp ${./reprobe.py} /tmp/reprobe.py")
            machine.succeed("rm -f /home/alice/live-results/reprobes-done")
            only = shlex.quote(os.environ.get("OBTAIN_REPROBE_ONLY", ""))
            command = f"nohup python -u /tmp/reprobe.py /tmp/repositories.json /home/alice/live-results {only} > /home/alice/live-results/reprobe-progress.log 2>&1 < /dev/null &"
            machine.succeed("su - alice -c " + shlex.quote(command))
            reprobe_deadline = time.monotonic() + 7200
            while time.monotonic() < reprobe_deadline:
                machine.succeed("cp -r /home/alice/live-results/reprobes* /tmp/shared/live/ 2>/dev/null || true; cp /home/alice/live-results/reprobe-progress.log /tmp/shared/live/")
                if machine.execute("test -f /home/alice/live-results/reprobes-done")[0] == 0:
                    break
                time.sleep(10)
            else:
                raise AssertionError("Reprobes timed out")
            machine.copy_from_machine("/home/alice/live-results", "live-reprobed-" + str(int(time.time())))
            reprobes = json.loads(machine.succeed("cat /home/alice/live-results/reprobes.json"))
            assert not any(r["status"] == "harness_error" for r in reprobes["results"])
            raise SystemExit(0)
        previous = -1
        deadline = time.monotonic() + 28800
        while time.monotonic() < deadline:
            machine.succeed(f"if test -f {guest_results}/report.json; then cp {guest_results}/report.json /tmp/shared/{shared_label}/report.tmp; mv /tmp/shared/{shared_label}/report.tmp /tmp/shared/{shared_label}/report.json; cp {guest_results}/progress.log /tmp/shared/{shared_label}/progress.log; cp -r {guest_results}/*-* /tmp/shared/{shared_label}/ 2>/dev/null || true; fi")
            report_path = machine.shared_dir / shared_label / "report.json"
            if report_path.exists():
                report = json.loads(report_path.read_text())
                completed = sum(r["status"] != "running" for r in report["results"])
                if completed != previous:
                    previous = completed
                    print(f"Completed {previous}/{report['requested']} repositories: {report['counts']}", flush=True)
            if machine.execute(f"test -f {guest_results}/done")[0] == 0:
                break
            time.sleep(10)
        else:
            raise AssertionError("Live test exceeded eight hours; partial report preserved in shared directory")
        machine.copy_from_machine(guest_results, result_label)
        report = json.loads(report_path.read_text())
        assert len(report["results"]) == report["requested"] == expected_count
        print(json.dumps(report["counts"], indent=2))
        assert not any(r["status"] == "harness_error" for r in report["results"]), "Harness errors invalidate the run"
    finally:
        machine.shutdown()

  '';
}).driver
