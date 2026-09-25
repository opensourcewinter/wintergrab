"""Where schema.org data (JSON-LD, microdata) and meta tags keep the values of common fields.

Field names are matched after lower-casing and treating ``-``, spaces and
camelCase like ``_``: ``review_count``, ``reviewCount`` and ``Review Count``
are the same field. A field's ``aliases`` are tried too, and
``sources=["jsonld:Product.offers.price"]`` in a schema names a path directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

__all__ = ["FIELD_PATHS", "META_KEYS", "TYPES", "field_key", "read_path", "target_types"]


def field_key(name: str) -> str:
    """``"reviewCount"``, ``"Review count"``, ``"review-count"`` -> ``"review_count"``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
    return re.sub(r"[\s\-.]+", "_", spaced).lower()


#: Schema names (the ``Schema.name``) and the schema.org types that hold such records.
TYPES: dict[str, frozenset[str]] = {
    "product": frozenset({"Product", "ProductGroup", "IndividualProduct", "ProductModel", "SomeProducts", "Vehicle", "Car"}),
    "offer": frozenset({"Offer", "AggregateOffer"}),
    "article": frozenset({"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle", "ScholarlyArticle",
                          "AnalysisNewsArticle", "OpinionNewsArticle", "ReportageNewsArticle", "LiveBlogPosting"}),
    "news": frozenset({"NewsArticle", "AnalysisNewsArticle", "OpinionNewsArticle", "ReportageNewsArticle"}),
    "post": frozenset({"BlogPosting", "SocialMediaPosting", "DiscussionForumPosting", "Article"}),
    "job": frozenset({"JobPosting"}),
    "event": frozenset({"Event", "MusicEvent", "SportsEvent", "BusinessEvent", "EducationEvent", "Festival",
                        "TheaterEvent", "ComedyEvent", "ExhibitionEvent", "SocialEvent"}),
    "organization": frozenset({"Organization", "Corporation", "LocalBusiness", "NGO", "EducationalOrganization",
                               "GovernmentOrganization", "NewsMediaOrganization"}),
    "company": frozenset({"Organization", "Corporation", "LocalBusiness"}),
    "business": frozenset({"LocalBusiness", "Store", "Restaurant", "FoodEstablishment", "Hotel", "LodgingBusiness",
                           "MedicalBusiness", "ProfessionalService", "AutomotiveBusiness", "FinancialService"}),
    "place": frozenset({"Place", "LocalBusiness", "TouristAttraction", "Accommodation", "Residence"}),
    "person": frozenset({"Person", "ProfilePage"}),
    "profile": frozenset({"Person", "ProfilePage"}),
    "recipe": frozenset({"Recipe"}),
    "review": frozenset({"Review", "CriticReview", "EmployerReview"}),
    "book": frozenset({"Book"}),
    "movie": frozenset({"Movie", "TVSeries", "TVEpisode"}),
    "video": frozenset({"VideoObject"}),
    "course": frozenset({"Course"}),
    "software": frozenset({"SoftwareApplication", "MobileApplication", "WebApplication", "VideoGame"}),
    "app": frozenset({"SoftwareApplication", "MobileApplication", "WebApplication"}),
    "faq": frozenset({"FAQPage", "Question"}),
    "dataset": frozenset({"Dataset"}),
}  # fmt: skip


def target_types(schema_name: str) -> frozenset[str]:
    """schema.org types for a schema called ``schema_name`` (``"products"`` -> Product...); empty if unknown."""
    key = field_key(schema_name)
    for candidate in (key, key.rstrip("s"), key[:-3] + "y" if key.endswith("ies") else key):
        if candidate in TYPES:
            return TYPES[candidate]
    return frozenset()


# Paths are dotted; lists along the way are searched item by item.
_PRICE = ("offers.price", "offers.lowPrice", "offers.priceSpecification.price", "price", "offers.highPrice")
_DATE_PUBLISHED = ("datePublished", "uploadDate", "dateCreated", "datePosted", "startDate")

