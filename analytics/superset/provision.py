"""Provision Thaqip Analytics content in Superset through its REST API.

Idempotent: the database connection, datasets, charts and dashboard are found
by name and updated in place, so re-running after a view or chart change is
safe and keeps ids (and any links people saved) stable.

    cd services/ingestion && uv run python ../../analytics/superset/provision.py

Reads SUPERSET_URL / SUPERSET_ADMIN_USER / SUPERSET_ADMIN_PASSWORD /
SUPERSET_RO_PASSWORD from the environment or var/credentials.env.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
DB_NAME = "Thaqip · analytics (read-only)"
DASHBOARD_TITLE = "ثاقب · ذكاء السوق"
DASHBOARD_SLUG = "thaqip-market"
SCHEME = "thaqipLedger"


def _env() -> dict[str, str]:
    vals: dict[str, str] = {}
    cred = REPO / "var" / "credentials.env"
    if cred.exists():
        for line in cred.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
    vals.update({k: v for k, v in os.environ.items() if k.startswith("SUPERSET_")})
    return vals


# ---------------------------------------------------------------- client
class Superset:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.c = httpx.Client(base_url=base, timeout=120)
        r = self.c.post("/api/v1/security/login", json={
            "username": user, "password": password, "provider": "db", "refresh": True})
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        self.c.headers["X-CSRFToken"] = self.c.get("/api/v1/security/csrf_token/").json()["result"]
        self.c.headers["Referer"] = base

    def req(self, method: str, path: str, **kw: Any) -> Any:
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.content else None

    def all(self, resource: str) -> list[dict[str, Any]]:
        out, page = [], 0
        while True:
            body = self.req("GET", f"/api/v1/{resource}/", params={"q": f"(page:{page},page_size:100)"})
            out += body["result"]
            if len(out) >= body["count"] or not body["result"]:
                return out
            page += 1

    def find(self, resource: str, field: str, value: str) -> int | None:
        for row in self.all(resource):
            if row.get(field) == value:
                return row["id"]
        return None


# --------------------------------------------------------------- datasets
# column -> Arabic label. Unlisted columns keep their names.
LABELS = {
    "tender_id": "رقم المنافسة", "source": "المصدر", "reference_number": "الرقم المرجعي",
    "tender_name": "المنافسة", "agency": "الجهة", "branch": "الفرع", "activity": "النشاط",
    "tender_type": "نوع المنافسة", "stage": "المرحلة", "inside_ksa": "داخل المملكة",
    "booklet_price": "قيمة الكراسة", "financial_fees": "الرسوم", "published_at": "تاريخ النشر",
    "last_offer_date": "آخر موعد للعروض", "offers_opening_date": "فتح العروض",
    "event_date": "التاريخ", "days_to_close": "أيام حتى الإغلاق", "offers_n": "عدد العروض",
    "lowest_offer": "أقل عرض", "highest_offer": "أعلى عرض", "median_offer": "وسيط العروض",
    "spread_pct": "تشتت العروض %", "award_value": "قيمة الترسية", "winner": "الفائز",
    "lowest_won": "فاز أقل عرض", "award_band": "شريحة القيمة", "offer_id": "رقم العرض",
    "vendor": "المورد", "offer_value": "قيمة العرض", "is_winner": "فائز", "outcome": "النتيجة",
    "technical_pass": "اجتاز الفني", "offer_rank": "ترتيب العرض", "bidders_n": "عدد المتنافسين",
    "ratio_to_lowest": "نسبة إلى أقل عرض", "ratio_to_median": "نسبة إلى الوسيط",
    "award_id": "رقم الترسية", "discount_vs_median_pct": "الخصم عن الوسيط %",
    "bids": "العروض", "wins": "مرات الفوز", "win_rate_pct": "نسبة الفوز %",
    "awarded_value": "قيمة الترسيات", "avg_ratio_to_lowest": "متوسط النسبة لأقل عرض",
    "agencies_n": "عدد الجهات", "activities_n": "عدد الأنشطة", "first_seen": "أول ظهور",
    "last_seen": "آخر ظهور", "tenders": "المنافسات", "open_now": "مفتوحة الآن",
    "awarded": "مُرسّاة", "avg_bidders": "متوسط المتقدمين", "lowest_wins_pct": "أقل عرض يفوز %",
    "avg_spread_pct": "متوسط التشتت %", "item_id": "رقم البند", "item_no": "رقم البند بالكراسة",
    "description": "الوصف", "unit": "الوحدة", "qty": "الكمية", "confidence": "ثقة الاستخراج",
    "run_id": "رقم التشغيل", "connector": "الموصل", "started_at": "بدأ", "finished_at": "انتهى",
    "duration_s": "المدة (ث)", "ok": "نجح", "result": "النتيجة", "pages": "الصفحات",
    "items_seen": "عناصر مقروءة", "items_new": "عناصر جديدة", "items_changed": "عناصر متغيرة",
    "error": "الخطأ",
}

DATASETS: dict[str, dict[str, Any]] = {
    "tenders": {
        "dttm": "event_date",
        "description": "كل منافسة في أرشيف اعتماد وفرصة مع أرقام العروض والترسية المشتقة.",
        "metrics": {
            "count": ("عدد المنافسات", "COUNT(*)", ",d"),
            "open_n": ("مفتوحة الآن", "COUNT(*) FILTER (WHERE stage = 'مفتوحة')", ",d"),
            "awarded_n": ("مُرسّاة", "COUNT(*) FILTER (WHERE stage = 'مُرسّاة')", ",d"),
            "awarded_value": ("قيمة الترسيات (ر.س)", "SUM(award_value)", ",.0f"),
            "median_award": ("وسيط الترسية (ر.س)",
                             "percentile_cont(0.5) WITHIN GROUP (ORDER BY award_value)", ",.0f"),
            "avg_bidders": ("متوسط المتقدمين", "AVG(NULLIF(offers_n, 0))", ".2f"),
            "lowest_wins_pct": ("أقل عرض يفوز %",
                                "100.0 * AVG(CASE WHEN lowest_won THEN 1.0 "
                                "WHEN lowest_won IS NOT NULL THEN 0.0 END)", ".1f"),
            "avg_spread": ("متوسط تشتت العروض %", "AVG(spread_pct)", ".1f"),
        },
    },
    "offers": {
        "dttm": "event_date",
        "description": "كل عرض مالي منشور، مع موقعه من أقل عرض ووسيط العروض في منافسته.",
        "metrics": {
            "count": ("عدد العروض", "COUNT(*)", ",d"),
            "offer_value_sum": ("مجموع العروض (ر.س)", "SUM(offer_value)", ",.0f"),
            "win_rate": ("نسبة الفوز %", "100.0 * AVG(CASE WHEN is_winner THEN 1.0 ELSE 0.0 END)", ".1f"),
            "avg_ratio_to_lowest": ("متوسط النسبة لأقل عرض", "AVG(ratio_to_lowest)", ".3f"),
            "vendors_n": ("عدد الموردين", "COUNT(DISTINCT vendor)", ",d"),
        },
    },
    "awards": {
        "dttm": "event_date",
        "description": "الترسيات المنشورة مع الفائز والخصم عن وسيط العروض.",
        "metrics": {
            "count": ("عدد الترسيات", "COUNT(*)", ",d"),
            "award_sum": ("قيمة الترسيات (ر.س)", "SUM(award_value)", ",.0f"),
            "avg_discount": ("متوسط الخصم عن الوسيط %", "AVG(discount_vs_median_pct)", ".1f"),
        },
    },
    "vendor_scorecard": {
        "dttm": "last_seen",
        "description": "بطاقة أداء لكل مورد عبر كل عروضه المرصودة.",
        "metrics": {
            "sum_bids": ("العروض", "SUM(bids)", ",d"),
            "sum_wins": ("مرات الفوز", "SUM(wins)", ",d"),
            "sum_awarded": ("قيمة الترسيات (ر.س)", "SUM(awarded_value)", ",.0f"),
            "win_rate_w": ("نسبة الفوز %", "100.0 * SUM(wins) / NULLIF(SUM(bids), 0)", ".1f"),
        },
    },
    "agency_scorecard": {
        "dttm": None,
        "description": "بطاقة لكل جهة حكومية: الحجم، المنافسة، وسلوك الترسية.",
        "metrics": {
            "sum_tenders": ("المنافسات", "SUM(tenders)", ",d"),
            "sum_awarded": ("قيمة الترسيات (ر.س)", "SUM(awarded_value)", ",.0f"),
        },
    },
    "boq_items": {
        "dttm": "event_date",
        "description": "بنود جداول الكميات المستخرجة من كراسات الشروط.",
        "metrics": {"count": ("عدد البنود", "COUNT(*)", ",d")},
    },
    "ingest_runs": {
        "dttm": "started_at",
        "description": "كل تشغيل لموصلات الاستيعاب — للصحة التشغيلية.",
        "metrics": {
            "count": ("عدد التشغيلات", "COUNT(*)", ",d"),
            "success_pct": ("نسبة النجاح %", "100.0 * AVG(CASE WHEN ok THEN 1.0 ELSE 0.0 END)", ".1f"),
            "items_new_sum": ("عناصر جديدة", "SUM(items_new)", ",d"),
            "avg_duration": ("متوسط المدة (ث)", "AVG(duration_s)", ".1f"),
        },
    },
}

_COL_KEYS = ("id", "column_name", "verbose_name", "description", "expression", "filterable",
             "groupby", "is_active", "is_dttm", "python_date_format", "type")
_MET_KEYS = ("id", "metric_name", "verbose_name", "expression", "metric_type", "d3format",
             "description", "warning_text")


def ensure_database(ss: Superset, ro_password: str) -> int:
    payload = {
        "database_name": DB_NAME,
        "sqlalchemy_uri": f"postgresql+psycopg2://superset_ro:{ro_password}@postgres:5432/thaqip",
        "expose_in_sqllab": True, "allow_ctas": False, "allow_cvas": False, "allow_dml": False,
        "allow_file_upload": False, "allow_run_async": False, "cache_timeout": 600,
        "extra": json.dumps({"allows_virtual_table_explore": True, "cost_estimate_enabled": True,
                             "schema_options": {"expand_rows": False},
                             "engine_params": {"connect_args": {"application_name": "superset"}}}),
    }
    dbid = ss.find("database", "database_name", DB_NAME)
    if dbid is None:
        dbid = ss.req("POST", "/api/v1/database/", json=payload)["id"]
    else:
        ss.req("PUT", f"/api/v1/database/{dbid}", json=payload)
    return dbid


def ensure_dataset(ss: Superset, dbid: int, table: str, spec: dict[str, Any]) -> int:
    existing = [d for d in ss.all("dataset")
                if d["table_name"] == table and d.get("schema") == "analytics"]
    if existing:
        dsid = existing[0]["id"]
        ss.req("PUT", f"/api/v1/dataset/{dsid}/refresh")
    else:
        dsid = ss.req("POST", "/api/v1/dataset/",
                      json={"database": dbid, "schema": "analytics", "table_name": table})["id"]
    ds = ss.req("GET", f"/api/v1/dataset/{dsid}")["result"]
    columns = []
    for col in ds["columns"]:
        c = {k: col.get(k) for k in _COL_KEYS if k in col}
        c["verbose_name"] = LABELS.get(col["column_name"], col.get("verbose_name"))
        c["is_dttm"] = col["column_name"] == spec["dttm"] or bool(col.get("is_dttm"))
        c["groupby"] = True
        c["filterable"] = True
        columns.append({k: v for k, v in c.items() if v is not None})
    by_name = {m["metric_name"]: m for m in ds["metrics"]}
    metrics = []
    for name, (label, expr, fmt) in spec["metrics"].items():
        m = {k: by_name[name].get(k) for k in _MET_KEYS if k in by_name.get(name, {})}
        m.update({"metric_name": name, "verbose_name": label, "expression": expr, "d3format": fmt})
        metrics.append({k: v for k, v in m.items() if v is not None})
    for name, m in by_name.items():            # keep metrics people added in the UI
        if name not in spec["metrics"]:
            metrics.append({k: m.get(k) for k in _MET_KEYS if m.get(k) is not None})
    ss.req("PUT", f"/api/v1/dataset/{dsid}", params={"override_columns": "false"}, json={
        "description": spec["description"],
        "main_dttm_col": spec["dttm"],
        "cache_timeout": 600,
        "columns": columns,
        "metrics": metrics,
    })
    return dsid


# ----------------------------------------------------------------- charts
def _flt(col: str, op: str, val: Any = None) -> dict[str, Any]:
    f = {"expressionType": "SIMPLE", "subject": col, "operator": op, "clause": "WHERE",
         "filterOptionName": f"f_{uuid.uuid5(uuid.NAMESPACE_URL, col + op + str(val)).hex[:12]}"}
    if val is not None:
        f["comparator"] = val
    return f


def chart_specs(ds: dict[str, int]) -> list[dict[str, Any]]:
    """(key, name, dataset, viz, form_data). Keys place charts in the layout."""
    big = {"header_font_size": 0.4, "subheader_font_size": 0.13, "y_axis_format": "SMART_NUMBER",
           "time_range": "No filter"}
    return [
        # --- market tab
        dict(key="kpi_open", name="منافسات مفتوحة الآن", ds="tenders", viz="big_number_total",
             fd={**big, "metric": "open_n", "subheader": "تقبل العروض اليوم"}),
        dict(key="kpi_awarded", name="قيمة الترسيات المرصودة", ds="tenders", viz="big_number_total",
             fd={**big, "metric": "awarded_value", "subheader": "ريال سعودي — ترسيات منشورة"}),
        dict(key="kpi_bidders", name="متوسط المتقدمين للمنافسة", ds="tenders", viz="big_number_total",
             fd={**big, "metric": "avg_bidders", "y_axis_format": ".2f",
                 "subheader": "من المنافسات ذات العروض المنشورة"}),
        dict(key="kpi_lowest", name="أقل عرض يفوز", ds="tenders", viz="big_number_total",
             fd={**big, "metric": "lowest_wins_pct", "y_axis_format": ".1f",
                 "subheader": "% من الترسيات متعددة العروض"}),
        dict(key="flow", name="المنافسات المنشورة أسبوعيًا حسب مرحلتها الآن", ds="tenders",
             viz="echarts_timeseries_bar",
             fd={"x_axis": "published_at", "time_grain_sqla": "P1W", "metrics": ["count"],
                 "groupby": ["stage"], "stack": "Stack", "row_limit": 10000,
                 "show_legend": True, "legendOrientation": "top", "rich_tooltip": True,
                 "zoomable": True, "time_range": "No filter", "y_axis_format": "SMART_NUMBER",
                 "x_axis_time_format": "smart_date", "truncateYAxis": False}),
        dict(key="treemap", name="أين تذهب قيمة الترسيات: الجهة ← النشاط", ds="tenders",
             viz="treemap_v2",
             fd={"groupby": ["agency", "activity"], "metric": "awarded_value", "row_limit": 300,
                 "adhoc_filters": [_flt("award_value", "IS NOT NULL")], "show_labels": True,
                 "show_upper_labels": True, "label_type": "key_value",
                 "number_format": "SMART_NUMBER", "time_range": "No filter"}),
        dict(key="sunburst", name="خريطة الأنشطة وأنواع المنافسات", ds="tenders", viz="sunburst_v2",
             fd={"columns": ["activity", "tender_type"], "metric": "count", "row_limit": 400,
                 "label_type": "key", "show_labels": True, "show_labels_threshold": 3,
                 "number_format": "SMART_NUMBER", "time_range": "No filter"}),
        dict(key="words", name="أكثر الأنشطة طرحًا", ds="tenders", viz="word_cloud",
             fd={"series": "activity", "metric": "count", "row_limit": 60, "size_from": 12,
                 "size_to": 54, "rotation": "flat", "time_range": "No filter"}),
        dict(key="open_list", name="منافسات مفتوحة تغلق قريبًا", ds="tenders", viz="table",
             fd={"query_mode": "raw",
                 "all_columns": ["tender_name", "agency", "activity", "tender_type",
                                 "last_offer_date", "days_to_close", "booklet_price"],
                 "order_by_cols": [json.dumps(["days_to_close", True])],
                 "adhoc_filters": [_flt("stage", "==", "مفتوحة"), _flt("days_to_close", ">=", 0)],
                 "row_limit": 2000, "include_search": True, "page_length": 15,
                 "show_cell_bars": False, "table_timestamp_format": "%Y-%m-%d",
                 "column_config": {"booklet_price": {"d3NumberFormat": ",.0f"}},
                 "time_range": "No filter"}),
        dict(key="agencies", name="بطاقات أداء الجهات", ds="agency_scorecard", viz="table",
             fd={"query_mode": "raw",
                 "all_columns": ["agency", "tenders", "open_now", "awarded", "awarded_value",
                                 "avg_bidders", "lowest_wins_pct", "avg_spread_pct"],
                 "order_by_cols": [json.dumps(["tenders", False])], "row_limit": 1000,
                 "include_search": True, "page_length": 15, "show_cell_bars": True,
                 "column_config": {"awarded_value": {"d3NumberFormat": ",.0f"},
                                   "avg_bidders": {"d3NumberFormat": ".2f"}},
                 "time_range": "No filter"}),
        # --- competitors tab
        dict(key="vendors_bubble", name="خريطة الموردين: الحجم × نسبة الفوز × القيمة",
             ds="vendor_scorecard", viz="bubble_v2",
             fd={"entity": "vendor", "x": "sum_bids", "y": "win_rate_w", "size": "sum_awarded",
                 # Every vendor with 3+ bids (~100 today) — no ordering needed,
                 # and bubble_v2's orderby control rejects saved-metric names.
                 "max_bubble_size": "40", "row_limit": 500,
                 "adhoc_filters": [_flt("bids", ">=", 3)],
                 "x_axis_label": "عدد العروض", "y_axis_label": "نسبة الفوز %",
                 "xAxisFormat": "SMART_NUMBER", "y_axis_format": ".0f",
                 "show_legend": False, "time_range": "No filter"}),
        dict(key="vendors_top", name="أكثر الموردين فوزًا", ds="vendor_scorecard",
             viz="echarts_timeseries_bar",
             fd={"x_axis": "vendor", "metrics": ["sum_wins"], "groupby": [], "row_limit": 15,
                 "orientation": "horizontal", "x_axis_sort": "sum_wins", "x_axis_sort_asc": False,
                 "timeseries_limit_metric": "sum_wins", "order_desc": True,
                 "show_legend": False, "show_value": True, "time_range": "No filter"}),
        dict(key="vendors_table", name="بطاقات أداء الموردين", ds="vendor_scorecard", viz="table",
             fd={"query_mode": "raw",
                 "all_columns": ["vendor", "bids", "wins", "win_rate_pct", "awarded_value",
                                 "avg_ratio_to_lowest", "agencies_n", "activities_n", "last_seen"],
                 "order_by_cols": [json.dumps(["awarded_value", False])], "row_limit": 1000,
                 "include_search": True, "page_length": 15, "show_cell_bars": True,
                 "column_config": {"awarded_value": {"d3NumberFormat": ",.0f"},
                                   "avg_ratio_to_lowest": {"d3NumberFormat": ".2f"}},
                 "table_timestamp_format": "%Y-%m-%d", "time_range": "No filter"}),
        dict(key="sankey", name="من يفوز عند من: الجهة ← الفائز (القيمة)", ds="awards",
             viz="sankey_v2",
             fd={"source": "agency", "target": "winner", "metric": "award_sum", "row_limit": 40,
                 "time_range": "No filter"}),
        # --- pricing tab
        # Ratios above 5x are real (one valuation tender spans 3,450-345,000
        # SAR) but flatten every box; the title says what is shown.
        dict(key="box_ratio", name="كم يبعد كل عرض عن أقل عرض — حسب النشاط (حتى 5 أضعاف)",
             ds="offers", viz="box_plot",
             fd={"columns": ["offer_id"], "groupby": ["activity"],
                 "adhoc_filters": [_flt("ratio_to_lowest", "<=", 5)],
                 "metrics": ["avg_ratio_to_lowest"], "whiskerOptions": "Tukey",
                 "number_format": ".2f", "series_limit": 12, "row_limit": 10000,
                 "time_range": "No filter", "x_ticks_layout": "45°"}),
        dict(key="spread_hist", name="توزيع تشتت العروض داخل المنافسة (حتى 500%)", ds="tenders",
             viz="histogram_v2",
             fd={"column": "spread_pct", "groupby": [], "bins": 25, "normalize": False,
                 "adhoc_filters": [_flt("spread_pct", "<=", 500)],
                 "cumulative": False, "row_limit": 10000,
                 "x_axis_title": "(أعلى عرض ÷ أقل عرض − 1) %", "y_axis_title": "منافسات",
                 "show_value": False, "time_range": "No filter"}),
        dict(key="bands", name="شرائح قيم الترسيات", ds="awards", viz="pie",
             fd={"groupby": ["award_band"], "metric": "count", "donut": True, "show_labels": True,
                 "label_type": "key_percent", "sort_by_metric": False, "row_limit": 10,
                 "show_legend": True, "legendOrientation": "bottom", "time_range": "No filter"}),
        dict(key="heat", name="الترسيات: النشاط × شريحة القيمة", ds="awards",
             viz="pivot_table_v2",
             fd={"groupbyRows": ["activity"], "groupbyColumns": ["award_band"],
                 "metrics": ["count"], "aggregateFunction": "Sum", "rowTotals": True,
                 "colTotals": True, "rowOrder": "value_z_to_a", "colOrder": "key_a_to_z",
                 "valueFormat": ",d", "row_limit": 5000, "time_range": "No filter",
                 "conditional_formatting": [{"colorScheme": "#075E46", "column": "count",
                                             "operator": ">", "targetValue": 0}]}),
        dict(key="discount", name="متوسط الخصم عن وسيط العروض حسب النشاط", ds="awards",
             viz="echarts_timeseries_bar",
             fd={"x_axis": "activity", "metrics": ["avg_discount"], "groupby": [],
                 "row_limit": 15, "orientation": "horizontal",
                 "x_axis_sort": "avg_discount", "x_axis_sort_asc": False,
                 "timeseries_limit_metric": "count", "order_desc": True,
                 "adhoc_filters": [_flt("offers_n", ">=", 2)], "show_value": True,
                 "y_axis_format": ".1f", "show_legend": False, "time_range": "No filter"}),
        # --- pipeline tab
        dict(key="ops_success", name="نجاح تشغيلات الاستيعاب", ds="ingest_runs",
             viz="big_number_total",
             fd={**big, "metric": "success_pct", "y_axis_format": ".1f",
                 "subheader": "% من كل التشغيلات"}),
        dict(key="ops_new", name="عناصر جديدة يوميًا حسب الموصل", ds="ingest_runs",
             viz="echarts_timeseries_line",
             fd={"x_axis": "started_at", "time_grain_sqla": "P1D", "metrics": ["items_new_sum"],
                 "groupby": ["connector"], "row_limit": 10000, "show_legend": True,
                 "legendOrientation": "top", "rich_tooltip": True, "markerEnabled": True,
                 "time_range": "No filter"}),
        dict(key="ops_failures", name="آخر التشغيلات الفاشلة", ds="ingest_runs", viz="table",
             fd={"query_mode": "raw",
                 "all_columns": ["started_at", "connector", "duration_s", "error"],
                 "order_by_cols": [json.dumps(["started_at", False])],
                 "adhoc_filters": [_flt("ok", "IS FALSE")], "row_limit": 50,
                 "page_length": 10, "table_timestamp_format": "%Y-%m-%d %H:%M",
                 "time_range": "No filter"}),
    ]


# Charts this provisioner used to create under names it no longer uses.
RETIRED = [
    "تدفق المنافسات أسبوعيًا حسب المرحلة",
    "كم يبعد كل عرض عن أقل عرض — حسب النشاط",
    "توزيع تشتت العروض داخل المنافسة",
    "حرارة الترسيات: النشاط × شريحة القيمة",
]


def ensure_chart(ss: Superset, existing: dict[str, int], dash_id: int, spec: dict[str, Any],
                 ds: dict[str, int]) -> int:
    dsid = ds[spec["ds"]]
    fd = {"datasource": f"{dsid}__table", "viz_type": spec["viz"], "adhoc_filters": [],
          "color_scheme": SCHEME, **spec["fd"]}
    body = {"slice_name": spec["name"], "viz_type": spec["viz"], "datasource_id": dsid,
            "datasource_type": "table", "params": json.dumps(fd, ensure_ascii=False),
            "dashboards": [dash_id]}
    cid = existing.get(spec["name"])
    if cid is None:
        return ss.req("POST", "/api/v1/chart/", json=body)["id"]
    ss.req("PUT", f"/api/v1/chart/{cid}", json=body)
    return cid


# -------------------------------------------------------------- dashboard
LAYOUT = [  # tab title -> rows of (chart key, width /12, height)
    ("السوق", [
        [("kpi_open", 3, 26), ("kpi_awarded", 3, 26), ("kpi_bidders", 3, 26), ("kpi_lowest", 3, 26)],
        [("flow", 7, 56), ("sunburst", 5, 56)],
        [("treemap", 12, 64)],
        [("open_list", 7, 64), ("words", 5, 64)],
        [("agencies", 12, 64)],
    ]),
    ("المنافسون", [
        [("vendors_bubble", 7, 64), ("vendors_top", 5, 64)],
        [("sankey", 12, 72)],
        [("vendors_table", 12, 64)],
    ]),
    ("التسعير", [
        [("box_ratio", 12, 64)],
        [("spread_hist", 6, 56), ("bands", 6, 56)],
        [("heat", 7, 72), ("discount", 5, 72)],
    ]),
    ("خط الاستيعاب", [
        [("ops_success", 3, 40), ("ops_new", 9, 40)],
        [("ops_failures", 12, 56)],
    ]),
]

INTRO_MD = """<div dir="rtl" style="font-family:'IBM Plex Sans Arabic',sans-serif;line-height:1.7">

