"""Helpers for the Kenneth French factor tests (T-077): a writable copy of the fixture cut from
the real files, with a manifest whose SHA-256s are recomputed from whatever is on disk."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "french_factors"
FF3 = "F-F_Research_Data_Factors_daily.csv"
MOM = "F-F_Momentum_Factor_daily.csv"


def write_manifest(directory: Path) -> None:
    """(Re)write ``manifest.json`` pinning the files now in *directory*."""
    files = {}
    for key, name in (("ff3", FF3), ("mom", MOM)):
        text = (directory / name).read_text()
        dates = [ln[:8] for ln in text.splitlines() if ln[:8].isdigit()]
        files[key] = {
            "path": name,
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            "first_date": f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:]}",
            "last_date": f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}",
        }
    manifest = {"library_version": "202608 CRSP", "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest))


def copy_fixture(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in (FF3, MOM):
        shutil.copy(FIXTURE_DIR / name, directory / name)
    write_manifest(directory)
    return directory


def replace_rows(directory: Path, name: str, *, after: str, fn: Callable[[str], str]) -> None:
    """Rewrite every data line of file *name* dated after *after* (``YYYYMMDD``) with ``fn(line)``."""
    path = directory / name
    out = []
    for line in path.read_text().splitlines():
        late = line[:8].isdigit() and line[:8] > after
        out.append(fn(line) if late else line)
    path.write_text("\n".join(out) + "\n")
