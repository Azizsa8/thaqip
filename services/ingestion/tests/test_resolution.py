from thaqip_ingestion.resolution import match_key, normalize_ar


def test_hamza_and_taa_marbuta_unify():
    assert normalize_ar("وزارة الصحّة") == normalize_ar("وزاره الصحه")
    assert normalize_ar("أمانة منطقة الرياض") == normalize_ar("امانة منطقة الرياض")


def test_diacritics_and_tatweel_stripped():
    assert normalize_ar("شـركة النُور") == normalize_ar("شركة النور")


def test_match_key_drops_legal_prefix():
    assert match_key("شركة بن سواد للتجارة") == match_key("مؤسسة بن سواد للتجارة")
    assert match_key("شركة بن سواد للتجارة") != match_key("شركة رؤية النخبه للتجارة")


def test_distinct_entities_stay_distinct():
    assert normalize_ar("وزارة الصحة") != normalize_ar("وزارة التعليم")
