"""Config loader. There's just one global YAML now; variants live inside it."""
from __future__ import annotations
from pathlib import Path
import yaml

CONFIGS_DIR = Path(__file__).parent.parent / "configs"


def load_global() -> dict:
    return yaml.safe_load((CONFIGS_DIR / "_global.yaml").read_text())


def load_variants(cfg: dict | None = None) -> list[dict]:
    if cfg is None:
        cfg = load_global()
    return cfg.get("variants", [])


def variant_applies(variant: dict, scene: str, has_chat_msg: bool) -> bool:
    """Should this variant be rendered for a clip in the given scene?

    Note: YAML parses bare `on`/`off` (and `yes`/`no`/`true`/`false`) as
    booleans, so chat_overlay/music are stored as True/False, not strings.
    """
    apply_to = variant.get("apply_to", "both")
    if apply_to == "scene_a_only" and scene != "scene_a":
        return False
    if apply_to == "scene_b_only" and scene != "scene_b":
        return False
    if apply_to == "scene_c_only" and scene != "scene_c":
        return False
    if bool(variant.get("chat_overlay")) and not has_chat_msg:
        return False
    return True
