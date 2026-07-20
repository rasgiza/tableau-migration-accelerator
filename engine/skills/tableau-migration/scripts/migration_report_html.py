"""Render a **self-contained, offline** migration report (``migration-report.html``) from a
``report.json`` produced by :mod:`migrate_estate`.

Why this module exists
----------------------
``report.json`` is the accelerator's richest artifact, but it is machine-shaped. Migration leads
and report owners need a *human*, exec-facing view of the same facts: how much was translated,
which workbooks are signed off vs. need review, the calculation lineage, and the exact manual
follow-ups that remain. Commercial accelerators sell this as a "Migration Monitoring" dashboard;
here it is produced deterministically from data we already emit -- no server, no CDN, no JS.

Contract
--------
* **Pure + stdlib-only.** Input is the parsed ``report.json`` dict; output is a single HTML string.
  No network, no files read here (the CLI does the I/O). Deterministic for a given input.
* **Offline & safe to open from disk.** All CSS is inlined; there is no ``<script>`` and no external
  reference. **Every value drawn from the report is HTML-escaped** (:func:`_esc`) -- Tableau formula
  text, datasource names and follow-up strings are untrusted content and must never be able to
  inject markup when the file is opened in a browser.
* **Faithful to the report.** The report is rendered, not editorialised: a red definition-of-done
  status stays red. This view surfaces the honest gate, it does not paper over it.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# small, dependency-free helpers
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    """HTML-escape any value (``None`` -> empty). Quotes escaped too, for attribute safety."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _pct(part: Any, whole: Any) -> str:
    """``part/whole`` as a rounded percent string, guarding divide-by-zero. ``"73%"`` / ``"--"``."""
    try:
        p, w = float(part), float(whole)
    except (TypeError, ValueError):
        return "--"
    if w <= 0:
        return "--"
    return "%d%%" % round(100.0 * p / w)


_STATUS_CLASS = {
    "pass": "ok",
    "passed": "ok",
    "warn": "warn",
    "warned": "warn",
    "failed": "bad",
    "fail": "bad",
    "error": "bad",
    "skipped": "muted",
}


def _status_class(status: Any) -> str:
    """Map a report status token to a CSS badge class (defaults to ``muted``)."""
    return _STATUS_CLASS.get(str(status or "").lower(), "muted")


# ---------------------------------------------------------------------------
# section builders -- each returns an HTML fragment string
# ---------------------------------------------------------------------------

def _kpi_card(label: str, value: str, sub: str = "") -> str:
    sub_html = '<div class="kpi-sub">%s</div>' % _esc(sub) if sub else ""
    return (
        '<div class="kpi">'
        '<div class="kpi-value">%s</div>'
        '<div class="kpi-label">%s</div>%s</div>'
    ) % (_esc(value), _esc(label), sub_html)


def _render_header(report: Dict[str, Any]) -> str:
    src = report.get("source") or {}
    root = src.get("root") or src.get("kind") or "(unknown source)"
    generated = report.get("generated_at") or ""
    return (
        '<header>'
        '<h1>Tableau &rarr; Power BI / Fabric &mdash; Migration Report</h1>'
        '<div class="meta">Source: <code>%s</code> &nbsp;&middot;&nbsp; '
        'Generated: <code>%s</code> &nbsp;&middot;&nbsp; Tool: <code>%s</code></div>'
        '</header>'
    ) % (_esc(root), _esc(generated), _esc(report.get("tool") or "migrate_estate"))


def _render_dod_banner(report: Dict[str, Any]) -> str:
    dod = report.get("definition_of_done") or {}
    if not dod.get("applicable", True):
        return (
            '<section class="banner muted"><strong>Definition of done:</strong> '
            'not applicable (no report-binding stage ran).</section>'
        )
    status = dod.get("status", "unknown")
    cls = _status_class(status)
    bound = dod.get("reports_bound", 0)
    warned = dod.get("reports_warned", 0)
    failed = dod.get("reports_failed", 0)
    total = dod.get("workbooks_total", bound + warned + failed)
    verdict = {
        "ok": "All reports rebuilt and bound.",
        "warn": "Reports bound, some need review before sign-off.",
        "bad": "Not all reports could be rebuilt and bound &mdash; see the workbook table.",
    }.get(cls, "")
    return (
        '<section class="banner %s">'
        '<div class="banner-title">Definition of done: <strong>%s</strong></div>'
        '<div class="banner-body">%s of %s workbooks bound '
        '&nbsp;&middot;&nbsp; %s warned &nbsp;&middot;&nbsp; %s failed. %s</div>'
        '</section>'
    ) % (cls, _esc(status), _esc(bound), _esc(total), _esc(warned), _esc(failed), verdict)


