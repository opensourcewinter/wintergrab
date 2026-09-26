"""What kind of page is this? Product, article, category, job, login, search results...

:func:`classify_page` weighs evidence from the URL, the page's structured data
(``Product``, ``NewsArticle``, ``JobPosting``...), its layout (an add-to-cart
button, a password field, many repeated cards, code blocks, a long
``<article>``) and its title, and reports the winning type with the evidence
behind it::

    >>> classify_page(response)
    PageType('product', confidence=0.956, evidence=['schema.org Product', 'URL /p/phone-x-123', 'add-to-cart button', ...])

:func:`classify_url` does the same from the URL alone, before fetching (to
prioritise a crawl, see ``Spider.priority_fn``).

The weights are evidence strengths, not trained probabilities: strong,
specific signals (schema.org types, a password form) count 4-6; weak or
shared ones (a URL word, a price somewhere) count 1-2. Confidence grows with
the winning score and with its margin over the runner-up. Add your own rules
with :meth:`PageClassifier.add_rule`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..extraction.page import PageContext, schema_types
from ..parser.text import tag_name

__all__ = ["PAGE_TYPES", "PageClassifier", "PageFeatures", "PageType", "classify_page", "classify_url"]

#: The page types :func:`classify_page` can answer (plus ``"unknown"``).
PAGE_TYPES = (
    "product",
    "property",
    "category",
    "listing",
    "article",
    "news",
    "job",
    "event",
    "company",
    "profile",
    "review",
    "directory",
    "documentation",
    "homepage",
    "login",
    "search",
    "archive",
    "contact",
    "error",
)
_MIN_SCORE = 2.0  # below this nothing is convincing: "unknown"
# Types that refine another rather than compete with it: a news page is an article, search results are a listing.
_REFINES = {"news": "article", "category": "listing", "search": "listing", "archive": "listing", "directory": "listing"}
_NAVIGATION = frozenset({"nav", "header", "footer", "aside"})

_URL_RULES: list[tuple[str, float, re.Pattern[str]]] = [
    ("product", 2.0, re.compile(r"/(?:products?|p|dp|item|items|sku|pd|gp/product)/(?!page(?:/|$))[^/]+|[-_/]p[-_]?\d{3,}(?:\.html?)?$|/\d{5,}\.html?$")),
    ("category", 2.0, re.compile(r"/(?:category|categories|c|collections?|shop|department|departments|catalog|catalogue|browse)(?:/|$)")),
    ("news", 2.0, re.compile(r"/(?:news|press|press-releases?|newsroom)(?:/|$)")),
    ("article", 2.0, re.compile(r"/(?:blog|article|articles|post|posts|stories|story|insights|magazine)/[^/]+|/\d{4}/\d{2}/(?:\d{2}/)?[^/]+")),
    ("job", 3.0, re.compile(r"/(?:jobs?|careers?|vacanc(?:y|ies)|positions?|openings?)(?:/|$)")),
    ("event", 2.0, re.compile(r"/(?:events?|tickets?|concerts?|conferences?|meetups?)(?:/|$)")),
    ("property", 2.0, re.compile(r"/(?:propert(?:y|ies)|real-?estate|homes?-for-(?:sale|rent)|for-(?:sale|rent)|rentals?|apartments?|condos?|immobilien|expose)/[^/]+")),
    ("profile", 2.0, re.compile(r"/(?:users?|profiles?|people|members?|authors?)/[^/]+|/@[\w.-]+/?$")),
    ("company", 2.0, re.compile(r"/(?:company|companies|organi[sz]ations?|about(?:-us)?|who-we-are)(?:/|$)")),
    ("review", 2.0, re.compile(r"/(?:reviews?|ratings?|testimonials?)(?:/|$)")),
    ("directory", 2.0, re.compile(r"/(?:directory|listings?|places|locations|stores|dealers|find-a-\w+)(?:/|$)")),
    ("documentation", 3.0, re.compile(r"/(?:docs?|documentation|reference|api-reference|manual|guides?|handbook|wiki|kb|knowledge-?base)(?:/|$)|readthedocs")),
    ("login", 4.0, re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|auth|sso|account/login|users/sign_in)(?:/|$|\?)")),
    ("search", 3.0, re.compile(r"/(?:search|find|results)(?:/|$|\?)")),
    ("archive", 2.0, re.compile(r"/(?:archives?|tags?|topics?)(?:/|$)|/\d{4}/(?:\d{2}/)?$")),
    ("contact", 3.0, re.compile(r"/(?:contact(?:-us)?|kontakt|contacto|support/contact)(?:/|$|\.html?$)")),
]  # fmt: skip
_SEARCH_PARAMS = frozenset({"q", "query", "search", "s", "keyword", "keywords", "term", "k"})
_PAGE_PARAMS = frozenset({"page", "p", "pg", "offset", "start"})

_SCHEMA_TYPES: dict[str, tuple[str, float]] = {
    "Product": ("product", 6.0), "ProductGroup": ("product", 6.0), "IndividualProduct": ("product", 6.0),
    "Vehicle": ("product", 5.0), "Book": ("product", 3.0), "SoftwareApplication": ("product", 3.0),
    "NewsArticle": ("news", 6.0), "AnalysisNewsArticle": ("news", 6.0), "ReportageNewsArticle": ("news", 6.0),
    "Article": ("article", 5.0), "BlogPosting": ("article", 5.0), "TechArticle": ("documentation", 4.0),
    "ScholarlyArticle": ("article", 5.0), "Report": ("article", 4.0),
    "JobPosting": ("job", 6.0), "Event": ("event", 6.0), "MusicEvent": ("event", 6.0), "SportsEvent": ("event", 6.0),
    "Person": ("profile", 3.0), "ProfilePage": ("profile", 6.0),
    "Organization": ("company", 1.0), "Corporation": ("company", 3.0), "LocalBusiness": ("company", 3.0),
    "Review": ("review", 4.0), "SearchResultsPage": ("search", 6.0), "CollectionPage": ("category", 3.0),
    "ItemList": ("listing", 2.0), "FAQPage": ("documentation", 1.0), "AboutPage": ("company", 4.0),
    "ContactPage": ("contact", 6.0), "Place": ("directory", 1.0),
    "RealEstateListing": ("property", 6.0), "Apartment": ("property", 5.0), "House": ("property", 5.0),
    "SingleFamilyResidence": ("property", 5.0), "Residence": ("property", 4.0), "ApartmentComplex": ("property", 4.0),
    "Accommodation": ("property", 3.0),
}  # fmt: skip
_OG_TYPES = {"product": "product", "product.item": "product", "article": "article", "profile": "profile",
             "book": "product", "business.business": "company", "event": "event"}  # fmt: skip

_CART = re.compile(r"add to (?:cart|bag|basket)|buy now|in den warenkorb|ajouter au panier|añadir al carrito", re.I)
# "$12.99", "12,99 €". Every alternative starts with a literal character, so the regular expression
# engine skips to the characters a price can start with instead of trying every position of the text.
_MONEY = re.compile(
    "|".join(
        [
            re.escape(sign) + r"\s?\d[\d.,]*"
            for sign in ("$", "€", "£", "¥", "₹", "USD", "EUR", "GBP", "INR", "Rs.", "Rs")
        ]
        + [digit + r"[\d.,]*\s?(?:€|EUR|USD|kr|zł)" for digit in "0123456789"]
    )
)
_MAX_TEXT = 100_000  # wording rules read this much of the page's text
_NOT_FOUND = re.compile(r"\b(?:404|page not found|not found|nicht gefunden|introuvable|no encontrada)\b", re.I)
_LOGIN_WORDS = re.compile(r"\b(?:log ?in|sign ?in|anmelden|connexion|iniciar sesi[oó]n)\b", re.I)
_SEARCH_WORDS = re.compile(r"\b(?:search results|results for|no results|resultados|suchergebnisse|résultats)\b", re.I)
_PROPERTY_WORDS = re.compile(
    r"\b\d+\s*(?:bed(?:room)?s?|bath(?:room)?s?|rooms)\b|\b\d[\d,.]*\s*(?:sq\.?\s?ft|square (?:feet|metres|meters)|m²|sqm)",
    re.I,
)
_JOB_WORDS = re.compile(
    r"\b(?:apply (?:now|for this job)|job description|responsibilities|qualifications|salary|full[- ]time|part[- ]time)\b",
    re.I,
)
_EVENT_WORDS = re.compile(r"\b(?:get tickets|buy tickets|register now|venue|doors open|rsvp)\b", re.I)
_PROFILE_WORDS = re.compile(r"\b(?:followers|following|joined|member since|posts by)\b", re.I)
_CONTACT_WORDS = re.compile(r"\b(?:contact us|get in touch|kontakt|contactez-nous|contáctanos)\b", re.I)
_ARCHIVE_WORDS = re.compile(r"\b(?:archives?|posts tagged|tag:|category:)\b", re.I)


def _path_of(url: str | None) -> tuple[str, dict[str, list[str]]]:
    if not url:
        return "", {}
    parts = urlsplit(url)
    return parts.path or "/", parse_qs(parts.query)


_PAGE_PATH = re.compile(r"/page/\d+/?$")
_HOME = re.compile(r"/(?:index\.\w+|home|[a-z]{2}(?:-[a-z]{2})?/?)")


def _url_evidence(path: str, query: dict[str, list[str]]) -> list[tuple[str, float, str]]:
    """``(type, weight, evidence)`` from a URL's path and query."""
    out = [
        (kind, weight, "URL " + m.group(0)[:40]) for kind, weight, pattern in _URL_RULES if (m := pattern.search(path))
    ]
    if path == "/" or _HOME.fullmatch(path):
        out.append(("homepage", 4.0, "site root"))
    if _SEARCH_PARAMS & set(query):
        out.append(("search", 3.0, "search query in the URL"))
    if _PAGE_PARAMS & set(query) or _PAGE_PATH.search(path):
        out.append(("listing", 1.0, "page number in the URL"))
    return out


