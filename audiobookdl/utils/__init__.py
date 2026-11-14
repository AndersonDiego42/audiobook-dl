import importlib.resources
import os
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence
import shutil
from urllib3.poolmanager import PoolManager
from requests.adapters import HTTPAdapter
from ssl import SSLContext


def levenstein_distance(a: str, b: str) -> int:
    """
    Calculates the levenstein distance between `a` and `b`

    https://en.wikipedia.org/wiki/Levenshtein_distance
    """
    if len(a) == 0:
        return len(b)
    if len(b) == 0:
        return len(a)
    if a[0] == b[0]:
        return levenstein_distance(a[1:], b[1:])
    return 1 + min(
        levenstein_distance(a, b[1:]), # Character is inserted
        levenstein_distance(a[1:], b), # Character is deleted
        levenstein_distance(a[1:], b[1:]) # Character is replaced
    )


def nearest_string(input: str, list: Sequence[str]) -> str:
    """
    Returns the closest element in `list` to `input` based on the levenstein
    distance
    """
    return sorted(list, key = lambda x: levenstein_distance(input, x))[0]


def read_asset_file(path: str) -> str:
    """
    Read audiobook-dl asset file.

    :param path: the path of the asset file relative to the audiobook-dl root dir
    :returns: the content of the asset file
    """
    return importlib.resources.files("audiobookdl") \
        .joinpath(path) \
        .read_text(encoding="utf8")


def find_executable(program: str) -> Optional[str]:
    """
    Locate an external executable (e.g. ffmpeg, ffprobe).

    Searches PyInstaller's bundle dir (`_MEIPASS`), the directory containing
    the frozen executable, and finally the system PATH.
    """
    candidates = [program]
    if os.name == "nt" and not program.lower().endswith(".exe"):
        candidates.append(f"{program}.exe")

    search_dirs = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            search_dirs.append(meipass)
        search_dirs.append(os.path.dirname(sys.executable))

    for directory in search_dirs:
        for candidate in candidates:
            full_path = os.path.join(directory, candidate)
            if os.path.isfile(full_path):
                return full_path

    return shutil.which(program)


def program_in_path(program: str) -> bool:
    """Checks whether `program` is available locally or in PATH"""
    return find_executable(program) is not None


def subprocess_kwargs(capture_output: bool = True) -> Dict[str, Any]:
    """Return platform-specific subprocess kwargs to avoid console flashes in windowed GUI apps."""
    kwargs: Dict[str, Any] = {}
    if (
        os.name == "nt"
        and hasattr(subprocess, "CREATE_NO_WINDOW")
        and (capture_output or sys.stdout is None)
    ):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


class CustomSSLContextHTTPAdapter(HTTPAdapter):
    """Transport adapter that allows us to use a custom SSLContext."""

    def __init__(self, ssl_context: SSLContext, **kwargs) -> None:
        self.ssl_context: SSLContext = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_context=self.ssl_context,
        )
