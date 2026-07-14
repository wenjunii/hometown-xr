from language_detector import LanguageDetector


class FakeModel:
    def __init__(self, language, confidence):
        self.language = language
        self.confidence = confidence

    def predict(self, text, k=1):
        assert text
        assert k == 1
        return [f"__label__{self.language}"], [self.confidence]


def test_low_confidence_language_routes_to_unknown():
    detector = LanguageDetector(threshold=0.5, model=FakeModel("en", 0.49))
    assert detector.detect("Some ambiguous text") == ("unknown", 0.49)


def test_confident_language_is_preserved():
    detector = LanguageDetector(threshold=0.5, model=FakeModel("es", 0.9))
    assert detector.detect("Mi hogar") == ("es", 0.9)
