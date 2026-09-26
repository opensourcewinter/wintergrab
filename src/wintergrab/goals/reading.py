"""Reading a request with a language model: for requests the built-in rules read poorly.

::

    from wintergrab.models import load_model

    goal = parse_goal(text, parser=model_reader(load_model("ollama:NAME")))
    # wintergrab goal "..." --model ollama:NAME

The model is asked for the goal as JSON: the kind of record, the fields, the sites, the part of
the site to look in, the conditions as expressions, a limit, and whether to keep watching. Its
answer is checked like any goal: an unknown kind or a condition that is not a valid expression
is refused. A site that the request does not name is dropped, because a model may invent one.
When the answer cannot be used (the model fails, gives no JSON, or gives an invalid goal), the
request is read by the built-in rules, and the goal's notes say so.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..errors import WintergrabError
from ..extraction.model import parse_answer

__all__ = ["model_reader"]

log = logging.getLogger("wintergrab.goals")


def _type_of(spec: Any) -> str:
    return spec if isinstance(spec, str) else str(spec.get("type", "string"))


def _prompt(text: str, today: str) -> str:
    from .goal import ENTITIES

    kinds = "\n".join(
        f"- {name}: " + ", ".join(f"{field} ({_type_of(spec)})" for field, spec in kind.fields.items())
        for name, kind in ENTITIES.items()
    )
    return f"""Read this request for data from websites, and describe it as one JSON object:

{{"entity": the kind of record, one of: {", ".join(ENTITIES)},
 "fields": the fields wanted (the entity's field names below when one fits; else short snake_case names),
 "sites": the websites or URLs the request names (only those; [] if it names none),
 "scope": words naming the part of the site to look in, such as a category ("laptops"); [] if none,
 "filters": conditions on the fields, as expressions (see below); [] if none,
 "limit": the most records wanted, or null,
 "monitor": "hourly", "daily" or "weekly" when it asks to keep watching, else null,
 "notes": what you had to assume, as short sentences}}

The fields of each kind (with their types):
{kinds}

Expressions compare fields: price < 1000, rating >= 4, bedrooms >= 2, and combine with and, or, not.
Text: icontains(location, 'Berlin') for a field containing a word; remote == 'yes'.
Dates: date(published) >= '2026-01-31' (today is {today}; "the last 30 days" is a date 30 days ago).
A price in a currency: price < 1000 and (currency is None or currency == 'EUR').

Request: {text}"""


def _named_in(site: str, text: str) -> bool:
    host = urlsplit(site if "://" in site else "https://" + site).hostname or ""
    host = host.removeprefix("www.")
    return bool(host) and host in text.lower()


def model_reader(model: Any, *, now: datetime | None = None) -> Callable[[str], Mapping[str, Any]]:
    """A ``parser`` for :func:`~wintergrab.goals.parse_goal` that asks ``model`` (a
    :class:`~wintergrab.models.ModelProvider`), and falls back on the built-in rules (see the module docs)."""

    def read(text: str) -> Mapping[str, Any]:
        from .goal import Goal, parse_goal

        today = (now or datetime.now(timezone.utc)).date().isoformat()
        name = getattr(model, "name", type(model).__name__)
        try:
            answer = model.complete(_prompt(text, today), json_output=True)
            data = parse_answer(answer)
            if not data:
                raise ValueError(f"no JSON object in the answer: {answer[:120]!r}")
            sites = [str(s) for s in data.get("sites") or ()]
            invented = [s for s in sites if not _named_in(s, text)]
            data["sites"] = [s for s in sites if s not in invented]
            notes = [str(n) for n in data.get("notes") or ()]
            notes.append(f"read by {name}")
            if invented:
                notes.append(f"sites the request does not name were dropped: {', '.join(invented)}")
            data["notes"] = notes
            Goal.from_dict(data, text=text)  # checked here, so that a bad answer falls back
            return data
        except (WintergrabError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            log.warning("%s could not read the request (%s); reading it by the rules", name, exc)
            goal = parse_goal(text)
            fallback = goal.to_dict()
            fallback["notes"] = [*goal.notes, f"{name} could not read it ({exc}): read by the rules"]
            return fallback

    return read
