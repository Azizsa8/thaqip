# Thaqip Analytics (Apache Superset 6.1)

Self-serve BI over the public tender corpus: dashboards, drill-downs, custom
charts, SQL Lab, themes. Runs as the `superset` compose service on
`127.0.0.1:8092` and the Tailscale address — the same exposure as the console.

## First run

```bash
bin/bootstrap-superset.sh
```

Generates secrets (`var/superset.env`, and the admin + read-only DB passwords
appended to `var/credentials.env`, both gitignored, mode 600), creates the
`superset` metadata database, the admin user, and provisions the content.

## Data boundary

Superset reads through the Postgres role `superset_ro`, which can `SELECT`
the `analytics` schema (db/migrations/0017) and nothing else: no public
table, no writes, no temp tables. That holds for every Superset user,
including admins in SQL Lab, because it is enforced by Postgres, not by
Superset. Tenant-private data (pursuits, bid scenarios, settings, users,
alerts, Telegram links) is therefore not reachable from here by design.

| dataset | what it is |
|---|---|
| `tenders` | every tender, stage, dates, offer stats, award, lowest-won |
| `offers` | every priced offer, ratio to lowest / median, outlier flag |
| `awards` | awards with winner and discount vs. the median offer |
| `vendor_scorecard` | bids, wins, win rate, awarded value per vendor |
| `agency_scorecard` | volume, competition and award behaviour per agency |
| `boq_items` | bill-of-quantity lines extracted from booklets |
| `ingest_runs` | pipeline health per connector run |

Offers under 5% or over 20× their tender's median are flagged `is_outlier`
(placeholder or unit-price bids) and never define "lowest", spread or ratios.

## Content

`provision.py` is idempotent — re-run it after changing a view or a chart:

```bash
cd services/ingestion && /home/ais04/.local/bin/uv run python ../../analytics/superset/provision.py
cd services/ingestion && /home/ais04/.local/bin/uv run --extra browser python ../../analytics/superset/verify.py
```

`verify.py` opens every chart in a real browser; the API accepts some form
data that the chart plugins reject at render time.

## What users can do

- **Drill:** right-click any mark → *Drill to detail* (rows) or *Drill by*
  (re-slice by another column); click a mark to cross-filter the dashboard.
- **Customise a dashboard:** *Edit dashboard* → drag to move, drag edges to
  resize, add rows/tabs/markdown, pick a colour palette, edit CSS, set a theme.
- **New charts:** *+ → Chart*, pick an `analytics` dataset and any of ~40
  chart types; saved metrics (Arabic-labelled) are ready to use.
- **Themes:** *Settings → Themes* (admin) for system themes; per-dashboard
  theme from the dashboard properties. Thaqip's palette ships as the default
  plus `Thaqip · Ledger`, `Thaqip · Win-Loss` and `Thaqip · Greens`.
- **Language:** Arabic by default (upstream catalog, compiled at build time,
  with curated corrections in `ar_overrides.json`); English from the flag menu.
