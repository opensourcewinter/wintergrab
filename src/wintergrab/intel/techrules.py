r"""Built-in technology fingerprints for :mod:`wintergrab.intel.tech`.

Each rule lists signals that identify one technology. Patterns are regular
expressions (case-insensitive) matched against:

* ``headers``: ``{header name: value pattern}`` (``""`` = the header is present);
* ``cookies``: cookie name patterns;
* ``meta``: ``{meta name: content pattern}`` (e.g. ``generator``);
* ``scripts``: ``<script src>``, stylesheet and preload URLs;
* ``html``: the page's HTML (first 500 kB) - use distinctive markers only;
* ``url``: the page URL.

A named group ``(?P<version>...)`` captures a version. ``implies`` adds other
technologies (Next.js implies React).

A page is only scanned with an ``html`` pattern when it contains the
pattern's literal parts (``Shopify.theme`` in ``\bShopify\.theme\b``), so
give every pattern a distinctive literal: ``\bdata-v-[0-9a-f]{8}\b``, not
``\b[a-z]+-v-[0-9a-f]{8}\b``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["RULES", "TechRule"]


@dataclass(frozen=True)
class TechRule:
    name: str
    category: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: tuple[str, ...] = ()
    meta: dict[str, str] = field(default_factory=dict)
    scripts: tuple[str, ...] = ()
    html: tuple[str, ...] = ()
    url: tuple[str, ...] = ()
    implies: tuple[str, ...] = ()
    website: str = ""


_V = r"(?:[/ -]v?(?P<version>\d+(?:\.\d+){0,3}))?"  # an optional version after a name

RULES: tuple[TechRule, ...] = (
    # --- content management -------------------------------------------------------------
    TechRule("WordPress", "cms", meta={"generator": r"^WordPress" + _V},
             scripts=(r"/wp-(?:content|includes)/",
                      r"/wp-includes/css/dist/block-library/style(?:\.min)?\.css\?ver=(?P<version>\d+(?:\.\d+)+)"),
             html=(r"/wp-content/", r"<link[^>]+rel=.https://api\.w\.org/"),
             implies=("PHP", "MySQL")),
    TechRule("Drupal", "cms", headers={"x-drupal-cache": "", "x-generator": r"^Drupal" + _V},
             meta={"generator": r"^Drupal" + _V}, scripts=(r"/sites/(?:default|all)/(?:files|modules|themes)/",),
             html=(r"drupal-settings-json", r"\bDrupal\.settings\b"), implies=("PHP",)),
    TechRule("Joomla", "cms", meta={"generator": r"Joomla!?" + _V}, scripts=(r"/media/(?:jui|system)/js/",),
             implies=("PHP",)),
    TechRule("TYPO3", "cms", meta={"generator": r"TYPO3" + _V}, scripts=(r"/typo3(?:conf|temp)/",), implies=("PHP",)),
    TechRule("Ghost", "cms", meta={"generator": r"^Ghost" + _V}, scripts=(r"/ghost/(?:assets|content)/",),
             implies=("Node.js",)),
    TechRule("Wix", "cms", headers={"x-wix-request-id": ""}, scripts=(r"static\.parastorage\.com", r"static\.wixstatic\.com"),
             meta={"generator": r"Wix\.com"}),
    TechRule("Squarespace", "cms", scripts=(r"static1\.squarespace\.com", r"assets\.squarespace\.com"),
             html=(r"Static\.SQUARESPACE_CONTEXT",)),
    TechRule("Webflow", "cms", meta={"generator": r"^Webflow"}, html=(r"\bdata-wf-(?:page|site)=",),
             scripts=(r"assets[\w.-]*\.website-files\.com", r"webflow\.[\w.]*js")),
    TechRule("HubSpot CMS", "cms", headers={"x-hs-hub-id": ""}, scripts=(r"\.hs-sites\.com", r"js\.hs-scripts\.com"),
             meta={"generator": r"HubSpot"}),
    TechRule("Adobe Experience Manager", "cms", scripts=(r"/etc\.clientlibs/",), html=(r"/content/dam/",)),
    TechRule("Sitecore", "cms", cookies=(r"^SC_ANALYTICS_GLOBAL_COOKIE$", r"^sxa_site$"), html=(r"/-/media/",)),
    TechRule("Contentful", "cms", scripts=(r"images\.ctfassets\.net", r"cdn\.contentful\.com"),
             html=(r"images\.ctfassets\.net",)),
    TechRule("Blogger", "cms", meta={"generator": r"^Blogger"}, scripts=(r"blogger\.com/static",)),
    TechRule("Hugo", "static-site", meta={"generator": r"^Hugo" + _V}),
    TechRule("Jekyll", "static-site", meta={"generator": r"^Jekyll" + _V}),
    TechRule("Gatsby", "static-site", meta={"generator": r"^Gatsby" + _V}, html=(r'id="___gatsby"',),
             implies=("React",)),
    TechRule("Docusaurus", "static-site", meta={"generator": r"^Docusaurus" + _V}, implies=("React",)),
    TechRule("MkDocs", "static-site", meta={"generator": r"^mkdocs" + _V}, implies=("Python",)),
    TechRule("Sphinx", "static-site", html=(r"Created using <a href=\"https?://www\.sphinx-doc\.org", r"_static/sphinx_"),
             implies=("Python",)),
    TechRule("Read the Docs", "hosting", scripts=(r"readthedocs\.(?:org|io|com)/",), url=(r"\.readthedocs\.io",)),
    # --- e-commerce -------------------------------------------------------------------------
    TechRule("Shopify", "ecommerce", headers={"x-shopid": "", "x-shopify-stage": "", "powered-by": r"Shopify"},
             scripts=(r"cdn\.shopify\.com", r"cdn\.shopifycdn\.net"), html=(r"\bShopify\.theme\b",),
             url=(r"\.myshopify\.com",), cookies=(r"^_shopify_y$", r"^_shopify_s$")),
    TechRule("WooCommerce", "ecommerce", scripts=(r"/wp-content/plugins/woocommerce/",),
             html=(r"\bwoocommerce-(?:page|cart|checkout)\b", r"wc-ajax="), implies=("WordPress",)),
    TechRule("Magento", "ecommerce", headers={"x-magento-cache-debug": "", "x-magento-tags": ""},
             cookies=(r"^mage-(?:cache-sessid|messages|translation-storage)$",),
             scripts=(r"/static/(?:version\d+/)?frontend/", r"/skin/frontend/"), html=(r"Mage\.Cookies",),
             implies=("PHP",)),
    TechRule("BigCommerce", "ecommerce", scripts=(r"cdn\d*\.bigcommerce\.com",), html=(r"\bBCData\b",)),
    TechRule("PrestaShop", "ecommerce", meta={"generator": r"PrestaShop"}, html=(r"\bprestashop\s*=\s*\{",),
             cookies=(r"^PrestaShop-",), implies=("PHP",)),
    TechRule("OpenCart", "ecommerce", scripts=(r"catalog/view/(?:theme|javascript)/",), implies=("PHP",)),
    TechRule("Salesforce Commerce Cloud", "ecommerce", scripts=(r"demandware\.static", r"/on/demandware\."),
             cookies=(r"^dwanonymous_", r"^dwsid$")),
    TechRule("Shopware", "ecommerce", scripts=(r"/bundles/storefront/", r"/themes/Frontend/"),
             cookies=(r"^sw-context-token$",), implies=("PHP",)),
    TechRule("Ecwid", "ecommerce", scripts=(r"app\.ecwid\.com",)),
    TechRule("VTEX", "ecommerce", headers={"x-vtex-cache-status": ""}, scripts=(r"vtexassets\.com", r"\.vteximg\.com")),
    # --- JavaScript frameworks and libraries -------------------------------------------------
    TechRule("Next.js", "js-framework", headers={"x-powered-by": r"Next\.js" + _V},
             html=(r'<script id="__NEXT_DATA__"',), scripts=(r"/_next/static/",), implies=("React",)),
    TechRule("Nuxt", "js-framework", html=(r"\bwindow\.__NUXT__\b", r'id="__nuxt"'), scripts=(r"/_nuxt/",),
             implies=("Vue.js",)),
    TechRule("React", "js-framework", html=(r"\bdata-reactroot\b", r"\bdata-reactid\b"),
             scripts=(r"react(?:\.production)?(?:\.min)?\.js", r"react-dom")),
    TechRule("Vue.js", "js-framework", html=(r"\bdata-v-[0-9a-f]{8}\b", r"\bdata-server-rendered\b"),
             scripts=(r"vue(?:\.runtime)?(?:\.global)?(?:\.prod)?(?:\.min)?\.js",)),
    TechRule("Angular", "js-framework", html=(r'\bng-version="(?P<version>[\d.]+)"',)),
    TechRule("AngularJS", "js-framework", html=(r"\bng-app\b",), scripts=(r"angular(?:\.min)?\.js",)),
    TechRule("Svelte", "js-framework", html=(r"\bclass=\"[^\"]*\bsvelte-[a-z0-9]{6,}\b", r"__sveltekit"),
             scripts=(r"/_app/immutable/",)),
    TechRule("Remix", "js-framework", html=(r"window\.__remixContext",), implies=("React",)),
    TechRule("Astro", "js-framework", html=(r"<astro-island\b",), meta={"generator": r"^Astro" + _V}),
    TechRule("Ember.js", "js-framework", html=(r"\bember-application\b",), scripts=(r"ember(?:\.min)?\.js",)),
    TechRule("jQuery", "js-library", scripts=(r"jquery[.-]?(?P<version>\d+(?:\.\d+)+)?(?:\.slim)?(?:\.min)?\.js",
                                              r"/jquery(?:\.slim)?(?:\.min)?\.js\?ver=(?P<version>\d+(?:\.\d+)+)")),
    TechRule("Alpine.js", "js-library", scripts=(r"alpinejs",), html=(r"\bx-data=\"",)),
    TechRule("htmx", "js-library", scripts=(r"htmx(?:\.org)?(?:@[\d.]+)?(?:/dist)?/?htmx(?:\.min)?\.js", r"unpkg\.com/htmx"),
             html=(r"\bhx-(?:get|post|swap|target)=",)),
    TechRule("Bootstrap", "ui-framework", scripts=(r"bootstrap(?:@(?P<version>[\d.]+))?[/\w.-]*?(?:\.bundle)?(?:\.min)?\.(?:js|css)",)),
    TechRule("Tailwind CSS", "ui-framework", scripts=(r"tailwind(?:css)?[\w.@/-]*\.(?:js|css)", r"cdn\.tailwindcss\.com")),
    TechRule("Font Awesome", "font", scripts=(r"font-?awesome", r"kit\.fontawesome\.com")),
    TechRule("Google Fonts", "font", scripts=(r"fonts\.googleapis\.com", r"fonts\.gstatic\.com")),
    TechRule("Adobe Fonts", "font", scripts=(r"use\.typekit\.net",)),
    # --- analytics, tags and marketing ------------------------------------------------------
    TechRule("Google Analytics", "analytics", scripts=(r"google-analytics\.com/(?:analytics|ga)\.js",
             r"googletagmanager\.com/gtag/js\?id=(?:G|UA)-"), cookies=(r"^_ga$", r"^_gid$")),
    TechRule("Google Tag Manager", "tag-manager", scripts=(r"googletagmanager\.com/gtm\.js",),
             html=(r"googletagmanager\.com/ns\.html\?id=GTM-",)),
    TechRule("Adobe Analytics", "analytics", scripts=(r"\.omtrdc\.net", r"assets\.adobedtm\.com"), cookies=(r"^s_cc$",)),
    TechRule("Matomo", "analytics", scripts=(r"matomo\.js", r"piwik\.js"), cookies=(r"^_pk_id\.",)),
    TechRule("Plausible", "analytics", scripts=(r"plausible\.io/js/",)),
    TechRule("Fathom", "analytics", scripts=(r"cdn\.usefathom\.com",)),
    TechRule("Hotjar", "analytics", scripts=(r"static\.hotjar\.com",), cookies=(r"^_hjSessionUser_",)),
    TechRule("Microsoft Clarity", "analytics", scripts=(r"clarity\.ms/tag",)),
    TechRule("Mixpanel", "analytics", scripts=(r"cdn\.mxpnl\.com", r"mixpanel"), cookies=(r"^mp_[0-9a-f]+_mixpanel$",)),
    TechRule("Segment", "analytics", scripts=(r"cdn\.segment\.com",), cookies=(r"^ajs_anonymous_id$",)),
    TechRule("Amplitude", "analytics", scripts=(r"cdn\.amplitude\.com",)),
    TechRule("Heap", "analytics", scripts=(r"cdn\.heapanalytics\.com",)),
    TechRule("Meta Pixel", "advertising", scripts=(r"connect\.facebook\.net/[\w_]+/fbevents\.js",)),
    TechRule("Google Ads", "advertising", scripts=(r"googleadservices\.com", r"googlesyndication\.com")),
    TechRule("New Relic", "monitoring", scripts=(r"js-agent\.newrelic\.com",), html=(r"\bNREUM\b",)),
    TechRule("Datadog RUM", "monitoring", scripts=(r"datadoghq-browser-agent\.com", r"browser-intake-datadoghq")),
    TechRule("Sentry", "monitoring", scripts=(r"browser\.sentry-cdn\.com", r"js\.sentry-cdn\.com")),
    TechRule("Optimizely", "ab-testing", scripts=(r"cdn\.optimizely\.com",)),
    TechRule("OneTrust", "consent", scripts=(r"cdn\.cookielaw\.org", r"optanon"), cookies=(r"^OptanonConsent$",)),
    TechRule("Cookiebot", "consent", scripts=(r"consent\.cookiebot\.com",)),
    TechRule("Klaviyo", "marketing", scripts=(r"static\.klaviyo\.com",)),
    TechRule("Mailchimp", "marketing", scripts=(r"chimpstatic\.com", r"list-manage\.com")),
    TechRule("Intercom", "widget", scripts=(r"widget\.intercom\.io",)),
    TechRule("Zendesk", "widget", scripts=(r"static\.zdassets\.com",)),
    TechRule("Algolia", "search", scripts=(r"algolia(?:search|net)?", r"\.algolia\.net")),
    # --- payments -----------------------------------------------------------------------------
    TechRule("Stripe", "payment", scripts=(r"js\.stripe\.com",)),
    TechRule("PayPal", "payment", scripts=(r"paypal\.com/sdk/js", r"paypalobjects\.com")),
    TechRule("Adyen", "payment", scripts=(r"checkoutshopper-(?:live|test)\.adyen\.com",)),
    TechRule("Klarna", "payment", scripts=(r"klarna(?:services|cdn)?\.(?:com|net)",)),
    TechRule("Braintree", "payment", scripts=(r"js\.braintreegateway\.com",)),
    TechRule("Square", "payment", scripts=(r"js\.squareup\.com", r"squarecdn\.com")),
    TechRule("Razorpay", "payment", scripts=(r"checkout\.razorpay\.com",)),
    # --- security and bot protection ----------------------------------------------------------
    TechRule("reCAPTCHA", "security", scripts=(r"google\.com/recaptcha/", r"recaptcha\.net/recaptcha")),
    TechRule("hCaptcha", "security", scripts=(r"hcaptcha\.com/1/api\.js", r"js\.hcaptcha\.com")),
    TechRule("Cloudflare Turnstile", "security", scripts=(r"challenges\.cloudflare\.com/turnstile",)),
    # --- media and maps --------------------------------------------------------------------------
    TechRule("YouTube", "video", html=(r"youtube(?:-nocookie)?\.com/embed/",)),
    TechRule("Vimeo", "video", html=(r"player\.vimeo\.com/video/",)),
    TechRule("Google Maps", "maps", scripts=(r"maps\.googleapis\.com/maps/api/js",), html=(r"google\.com/maps/embed",)),
    TechRule("Mapbox", "maps", scripts=(r"api\.mapbox\.com",)),
    TechRule("Leaflet", "maps", scripts=(r"leaflet(?:\.min)?\.js", r"unpkg\.com/leaflet")),
    TechRule("Cloudinary", "media", html=(r"res\.cloudinary\.com/",)),
    TechRule("imgix", "media", html=(r"\.imgix\.net/",)),
    # --- CDN, hosting and edge ---------------------------------------------------------------------
    TechRule("Cloudflare", "cdn", headers={"server": r"^cloudflare$", "cf-ray": "", "cf-cache-status": ""},
             cookies=(r"^__cf_bm$", r"^__cflb$", r"^cf_clearance$")),
    TechRule("Fastly", "cdn", headers={"x-served-by": r"cache-", "fastly-debug-digest": "", "x-fastly-request-id": ""}),
    TechRule("Akamai", "cdn", headers={"x-akamai-transformed": "", "akamai-grn": "", "server": r"AkamaiGHost"},
             scripts=(r"\.akamaized\.net",)),
    TechRule("Amazon CloudFront", "cdn", headers={"x-amz-cf-id": "", "x-amz-cf-pop": "", "via": r"CloudFront"}),
    TechRule("Varnish", "cache", headers={"x-varnish": "", "via": r"varnish"}),
    TechRule("Vercel", "hosting", headers={"x-vercel-id": "", "x-vercel-cache": "", "server": r"^Vercel$"}),
    TechRule("Netlify", "hosting", headers={"x-nf-request-id": "", "server": r"^Netlify$"}),
    TechRule("GitHub Pages", "hosting", headers={"server": r"^GitHub\.com$"}, url=(r"\.github\.io",)),
    TechRule("Amazon S3", "hosting", headers={"server": r"^AmazonS3$", "x-amz-bucket-region": ""}),
    TechRule("Google Cloud", "hosting", headers={"via": r"\bgoogle\b", "server": r"^Google Frontend$"}),
    TechRule("Microsoft Azure", "hosting", headers={"x-azure-ref": "", "x-ms-request-id": ""}),
    TechRule("Heroku", "hosting", headers={"via": r"\bvegur\b"}, url=(r"\.herokuapp\.com",)),
    TechRule("Fly.io", "hosting", headers={"fly-request-id": "", "server": r"^Fly/"}),
    # --- web servers ------------------------------------------------------------------------------
    TechRule("nginx", "web-server", headers={"server": r"^nginx" + _V}),
    TechRule("OpenResty", "web-server", headers={"server": r"^openresty" + _V}, implies=("nginx",)),
    TechRule("Apache HTTP Server", "web-server", headers={"server": r"^Apache" + _V}),
    TechRule("Microsoft IIS", "web-server", headers={"server": r"^Microsoft-IIS" + _V}, implies=("Windows Server",)),
    TechRule("LiteSpeed", "web-server", headers={"server": r"^LiteSpeed"}),
    TechRule("Caddy", "web-server", headers={"server": r"^Caddy"}),
    TechRule("Envoy", "web-server", headers={"server": r"^envoy", "x-envoy-upstream-service-time": ""}),
    TechRule("Gunicorn", "web-server", headers={"server": r"^gunicorn" + _V}, implies=("Python",)),
    TechRule("Uvicorn", "web-server", headers={"server": r"^uvicorn"}, implies=("Python",)),
    # --- back ends and languages ---------------------------------------------------------------------
    TechRule("PHP", "language", headers={"x-powered-by": r"PHP" + _V}, cookies=(r"^PHPSESSID$",)),
    TechRule("ASP.NET", "framework", headers={"x-aspnet-version": "", "x-powered-by": r"ASP\.NET"},
             cookies=(r"^ASP\.NET_SessionId$", r"^\.AspNetCore\."), html=(r'name="__VIEWSTATE"',)),
    TechRule("Express", "framework", headers={"x-powered-by": r"^Express$"}, implies=("Node.js",)),
    TechRule("Ruby on Rails", "framework", meta={"csrf-param": r"^authenticity_token$"},
             headers={"x-runtime": r"^\d+\.\d+$"}, implies=("Ruby",)),
    TechRule("Django", "framework", cookies=(r"^csrftoken$", r"^django_language$"),
             html=(r'name="csrfmiddlewaretoken"',), implies=("Python",)),
    TechRule("Laravel", "framework", cookies=(r"^laravel_session$",), implies=("PHP",)),
    TechRule("Java", "language", cookies=(r"^JSESSIONID$",)),
    TechRule("Node.js", "language", headers={"x-powered-by": r"Node\.js"}),
    TechRule("Python", "language", headers={"x-powered-by": r"Python"}),
    TechRule("Ruby", "language", headers={"x-powered-by": r"Phusion Passenger"}),
    TechRule("MySQL", "database"),
    TechRule("Windows Server", "operating-system"),
)  # fmt: skip
