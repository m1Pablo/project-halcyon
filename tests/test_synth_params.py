import math

import pytest

from halcyon.synth_params import PARAM_BY_ID, PRESETS, SynthPatch, normalized_to_dsp_patch


def test_default_patch_matches_rust_parameter_names() -> None:
    patch = SynthPatch()

    assert patch.dsp_patch() == pytest.approx(
        {
            "master_gain": 0.24,
            "cutoff_hz": 3600.0,
            "saw_square_blend": 0.45,
            "attack_seconds": 0.006,
            "decay_seconds": 0.12,
            "sustain_level": 0.72,
            "release_seconds": 0.25,
            "drive": 1.0,
        }
    )


def test_tone_uses_logarithmic_mapping() -> None:
    tone = PARAM_BY_ID["tone"]
    midpoint = tone.dsp_from_normalized(0.5)
    linear_midpoint = (tone.minimum + tone.maximum) / 2

    assert midpoint < linear_midpoint
    assert math.isclose(tone.normalized_from_dsp(midpoint), 0.5, rel_tol=1e-6)


def test_patch_clamps_and_serializes_preset_values() -> None:
    patch = SynthPatch()
    dsp_values = patch.apply_preset("Lead")

    assert set(dsp_values) == {
        "master_gain",
        "cutoff_hz",
        "saw_square_blend",
        "attack_seconds",
        "decay_seconds",
        "sustain_level",
        "release_seconds",
        "drive",
    }
    assert patch.preset_name == "Lead"
    assert patch.values == PRESETS["Lead"]

    single = patch.set_param("volume", 10.0)
    assert single == {"master_gain": 1.0}
    assert patch.values["volume"] == 1.0
    assert patch.preset_name is None


def test_unknown_param_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown synth parameter"):
        normalized_to_dsp_patch({"brightness": 0.5})
