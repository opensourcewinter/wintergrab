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
"""

from __future__ import annotations

from .classify import PAGE_TYPES, PageClassifier, PageFeatures, PageType, classify_page, classify_url
from .profile import Endpoint, SiteProfile, SiteProfiler, TemplateCluster
from .survey import SitemapRead, SiteSurvey, read_sitemaps, survey_site
from .tech import TechDetector, Technology, detect_technologies
from .techrules import RULES, TechRule
from .topology import Topology, TopologyBuilder, TopologyNode

__all__ = [
    "PAGE_TYPES",
    "RULES",
    "Endpoint",
    "PageClassifier",
    "PageFeatures",
    "PageType",
    "SiteProfile",
    "SiteProfiler",
    "SiteSurvey",
    "SitemapRead",
    "TechDetector",
    "TechRule",
    "Technology",
    "TemplateCluster",
    "Topology",
    "TopologyBuilder",
    "TopologyNode",
    "classify_page",
    "classify_url",
    "detect_technologies",
    "read_sitemaps",
    "survey_site",
]
