"""Content intelligence: a text's language, its keywords and length, and, with a model, its topic and tone.

::

    analysis = analyze_text(article["body"])
    analysis.language, analysis.language_confidence    # "de", 0.96
    analysis.keywords                                  # ["solarzellen", "wirkungsgrad", ...]
    analysis.words, analysis.reading_minutes           # 812, 3.5

    labels = classify_text(article["body"], model, categories=["politics", "sport", "science"])
    labels.category, labels.sentiment, labels.entities # "science", "positive", [{"name": "ETH Zürich", ...}]

Without a model, nothing leaves the machine:

* **language**: from the text's script where a script is one language's
  (Greek, Hebrew, Thai, Korean...), from the letters where a script is
  shared (Persian and Urdu letters in Arabic script; hiragana for Japanese,
  ten Chinese characters without it for Chinese, ``zh`` whichever characters
  it is written in), and from the commonest words of 25 languages otherwise:
  in Latin script English, French, German, Spanish, Italian, Portuguese,
  Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Czech, Slovak,
  Hungarian, Romanian, Turkish, Indonesian, Vietnamese and Catalan; in
  Cyrillic Russian, Ukrainian and Bulgarian; in Devanagari Hindi and Marathi.
  A text too short to tell (fewer than three letters of a script, five
  words, or ten Chinese characters), one whose letters are not mostly of one
  script, or one whose words are as much another language's gives no
  language (``None``), not a guess.
* **keywords**: the words and short phrases that stand out, the language's
  common words aside;
* **size**: words, sentences, characters, and reading time at 230 words a
  minute. A text written without spaces between words (Chinese, Japanese,
  Thai...) gets no word count, reading time or keywords, as its words cannot
  be told apart without a dictionary; its characters are counted.

With a model (:mod:`wintergrab.models`, optional), :func:`classify_text` asks for
a topic, a category from a list you give, a sentiment and the entities named,
and checks the answer: a category that is not one of yours, a sentiment
that is not ``positive``, ``negative``, ``neutral`` or ``mixed``, and an entity
the text does not contain are left out.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

__all__ = ["LANGUAGES", "TextAnalysis", "TextLabels", "analyze_text", "classify_text", "detect_language"]

#: Words a minute, for reading time.
READING_SPEED = 230
_SENTENCE_END = re.compile("[.!?\u3002\uff01\uff1f]+(?=\\s|$)|[\u3002\uff01\uff1f]")  # and CJK full stops
_MIN_WORDS = 5  # fewer tell no language apart
_MIN_LETTERS = 3  # a script's letters needed to name its language ("\u03a9" or "\u03c0" alone names none)
_MIN_HAN = 10  # Chinese characters, without kana, needed to name Chinese: fewer could as well be Japanese
_HIRAGANA_SHARE = 0.1  # hiragana among Japanese characters (its particles are in every sentence); katakana
# alone may be a name quoted in Chinese ("\u30dd\u30b1\u30e2\u30f3")
_MIN_MATCHES = 3  # a language's common words needed in a text to name it
_MIN_SHARE = 0.06  # a language's common words, weighted, must be at least this share of the words
_MARGIN = 1.15  # ... and this many times the next language's

# The commonest words of each language (function words: articles, pronouns, prepositions, conjunctions,
# auxiliaries), including those that tell close languages apart.
_COMMON: dict[str, list[str]] = {
    language: words
    for language, words in json.loads((files("wintergrab.intel") / "common_words.json").read_text("utf-8")).items()
    if not language.startswith("_")
}
_COMMON_SETS = {language: frozenset(words) for language, words in _COMMON.items()}
_GROUPS = {
    "LATIN": tuple(c for c in _COMMON if c not in ("ru", "uk", "bg", "hi", "mr")),
    "CYRILLIC": ("ru", "uk", "bg"),
    "DEVANAGARI": ("hi", "mr"),
}
# A word counts for a language by how few of its script's languages share it: "the" (English only) counts 1,
# "de" (in eight languages) an eighth.
_WEIGHTS = {
    script: {
        language: {w: 1 / sum(1 for other in languages if w in _COMMON_SETS[other]) for w in _COMMON_SETS[language]}
        for language in languages
    }
    for script, languages in _GROUPS.items()
}
#: Every language :func:`detect_language` can name.
_BY_SCRIPT = ("ar", "fa", "ur", "el", "he", "th", "ko", "ja", "zh", "ka", "hy", "bn", "ta", "te", "kn", "ml", "gu",
              "pa", "km", "lo", "my", "si", "am")  # fmt: skip
LANGUAGES = tuple(sorted({*_COMMON, *_BY_SCRIPT}))
# Scripts that are one language's (for the languages named here).
_SCRIPT_LANGUAGE = {"GREEK": "el", "HEBREW": "he", "THAI": "th", "HANGUL": "ko", "GEORGIAN": "ka", "ARMENIAN": "hy",
                    "BENGALI": "bn", "TAMIL": "ta", "TELUGU": "te", "KANNADA": "kn", "MALAYALAM": "ml",
                    "GUJARATI": "gu", "GURMUKHI": "pa", "KHMER": "km", "LAO": "lo", "MYANMAR": "my", "SINHALA": "si",
                    "ETHIOPIC": "am"}  # fmt: skip
_SCRIPT_WORDS = {"LATIN": "Latin", "CYRILLIC": "Cyrillic", "ARABIC": "Arabic", "DEVANAGARI": "Devanagari",
                 "CJK": "Han", "HIRAGANA": "Kana", "KATAKANA": "Kana"}  # fmt: skip
_URDU_LETTERS = frozenset("ٹڈڑںےۓھ")
_PERSIAN_LETTERS = frozenset("پچژگ")
# Scripts written without spaces between words: their words cannot be counted without a dictionary.
_NO_SPACES = frozenset({"CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR"})


@dataclass
class TextAnalysis:
    """What :func:`analyze_text` found.

    Attributes:
        language: An ISO 639-1 code, or ``None`` when it cannot be told.
        language_confidence: How clearly (0 to 1).
        runner_up: The next likeliest language, when words decided.
        script: The script most letters are in (``"Latin"``, ``"Cyrillic"``, ``"Han"``...).
        words: How many; ``None`` for a text written without spaces between words (Chinese,
            Japanese, Thai...), whose words cannot be told apart without a dictionary.
        sentences: How many.
        characters: How many, spaces aside.
        reading_minutes: At :data:`READING_SPEED` words a minute; ``None`` when ``words`` is.
        keywords: The words and phrases that stand out, most first (none when ``words`` is ``None``).
    """

    language: str | None
    language_confidence: float
    script: str | None
    words: int | None
    sentences: int
    reading_minutes: float | None
    keywords: list[str] = field(default_factory=list)
    runner_up: str | None = None
    characters: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TextLabels:
    """What :func:`classify_text` got from a model, checked.

    Attributes:
        topic: A few words.
        category: One of the categories given (``None`` when the answer was none of them).
        sentiment: ``"positive"``, ``"negative"``, ``"neutral"`` or ``"mixed"``.
        entities: ``[{"name", "type"}]``, each found in the text.
        dropped: What the model said that was left out, and why.
    """

    topic: str | None = None
    category: str | None = None
    sentiment: str | None = None
    entities: list[dict[str, str]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------------------------- #
# language
# ---------------------------------------------------------------------------------------------- #
def detect_language(text: str) -> tuple[str | None, float, str | None]:
    """``(language, confidence, runner_up)`` of ``text`` (see the module docs); ``(None, 0.0, None)``
    when it cannot be told."""
    counts, letters = _scripts(text)
    total = sum(counts.values())
    han, hiragana, katakana = counts["CJK"], counts["HIRAGANA"], counts["KATAKANA"]
    if han + hiragana + katakana > total / 2:  # Chinese characters, and the kana Japanese writes with them
        share = (han + hiragana + katakana) / total
        if (hiragana >= _MIN_LETTERS and hiragana >= _HIRAGANA_SHARE * (han + hiragana + katakana)) or (
            katakana >= _MIN_LETTERS and katakana > han + hiragana
        ):
            return "ja", _sure(0.99, share), None
        if han >= _MIN_HAN:
            return "zh", _sure(0.95, share), "ja"
        return None, 0.0, None
    if not total:
        return None, 0.0, None
    script, count = counts.most_common(1)[0]
    share = count / total
    if count < _MIN_LETTERS or share <= 0.5:  # a symbol, or letters of several scripts alike
        return None, 0.0, None
    if script in _SCRIPT_LANGUAGE:
        return _SCRIPT_LANGUAGE[script], _sure(0.99, share), None
    if script == "ARABIC":
        chars = set(letters)
        if chars & _URDU_LETTERS:
            return "ur", _sure(0.9, share), "fa"
        if chars & _PERSIAN_LETTERS:
            return "fa", _sure(0.9, share), "ar"
        return "ar", _sure(0.85, share), "fa"
    weights = _WEIGHTS.get(script)
    if not weights:
        return None, 0.0, None
    words = [w.casefold() for w in _words(text)]
    if len(words) < _MIN_WORDS:
        return None, 0.0, None
    scores = sorted(((sum(table.get(w, 0.0) for w in words) / len(words), c) for c, table in weights.items()),
                    reverse=True)  # fmt: skip
    (best, language), (second, runner_up) = scores[0], scores[1]
    matched = sum(1 for w in words if w in _COMMON_SETS[language])
    if best < _MIN_SHARE or best < second * _MARGIN or matched < _MIN_MATCHES:
        return None, 0.0, None
    separation = (best - second) / best  # 1: no other language's words at all
    evidence = min(1.0, matched / 10)  # ten common words are plenty
    return language, round(min(0.99, 0.4 + 0.35 * separation + 0.25 * evidence), 3), runner_up


def _words(text: str) -> list[str]:
    """The words of ``text``: runs between spaces, without the punctuation around them (a word keeps its
    combining marks: Devanagari vowel signs), and not numbers."""
    out = []
    for token in text.split():
        start, end = 0, len(token)
        while start < end and unicodedata.category(token[start])[0] not in "LMN":
            start += 1
        while end > start and unicodedata.category(token[end - 1])[0] not in "LMN":
            end -= 1
        word = token[start:end]
        if word and any(unicodedata.category(c)[0] == "L" for c in word):
            out.append(word)
    return out


def _sure(confidence: float, share: float) -> float:
    """A script's ``confidence``, less as less of the text is in that script (``share``)."""
    return round(confidence * (0.5 + 0.5 * share), 3)