@dataclass
class PageType:
    """The kind of a page, with the evidence for it.

    Attributes:
        type: One of :data:`PAGE_TYPES`, or ``"unknown"``.
        confidence: 0-1, from the winning score and its margin over the best competing type (a
            related type does not compete: a news page is also an article).
        evidence: What pointed to ``type``.
        scores: Every type's total score (the evidence weights). A refined type with convincing
            evidence of its own (``news``; ``category``, ``search``, ``archive``, ``directory``)
            also counts its parent's (``article``; ``listing``).
    """

    type: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "confidence": self.confidence, "evidence": list(self.evidence)}

    def __str__(self) -> str:
        return f"{self.type} ({self.confidence:.0%}): " + ", ".join(self.evidence)

    def __repr__(self) -> str:
        return f"PageType({self.type!r}, confidence={self.confidence}, evidence={self.evidence!r})"


class PageFeatures:
    """Cheap facts about a page that classification rules look at (each computed once)."""

    def __init__(self, page: PageContext, status: int | None = None) -> None:
        self.page = page
        self.url = page.url
        self.status = status
        self.path, self.query = _path_of(page.url)

    @cached_property
    def schema_types(self) -> list[str]:
        types = []
        for kind in ("json-ld", "microdata"):
            for _path, node in self.page.nodes(kind):
                types.extend(schema_types(node))
        return types

    @cached_property
    def og_type(self) -> str:
        return str((self.page.structured.get("opengraph") or {}).get("type") or "").strip().lower()

    @cached_property
    def title(self) -> str:
        return str((self.page.structured.get("meta") or {}).get("title") or "")

    @cached_property
    def h1(self) -> list[str]:
        return [h.text for h in self.page.root.css("h1") if h.text]

    @cached_property
    def text(self) -> str:
        """The page's visible text (its first 100,000 characters: enough to tell what the page is)."""
        return self.page.text[:_MAX_TEXT]

    @cached_property
    def prices(self) -> int:
        return len(set(_MONEY.findall(self.text)))

    @cached_property
    def password_inputs(self) -> int:
        return len(self.page.root.css("input[type=password]"))

    @cached_property
    def form_inputs(self) -> int:
        return len(self.page.root.css("form input:not([type=hidden]):not([type=submit]), form select, form textarea"))

    @cached_property
    def cart_button(self) -> bool:
        for el in self.page.root.css("button, input[type=submit], a[class*=cart], a[class*=button], a[class*=btn]"):
            label = el.text or el.attr("value") or el.attr("aria-label") or ""
            if label and _CART.search(label):
                return True
        return False

    @cached_property
    def article_text(self) -> int:
        """Characters of text in the page's longest ``<article>`` (or ``<main>``) element."""
        lengths = [
            len(el.text) for el in self.page.root.css("article, [itemprop=articleBody], .article-body, .post-content")
        ]
        return max(lengths, default=0)

    @cached_property
    def paragraphs(self) -> int:
        return sum(1 for p in self.page.root.css("p") if len(p.text) >= 80)

    @cached_property
    def code_blocks(self) -> int:
        return len(self.page.root.css("pre, code"))

    @cached_property
    def time_elements(self) -> int:
        return len(self.page.root.css("time"))

    @cached_property
    def byline(self) -> bool:
        return bool(self.page.root.css("[rel=author], [class*=author], [class*=byline], [itemprop=author]"))

    @cached_property
    def repeated(self) -> int:
        """The largest group of sibling elements with the same tag and class that hold a link (cards, rows)."""
        root = self.page.root.root
        if root is None:
            return 0
        best = 0
        for parent in root.iter():
            if not isinstance(parent.tag, str) or len(parent) < 3:
                continue
            if tag_name(parent) in _NAVIGATION or any(tag_name(a) in _NAVIGATION for a in parent.iterancestors()):
                continue  # menus repeat links too, but are not the page's content
            counts: Counter[tuple[str, str]] = Counter()
            for child in parent:
                if not isinstance(child.tag, str):
                    continue
                name = tag_name(child)
                if name not in ("script", "style", "option", "br") and (name == "a" or child.find(".//a") is not None):
                    counts[(name, (child.get("class") or "").strip())] += 1
            if counts:
                best = max(best, counts.most_common(1)[0][1])
        return best

    @cached_property
    def links(self) -> int:
        return len(self.page.root.css("a[href]"))

    @cached_property
    def pagination(self) -> bool:
        if any(key in _PAGE_PARAMS for key in self.query):
            return True
        return bool(self.page.root.css("[class*=pagination], [class*=pager], a[rel=next], link[rel=next]"))

    @cached_property
    def search_box(self) -> bool:
        return bool(self.page.root.css("input[type=search], input[name=q], input[name=query], input[name=s]"))


