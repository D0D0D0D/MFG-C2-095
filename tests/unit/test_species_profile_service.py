# MFG-C2-095 — Unit Tests: SpeciesProfileService (docs/02_design.md §(a))

import pytest

from src.services.species_profile_service import SpeciesProfileNotFoundError, SpeciesProfileService


class TestSpeciesProfileService:
    def test_known_species_codes_span_multiple_taxon_groups(self):
        """docs/02_design.md §(a): the agent must not assume fish-only scope."""
        service = SpeciesProfileService()
        codes = service.list_known_species_codes()
        assert set(codes) >= {"madai", "kuruma_ebi", "magaki", "nori"}

    @pytest.mark.parametrize(
        "species_code,expected_taxon",
        [
            ("madai", "fish"),
            ("kuruma_ebi", "crustacean"),
            ("magaki", "bivalve"),
            ("nori", "seaweed"),
        ],
    )
    def test_profile_taxon_group(self, species_code, expected_taxon):
        service = SpeciesProfileService()
        profile = service.get_profile(species_code)
        assert profile["taxon_group"] == expected_taxon
        assert "metrics" in profile
        assert "disease_triggers" in profile
        assert "input_types" in profile

    def test_unknown_species_code_raises(self):
        service = SpeciesProfileService()
        with pytest.raises(SpeciesProfileNotFoundError):
            service.get_profile("nonexistent_species")

    def test_path_traversal_is_defended(self):
        service = SpeciesProfileService()
        with pytest.raises(SpeciesProfileNotFoundError):
            service.get_profile("../../../etc/passwd")
