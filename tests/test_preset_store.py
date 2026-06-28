import json

import pytest

from halcyon.preset_store import UserPresetStore, normalize_preset_name
from halcyon.synth_params import SynthPatch


def test_user_preset_store_saves_and_loads_patch(tmp_path) -> None:
    path = tmp_path / "synth-presets.json"
    patch = SynthPatch()
    patch.set_param("tone", 0.23)

    store = UserPresetStore(path=path)
    saved = store.save("Dusty Pad", patch.values)

    assert saved["tone"] == 0.23
    assert json.loads(path.read_text(encoding="utf-8"))["presets"]["Dusty Pad"]["tone"] == 0.23

    loaded = UserPresetStore.load(path)

    assert loaded.get("Dusty Pad")["tone"] == 0.23


def test_user_preset_names_cannot_shadow_builtins() -> None:
    with pytest.raises(ValueError, match="built-in preset"):
        normalize_preset_name("Lead")


def test_user_preset_store_ignores_invalid_file(tmp_path) -> None:
    path = tmp_path / "synth-presets.json"
    path.write_text("{bad json", encoding="utf-8")

    store = UserPresetStore.load(path)

    assert store.presets == {}
