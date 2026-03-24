"""
report_generator.py
-------------------
Renders OrgAnalyser findings into a self-contained interactive HTML report
using a Jinja2 template.

Usage:
    from src.report_generator import ReportGenerator

    generator = ReportGenerator()
    path = generator.generate(
        report_data=analyser.analyse(org_data),
        org_info=client.test_connection(),
        output_dir="reports",
    )
    print(f"Report saved to: {path}")
"""

import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT          = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _ROOT / "templates"

# Severity display order (most severe first)
_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

# Colours matching the HTML template
_SEVERITY_COLORS = {
    "critical": "#e53e3e",
    "high":     "#dd6b20",
    "medium":   "#d69e2e",
    "low":      "#38a169",
    "info":     "#718096",
}


class ReportGenerator:
    """
    Renders an OrgAnalyser report dict into a polished, self-contained HTML file
    and optionally opens it in the default browser.
    """

    def __init__(self):
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        report_data: dict,
        org_info: dict | None = None,
        output_dir: str = "reports",
        open_browser: bool = True,
    ) -> str:
        """
        Render the report and save it to disk.

        Args:
            report_data:  Dict returned by OrgAnalyser.analyse().
            org_info:     Dict returned by SalesforceClient.test_connection().
                          Used for org name, type, instance, etc.
            output_dir:   Directory where the HTML file will be saved.
                          Created if it doesn't exist.
            open_browser: If True, open the report in the default browser after saving.

        Returns:
            Absolute path to the saved HTML file.
        """
        org_info = org_info or {}

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc)
        filename  = f"org_health_{timestamp.strftime('%Y%m%d_%H%M%S')}.html"
        filepath  = out_path / filename

        context = self._build_context(report_data, org_info, timestamp)

        template = self._env.get_template("report_template.html")
        html     = template.render(**context)

        filepath.write_text(html, encoding="utf-8")
        print(f"  Report saved → {filepath.resolve()}")

        if open_browser:
            webbrowser.open(filepath.resolve().as_uri())

        return str(filepath.resolve())

    # ------------------------------------------------------------------
    # Context preparation
    # ------------------------------------------------------------------

    def _build_context(
        self,
        report_data: dict,
        org_info: dict,
        timestamp: datetime,
    ) -> dict:
        """
        Transform raw analyser output into a template-ready context dict.
        All derived values (gauge arcs, bar widths, groupings) are computed
        here so the template stays logic-free.
        """
        summary  = report_data.get("summary", {})
        findings = report_data.get("findings", [])

        health_score = summary.get("health_score", 0)

        # Score label and colour band
        if health_score <= 40:
            score_label = "Critical Risk"
            score_color = _SEVERITY_COLORS["critical"]
        elif health_score <= 70:
            score_label = "Needs Attention"
            score_color = _SEVERITY_COLORS["high"]
        else:
            score_label = "Healthy"
            score_color = _SEVERITY_COLORS["low"]

        # SVG gauge: r=80, circumference ≈ 502.65
        circumference = 2 * 3.14159265 * 80
        gauge_filled  = round(circumference * health_score / 100, 2)
        gauge_empty   = round(circumference - gauge_filled, 2)

        # Only FAIL findings go into the report body
        fail_findings = [f for f in findings if f.get("status") == "FAIL"]

        # Group FAIL findings by severity in display order
        severity_groups: dict[str, list] = {}
        for sev in _SEVERITY_ORDER:
            group = [f for f in fail_findings if f.get("severity", "info").lower() == sev]
            if group:
                severity_groups[sev] = group

        # Category stats for the bar chart
        category_counts: dict[str, int]  = defaultdict(int)
        category_highest: dict[str, str] = {}
        for f in fail_findings:
            cat = f.get("category", "Other")
            sev = f.get("severity", "info").lower()
            category_counts[cat] += 1
            existing = category_highest.get(cat, "info")
            if _SEVERITY_ORDER.index(sev) < _SEVERITY_ORDER.index(existing):
                category_highest[cat] = sev

        max_cat_count = max(category_counts.values(), default=1)
        category_stats = sorted(
            [
                {
                    "name":             cat,
                    "count":            count,
                    "highest_severity": category_highest.get(cat, "info"),
                    "color":            _SEVERITY_COLORS.get(
                                            category_highest.get(cat, "info"), "#718096"
                                        ),
                    "bar_width_pct":    round(count / max_cat_count * 100),
                }
                for cat, count in category_counts.items()
            ],
            key=lambda x: (
                _SEVERITY_ORDER.index(x["highest_severity"]),
                -x["count"],
            ),
        )

        return {
            "report": {
                "org_name":     org_info.get("org_name", "Salesforce Org"),
                "org_id":       org_info.get("org_id", ""),
                "org_type":     org_info.get("org_type", ""),
                "instance":     org_info.get("instance", ""),
                "is_sandbox":   org_info.get("is_sandbox", False),
                "generated_at": timestamp.strftime("%B %d, %Y at %H:%M UTC"),
                "summary": {
                    **summary,
                    "score_label":         score_label,
                    "score_color":         score_color,
                    "gauge_filled":        gauge_filled,
                    "gauge_empty":         gauge_empty,
                    "gauge_circumference": round(circumference, 2),
                },
                "fail_findings":   fail_findings,
                "all_findings":    findings,
                "severity_groups": severity_groups,
                "category_stats":  category_stats,
                "severity_colors": _SEVERITY_COLORS,
                "severity_order":  [s for s in _SEVERITY_ORDER if s in severity_groups],
            }
        }
