from egoassist.backends.real import _truncated_generation, _valid_generation


def test_cloud_backend_does_not_load_clip_for_captioning(monkeypatch):
    from types import SimpleNamespace
    from egoassist.backends import real

    def forbidden(**kwargs):
        raise AssertionError('Caption-only API must not initialize CLIP')

    monkeypatch.setattr(real, 'ClipEncoder', forbidden)
    backend = real.DashScopeQwenBackend(api_key='test-only')
    backend.client = SimpleNamespace(call=lambda **kwargs: SimpleNamespace(
        status_code=200, usage={}, output={'choices': [{'message': {'content': [{'text': 'A person at a desk.'}]}}]}))
    assert backend.caption([]) == 'A person at a desk.'
    assert backend._encoder is None


def test_valid_generation_accepts_normal_answer():
    assert _valid_generation("You were holding a phone.\nCONFIDENCE: 0.84")


def test_valid_generation_rejects_repeated_punctuation():
    assert not _valid_generation("!" * 192)


def test_valid_generation_rejects_empty_confidence_only_output():
    assert not _valid_generation("CONFIDENCE: 0.50")


def test_truncated_generation_detects_incomplete_confidence_suffix():
    assert _truncated_generation("A long otherwise valid answer.\nCONF")
    assert _truncated_generation("A long otherwise valid answer.\nCONFIDENCE:")
    assert not _truncated_generation("A complete answer.\nCONFIDENCE: 0.82")
