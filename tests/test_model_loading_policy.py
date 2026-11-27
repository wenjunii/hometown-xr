import sys
from types import SimpleNamespace

from config import SEMANTIC_MODEL_NAME, SEMANTIC_MODEL_REVISION
from matcher import SemanticMatcher


def test_model_loading_requires_pinned_safetensors_without_remote_code(monkeypatch):
    captured = {}

    def fake_model(name, **kwargs):
        captured.update(name=name, **kwargs)
        return SimpleNamespace(encode=lambda *args, **kwargs: [])

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=fake_model, util=None,
    ))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False), backends=SimpleNamespace(),
    ))
    SemanticMatcher()
    assert captured["name"] == SEMANTIC_MODEL_NAME
    assert captured["revision"] == SEMANTIC_MODEL_REVISION
    assert captured["trust_remote_code"] is False
    assert captured["model_kwargs"] == {"use_safetensors": True, "attn_implementation": "eager"}
