"""Verify schema-backed documentation stays synchronized with codebase registries."""

from __future__ import annotations

from pathlib import Path
import re

from audiobookdl.options_schema import (
    CONFIG_OPTION_SPECS,
    GUI_CONFIG_KEYS,
    SOURCE_CONFIG_KEYS,
)
from audiobookdl.sources import PLUGIN_API_VERSION
from audiobookdl.utils.enrichment import ENRICHMENT_ENTRY_POINT_GROUP

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_backtick_tokens(markdown: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z0-9_.:-]+)`", markdown))


def test_configuration_docs_cover_all_schema_keys():
    cfg_doc = (_REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented_tokens = _extract_backtick_tokens(cfg_doc)

    expected_config_keys = {spec.config_key for spec in CONFIG_OPTION_SPECS if spec.config_key}
    missing_top_level = expected_config_keys - documented_tokens
    assert not missing_top_level, f"Undocumented CONFIG_OPTION_SPECS keys in docs/configuration.md: {sorted(missing_top_level)}"

    missing_gui_keys = set(GUI_CONFIG_KEYS) - documented_tokens
    assert not missing_gui_keys, f"Undocumented GUI_CONFIG_KEYS in docs/configuration.md: {sorted(missing_gui_keys)}"

    missing_source_keys = set(SOURCE_CONFIG_KEYS) - documented_tokens
    assert not missing_source_keys, f"Undocumented SOURCE_CONFIG_KEYS in docs/configuration.md: {sorted(missing_source_keys)}"


def test_plugin_and_community_docs_exist_and_reference_current_contracts():
    plugins_doc = (_REPO_ROOT / "docs" / "plugins.md").read_text(encoding="utf-8")
    assert "audiobookdl.sources" in plugins_doc
    assert ENRICHMENT_ENTRY_POINT_GROUP in plugins_doc
    assert f"PLUGIN_API_VERSION = {PLUGIN_API_VERSION}" in plugins_doc
    assert "get_or_create_device_id" in plugins_doc
    assert "fetch_cover_safe" in plugins_doc

    for rel_path in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        doc_path = _REPO_ROOT / rel_path
        assert doc_path.is_file(), f"Missing repository document: {rel_path}"
        assert doc_path.read_text(encoding="utf-8").strip() != ""