def _scripts(text: str) -> tuple[Counter[str], str]:
    """How many of ``text``'s letters (the first 5,000) are in each script, and those letters."""
    counts: Counter[str] = Counter()
    letters = []
    for char in text:
        if not char.isalpha():
            continue
        letters.append(char)
        if len(letters) > 5000:
            break
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        counts[_script_of(name)] += 1
    return counts, "".join(letters)


def _script(text: str) -> str | None:
    """The script most of ``text``'s letters are in."""
    counts, _ = _scripts(text)
    return counts.most_common(1)[0][0] if counts else None


def _script_of(name: str) -> str:
    """The script of a character, from its Unicode name ("LATIN SMALL LETTER A": "LATIN")."""
    first = name.split(" ", 1)[0]
    if (first == "HALFWIDTH" and "KATAKANA" in name) or first == "KATAKANA-HIRAGANA":  # the long vowel mark
        return "KATAKANA"
    return "CJK" if first == "IDEOGRAPHIC" else first  # the iteration mark of Chinese and Japanese


# ---------------------------------------------------------------------------------------------- #
# analysis
# ---------------------------------------------------------------------------------------------- #
def analyze_text(text: str, *, keywords: int = 8) -> TextAnalysis:
    """The language, size and keywords of ``text`` (see the module docs)."""
    language, confidence, runner_up = detect_language(text)
    script = _script(text)
    sentences = len([s for s in _SENTENCE_END.split(text) if s.strip()])
    spaced = script not in _NO_SPACES
    words = _words(text) if spaced else []
    return TextAnalysis(
        language=language,
        language_confidence=confidence,
        runner_up=runner_up,
        script=_SCRIPT_WORDS.get(script, script.title()) if script else None,
        words=len(words) if spaced else None,
        sentences=sentences,
        characters=sum(1 for c in text if not c.isspace()),
        reading_minutes=round(len(words) / READING_SPEED, 1) if spaced else None,
        keywords=_keywords(words, language, keywords) if spaced else [],
    )


