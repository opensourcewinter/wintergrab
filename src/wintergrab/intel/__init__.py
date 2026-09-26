"""Page and site intelligence: page types, technologies, site profiles.

* :func:`~wintergrab.intel.classify.classify_page` / :func:`~wintergrab.intel.classify.classify_url`:
  what kind of page (product, article, category, login, search...), with the evidence.
* :func:`~wintergrab.intel.tech.detect_technologies`: the CMS, e-commerce platform, frameworks,
  analytics, CDN... a site uses, with versions and the evidence.
* :class:`~wintergrab.intel.profile.SiteProfiler`: a whole site's profile: technologies, page
  types, template clusters, API endpoints, links, latency, errors, crawlability.
"""

from __future__ import annotations

from .classify import PAGE_TYPES, PageClassifier, PageFeatures, PageType, classify_page, classify_url
from .profile import Endpoint, SiteProfile, SiteProfiler, TemplateCluster
from .tech import TechDetector, Technology, detect_technologies
from .techrules import RULES, TechRule

__all__ = [
    "PAGE_TYPES",
    "RULES",
    "Endpoint",
    "PageClassifier",
    "PageFeatures",
    "PageType",
    "SiteProfile",
    "SiteProfiler",
    "TechDetector",
    "TechRule",
    "Technology",
    "TemplateCluster",
    "classify_page",
    "classify_url",
    "detect_technologies",
]
