"""Entry point of the frozen BURNRATE service the desktop app ships.

PyInstaller builds this into ``burnrate-service`` (see burnrate-service.spec).
The desktop app starts it with ``--port``; it runs ``spend_app.service``, the
same windowless service the scheduled-task install runs. Any other first
argument is a CLI command (``burnrate-service doctor``).
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    # The collector worker is a spawned copy of this executable.
    multiprocessing.freeze_support()
    # A windowed build has no console streams; libraries that write to them
    # must not crash the service.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    arguments = sys.argv[1:]
    if arguments and not arguments[0].startswith("-"):
        from spend_app.cli import main as cli_main

        sys.argv = ["burnrate", *arguments]
        return cli_main()
    from spend_app.service import main as service_main

    return service_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