#: Field name -> where schema.org objects keep it, most specific first.
FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "name": ("name", "headline", "title"),
    "title": ("headline", "name", "title"),
    "headline": ("headline", "name"),
    "description": ("description", "abstract", "articleBody"),
    "summary": ("abstract", "description"),
    "body": ("articleBody", "text", "description"),
    "text": ("text", "articleBody", "reviewBody"),
    "price": _PRICE,
    "list_price": ("offers.priceSpecification.price",),  # not offers.price: that is what the item costs now
    "low_price": ("offers.lowPrice", "offers.price"),
    "high_price": ("offers.highPrice", "offers.price"),
    "currency": ("offers.priceCurrency", "offers.priceSpecification.priceCurrency", "priceCurrency",
                 "baseSalary.currency", "estimatedSalary.currency", "currency"),
    "availability": ("offers.availability", "availability"),
    "condition": ("offers.itemCondition", "itemCondition"),
    "seller": ("offers.seller.name", "offers.seller"),
    "brand": ("brand.name", "brand", "manufacturer.name", "manufacturer"),
    "manufacturer": ("manufacturer.name", "manufacturer", "brand.name"),
    "model": ("model.name", "model"),
    "sku": ("sku", "mpn", "productID", "offers.sku"),
    "mpn": ("mpn",),
    "gtin": ("gtin13", "gtin", "gtin12", "gtin14", "gtin8", "offers.gtin13", "offers.gtin"),
    "isbn": ("isbn",),
    "color": ("color",),
    "size": ("size.name", "size"),
    "material": ("material",),
    "weight": ("weight",),
    "rating": ("aggregateRating.ratingValue", "reviewRating.ratingValue", "ratingValue"),
    "review_count": ("aggregateRating.reviewCount", "aggregateRating.ratingCount", "reviewCount", "ratingCount"),
    "rating_count": ("aggregateRating.ratingCount", "aggregateRating.reviewCount", "ratingCount"),
    "image": ("image", "thumbnailUrl", "logo", "photo"),
    "images": ("image",),
    "thumbnail": ("thumbnailUrl", "image"),
    "logo": ("logo",),
    "url": ("url", "mainEntityOfPage", "@id"),
    "link": ("url", "mainEntityOfPage"),
    "author": ("author.name", "author", "creator.name", "creator"),
    "publisher": ("publisher.name", "publisher"),
    "published": _DATE_PUBLISHED,
    "date_published": _DATE_PUBLISHED,
    "published_at": _DATE_PUBLISHED,
    "date": _DATE_PUBLISHED,
    "modified": ("dateModified",),
    "date_modified": ("dateModified",),
    "updated": ("dateModified",),
    "updated_at": ("dateModified",),
    "category": ("category", "articleSection", "genre", "recipeCategory"),
    "section": ("articleSection",),
    "keywords": ("keywords",),
    "tags": ("keywords",),
    "language": ("inLanguage",),
    "word_count": ("wordCount",),
    "company": ("hiringOrganization.name", "worksFor.name", "organizer.name", "publisher.name", "brand.name"),
    "organization": ("hiringOrganization.name", "worksFor.name", "organizer.name", "publisher.name"),
    "employer": ("hiringOrganization.name",),
    "job_title": ("title", "jobTitle", "name"),
    "salary": ("baseSalary.value.value", "baseSalary.value.minValue", "baseSalary.value", "estimatedSalary.value.value"),
    "min_salary": ("baseSalary.value.minValue",),
    "max_salary": ("baseSalary.value.maxValue",),
    "employment_type": ("employmentType",),
    "date_posted": ("datePosted",),
    "valid_through": ("validThrough",),
    "remote": ("jobLocationType",),
    "location": ("jobLocation.address.addressLocality", "location.name", "location.address.addressLocality",
                 "address.addressLocality", "contentLocation.name"),
    "venue": ("location.name",),
    "city": ("address.addressLocality", "jobLocation.address.addressLocality", "location.address.addressLocality"),
    "region": ("address.addressRegion", "jobLocation.address.addressRegion", "location.address.addressRegion"),
    "state": ("address.addressRegion", "jobLocation.address.addressRegion"),
    "country": ("address.addressCountry.name", "address.addressCountry", "jobLocation.address.addressCountry",
                "location.address.addressCountry", "addressCountry"),
    "postal_code": ("address.postalCode", "postalCode", "location.address.postalCode"),
    "zip": ("address.postalCode", "postalCode"),
    "street": ("address.streetAddress", "streetAddress"),
    "address": ("address", "location.address", "jobLocation.address"),
    "phone": ("telephone", "contactPoint.telephone"),
    "telephone": ("telephone", "contactPoint.telephone"),
    "email": ("email", "contactPoint.email"),
    "latitude": ("geo.latitude", "latitude"),
    "longitude": ("geo.longitude", "longitude"),
    "coordinates": ("geo",),
    "opening_hours": ("openingHours", "openingHoursSpecification"),
    "price_range": ("priceRange",),
    "start_date": ("startDate",),
    "end_date": ("endDate",),
    "duration": ("duration", "totalTime", "timeRequired"),
    "cook_time": ("cookTime",),
    "prep_time": ("prepTime",),
    "total_time": ("totalTime",),
    "ingredients": ("recipeIngredient",),
    "servings": ("recipeYield",),
    "calories": ("nutrition.calories",),
    "event_status": ("eventStatus",),
    "attendance_mode": ("eventAttendanceMode",),
    "performer": ("performer.name", "performer"),
    "organizer": ("organizer.name", "organizer"),
    "job_location": ("jobLocation.address.addressLocality",),
    "reviewer": ("author.name",),
    "review_rating": ("reviewRating.ratingValue",),
    "same_as": ("sameAs",),
    "founded": ("foundingDate",),
    "employees": ("numberOfEmployees.value", "numberOfEmployees"),
    "given_name": ("givenName",),
    "family_name": ("familyName",),
    "job_title_person": ("jobTitle",),
}  # fmt: skip

