{
  description = "audiobook-dl flake";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = pkgs.lib;

        mkDate =
          longDate:
          (lib.concatStringsSep "-" [
            (lib.substring 0 4 longDate)
            (lib.substring 4 2 longDate)
            (lib.substring 6 2 longDate)
          ]);

        date = mkDate (self.lastModifiedDate or "19700101");
        python = pkgs.python3;
      in
      {
        packages.default = self.packages.${system}.audiobook-dl;
        packages.audiobook-dl = python.pkgs.buildPythonApplication {
          pname = "audiobook-dl";
          version = "${date}_${self.shortRev or "dirty"}";
          pyproject = true;

          src = ./.;

          build-system = with python.pkgs; [
            setuptools
          ];

          dependencies =
            (with python.pkgs; [
              attrs
              cssselect
              keyring
              lxml
              m3u8
              mutagen
              openpyxl
              pillow
              platformdirs
              pycountry
              pycryptodome
              requests
              rich
              tkinter
              urllib3
              python-dateutil
            ])
            ++ lib.optionals (python.pythonOlder "3.11") [ python.pkgs.tomli ]
            ++ lib.optionals (python.pkgs ? curl-cffi) [ python.pkgs.curl-cffi ];

          makeWrapperArgs = [
            "--prefix" "PATH" ":" "${lib.makeBinPath [ pkgs.ffmpeg ]}"
          ];

          meta = {
            description = "Desktop GUI audiobook downloader";
            homepage = "https://github.com/jo1gi/audiobook-dl";
            license = lib.licenses.gpl3Only;
            maintainers = with lib.maintainers; [ ];
            mainProgram = "audiobook-dl";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.ffmpeg
            (python.withPackages(ps: with ps; [
              mutagen
              openpyxl
              requests
              rich
              lxml
              cssselect
              pillow
              m3u8
              pycryptodome
              platformdirs
              keyring
              attrs
              pycountry
              urllib3
              python-dateutil

              # Test
              pytest
              mypy
              ruff
              types-requests
              types-setuptools

              # Build
              build
              setuptools
              twine
            ] ++ lib.optionals (ps.python.pythonOlder "3.11") [ ps.tomli ]
              ++ lib.optionals (ps ? curl-cffi) [ ps.curl-cffi ]))
          ];

        };
      }
    );
}
