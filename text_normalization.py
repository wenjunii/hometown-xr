"""Versioned cleanup for extracted text before matching and storage."""

from __future__ import annotations

from ftfy import TextFixerConfig, fix_text

from config import TEXT_NORMALIZATION_VERSION

_FIXER_CONFIG = TextFixerConfig(
    unescape_html=True,
    fix_encoding=True,
    fix_c1_controls=True,
    fix_latin_ligatures=True,
    fix_character_width=False,
    uncurl_quotes=False,
    normalization="NFC",
)


def normalize_extracted_text(text: str) -> str:
    """Repair entities and encoding damage without flattening paragraph boundaries."""
    if not text:
        return ""
    return fix_text(text, config=_FIXER_CONFIG)


def normalization_contract() -> dict[str, object]:
    """Return the matching-relevant cleanup contract for filter signatures."""
    return {
        "version": TEXT_NORMALIZATION_VERSION,
        "html_entities": True,
        "encoding_repair": True,
        "unicode_normalization": "NFC",
        "character_width_repair": False,
        "curly_quotes_preserved": True,
    }