def _render_kpis(report: Dict[str, Any]) -> str:
    s = report.get("summary") or {}
    cards = [
        _kpi_card(
            "Datasources migrated",
            "%s / %s" % (s.get("datasources_migrated", 0), s.get("datasources_total", 0)),
            "%s partial &middot; %s fallback"
            % (s.get("datasources_partial", 0), s.get("datasources_fallback", 0)),
        ),
        _kpi_card(
            "Model measures translated",
            "%s / %s" % (s.get("measures_translated", 0), s.get("measures_total", 0)),
            "%s stubbed" % s.get("measures_stubbed", 0),
        ),
        _kpi_card(
            "Model calc columns",
            "%s / %s" % (s.get("calc_columns_translated", 0), s.get("calc_columns_total", 0)),
            "%s stubbed" % s.get("calc_columns_stubbed", 0),
        ),
        _kpi_card(
            "Workbook calcs translated",
            _pct(s.get("workbook_calcs_translated"), s.get("workbook_calcs_total")),
            "%s of %s &middot; %s need review"
            % (
                s.get("workbook_calcs_translated", 0),
                s.get("workbook_calcs_total", 0),
                s.get("workbook_calcs_needs_review", 0),
            ),
        ),
        _kpi_card(
            "Visuals rebuilt",
            str(s.get("visuals_rebuilt", 0)),
            "%s with warnings" % s.get("visuals_warned", 0),
        ),
        _kpi_card(
            "Workbooks viz built",
            "%s / %s" % (s.get("workbooks_viz_built", 0), s.get("workbooks_total", 0)),
            "%s errored" % s.get("workbooks_viz_error", 0),
        ),
    ]
    return '<section><h2>Coverage</h2><div class="kpis">%s</div></section>' % "".join(cards)


def _render_signoff_table(report: Dict[str, Any]) -> str:
    dod = report.get("definition_of_done") or {}
    rows = dod.get("workbooks") or []
    if not rows:
        return ""
    body = []
    for w in rows:
        cls = _status_class(w.get("status"))
        pbip = w.get("pbip_folder")
        pbip_html = "<code>%s</code>" % _esc(pbip) if pbip else '<span class="muted">&mdash;</span>'
        model = w.get("bound_model")
        model_html = _esc(model) if model else '<span class="muted">&mdash;</span>'
        body.append(
            "<tr>"
            "<td>%s</td>"
            '<td><span class="badge %s">%s</span></td>'
            "<td>%s</td>"
            "<td>%s</td>"
            '<td class="reason">%s</td>'
            "</tr>"
            % (
                _esc(w.get("workbook")),
                cls,
                _esc(w.get("status")),
                model_html,
                pbip_html,
                _esc(w.get("reason")) or '<span class="muted">&mdash;</span>',
            )
        )
    return (
        '<section><h2>Workbook sign-off</h2>'
        '<table class="grid"><thead><tr>'
        "<th>Workbook</th><th>Status</th><th>Bound model</th><th>PBIP</th><th>Reason / next step</th>"
        "</tr></thead><tbody>%s</tbody></table></section>"
    ) % "".join(body)


# Human-facing labels for the machine ``category`` tokens on each needs-review calc. Anything not
# in this map falls back to the token with underscores turned into spaces (never a KeyError).
_CATEGORY_LABELS = {
    "model_object_parameter": "Parameter-driven (Power BI model object)",
    "unresolved_reference": "Unresolved field reference",
    "type_or_shape_mismatch": "Type / shape mismatch",
    "missing_addressing_intent": "Table calc &mdash; addressing (window / index)",
    "unsupported_function": "Unsupported function",
}


