from egoassist.eval.real_metrics import exact_match, temporal_iou, token_f1


def test_text_metrics_normalize_articles_and_punctuation():
    assert exact_match("You held the phone.", "You held phone") == 1.0
    assert token_f1("turned off phone", "turned off the phone and put it away") > 0.5


def test_real_temporal_iou():
    assert temporal_iou(1, 5, 1, 5) == 1.0
    assert temporal_iou(0, 2, 3, 5) == 0.0
