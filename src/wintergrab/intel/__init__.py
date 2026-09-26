"""Page and site intelligence: page types, technologies, site profiles and topology.

* :func:`~wintergrab.intel.classify.classify_page` / :func:`~wintergrab.intel.classify.classify_url`:
  what kind of page (product, article, category, login, search...), with the evidence.
* :func:`~wintergrab.intel.tech.detect_technologies`: the CMS, e-commerce platform, frameworks,
  analytics, CDN... a site uses, with versions and the evidence.
* :class:`~wintergrab.intel.profile.SiteProfiler`: a whole site's profile: technologies, page
  types, template clusters, API endpoints, links, latency, errors, crawlability.
* :class:`~wintergrab.intel.topology.TopologyBuilder`: how the site is organized: its sections
  as a tree, its navigation, dead ends, duplicate routes and orphans.
* :func:`~wintergrab.intel.survey.survey_site`: a quick look at a site (robots.txt, sitemaps, a
  sample of pages) with its profile, as ``wintergrab inspect`` prints it.
* :func:`~wintergrab.intel.content.analyze_text`: a text's language, keywords and size, with no model;
  :func:`~wintergrab.intel.content.classify_text`: its topic, category, sentiment and entities, with one.
* :func:`~wintergrab.intel.sources.data_sources`: where a page's data is (HTML, tables, JSON-LD, embedded
  JSON, the API calls it makes), with the records each holds, GraphQL operations and pagination.
"""

from __future__ import annotations

from .classify import PAGE_TYPES, PageClassifier, PageFeatures, PageType, classify_page, classify_url
from .content import LANGUAGES, TextAnalysis, TextLabels, analyze_text, classify_text, detect_language
from .profile import Endpoint, SiteProfile, SiteProfiler, TemplateCluster, script_endpoints
from .sources import (
    ApiCall,
    Collection,
    DataSources,
    HtmlRecords,
    Pagination,
    Source,
    api_calls,
    data_sources,
    json_collections,
    pagination_of,
)
from .survey import SitemapRead, SiteSurvey, read_sitemaps, survey_site
from .tech import TechDetector, Technology, detect_technologies
from .techrules import RULES, TechRule
from .topology import Topology, TopologyBuilder, TopologyNode

__all__ = [
    "LANGUAGES",
    "PAGE_TYPES",
    "RULES",
    "ApiCall",
    "Collection",
    "DataSources",
    "Endpoint",
    "HtmlRecords",
    "PageClassifier",
    "PageFeatures",
    "PageType",
    "Pagination",
    "SiteProfile",
    "SiteProfiler",
    "SiteSurvey",
    "SitemapRead",
    "Source",
    "TechDetector",
    "TechRule",
    "Technology",
    "TemplateCluster",
    "TextAnalysis",
    "TextLabels",
    "Topology",
    "TopologyBuilder",
    "TopologyNode",
    "analyze_text",
    "api_calls",
    "classify_page",
    "classify_text",
    "classify_url",
    "data_sources",
    "detect_language",
    "detect_technologies",
    "json_collections",
    "pagination_of",
    "read_sitemaps",
    "script_endpoints",
    "survey_site",
]