def _category_label(category: Any) -> str:
    """Map a needs-review ``category`` token to a readable label (defaults to a de-underscored token)."""
    key = str(category or "").lower()
    if key in _CATEGORY_LABELS:
        return _CATEGORY_LABELS[key]
    return _esc(key.replace("_", " ")) if key else "uncategorised"


def _render_needs_review(report: Dict[str, Any]) -> str:
    """Per-workbook, expandable worklist of every calc the engine would **not** translate
    deterministically -- calc name, Tableau formula, category and the concrete reason.

    This is the actionable counterpart to the sign-off scoreboard: the aggregate table says *how
    many* calcs need review; this section says *which ones, and why*, so a Power BI developer has a
    real to-do list. Data is read straight from each workbook's
    ``model_translation_handoff.needs_review`` -- the engine refuses to guess a measure, so this is
    the honest handoff, not a shortfall to hide. Rendered with ``<details>`` (no JS) to stay
    self-contained and offline. All formula / reason text is untrusted and HTML-escaped.
    """
    workbooks = report.get("workbooks") or []
    wb_blocks: List[str] = []
    for wb in workbooks:
        handoff = wb.get("model_translation_handoff") or {}
        items = handoff.get("needs_review") or []
        if not items:
            continue
        name = wb.get("name") or "(unnamed workbook)"
        summary = handoff.get("summary") or {}
        translated = summary.get("translated", summary.get("live", 0))
        total = summary.get("total", 0)
        cov = summary.get("coverage_pct")
        cov_txt = ("%s%%" % cov) if cov is not None else _pct(translated, total)
        meta = (
            "%s of %s calcs translated &nbsp;&middot;&nbsp; %s coverage "
            "&nbsp;&middot;&nbsp; %s need review"
        ) % (_esc(translated), _esc(total), _esc(cov_txt), _esc(len(items)))

        # Group entries by category, preserving first-seen order, and keep the first non-empty
        # category_guidance we see for each category (it is identical across a category's entries).
        groups: Dict[str, List[Dict[str, Any]]] = {}
        guidance: Dict[str, str] = {}
        order: List[str] = []
        for it in items:
            cat = str(it.get("category") or "uncategorised")
            if cat not in groups:
                groups[cat] = []
                order.append(cat)
            groups[cat].append(it)
            if cat not in guidance and it.get("category_guidance"):
                guidance[cat] = str(it["category_guidance"])

        cat_blocks: List[str] = []
        for cat in order:
            entries = groups[cat]
            rows = []
            for it in entries:
                formula = it.get("formula")
                formula_html = (
                    "<code>%s</code>" % _esc(formula) if formula
                    else '<span class="muted">&mdash;</span>'
                )
                rows.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    '<td class="reason">%s</td></tr>'
                    % (
                        _esc(it.get("name")),
                        _esc(it.get("role")),
                        formula_html,
                        _esc(it.get("fallback_reason")) or '<span class="muted">&mdash;</span>',
                    )
                )
            guide_html = (
                '<div class="cat-guidance">%s</div>' % _esc(guidance[cat])
                if cat in guidance else ""
            )
            cat_blocks.append(
                '<details class="cat"><summary>%s <span class="cat-count">%s</span></summary>'
                '%s<table class="grid"><thead><tr>'
                "<th>Calculation</th><th>Role</th><th>Tableau formula</th>"
                "<th>Why it needs review</th>"
                "</tr></thead><tbody>%s</tbody></table></details>"
                % (_category_label(cat), _esc(len(entries)), guide_html, "".join(rows))
            )

        wb_blocks.append(
            '<div class="wb-review"><div class="wb-review-head">'
            '<span class="ds-name">%s</span> '
            '<span class="badge warn">needs review</span> '
            '<span class="muted">%s</span></div>%s</div>'
            % (_esc(name), meta, "".join(cat_blocks))
        )

    if not wb_blocks:
        return ""
    return (
        '<section><h2>Needs review &mdash; calculation worklist</h2>'
        '<p class="muted">Every calculation the accelerator would not translate deterministically, '
        "grouped by why. Expand a category for the Tableau formula and the concrete reason. Working "
        "these in Power BI is what moves a workbook from <strong>warn</strong> to sign-off &mdash; "
        "the engine refuses to emit a guessed measure rather than ship a wrong number.</p>"
        "%s</section>"
    ) % "".join(wb_blocks)


