from concepts import concept_anchor_fidelity

ANCHOR = (
    "My grandmother used to tell me stories about our ancestors. "
    "Those stories made me proud of where my family comes from."
)


def test_specific_anchor_fidelity_requires_every_core_story_facet():
    passing = concept_anchor_fidelity(
        ANCHOR,
        "My grandmother told family stories about our ancestors and traditions.",
    )
    broad_similarity = concept_anchor_fidelity(
        ANCHOR,
        "I was proud of my heritage and remembered where my family came from.",
    )

    assert passing["passes"]
    assert passing["matched_facets"] == [
        "grandparent",
        "storytelling",
        "ancestry",
    ]
    assert not broad_similarity["passes"]
    assert broad_similarity["missing_facets"] == [
        "grandparent",
        "storytelling",
    ]


def test_unspecified_anchor_does_not_claim_literal_fidelity():
    result = concept_anchor_fidelity("A broad home-memory reference.", "Any text.")

    assert not result["evaluated"]
    assert result["passes"]
