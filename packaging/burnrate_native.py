"""Windowless native app and its owned collector, from one frozen bundle."""

from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    multiprocessing.freeze_support()
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 -- GUI process lifetime
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--service":
        from spend_app.service import main as service_main

        return service_main(arguments[1:])
    # Reuse CLI validation and configuration; desktop is the executable's
    # default action. No embedded web renderer or browser process is used.
    from spend_app.cli import main as cli_main

    sys.argv = ["burnrate", "desktop", *arguments]
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