def _render_lineage(report: Dict[str, Any]) -> str:
    datasources = report.get("datasources") or []
    blocks = []
    for ds in datasources:
        lineage = ds.get("lineage") or []
        name = ds.get("name") or "(unnamed datasource)"
        status = ds.get("status")
        cls = _status_class(status)
        connector = ds.get("connector") or ds.get("m_connector") or ""
        sd = ds.get("storage_decision") or {}
        mode = sd.get("mode") or ds.get("storage_mode") or ""
        rationale = sd.get("rationale") or ""

        head = (
            '<div class="ds-head">'
            '<span class="ds-name">%s</span> '
            '<span class="badge %s">%s</span> '
            '<span class="muted">%s%s</span>'
            "</div>"
        ) % (
            _esc(name),
            cls,
            _esc(status or "?"),
            _esc(connector),
            (" &middot; " + _esc(mode)) if mode else "",
        )
        rationale_html = (
            '<div class="ds-rationale">%s</div>' % _esc(rationale) if rationale else ""
        )

        if lineage:
            lrows = []
            for entry in lineage:
                refs = ", ".join(entry.get("references") or [])
                deps = ", ".join(entry.get("depends_on_calcs") or [])
                params = ", ".join(entry.get("parameters") or [])
                depends = " &middot; ".join(
                    p for p in (
                        ("refs: " + _esc(refs)) if refs else "",
                        ("calcs: " + _esc(deps)) if deps else "",
                        ("params: " + _esc(params)) if params else "",
                    ) if p
                ) or '<span class="muted">&mdash;</span>'
                lrows.append(
                    "<tr><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td></tr>"
                    % (
                        _esc(entry.get("calc")),
                        _esc(entry.get("role")),
                        _esc(entry.get("formula")),
                        depends,
                    )
                )
            lineage_html = (
                '<table class="grid lineage"><thead><tr>'
                "<th>Calculation</th><th>Role</th><th>Tableau formula</th><th>Depends on</th>"
                "</tr></thead><tbody>%s</tbody></table>"
            ) % "".join(lrows)
        else:
            lineage_html = '<div class="muted">No calculation lineage recorded.</div>'

        followups = ds.get("manual_followups") or sd.get("manual_followups") or []
        if followups:
            fitems = "".join("<li>%s</li>" % _esc(f) for f in followups)
            followups_html = (
                '<div class="followups"><div class="followups-title">Manual follow-ups</div>'
                "<ul>%s</ul></div>"
            ) % fitems
        else:
            followups_html = ""

        blocks.append(
            '<div class="ds">%s%s%s%s</div>'
            % (head, rationale_html, lineage_html, followups_html)
        )

    if not blocks:
        return ""
    return '<section><h2>Datasource lineage &amp; follow-ups</h2>%s</section>' % "".join(blocks)


def _render_followups_rollup(report: Dict[str, Any]) -> str:
    """One de-duplicated list of every manual follow-up across all datasources."""
    seen: List[str] = []
    for ds in report.get("datasources") or []:
        sd = ds.get("storage_decision") or {}
        for f in (ds.get("manual_followups") or []) + (sd.get("manual_followups") or []):
            f = str(f)
            if f not in seen:
                seen.append(f)
    if not seen:
        return ""
    items = "".join("<li>%s</li>" % _esc(f) for f in seen)
    return (
        '<section><h2>All manual follow-ups</h2>'
        '<p class="muted">Every action a human still needs to take, de-duplicated across the '
        "estate.</p><ul class=\"rollup\">%s</ul></section>"
    ) % items