#: A rule returns ``None`` (does not apply), a weight, ``(weight, evidence)``, or a list of ``(type, weight, evidence)``.
Rule = Callable[[PageFeatures], Any]


def _as_evidence(kind: str, name: str, outcome: Any) -> list[tuple[str, float, str]]:
    if outcome is None or outcome is False:
        return []
    if isinstance(outcome, list):
        return [(str(t), float(w), str(e)) for t, w, e in outcome]
    if isinstance(outcome, tuple):
        weight, text = outcome
        return [(kind, float(weight), str(text))]
    return [(kind, float(outcome), name)]


def _default_rules() -> list[tuple[str, str, Rule]]:
    """``(type, name, rule)``: a rule returns a weight (or ``(weight, evidence)``) when it applies."""
    rules: list[tuple[str, str, Rule]] = []

    def add(kind: str, name: str, rule: Rule) -> None:
        rules.append((kind, name, rule))

    # structured data
    def schema_rule(f: PageFeatures) -> list[tuple[str, float, str]]:
        out = []
        seen = set()
        for name in f.schema_types:
            mapped = _SCHEMA_TYPES.get(name)
            if mapped and name not in seen:
                seen.add(name)
                out.append((mapped[0], mapped[1], f"schema.org {name}"))
        if f.og_type in _OG_TYPES:
            out.append((_OG_TYPES[f.og_type], 3.0, f"og:type {f.og_type}"))
        return out

    add("*", "schema.org types", schema_rule)

    add("*", "URL patterns", lambda f: _url_evidence(f.path, f.query) if f.url else None)

    # layout and wording
    add("product", "add-to-cart button", lambda f: 3.0 if f.cart_button else None)
    add("product", "one price near the title", lambda f: 1.5 if 1 <= f.prices <= 3 and len(f.h1) == 1 else None)
    add("category", "many prices", lambda f: 3.0 if f.prices >= 5 and f.repeated >= 5 else None)
    add("category", "repeated cards with prices", lambda f: 2.0 if f.repeated >= 8 and f.prices >= 3 else None)
    add("listing", "repeated cards", lambda f: 3.0 if f.repeated >= 8 and f.prices < 3 else None)
    add("listing", "pagination", lambda f: 1.0 if f.pagination and f.repeated >= 5 else None)
    add(
        "category",
        "priced cards and a pager",
        lambda f: 2.5 if f.pagination and f.repeated >= 3 and f.prices >= 3 else None,
    )
    add("article", "long article text", lambda f: 3.0 if f.article_text >= 1500 else None)
    add("article", "many paragraphs", lambda f: 1.5 if f.paragraphs >= 6 else None)
    add("article", "byline and date", lambda f: 2.0 if f.byline and f.time_elements else None)
    add("documentation", "code blocks", lambda f: 3.0 if f.code_blocks >= 3 else None)
    add("login", "password field", lambda f: 6.0 if f.password_inputs and f.form_inputs <= 6 else None)
    add(
        "login",
        "sign-in wording",
        lambda f: 1.5 if any(_LOGIN_WORDS.search(h) for h in f.h1) or _LOGIN_WORDS.search(f.title) else None,
    )
    add("search", "results wording", lambda f: 2.5 if _SEARCH_WORDS.search(" ".join([f.title, *f.h1])) else None)
    add(
        "search",
        "search box and results",
        lambda f: 1.0 if f.search_box and f.repeated >= 5 and _SEARCH_PARAMS & set(f.query) else None,
    )
    add("job", "job wording", lambda f: 2.0 if len(_JOB_WORDS.findall(f.text)) >= 2 else None)
    add("event", "event wording", lambda f: 2.0 if _EVENT_WORDS.search(f.text) and f.time_elements else None)
    add("property", "rooms and floor area", lambda f: 2.5 if len(_PROPERTY_WORDS.findall(f.text)) >= 2 else None)
    add("profile", "profile wording", lambda f: 2.0 if _PROFILE_WORDS.search(f.text) else None)
    add("contact", "contact wording", lambda f: 3.0 if _CONTACT_WORDS.search(" ".join([f.title, *f.h1])) else None)
    add("archive", "archive wording", lambda f: 2.0 if _ARCHIVE_WORDS.search(" ".join([f.title, *f.h1])) else None)
    add("error", "not-found status", lambda f: 8.0 if f.status == 404 else None)
    add("error", "not-found wording", lambda f: 5.0 if _NOT_FOUND.search(" ".join([f.title, *f.h1])) else None)
    add("homepage", "many links from the root", lambda f: 1.0 if f.path in ("", "/") and f.links >= 30 else None)
    return rules


