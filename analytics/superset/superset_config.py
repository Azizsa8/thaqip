"""Superset configuration for Thaqip analytics.

Secrets come from the environment (var/superset.env, written by
bin/bootstrap-superset.sh, gitignored). Nothing secret lives in this file.

Security model: Superset has its own login and roles, and it is published
only on loopback and the Tailscale address, like the console. Its data
connection uses the `superset_ro` role, which can read the curated
`analytics` schema and nothing else — so SQL Lab cannot reach tenant-private
tables even for an admin.
"""
import os


SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_METADATA_URI"]

APP_NAME = "Thaqip Analytics"
APP_ICON = "/static/assets/thaqip/thaqip-logo.svg"
FAVICONS = [{"href": "/static/assets/thaqip/thaqip-logo.svg"}]

# ------------------------------------------------------------------ caching
_REDIS = os.environ.get("SUPERSET_REDIS_URL", "redis://redis:6379")


def _cache(db: int, prefix: str, timeout: int) -> dict:
    return {"CACHE_TYPE": "RedisCache", "CACHE_DEFAULT_TIMEOUT": timeout,
            "CACHE_KEY_PREFIX": prefix, "CACHE_REDIS_URL": f"{_REDIS}/{db}"}


CACHE_CONFIG = _cache(2, "thq_ss_", 300)
DATA_CACHE_CONFIG = _cache(3, "thq_ss_data_", 600)
FILTER_STATE_CACHE_CONFIG = _cache(4, "thq_ss_filter_", 86400)
EXPLORE_FORM_DATA_CACHE_CONFIG = _cache(4, "thq_ss_explore_", 86400)

# ------------------------------------------------------------ capabilities
FEATURE_FLAGS = {
    # drill-downs and interactivity
    "DRILL_TO_DETAIL": True,
    "DRILL_BY": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "DATE_RANGE_TIMESHIFTS_ENABLED": True,
    # building and customising
    "CSS_TEMPLATES": True,
    "ENABLE_TEMPLATE_PROCESSING": True,     # Jinja in SQL Lab / virtual datasets
    "AG_GRID_TABLE_ENABLED": True,          # interactive grid table chart
    "TAGGING_SYSTEM": True,
    "DASHBOARD_RBAC": True,
    "ALLOW_FULL_CSV_EXPORT": True,
    "ESTIMATE_QUERY_COST": True,
    "DASHBOARD_VIRTUALIZATION": True,
}

ROW_LIMIT = 50000
SQL_MAX_ROW = 100000
SQLLAB_TIMEOUT = 60
SUPERSET_WEBSERVER_TIMEOUT = 90

# ------------------------------------------------------------------ locale
BABEL_DEFAULT_LOCALE = "ar"
LANGUAGES = {
    "ar": {"flag": "sa", "name": "العربية"},
    "en": {"flag": "us", "name": "English"},
}

# ---------------------------------------------------------------- theming
# Thaqip's palette as the system theme; admins can add more themes in
# Settings → Themes and apply one per dashboard.
_FONT_URL = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700"
             "&family=IBM+Plex+Mono:wght@400;600&display=swap")
_BASE_TOKENS = {
    "brandAppName": APP_NAME,
    "brandLogoAlt": "Thaqip",
    "brandLogoUrl": APP_ICON,
    "brandLogoMargin": "14px 0",
    "brandLogoHref": "/",
    "brandLogoHeight": "28px",
    "colorPrimary": "#075E46",
    "colorLink": "#075E46",
    "colorSuccess": "#087B55",
    "colorWarning": "#8F5608",
    "colorError": "#A84032",
    "colorInfo": "#24798A",
    "fontUrls": [_FONT_URL],
    "fontFamily": "'IBM Plex Sans Arabic', Tahoma, Arial, sans-serif",
    "fontFamilyCode": "'IBM Plex Mono', 'Courier New', monospace",
    "borderRadius": 10,
    "transitionTiming": 0.3,
    "brandIconMaxWidth": 37,
    "fontSizeXS": "8",
    "fontSizeXXL": "28",
    "fontWeightNormal": "400",
    "fontWeightLight": "300",
    "fontWeightStrong": "600",
    "fontWeightBold": "700",
    "colorEditorSelection": "#DDECE4",
}
THEME_DEFAULT = {"token": {**_BASE_TOKENS, "colorBgLayout": "#F5F1E8",
                           "colorBgContainer": "#FFFEFA"},
                 "algorithm": "default"}
THEME_DARK = {"token": {**_BASE_TOKENS, "colorPrimary": "#2FA37C", "colorLink": "#76D6B0",
                        "colorEditorSelection": "#17382D"},
              "algorithm": "dark"}
ENABLE_UI_THEME_ADMINISTRATION = True

# Categorical palettes offered in every chart's colour picker.
EXTRA_CATEGORICAL_COLOR_SCHEMES = [
    {"id": "thaqipLedger", "description": "Thaqip ledger", "label": "Thaqip · Ledger",
     "isDefault": True,
     "colors": ["#075E46", "#24798A", "#A66A12", "#A84032", "#5B7F3A", "#6B5B95",
                "#2E7C8C", "#C08A2E", "#3E5C76", "#8C4A6B"]},
    {"id": "thaqipWinLoss", "description": "Won / lost", "label": "Thaqip · Win-Loss",
     "colors": ["#087B55", "#A84D10", "#8A948F", "#24798A"]},
]
EXTRA_SEQUENTIAL_COLOR_SCHEMES = [
    {"id": "thaqipGreens", "label": "Thaqip · Greens", "isDiverging": False,
     "colors": ["#EEF6F1", "#C4E2D3", "#8CC5AB", "#4E9F80", "#1D7A5A", "#075E46", "#053F2F"]},
]

# ------------------------------------------------------------------ web
# Laptop: plain HTTP over loopback / Tailscale, where a Secure cookie would
# never be sent. Production sits behind Cloudflare (TLS at the edge) and sets
# SUPERSET_COOKIE_SECURE=1 in docker-compose.prod.yml.
SESSION_COOKIE_SECURE = os.environ.get("SUPERSET_COOKIE_SECURE", "0") == "1"
SESSION_COOKIE_SAMESITE = "Lax"
TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "content_security_policy": {
        "base-uri": ["'self'"],
        "default-src": ["'self'"],
        "img-src": ["'self'", "blob:", "data:"],
        "worker-src": ["'self'", "blob:"],
        "connect-src": ["'self'"],
        "object-src": "'none'",
        "style-src": ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
        "font-src": ["'self'", "data:", "https://fonts.gstatic.com"],
        "script-src": ["'self'", "'strict-dynamic'"],
    },
    "content_security_policy_nonce_in": ["script-src"],
    "force_https": False,
    "session_cookie_secure": SESSION_COOKIE_SECURE,
}
PREVENT_UNSAFE_DB_CONNECTIONS = True
