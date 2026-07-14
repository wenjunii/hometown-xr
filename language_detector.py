"""FastText language detection with confidence-based unknown routing."""

from __future__ import annotations

import logging
import os
import urllib.request

from config import (
    FASTTEXT_MODEL_PATH,
    FASTTEXT_MODEL_URL,
    LANG_DETECT_CHARS,
    LANG_DETECTION_THRESHOLD,
)

logger = logging.getLogger(__name__)


class LanguageDetector:
    def __init__(
        self,
        threshold: float = LANG_DETECTION_THRESHOLD,
        model=None,
    ):
        self.threshold = threshold
        if model is not None:
            self.model = model
            return

        import fasttext

        fasttext.FastText.eprint = lambda _message: None
        self._ensure_model()
        logger.info("Loading FastText language model from %s", FASTTEXT_MODEL_PATH)
        self.model = fasttext.load_model(str(FASTTEXT_MODEL_PATH))

    def _ensure_model(self) -> None:
        if FASTTEXT_MODEL_PATH.exists():
            return

        logger.info("Downloading FastText language model to %s", FASTTEXT_MODEL_PATH)
        FASTTEXT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = FASTTEXT_MODEL_PATH.with_suffix(".bin.part")
        try:
            urllib.request.urlretrieve(FASTTEXT_MODEL_URL, str(temporary_path))
            os.replace(temporary_path, FASTTEXT_MODEL_PATH)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def detect(self, text: str) -> tuple[str, float]:
        clean = text[:LANG_DETECT_CHARS].replace("\n", " ").strip()
        if not clean:
            return "unknown", 0.0

        labels, probabilities = self.model.predict(clean, k=1)
        label = labels[0].replace("__label__", "")
        confidence = float(probabilities[0])
        if confidence < self.threshold:
            return "unknown", confidence
        return label, confidence