def _keywords(words: Sequence[str], language: str | None, top: int) -> list[str]:
    """The words and phrases (runs of up to three words between common words) that recur most."""
    common = _COMMON_SETS.get(language or "", frozenset()) | _COMMON_SETS["en"] if language else _COMMON_SETS["en"]
    folded = [w.casefold() for w in words]
    phrases: Counter[str] = Counter()
    singles: Counter[str] = Counter()
    run: list[str] = []
    for word in [*folded, ""]:
        if word and word not in common and len(word) > 2:
            run.append(word)
            singles[word] += 1
            continue
        for size in (2, 3):
            for i in range(len(run) - size + 1):
                phrases[" ".join(run[i : i + size])] += 1
        run = []
    ranked: list[tuple[float, str]] = []
    for phrase, count in phrases.items():
        if count >= 2:
            ranked.append((count * len(phrase.split()) * 1.5, phrase))
    for word, count in singles.items():
        if count >= 2 or len(folded) < 60:
            ranked.append((float(count), word))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[str] = []
    for _, term in ranked:
        if any(f" {term} " in f" {kept} " or f" {kept} " in f" {term} " for kept in chosen):
            continue  # a word of a phrase kept, or a phrase around a word kept
        chosen.append(term)
        if len(chosen) >= top:
            break
    return chosen


