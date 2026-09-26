"""Content intelligence: languages, keywords and size without a model; topics and tone with one, checked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wintergrab.data import Analyze, Classify, Pipeline
from wintergrab.errors import ConfigurationError
from wintergrab.intel.content import LANGUAGES, analyze_text, classify_text, detect_language

SAMPLES = json.loads((Path(__file__).parent / "data" / "languages.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("topic", ["news", "product"])
def test_every_language_of_the_samples_is_named(topic: str) -> None:
    texts = SAMPLES[topic]
    named = {language: detect_language(text)[0] for language, text in texts.items()}
    assert named == {language: language for language in texts}  # every one, none mistaken
    assert set(texts) <= set(LANGUAGES)


def test_what_is_too_short_or_mixed_to_tell() -> None:
    for text in ["OK", "Hello world", "SKU 12345 ABC-99", "iPhone 15 Pro Max 256 GB Titanium", "Le Monde", "Die Hard",
                 "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
                 "Ω", "5 Ω resistor, αβ grade",  # a symbol; Greek letters among a few English words
                 "東京", "小米手机",  # a few Chinese characters: as well Japanese
                 "台灣の味"]:  # fmt: skip
        assert detect_language(text) == (None, 0.0, None), text
    # a name in katakana, quoted in Chinese, is not Japanese: Japanese writes its particles in hiragana
    assert detect_language("这款手机采用了ポケモン主题设计，售价为两千元左右。")[0] == "zh"
    assert detect_language("ポケモンセンター")[0] == "ja"  # katakana alone
    language, confidence, runner_up = detect_language(SAMPLES["news"]["da"])
    assert (language, runner_up) == ("da", "nb") and 0.5 < confidence < 0.99  # close languages: less sure
    assert detect_language(SAMPLES["news"]["en"])[1] > detect_language(SAMPLES["news"]["da"])[1]


def test_what_a_text_says_of_itself() -> None:
    text = (
        "Solar panels on the roof of the station now cover half of its needs. Solar panels are cheaper than they "
        "were, and the station will add more solar panels next year. The mayor said the panels paid for themselves!"
    )
    analysis = analyze_text(text)
    assert analysis.language == "en" and analysis.script == "Latin" and analysis.sentences == 3
    assert analysis.words == 39 and analysis.reading_minutes == round(39 / 230, 1)
    assert analysis.keywords[0] == "solar panels" and "station" in analysis.keywords
    assert "the" not in analysis.keywords and "panels" not in analysis.keywords  # inside the phrase kept
    chinese = analyze_text(SAMPLES["news"]["zh"])  # written without spaces: no words to count
    assert (chinese.words, chinese.reading_minutes, chinese.keywords) == (None, None, [])
    assert chinese.characters == len("".join(SAMPLES["news"]["zh"].split())) and chinese.script == "Han"
    assert analyze_text(SAMPLES["news"]["ja"]).sentences == 1 and analyze_text(SAMPLES["news"]["th"]).words is None


class Model:
    """A stand-in model provider: answers what it is told to."""

    def __init__(self, answer: object) -> None:
        self.answer, self.prompts = answer, []

    def complete(self, prompt: str, **options: object) -> str:
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


def test_labels_from_a_model_are_checked() -> None:
    text = "ETH Zürich and the city of Basel opened a new solar research centre, which scientists welcomed."
    model = Model({"topic": "solar research", "category": "Science", "sentiment": "Positive",
                   "entities": [{"name": "ETH Zürich", "type": "organization"}, {"name": "Basel", "type": "place"},
                                {"name": "Geneva", "type": "place"}]})  # fmt: skip
    labels = classify_text(text, model, categories=["politics", "sport", "science"])
    assert (labels.topic, labels.category, labels.sentiment) == ("solar research", "science", "positive")
    assert [e["name"] for e in labels.entities] == ["ETH Zürich", "Basel"]
    assert labels.dropped == ["entity 'Geneva': not in the text"]  # a name the text does not contain
    assert '"category": one of "politics", "sport", "science"' in model.prompts[0]
    wrong = classify_text(text, Model({"category": "weather", "sentiment": "ecstatic"}), categories=["sport"])
    assert wrong.category is None and wrong.sentiment is None and len(wrong.dropped) == 2


def test_the_stages() -> None:
    records = [{"body": SAMPLES["news"]["fr"]}, {"body": ["Ein", "Text"]}, {"title": "no body"}]
    out = Pipeline([Analyze("body", add=["language", "words"], prefix="body_")]).run(records)
    assert out[0]["body_language"] == "fr" and out[0]["body_words"] > 20
    assert out[1]["body_language"] is None and "body_words" not in out[2]  # too short; no text at all
    with pytest.raises(ConfigurationError, match="unknown feature"):
        Analyze("body", add=["mood"])
    model = Model({"topic": "budget", "sentiment": "neutral", "entities": []})
    labelled = Pipeline([Classify("body", model, add=["topic", "sentiment"])]).run(
        [{"body": "The council passed the budget."}]
    )
    assert labelled[0]["topic"] == "budget" and labelled[0]["sentiment"] == "neutral"
    failing = Classify("body", Model(RuntimeError("the model is down")))
    assert failing.process({"body": "x y z"}) == {"body": "x y z"} and failing.stats["errors"] == 1  # kept, counted
    stage = Pipeline.from_config({"stages": [{"analyze": {"field": "body", "add": ["language"]}}]})
    assert stage.run([{"body": SAMPLES["news"]["es"]}])[0]["language"] == "es"


def test_the_analyze_command(tmp_path, capsys) -> None:
    from wintergrab.cli import main

    source = tmp_path / "articles.jsonl"
    source.write_text("\n".join(json.dumps({"body": text}) for text in SAMPLES["news"].values()), encoding="utf-8")
    out = tmp_path / "analyzed.jsonl"
    assert main(["data", "analyze", str(source), "--field", "body", "-o", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["language"] for r in rows] == list(SAMPLES["news"])
    printed = capsys.readouterr().err
    assert "34 record(s)" in printed and "languages:" in printed
