"""Build the environment for helpers that do not need application secrets."""

from __future__ import annotations

import os


_SYSTEM_NAMES = frozenset(
    {
        "path",
        "pathext",
        "systemroot",
        "systemdrive",
        "windir",
        "comspec",
        "home",
        "userprofile",
        "appdata",
        "localappdata",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "programw6432",
        "temp",
        "tmp",
        "tmpdir",
        "xdg_config_home",
        "xdg_data_home",
        "xdg_cache_home",
        "lang",
        "lc_all",
        "lc_ctype",
    }
)


def child_environment(**explicit: str) -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name.casefold() in _SYSTEM_NAMES
    } | explicit