_CSS = """
:root{--fg:#1b1b1f;--muted:#6b6b75;--line:#e3e3e8;--bg:#fff;--card:#f7f7f9;
--ok:#1a7f37;--ok-bg:#eaf6ec;--warn:#9a6700;--warn-bg:#fdf6e3;--bad:#b32424;--bad-bg:#fbeaea;--accent:#0b5cad;}
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--fg);
margin:0;padding:0 24px 64px;background:var(--bg);}
header{padding:28px 0 16px;border-bottom:2px solid var(--line);margin-bottom:20px}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.meta{color:var(--muted);font-size:13px}
code{background:var(--card);padding:1px 5px;border-radius:4px;font-size:12px;
font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.banner{border-radius:8px;padding:14px 16px;margin:8px 0 4px;border:1px solid var(--line)}
.banner-title{font-size:15px;margin-bottom:2px}
.banner.ok{background:var(--ok-bg);border-color:#bfe3c6}
.banner.warn{background:var(--warn-bg);border-color:#e6d8a8}
.banner.bad{background:var(--bad-bg);border-color:#efc4c4}
.banner.muted{background:var(--card)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.kpi-value{font-size:22px;font-weight:600}
.kpi-label{color:var(--muted);font-size:12px;margin-top:2px}
.kpi-sub{color:var(--muted);font-size:11px;margin-top:6px}
table.grid{border-collapse:collapse;width:100%;font-size:13px}
table.grid th,table.grid td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
table.grid th{background:var(--card);font-weight:600}
table.grid td.reason{color:var(--muted)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.badge.ok{background:var(--ok-bg);color:var(--ok)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.bad{background:var(--bad-bg);color:var(--bad)}
.badge.muted{background:var(--card);color:var(--muted)}
.muted{color:var(--muted)}
.note{margin:12px 0 4px;background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;padding:10px 14px;font-size:13px}
.note ul{margin:6px 0 0;padding-left:20px}
.note li{margin:3px 0}
.ds{border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.ds-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.ds-name{font-weight:600;font-size:15px}
.ds-rationale{color:var(--muted);font-size:12px;margin:0 0 10px}
table.lineage{margin-top:6px}
.followups{margin-top:12px;background:var(--warn-bg);border:1px solid #e6d8a8;border-radius:6px;padding:10px 14px}
.followups-title{font-weight:600;font-size:12px;margin-bottom:4px}
.followups ul,ul.rollup{margin:4px 0 0;padding-left:20px}
ul.rollup li,.followups li{margin:2px 0}
.wb-review{border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.wb-review-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
details.cat{border:1px solid var(--line);border-radius:6px;margin:8px 0;background:var(--card)}
details.cat>summary{cursor:pointer;padding:8px 12px;font-weight:600;font-size:13px;list-style:none}
details.cat>summary::-webkit-details-marker{display:none}
details.cat>summary::before{content:"\25B8";display:inline-block;margin-right:8px;color:var(--muted)}
details.cat[open]>summary::before{content:"\25BE"}
details.cat[open]>summary{border-bottom:1px solid var(--line)}
.cat-count{color:var(--muted);font-weight:400}
.cat-guidance{color:var(--muted);font-size:12px;line-height:1.5;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--bg)}
details.cat table.grid{font-size:12px}
details.cat table.grid th{background:var(--bg)}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
"""


