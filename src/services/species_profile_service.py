"""AgentCore Platform v1.0"""

# Loads config/species/{species_code}.yaml — the taxon-agnostic dictionary
# that Stage 1/2 use instead of hardcoded enums (docs/02_design.md §(a)).
# Pure domain service: no agenticstar imports, no side effects beyond
# reading local config files.

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "species")


class SpeciesProfileNotFoundError(Exception):
    """Raised when a species_code has no corresponding profile file."""


@lru_cache(maxsize=64)
def _load_yaml(species_code: str) -> dict[str, Any]:
    safe_code = os.path.basename(species_code)  # defend against path traversal
    path = os.path.join(_CONFIG_DIR, f"{safe_code}.yaml")
    if not os.path.exists(path):
        raise SpeciesProfileNotFoundError(f"No species profile for species_code={species_code!r}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


class SpeciesProfileService:
    """Domain service: resolve a species_code to its full profile dict."""

    def get_profile(self, species_code: str) -> dict[str, Any]:
        """Return the full species profile dict for species_code.

        Raises SpeciesProfileNotFoundError if no profile file exists —
        callers (pre_process) treat this as a validation failure, not a
        silent pass-through (species_code is part of the tenant/context
        resolution, same fail-closed posture as farm_id).
        """
        return dict(_load_yaml(species_code))

    def list_known_species_codes(self) -> list[str]:
        if not os.path.isdir(_CONFIG_DIR):
            return []
        return sorted(
            fname[: -len(".yaml")] for fname in os.listdir(_CONFIG_DIR) if fname.endswith(".yaml")
        )