**ثاقب · ذكاء السوق** — بيانات المنافسات العامة المرصودة من اعتماد وفرصة.

- **الحفر للتفاصيل:** انقر بالزر الأيمن على أي عنصر ← *Drill to detail* لرؤية الصفوف، أو *Drill by* للتقسيم بعمود آخر.
- **الفلترة المتقاطعة:** انقر على عنصر في أي رسم لفلترة بقية اللوحة.
- **التخصيص:** زر *Edit dashboard* لسحب الرسوم وتغيير أحجامها وإضافة رسوم جديدة وتغيير الثيم ولوحة الألوان.
- **رسم جديد:** قائمة *+ ← Chart* ثم اختر مجموعة بيانات من مخطط `analytics`.
</div>"""


def _id(prefix: str, key: str) -> str:
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_URL, 'thaqip/' + key).hex[:10]}"


def build_position(charts: dict[str, int], names: dict[str, str]) -> dict[str, Any]:
    pos: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": DASHBOARD_TITLE}},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
    }
    intro_row, intro = _id("ROW", "intro"), _id("MARKDOWN", "intro")
    tabs_id = _id("TABS", "main")
    pos["GRID_ID"]["children"] = [intro_row, tabs_id]
    pos[intro_row] = {"type": "ROW", "id": intro_row, "children": [intro],
                      "parents": ["ROOT_ID", "GRID_ID"], "meta": {"background": "BACKGROUND_TRANSPARENT"}}
    pos[intro] = {"type": "MARKDOWN", "id": intro, "children": [],
                  "parents": ["ROOT_ID", "GRID_ID", intro_row],
                  "meta": {"width": 12, "height": 22, "code": INTRO_MD}}
    pos[tabs_id] = {"type": "TABS", "id": tabs_id, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
                    "meta": {}}
    for t_i, (title, rows) in enumerate(LAYOUT):
        tab = _id("TAB", f"tab{t_i}")
        pos[tabs_id]["children"].append(tab)
        tab_parents = ["ROOT_ID", "GRID_ID", tabs_id]
        pos[tab] = {"type": "TAB", "id": tab, "children": [], "parents": tab_parents,
                    "meta": {"text": title, "defaultText": "Tab title", "placeholder": "Tab title"}}
        for r_i, row in enumerate(rows):
            rid = _id("ROW", f"{t_i}-{r_i}")
            pos[tab]["children"].append(rid)
            row_parents = [*tab_parents, tab]
            pos[rid] = {"type": "ROW", "id": rid, "children": [], "parents": row_parents,
                        "meta": {"background": "BACKGROUND_TRANSPARENT"}}
            for key, width, height in row:
                cid = _id("CHART", key)
                pos[rid]["children"].append(cid)
                pos[cid] = {"type": "CHART", "id": cid, "children": [],
                            "parents": [*row_parents, rid],
                            "meta": {"chartId": charts[key], "width": width, "height": height,
                                     "sliceName": names[key], "uuid": str(uuid.uuid5(
                                         uuid.NAMESPACE_URL, f"thaqip/chart/{key}"))}}
    return pos


def native_filters(ds: dict[str, int], chart_ids: list[int]) -> list[dict[str, Any]]:
    def select(key: str, name: str, dataset: str, column: str) -> dict[str, Any]:
        return {"id": f"NATIVE_FILTER-{key}", "name": name, "filterType": "filter_select",
                "type": "NATIVE_FILTER", "targets": [{"datasetId": ds[dataset], "column": {"name": column}}],
                "controlValues": {"multiSelect": True, "enableEmptyFilter": False,
                                  "defaultToFirstItem": False, "searchAllOptions": True,
                                  "inverseSelection": False, "sortAscending": True},
                "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
                "cascadeParentIds": [], "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                "chartsInScope": chart_ids, "tabsInScope": [], "description": ""}
    time_filter = {
        "id": "NATIVE_FILTER-period", "name": "الفترة", "filterType": "filter_time",
        "type": "NATIVE_FILTER", "targets": [{}], "controlValues": {"enableEmptyFilter": False},
        "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
        "cascadeParentIds": [], "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "chartsInScope": chart_ids, "tabsInScope": [], "description": ""}
    return [time_filter,
            select("agency", "الجهة", "tenders", "agency"),
            select("activity", "النشاط", "tenders", "activity"),
            select("type", "نوع المنافسة", "tenders", "tender_type"),
            select("vendor", "المورد", "offers", "vendor")]


DASH_CSS = """
/* Thaqip: Arabic-first typography inside dashboards */
.dashboard, .dashboard-markdown, .header-title, .chart-header, .ant-tabs-tab {
  font-family: 'IBM Plex Sans Arabic', Tahoma, sans-serif !important;
}
.chart-header .header-title { direction: rtl; text-align: right; }
.dashboard-component-chart-holder { border-radius: 14px; }
"""


# Named themes for the per-dashboard theme picker (and Settings -> Themes).
# The system default/dark themes come from superset_config.py.
_FONT = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700"
         "&family=IBM+Plex+Mono:wght@400;600&display=swap")
_TYPE = {"fontUrls": [_FONT], "fontFamily": "'IBM Plex Sans Arabic', Tahoma, Arial, sans-serif",
         "fontFamilyCode": "'IBM Plex Mono', monospace", "borderRadius": 10}
THEMES = {
    "Thaqip · Ledger": {"algorithm": "default", "token": {
        **_TYPE, "colorPrimary": "#075E46", "colorLink": "#075E46", "colorSuccess": "#087B55",
        "colorWarning": "#8F5608", "colorError": "#A84032", "colorInfo": "#24798A",
        "colorBgLayout": "#F5F1E8", "colorBgContainer": "#FFFEFA"}},
    "Thaqip · Night": {"algorithm": "dark", "token": {
        **_TYPE, "colorPrimary": "#2FA37C", "colorLink": "#76D6B0", "colorSuccess": "#3DBB8A",
        "colorWarning": "#E4B45F", "colorError": "#E07A6A", "colorInfo": "#7ED0DF",
        "colorBgLayout": "#0D1714", "colorBgContainer": "#14221D"}},
    "Thaqip · Board (high contrast)": {"algorithm": "default", "token": {
        **_TYPE, "colorPrimary": "#053F2F", "colorLink": "#053F2F", "colorText": "#0A1210",
        "colorBgLayout": "#FFFFFF", "colorBgContainer": "#FFFFFF", "fontSize": 16,
        "borderRadius": 6}},
}


def ensure_themes(ss: Superset) -> dict[str, int]:
    existing = {t["theme_name"]: t["id"] for t in ss.req("GET", "/api/v1/theme/",
                params={"q": "(page_size:100)"})["result"]}
    ids = {}
    for name, body in THEMES.items():
        payload = {"theme_name": name, "json_data": json.dumps(body, ensure_ascii=False)}
        if name in existing:
            ss.req("PUT", f"/api/v1/theme/{existing[name]}", json=payload)
            ids[name] = existing[name]
        else:
            ids[name] = ss.req("POST", "/api/v1/theme/", json=payload)["id"]
    return ids


def main() -> int:
    env = _env()
    missing = [k for k in ("SUPERSET_URL", "SUPERSET_ADMIN_USER", "SUPERSET_ADMIN_PASSWORD",
                           "SUPERSET_RO_PASSWORD") if not env.get(k)]
    if missing:
        print(f"missing {missing}; run bin/bootstrap-superset.sh", file=sys.stderr)
        return 2
    ss = Superset(env["SUPERSET_URL"], env["SUPERSET_ADMIN_USER"], env["SUPERSET_ADMIN_PASSWORD"])

    print(f"themes {ensure_themes(ss)}")
    dbid = ensure_database(ss, env["SUPERSET_RO_PASSWORD"])
    print(f"database {dbid}")
    ds = {name: ensure_dataset(ss, dbid, name, spec) for name, spec in DATASETS.items()}
    print(f"datasets {ds}")

    dash_id = ss.find("dashboard", "dashboard_title", DASHBOARD_TITLE)
    if dash_id is None:
        dash_id = ss.req("POST", "/api/v1/dashboard/", json={
            "dashboard_title": DASHBOARD_TITLE, "slug": DASHBOARD_SLUG, "published": True})["id"]

    specs = chart_specs(ds)
    existing = {c["slice_name"]: c["id"] for c in ss.all("chart")}
    for name in RETIRED:
        if name in existing:
            ss.req("DELETE", f"/api/v1/chart/{existing.pop(name)}")
    charts = {s["key"]: ensure_chart(ss, existing, dash_id, s, ds) for s in specs}
    names = {s["key"]: s["name"] for s in specs}
    print(f"charts {len(charts)}")

    position = build_position(charts, names)
    metadata = {
        "color_scheme": SCHEME, "label_colors": {}, "shared_label_colors": [],
        "color_scheme_domain": [], "refresh_frequency": 0, "timed_refresh_immune_slices": [],
        "expanded_slices": {}, "cross_filters_enabled": True,
        "native_filter_configuration": native_filters(ds, list(charts.values())),
        "chart_configuration": {},
        "global_chart_configuration": {"scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                                       "chartsInScope": list(charts.values())},
        "filter_bar_orientation": "HORIZONTAL",
    }
    ss.req("PUT", f"/api/v1/dashboard/{dash_id}", json={
        "dashboard_title": DASHBOARD_TITLE, "slug": DASHBOARD_SLUG, "published": True,
        "position_json": json.dumps(position, ensure_ascii=False),
        "json_metadata": json.dumps(metadata, ensure_ascii=False),
        "css": DASH_CSS,
    })
    print(f"dashboard {dash_id}: {env['SUPERSET_URL']}/superset/dashboard/{DASHBOARD_SLUG}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