def _render_copilot_readiness(report: Dict[str, Any]) -> str:
    """Copilot / Q&A readiness scorecard: an overall verdict banner + a per-check table.

    Rendered only when the report carries a ``copilot_readiness`` block (older report.json files
    predate it, so the section is simply absent for them). Every value is escaped; ``na`` checks
    render muted so they never read as a failure.
    """
    card = report.get("copilot_readiness") or {}
    checks = card.get("checks") or []
    if not checks:
        return ""
    overall = str(card.get("overall") or "")
    banner_cls = {"ready": "ok", "ready_with_warnings": "warn", "not_ready": "bad"}.get(
        overall, "muted")
    verdict = {
        "ready": "This model is grounded for Power BI Q&amp;A / Copilot.",
        "ready_with_warnings": "Usable for Copilot, with items to review below.",
        "not_ready": "Not yet grounded for Copilot &mdash; address the failed checks below.",
    }.get(overall, "")
    totals = card.get("totals") or {}
    banner = (
        '<div class="banner-title">Copilot readiness: <strong>%s</strong></div>'
        '<div class="banner-body">%s passed &nbsp;&middot;&nbsp; %s warned '
        '&nbsp;&middot;&nbsp; %s failed. %s</div>'
    ) % (
        _esc(overall.replace("_", " ") or "unknown"),
        _esc(totals.get("passed", 0)),
        _esc(totals.get("warned", 0)),
        _esc(totals.get("failed", 0)),
        verdict,
    )
    rows = []
    for c in checks:
        status = str(c.get("status") or "")
        cls = _status_class(status)
        metric = c.get("metric")
        metric_html = ("%s%%" % _esc(metric)) if isinstance(metric, (int, float)) \
            else '<span class="muted">&mdash;</span>'
        rows.append(
            "<tr>"
            "<td>%s</td>"
            '<td><span class="badge %s">%s</span></td>'
            '<td class="reason">%s</td>'
            "<td>%s</td>"
            "</tr>"
            % (
                _esc(c.get("label")),
                cls,
                _esc(status),
                _esc(c.get("detail")) or '<span class="muted">&mdash;</span>',
                metric_html,
            )
        )
    return (
        '<section><h2>Copilot / Q&amp;A readiness</h2>'
        '<div class="banner %s">%s</div>'
        '<table class="grid"><thead><tr>'
        "<th>Check</th><th>Status</th><th>Detail</th><th>Coverage</th>"
        "</tr></thead><tbody>%s</tbody></table>%s</section>"
    ) % (banner_cls, banner, "".join(rows), _render_readiness_guidance(card))


def _render_readiness_guidance(card: Dict[str, Any]) -> str:
    """The honest 'make this fully AI-ready' next-steps list under the scorecard table.

    The migration ships a Copilot-*ready* scaffold; it cannot invent the business meaning a Tableau
    source never carried. This block spells out what a human must add so the report never implies a
    migrated model is automatically AI-grounded. Absent when the scorecard carries no guidance.
    """
    tips = card.get("guidance") or []
    if not tips:
        return ""
    items = "".join("<li>%s</li>" % _esc(t) for t in tips)
    return (
        '<div class="note"><strong>Make this fully AI-ready.</strong> The migration produced a '
        "grounded <em>scaffold</em>; these human steps turn it into a model Copilot can answer from "
        "with confidence:<ul>%s</ul></div>"
    ) % items


def render_report_html(report: Dict[str, Any]) -> str:
    """Build the full self-contained HTML document string from a parsed ``report.json`` dict."""
    parts = [
        _render_header(report),
        _render_dod_banner(report),
        _render_kpis(report),
        _render_copilot_readiness(report),
        _render_signoff_table(report),
        _render_needs_review(report),
        _render_lineage(report),
        _render_followups_rollup(report),
        '<footer>Generated offline by the Tableau &rarr; Power BI / Fabric accelerator from '
        '<code>report.json</code>. This is a faithful rendering of the migration facts &mdash; '
        "a red status is an honest gate, not a bug.</footer>",
    ]
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Tableau &rarr; Power BI Migration Report</title>"
        "<style>%s</style></head><body>%s</body></html>"
    ) % (_CSS, "".join(parts))


# ---------------------------------------------------------------------------
# CLI -- read a report.json (or an output folder containing one), write HTML
# ---------------------------------------------------------------------------

def _resolve_report_path(path: str) -> str:
    import os
    if os.path.isdir(path):
        return os.path.join(path, "report.json")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        prog="migration_report_html",
        description="Render a self-contained offline migration report (HTML) from report.json.",
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="path to report.json, or an output folder that contains report.json",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="output .html path (default: migration-report.html beside report.json)",
    )
    args = parser.parse_args(argv)

    report_path = _resolve_report_path(args.input)
    if not os.path.isfile(report_path):
        parser.error("no report.json found at: %s" % report_path)

    with open(report_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)

    out_path = args.output or os.path.join(os.path.dirname(report_path) or ".", "migration-report.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_report_html(report))

    print("[OK] wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
