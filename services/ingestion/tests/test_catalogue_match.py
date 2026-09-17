"""Tests for the deterministic BoQ-line -> catalogue-item matcher
(thaqip_ingestion.catalogue_match), covering PRD test T-MATCH-01.
"""
from __future__ import annotations

from thaqip_ingestion.catalogue_match import CatalogueItemLike, suggest_match

DOOR_90 = CatalogueItemLike(1, "door-fire-90min-single", "باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", "EA")
DOOR_60 = CatalogueItemLike(2, "door-fire-60min-single", "باب حديد مقاوم للحريق ضلفة واحدة 60 دقيقة", "EA")
DOOR_90_DOUBLE = CatalogueItemLike(3, "door-fire-90min-double", "باب حديد مقاوم للحريق ضلفتين 90 دقيقة", "EA")
CABLE = CatalogueItemLike(4, "cable-50mm", "كابل كهرباء نحاس 50 مم2", "M")
PAINT = CatalogueItemLike(5, "paint-interior", "دهانات داخلية للحوائط والأسقف", "M2")


def test_strong_match_returns_two_candidates_with_kind_suggested():
    result = suggest_match(
        "توريد وتركيب باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", None, "EA",
        [DOOR_90, DOOR_60, DOOR_90_DOUBLE, CABLE],
    )
    assert result is not None
    assert len(result) == 2
    assert all(c.kind == "suggested" for c in result)
    assert result[0].catalogue_item_id == DOOR_90.id
    assert result[0].score >= result[1].score


def test_unit_mismatch_excludes_candidate():
    # A cable-unit item must never be suggested for an EA-unit line, however
    # textually similar the description happens to be.
    result = suggest_match("كابل كهرباء نحاس", None, "EA", [CABLE, DOOR_90])
    assert result is None  # only 1 EA-unit item (DOOR_90) exists -> can't show 2


def test_fewer_than_two_unit_compatible_items_yields_no_suggestion():
    result = suggest_match("باب حديد مقاوم للحريق", None, "EA", [DOOR_90])
    assert result is None


def test_empty_catalogue_yields_no_suggestion():
    result = suggest_match("باب حديد مقاوم للحريق", None, "EA", [])
    assert result is None


def test_weak_similarity_below_floor_yields_no_suggestion():
    result = suggest_match(
        "توريد أنابيب مياه PVC قطر 4 بوصة", None, "EA", [DOOR_90, DOOR_60],
    )
    assert result is None


def test_capacity_difference_still_distinguishes_similar_items():
    # 60-minute and 90-minute fire doors share almost all vocabulary; the
    # matcher must still rank the exact capacity match first.
    result = suggest_match(
        "توريد وتركيب باب حديد مقاوم للحريق ضلفة واحدة 60 دقيقة", None, "EA",
        [DOOR_90, DOOR_60, DOOR_90_DOUBLE],
    )
    assert result is not None
    assert result[0].catalogue_item_id == DOOR_60.id


def test_missing_description_and_unit_yields_no_suggestion():
    assert suggest_match("", None, "EA", [DOOR_90, DOOR_60]) is None
    assert suggest_match("باب حديد", None, "", [DOOR_90, DOOR_60]) is None
    assert suggest_match("باب حديد", None, None, [DOOR_90, DOOR_60]) is None


def test_unrelated_items_never_forced_into_a_suggestion():
    result = suggest_match("توريد وتركيب باب حديد مقاوم للحريق", None, "M2", [PAINT])
    assert result is None  # only 1 M2 item exists


def test_unit_normalization_is_case_and_whitespace_insensitive():
    item = CatalogueItemLike(9, "x", "باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", " ea ")
    other = CatalogueItemLike(10, "y", "باب حديد مقاوم للحريق ضلفة واحدة 60 دقيقة", "EA")
    result = suggest_match("باب حديد مقاوم للحريق ضلفة واحدة 90 دقيقة", None, "EA", [item, other])
    assert result is not None
