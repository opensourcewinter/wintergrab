"""The interface to an extraction model (an LLM or any other "read this page" service).

wintergrab ships no model and calls none by default. You plug one in::

    def my_model(request: ModelRequest) -> dict:
        ...  # call a local or hosted model with request.prompt(), parse its JSON answer
        return {"price": "29.99", "brand": "Acme"}

    extractor = Extractor(schema, model=my_model)

The extractor asks the model only for fields the deterministic strategies
could not fill (or filled with low confidence), in one request per page, and
treats the answers as one more candidate: they are normalized with the field's
type, validated, compared with what other strategies found, and checked
against the page (*grounding*). A value that appears nowhere on the page keeps
a low confidence and the note ``"not-on-page"``: models can invent values, the
page cannot.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..data.normalize import iter_numbers
from ..data.similarity import normalize_for_hash

if TYPE_CHECKING:
    from ..data.schema import SchemaField

__all__ = ["ExtractionModel", "Image", "ModelField", "ModelRequest", "grounding"]


@dataclass(frozen=True)
class Image:
    """An image for a model that reads images (a screenshot, a chart): its bytes and media type."""

    data: bytes
    media_type: str = "image/png"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class ModelField:
    """A field the model is asked for."""

    name: str
    type: str
    description: str = ""
    required: bool = False
    many: bool = False
    enum: tuple[Any, ...] = ()

    @classmethod
    def of(cls, f: SchemaField) -> ModelField:
        return cls(f.name, f.type, f.description, f.required, f.many, tuple(f.enum or ()))


@dataclass
class ModelRequest:
    """What an extraction model gets: the fields wanted and the page's content (Markdown), and a
    screenshot of the page when the extractor was asked to send one (``Extractor(vision=True)``)."""

    fields: list[ModelField]
    text: str
    url: str | None = None
    schema_name: str = "record"
    #: Values other strategies already found (context for the model; it need not return them).
    known: dict[str, Any] = field(default_factory=dict)
    #: Images of the page (its screenshot), for models that read images.
    images: list[Image] = field(default_factory=list)

    def prompt(self) -> str:
        """A ready-made instruction for chat models (use it or build your own from the attributes)."""
        wanted = []
        for f in self.fields:
            line = f"- {f.name} ({f.type}{', list' if f.many else ''}{', required' if f.required else ''})"
            if f.description:
                line += f": {f.description}"
            if f.enum:
                line += f" One of: {', '.join(map(str, f.enum))}."
            wanted.append(line)
        known = f"\nAlready known: {json.dumps(self.known, ensure_ascii=False, default=str)}\n" if self.known else ""
        return (
            f"Extract these fields of the {self.schema_name} described by the page below.\n"
            + "\n".join(wanted)
            + known
            + (
                "\nA screenshot of the page is attached: values drawn in it (in a chart, an image, a canvas) "
                "count as stated.\n"
                if self.images
                else ""
            )
            + "\nAnswer with one JSON object mapping field names to values copied from the page. "
            "Use null for a field the page does not state. Do not guess or compute values.\n"
            + (f"\nPage URL: {self.url}\n" if self.url else "")
            + "\n---\n"
            + self.text
        )


@runtime_checkable
class ExtractionModel(Protocol):
    """Anything that turns a :class:`ModelRequest` into ``{field: value}`` (plain or ``async``).

    A plain function with this signature works; so does an object with an
    ``extract`` method (and optionally a ``name`` for provenance).
    """

    def __call__(self, request: ModelRequest) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


def model_name(model: Any) -> str:
    return str(getattr(model, "name", None) or getattr(model, "__name__", None) or type(model).__name__)


def call_model(model: Any, request: ModelRequest) -> Any:
    """Invoke ``model`` (a function, or an object with ``extract``)."""
    fn = getattr(model, "extract", None)
    return fn(request) if callable(fn) else model(request)


def parse_answer(answer: Any) -> dict[str, Any]:
    """The model's answer as a dict (a JSON string, possibly inside a code fence, is parsed)."""
    if isinstance(answer, Mapping):
        return dict(answer)
    if isinstance(answer, str):
        text = answer.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        if fenced:
            text = fenced.group(1)
        elif not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            text = text[start : end + 1] if start != -1 and end > start else text
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def grounding(raw: Any, page_text: str, page_numbers: set[str] | None = None) -> str:
    """How firmly a model's value is supported by the page.

    ``"exact"``: the text occurs on the page (ignoring case, spacing and
    punctuation). ``"number"``: the value is a number that occurs on the page
    (``29999`` for ``"₹29,999"``). ``"none"``: neither.
    """
    if raw is None:
        return "none"
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    page = normalize_for_hash(page_text)
    numbers = page_numbers
    results = []
    for value in values:
        text = normalize_for_hash(str(value))
        if text and text in page:
            results.append("exact")
            continue
        if numbers is None:
            numbers = {str(v.normalize()) for v, *_ in iter_numbers(page_text)}
        found = [str(v.normalize()) for v, *_ in iter_numbers(str(value))]
        results.append("number" if found and all(n in numbers for n in found) else "none")
    if all(r == "exact" for r in results):
        return "exact"
    if all(r in ("exact", "number") for r in results):
        return "number"
    return "none"