#: Field name -> keys in the OpenGraph / Twitter / meta dicts of ``structured_data()`` (source, key).
META_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "name": (("opengraph", "title"), ("twitter", "title"), ("meta", "title")),
    "title": (("opengraph", "title"), ("twitter", "title"), ("meta", "title")),
    "headline": (("opengraph", "title"), ("twitter", "title")),
    "description": (("opengraph", "description"), ("twitter", "description"), ("meta", "description")),
    "summary": (("opengraph", "description"), ("meta", "description")),
    "image": (("opengraph", "image"), ("opengraph", "image:url"), ("twitter", "image"), ("twitter", "image:src")),
    "thumbnail": (("opengraph", "image"), ("twitter", "image")),
    "url": (("opengraph", "url"),),
    "price": (("opengraph", "product:price:amount"), ("opengraph", "og:price:amount"), ("opengraph", "price:amount")),
    "currency": (("opengraph", "product:price:currency"), ("opengraph", "price:currency")),
    "availability": (("opengraph", "product:availability"), ("opengraph", "availability")),
    "condition": (("opengraph", "product:condition"),),
    "brand": (("opengraph", "product:brand"),),
    "published": (("opengraph", "article:published_time"),),
    "date_published": (("opengraph", "article:published_time"),),
    "date": (("opengraph", "article:published_time"),),
    "modified": (("opengraph", "article:modified_time"),),
    "date_modified": (("opengraph", "article:modified_time"),),
    "updated": (("opengraph", "article:modified_time"),),
    "author": (("opengraph", "article:author"), ("meta", "author")),
    "section": (("opengraph", "article:section"),),
    "category": (("opengraph", "article:section"),),
    "tags": (("opengraph", "article:tag"), ("meta", "keywords")),
    "keywords": (("meta", "keywords"),),
    "site_name": (("opengraph", "site_name"),),
    "language": (("meta", "language"), ("opengraph", "locale")),
    "type": (("opengraph", "type"),),
}

# UN/CEFACT unit codes used by schema.org QuantitativeValue.unitCode.
_UNIT_CODES = {
    "KGM": "kg", "GRM": "g", "MGM": "mg", "LBR": "lb", "ONZ": "oz", "TNE": "t",
    "MTR": "m", "CMT": "cm", "MMT": "mm", "KMT": "km", "INH": "in", "FOT": "ft", "YRD": "yd", "SMI": "mi",
    "LTR": "l", "MLT": "ml", "GLL": "gal", "MTK": "m2", "FTK": "ft2", "WTT": "W", "KWT": "kW", "HTZ": "Hz",
    "CEL": "degC", "FAH": "degF", "SEC": "s", "MIN": "min", "HUR": "h", "DAY": "day",
}  # fmt: skip


def _value_of(node: Any) -> Any:
    """A usable value from a schema.org node: text as is; names, values, URLs and addresses unwrapped."""
    if isinstance(node, (str, int, float, bool)):
        return node
    if isinstance(node, dict):
        if "@value" in node:
            return node["@value"]
        kinds = {
            t.rsplit("/", 1)[-1]
            for t in ([node.get("@type")] if not isinstance(node.get("@type"), list) else node["@type"])
            if isinstance(t, str)
        }
        if "PostalAddress" in kinds or any(k in node for k in ("streetAddress", "addressLocality")):
            parts = [
                node.get("streetAddress"),
                node.get("addressLocality"),
                " ".join(str(v) for v in (node.get("addressRegion"), node.get("postalCode")) if v),
                _value_of(node.get("addressCountry")),
            ]
            text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
            return text or None
        if "GeoCoordinates" in kinds or ("latitude" in node and "longitude" in node):
            return f"{node.get('latitude')}, {node.get('longitude')}"
        if "value" in node and ("unitCode" in node or "unitText" in node):
            unit = node.get("unitText") or _UNIT_CODES.get(str(node.get("unitCode")), node.get("unitCode"))
            return f"{node['value']} {unit}"
        for key in ("name", "url", "contentUrl", "value", "text", "@id"):
            value = node.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                return value
        return None
    return None


def read_path(node: Any, path: str) -> list[Any]:
    """Every value at dotted ``path`` inside ``node`` (lists along the way are searched item by item)."""
    current = [node]
    for part in path.split("."):
        found = []
        for item in current:
            for value in item if isinstance(item, list) else [item]:
                if isinstance(value, dict) and part in value:
                    found.append(value[part])
        current = found
        if not current:
            return []
    out = []
    for value in current:
        for item in value if isinstance(value, list) else [value]:
            unwrapped = _value_of(item)
            if unwrapped not in (None, "", []):
                out.append(unwrapped)
    return out


def candidate_names(name: str, aliases: Iterable[str] = ()) -> list[str]:
    """The field's name and aliases as :func:`field_key` keys, without repeats."""
    out: list[str] = []
    for value in (name, *aliases):
        key = field_key(value)
        if key not in out:
            out.append(key)
    return out


def camel(key: str) -> str:
    """``"review_count"`` -> ``"reviewCount"`` (how schema.org and most JSON APIs spell it)."""
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)