# ---------------------------------------------------------------------------------------------- #
# with a model
# ---------------------------------------------------------------------------------------------- #
_SENTIMENTS = ("positive", "negative", "neutral", "mixed")


def classify_text(
    text: str,
    model: Any,
    *,
    categories: Sequence[str] | None = None,
    entities: bool = True,
    max_chars: int = 12_000,
) -> TextLabels:
    """A topic, a category (one of ``categories``), a sentiment and the entities of ``text``, from ``model``
    (a :class:`~wintergrab.models.ModelProvider`), checked (see the module docs)."""
    from ..extraction.model import parse_answer

    wanted = ['"topic": a few words', f'"sentiment": one of {", ".join(_SENTIMENTS)}']
    if categories:
        wanted.append(f'"category": one of {", ".join(json.dumps(c) for c in categories)}')
    if entities:
        wanted.append('"entities": a list of {"name", "type"}, the people, organizations, places and products named')
    prompt = (
        "Read the text below and answer with one JSON object with these keys:\n- " + "\n- ".join(wanted)
        + "\nCopy names exactly as the text writes them. Use null when the text does not say.\n\n---\n"
        + text[:max_chars]
    )  # fmt: skip
    answer = parse_answer(model.complete(prompt, json_output=True)) or {}
    labels = TextLabels()
    topic = answer.get("topic")
    labels.topic = str(topic).strip() or None if isinstance(topic, str) else None
    category = answer.get("category")
    if categories and category is not None:
        match = next((c for c in categories if str(category).strip().casefold() == c.casefold()), None)
        if match is None:
            labels.dropped.append(f"category {category!r}: not one of those given")
        labels.category = match
    sentiment = answer.get("sentiment")
    if isinstance(sentiment, str) and sentiment.strip().lower() in _SENTIMENTS:
        labels.sentiment = sentiment.strip().lower()
    elif sentiment is not None:
        labels.dropped.append(f"sentiment {sentiment!r}: not one of {', '.join(_SENTIMENTS)}")
    folded = " ".join(text.casefold().split())
    for entity in _entities(answer.get("entities")):
        if " ".join(entity["name"].casefold().split()) in folded:
            labels.entities.append(entity)
        else:
            labels.dropped.append(f"entity {entity['name']!r}: not in the text")
    return labels


def _entities(value: Any) -> Iterable[dict[str, str]]:
    for item in value if isinstance(value, list) else ():
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            yield {"name": item["name"].strip(), "type": str(item.get("type") or "").strip()}
        elif isinstance(item, str) and item.strip():
            yield {"name": item.strip(), "type": ""}