class PageClassifier:
    """Rule-based page classification (see the module docs). ``PageClassifier()`` has the default rules."""

    def __init__(self) -> None:
        self.rules = _default_rules()

    def add_rule(self, page_type: str, name: str, rule: Rule) -> None:
        """Add evidence: ``rule(features)`` returns a weight (or ``(weight, evidence text)``) when it applies."""
        self.rules.append((page_type, name, rule))

    def scores(self, features: PageFeatures) -> tuple[dict[str, float], dict[str, list[str]]]:
        scores: dict[str, float] = {}
        evidence: dict[str, list[str]] = {}
        for kind, name, rule in self.rules:
            try:
                outcome = rule(features)
            except Exception:  # a broken rule must not break classification
                continue
            for target, weight, text in _as_evidence(kind, name, outcome):
                if weight:
                    scores[target] = scores.get(target, 0.0) + float(weight)
                    evidence.setdefault(target, []).append(text)
        return scores, evidence

    def classify(self, page: Any, *, url: str | None = None, status: int | None = None) -> PageType:
        ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
        if status is None and ctx.response is not None:
            status = ctx.response.status
        return self._decide(*self.scores(PageFeatures(ctx, status)))

    @staticmethod
    def _decide(scores: dict[str, float], evidence: dict[str, list[str]]) -> PageType:
        # A refined type builds on its parent's evidence (what shows an article also shows a news
        # article) once it has convincing evidence of its own.
        combined = dict(scores)
        merged = dict(evidence)
        for kind, parent in _REFINES.items():
            if scores.get(kind, 0.0) >= _MIN_SCORE and parent in scores:
                combined[kind] = scores[kind] + scores[parent]
                merged[kind] = list(dict.fromkeys([*evidence.get(kind, []), *evidence.get(parent, [])]))
        ranked = sorted(combined.items(), key=lambda kv: -kv[1])
        rounded = {k: round(v, 2) for k, v in ranked}
        if not ranked or ranked[0][1] < _MIN_SCORE:
            return PageType("unknown", 0.0, [], rounded)
        best, score = ranked[0]
        family = _REFINES.get(best, best)
        runner_up = next((v for k, v in ranked[1:] if _REFINES.get(k, k) != family), 0.0)
        confidence = (1 - math.exp(-score / 4)) * (score - runner_up) / score
        return PageType(best, round(confidence, 3), merged.get(best, []), rounded)


_DEFAULT = PageClassifier()


def classify_page(page: Any, *, url: str | None = None, status: int | None = None) -> PageType:
    """The type of a page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML)."""
    return _DEFAULT.classify(page, url=url, status=status)


def classify_url(url: str) -> PageType:
    """A guess from the URL alone (before fetching): cheap, and less sure than :func:`classify_page`."""
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for kind, weight, text in _url_evidence(*_path_of(url)):
        scores[kind] = scores.get(kind, 0.0) + weight
        evidence.setdefault(kind, []).append(text)
    return PageClassifier._decide(scores, evidence)
