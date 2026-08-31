{
  description = "voice2text - push-to-talk Whisper dictation";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";

      pkgs = import nixpkgs {
        inherit system;
        # cuDNN and the CUDA redistributables are unfree. Note that
        # config.cudaSupport is deliberately NOT set: faster-whisper pulls in
        # onnxruntime, and flipping it globally would trigger a multi-hour CUDA
        # onnxruntime build. Only CTranslate2 actually needs the GPU.
        config.allowUnfree = true;
      };

      # CTranslate2 is the only thing here compiled against CUDA, and it is not
      # in any binary cache, so keep the compile as small as possible. Its
      # default CUDA_ARCH_LIST is "Auto", which probes for a local GPU, finds
      # none inside the build sandbox and falls back to building eight
      # architectures. The GTX 1650 is Turing, sm_75 -- that alone.
      ctranslate2-cuda =
        (pkgs.ctranslate2.override {
          withCUDA = true;
          withCuDNN = true;
        }).overrideAttrs (old: {
          cmakeFlags = (old.cmakeFlags or [ ]) ++ [
            # CUDA_ARCH_LIST, not CMAKE_CUDA_ARCHITECTURES: CTranslate2 still
            # uses CMake's legacy FindCUDA path, which ignores the latter.
            (pkgs.lib.cmakeFeature "CUDA_ARCH_LIST" "7.5")
          ];
        });

      pythonPackages = pkgs.python3Packages.overrideScope (final: prev: {
        ctranslate2 = prev.ctranslate2.override {
          ctranslate2-cpp = ctranslate2-cuda;
        };
      });

      # Always pkgs.python3, never a pinned pythonXY: only the pinned nixpkgs'
      # default interpreter has a populated binary cache. Choosing any other
      # rebuilds the whole set (torch, transformers, onnxruntime, ...) from
      # source. Which version that is follows nixpkgs and does change between
      # revs -- do not hard-code it.
      pythonEnv = pkgs.python3.withPackages (_: with pythonPackages; [
        evdev
        faster-whisper
        numpy
        sounddevice
        tkinter
      ]);

      voice2text = pkgs.writeShellApplication {
        name = "voice2text";
        # wl-copy puts the transcript on the clipboard; the app then sends a
        # single paste chord through /dev/uinput.
        runtimeInputs = [ pkgs.wl-clipboard ];
        text = ''
          # The CUDA libraries are linked against cuda_cudart's link-time
          # libcuda stub; the real one ships with the running NVIDIA driver.
          export LD_LIBRARY_PATH=/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
          # -u because stdout is a pipe to the journal, and Python would
          # otherwise block-buffer every diagnostic into invisibility.
          exec ${pythonEnv}/bin/python -u ${./main.py} "$@"
        '';
      };
    in
    {
      packages.${system} = {
        inherit voice2text pythonEnv ctranslate2-cuda;
        default = voice2text;
      };

      apps.${system}.default = {
        type = "app";
        program = "${voice2text}/bin/voice2text";
        meta.description = "Push-to-talk Whisper dictation";
      };

      devShells.${system}.default = pkgs.mkShell {
        # wl-clipboard as well as the interpreter: the packaged app gets
        # wl-copy from runtimeInputs, and without it here `python main.py`
        # transcribes fine and then dies on the paste.
        packages = [ pythonEnv pkgs.wl-clipboard ];
        LD_LIBRARY_PATH = "/run/opengl-driver/lib";
        shellHook = ''
          echo "voice2text dev shell: $(python3 --version 2>&1). Run: python main.py"
        '';
      };

      homeModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.voice2text;
        in
        {
          options.services.voice2text = {
            enable = lib.mkEnableOption "voice2text push-to-talk dictation";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.voice2text;
              defaultText = lib.literalExpression "voice2text.packages.\${system}.voice2text";
              description = "The voice2text package to run.";
            };
          };

          config = lib.mkIf cfg.enable {
            home.packages = [ cfg.package ];

            systemd.user.services.voice2text = {
              Unit = {
                Description = "voice2text push-to-talk dictation";
                After = [ "graphical-session.target" ];
                PartOf = [ "graphical-session.target" ];
              };

              Service = {
                ExecStart = "${cfg.package}/bin/voice2text";
                # pynput needs DISPLAY, which gnome-session exports into the
                # user manager only once XWayland is up; retry rather than fail
                # the unit if we win that race.
                Restart = "on-failure";
                RestartSec = 5;
              };

              Install.WantedBy = [ "graphical-session.target" ];
            };
          };
        };
    };
}
