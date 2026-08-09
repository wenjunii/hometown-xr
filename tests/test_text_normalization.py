from text_normalization import (
    normalization_contract,
    normalize_extracted_text,
)


def test_normalization_repairs_entities_and_mojibake_without_changing_clean_text():
    damaged = "It\u00e2\u20ac\u2122s my Montr\u00c3\u00a9al home &amp; childhood."
    assert normalize_extracted_text(damaged) == "It\u2019s my Montr\u00e9al home & childhood."

    clean = "Eu cresci em S\u00e3o Paulo, perto da casa da minha fam\u00edlia."
    assert normalize_extracted_text(clean) == clean
    assert normalization_contract()["version"]
