"""Named, reusable version-constraint profiles for ``quant`` (T-093).

A profile file is TOML; each ``[profiles.<name>]`` table sets any of the constraint keys
below, in the same grammar as the matching flag::

    [profiles.baseline]
    metrics = "metrics-v2"
    returns = "qret-v2"

    [profiles.pre_fix]
    metrics = "valuation=metrics-v1"
    returns = ">=qret-v1,!=qret-v3"
    risk_model = "latest"

``--version-profile FILE`` loads it, ``--profile NAME`` picks one (optional when the file
holds exactly one). A flag given on the command line wins over the same key in the profile.
Each command uses the keys for the inputs it reads and ignores the rest: ``build-risk-model``
reads ``metrics`` and ``returns``, ``optimize`` also ``risk_model``, ``build-returns`` reads
``corpact``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from kg_schema.versions import (
    VersionError,
    engine_version_key,
    parse_constraint,
    parse_metric_constraints,
)
from quant.manifest import ENGINE_FAMILIES

# profile key -> the QuantSettings field it fills
PROFILE_KEYS: dict[str, str] = {
    "metrics": "metrics_version",
    "returns": "returns_version",
    "risk_model": "risk_model_select",
    "corpact": "corpact_version",
}


@dataclass(frozen=True)
class VersionProfile:
    name: str
    path: Path
    constraints: dict[str, str]  # profile key -> constraint text

    @property
    def label(self) -> str:
        return f"{self.name} ({self.path})"

    def settings_updates(self) -> dict[str, str]:
        return {PROFILE_KEYS[k]: v for k, v in self.constraints.items()}


def load_profile(path: str | Path, name: str | None = None) -> VersionProfile:
    """Read *path* and return profile *name*, validating every constraint now.

    Raises :class:`VersionError` for an unreadable file, a missing or ambiguous profile, an
    unknown key, or a constraint that does not parse."""
    file = Path(path).expanduser()
    profiles = _read_profiles(file)
    name = _pick_name(file, profiles, name)
    table = profiles[name]
    if not isinstance(table, dict):
        raise VersionError(f"profile {name!r} in {file} must be a table")
    unknown = sorted(set(table) - set(PROFILE_KEYS))
    if unknown:
        raise VersionError(
            f"profile {name!r} in {file} has unknown key(s) {unknown}; known: {list(PROFILE_KEYS)}"
        )
    constraints: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str):
            raise VersionError(f"profile {name!r} key {key!r} must be a string, got {value!r}")
        _validate(key, value)
        constraints[key] = value
    return VersionProfile(name=name, path=file, constraints=constraints)


def _read_profiles(file: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(file.read_text())
    except OSError as exc:
        raise VersionError(f"cannot read version profile {file}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise VersionError(f"version profile {file} is not valid TOML: {exc}") from exc
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise VersionError(f"version profile {file} defines no [profiles.<name>] table")
    return profiles


def _pick_name(file: Path, profiles: dict[str, object], name: str | None) -> str:
    if name is None:
        if len(profiles) != 1:
            raise VersionError(
                f"version profile {file} defines {len(profiles)} profiles "
                f"({', '.join(sorted(profiles))}); pick one with --profile NAME"
            )
        return next(iter(profiles))
    if name not in profiles:
        raise VersionError(
            f"profile {name!r} is not in {file}; it defines: {', '.join(sorted(profiles))}"
        )
    return name


def _validate(key: str, text: str) -> None:
    if key == "metrics":
        parse_metric_constraints(text)
    else:
        parse_constraint(text, key=engine_version_key(ENGINE_FAMILIES[key]))
