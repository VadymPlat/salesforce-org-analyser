"""
agent.py
--------
Main orchestrator for the Salesforce Org Health Analyser.

Connects all components in sequence:
    SalesforceClient → OrgAnalyser → ReportGenerator

Usage:
    python3 agent.py
    python3 agent.py --config config/checks_config.yaml
"""

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing modules that read env vars at import time
load_dotenv()

from src.salesforce_client import SalesforceClient
from src.analyser import OrgAnalyser
from src.report_generator import ReportGenerator

_ROOT        = Path(__file__).resolve().parent.parent
_REPORTS_DIR = _ROOT / "reports"

# ANSI colours for terminal output (degrade gracefully on Windows)
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


def _banner(msg: str) -> None:
    """Print a prominent step banner."""
    print(f"\n{_CYAN}{_BOLD}{'─' * 55}{_RESET}")
    print(f"{_CYAN}{_BOLD}  {msg}{_RESET}")
    print(f"{_CYAN}{_BOLD}{'─' * 55}{_RESET}")


def _ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_RESET}  {msg}")


def _info(msg: str) -> None:
    print(f"  {_YELLOW}→{_RESET}  {msg}")


def _err(msg: str) -> None:
    print(f"  {_RED}✗{_RESET}  {msg}", file=sys.stderr)


class OrgHealthAgent:
    """
    Orchestrates the full Salesforce org health analysis pipeline:
    connect → collect → analyse → report.
    """

    def __init__(self):
        """
        Initialise all components.
        Credentials are read from environment variables (loaded from .env).
        """
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        self._client    = SalesforceClient()
        self._analyser  = OrgAnalyser()
        self._generator = ReportGenerator()
        self._reports_dir = str(_REPORTS_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_with_token(
        self,
        access_token: str,
        instance_url: str,
        selected_categories: list | None = None,
        progress_callback=None,
    ) -> tuple[dict, str]:
        """
        Execute the full analysis pipeline using an existing OAuth access token.

        Designed for use by the Streamlit web UI — accepts an OAuth Bearer
        token instead of reading credentials from environment variables.

        Args:
            access_token:        Salesforce OAuth Bearer token.
            instance_url:        Salesforce instance base URL.
            selected_categories: Category names to analyse. None = all five.
                                 Valid values: "Security", "Automation",
                                 "Data Model", "Integrations", "Governance".
            progress_callback:   Optional callable(pct: int, message: str)
                                 called at each pipeline step for UI updates.

        Returns:
            Tuple of (report_data dict, absolute path to generated HTML report).
        """
        _cb = progress_callback or (lambda pct, msg: None)
        cats = set(selected_categories) if selected_categories else {
            "Security", "Automation", "Data Model", "Integrations", "Governance"
        }

        # ── Step 1: Inject OAuth token ────────────────────────────────
        _cb(10, "🔄 Connecting to org...")
        self._client.connect_with_token(access_token, instance_url)

        # ── Step 2: Verify connection ─────────────────────────────────
        org_info = self._client.test_connection()
        if "error" in org_info:
            raise RuntimeError(f"Connection test failed: {org_info['error']}")

        # ── Step 3: Collect data per selected category ────────────────
        _cb(25, "🔄 Collecting security data...")
        if "Security" in cats:
            security_data = self._client.get_user_security_data()
            owd_data      = self._client.get_owd_settings()
            perm_data     = self._client.get_permission_sets_data()
        else:
            security_data = owd_data = perm_data = {}

        _cb(40, "🔄 Collecting automation data...")
        if "Automation" in cats:
            automation_data         = self._client.get_automation_data()
            apex_data               = self._client.get_apex_code_data()
            record_triggered_data   = self._client.get_record_triggered_flows()
            scheduled_jobs_data     = self._client.get_scheduled_jobs()
        else:
            automation_data = apex_data = record_triggered_data = scheduled_jobs_data = {}

        _cb(55, "🔄 Collecting data model data...")
        if "Data Model" in cats:
            data_model_data           = self._client.get_data_model_data()
            duplicate_rules_data      = self._client.get_duplicate_rules()
            multiselect_fields_data   = self._client.get_multiselect_picklist_fields()
        else:
            data_model_data = duplicate_rules_data = multiselect_fields_data = {}

        _cb(70, "🔄 Collecting governance and integration data...")
        if "Governance" in cats or "Security" in cats:
            inactive_users_data   = self._client.get_inactive_users()
            users_no_role_data    = self._client.get_users_without_role()
        else:
            inactive_users_data = users_no_role_data = {}

        if "Governance" in cats or "Integrations" in cats:
            org_limits_data   = self._client.get_org_limits()
            org_coverage_data = self._client.get_org_wide_coverage()
        else:
            org_limits_data = org_coverage_data = {}

        if "Integrations" in cats:
            connected_apps_data    = self._client.get_connected_apps()
            remote_sites_data      = self._client.get_remote_site_settings()
        else:
            connected_apps_data = remote_sites_data = {}

        if "Security" in cats:
            guest_users_data = self._client.get_guest_users()
        else:
            guest_users_data = {}

        org_data = {
            "security":                 security_data,
            "owd":                      owd_data,
            "permissions":              perm_data,
            "automation":               automation_data,
            "data_model":               data_model_data,
            "apex":                     apex_data if "Automation" in cats else {},
            "inactive_users":           inactive_users_data,
            "users_without_role":       users_no_role_data,
            "org_limits":               org_limits_data,
            "org_coverage":             org_coverage_data,
            "guest_users":              guest_users_data,
            "duplicate_rules":          duplicate_rules_data,
            "record_triggered_flows":   record_triggered_data,
            "scheduled_jobs":           scheduled_jobs_data,
            "multiselect_picklist_fields": multiselect_fields_data,
            "connected_apps":           connected_apps_data,
            "remote_site_settings":     remote_sites_data,
        }

        # ── Step 4: AI analysis ───────────────────────────────────────
        _cb(85, "🤖 Running Claude AI analysis...")
        report_data = self._analyser.analyse(org_data)

        # ── Step 5: Generate report ───────────────────────────────────
        _cb(95, "📊 Generating report...")
        report_path = self._generator.generate(
            report_data=report_data,
            org_info=org_info,
            output_dir=self._reports_dir,
            open_browser=False,
        )

        _cb(100, "✅ Analysis complete!")
        return report_data, report_path

    def run(self) -> str:
        """
        Execute the full analysis pipeline.

        Returns:
            Absolute path to the generated HTML report.

        Raises:
            SystemExit on connection failure.
        """
        start_time = time.time()

        print(f"\n{_BOLD}Salesforce Org Health Analyser{_RESET}")
        print(f"{'=' * 55}")

        # ── Step 1: Connect ──────────────────────────────────────────
        _banner("Step 1 — Connecting to Salesforce")
        _info("Authenticating via SOAP login API ...")

        connected = self._client.connect()
        if not connected:
            _err("Authentication failed. Check credentials in .env and retry.")
            sys.exit(1)

        # ── Step 2: Test connection ──────────────────────────────────
        _banner("Step 2 — Verifying Connection")
        org_info = self._client.test_connection()

        if "error" in org_info:
            _err(f"Connection test failed: {org_info['error']}")
            sys.exit(1)

        _ok(f"Connected to: {org_info.get('org_name')} "
            f"({org_info.get('org_type')})")
        _ok(f"Instance: {org_info.get('instance')}  |  "
            f"Sandbox: {org_info.get('is_sandbox')}  |  "
            f"API: {org_info.get('api_version')}")

        # ── Step 3: Collect all org data ─────────────────────────────
        _banner("Step 3 — Collecting Org Data")

        _info("Collecting security data ...")
        security_data = self._client.get_user_security_data()
        _ok(f"Security data collected "
            f"({security_data.get('total_active_users', 0)} active users, "
            f"{security_data.get('sys_admin_count', 0)} sys admins)")

        _info("Collecting OWD sharing settings ...")
        owd_data = self._client.get_owd_settings()
        _ok(f"OWD data collected "
            f"({len(owd_data.get('standard_objects', []))} standard objects, "
            f"{len(owd_data.get('custom_objects', []))} custom objects)")

        _info("Collecting permission set data ...")
        perm_data = self._client.get_permission_sets_data()
        _ok(f"Permission sets collected "
            f"({perm_data.get('total_count', 0)} total, "
            f"{len(perm_data.get('dangerous_perm_sets', []))} flagged)")

        _info("Collecting automation data ...")
        automation_data = self._client.get_automation_data()
        _ok(f"Automation data collected "
            f"({automation_data.get('total_active_flows', 0)} flows, "
            f"{automation_data.get('total_active_triggers', 0)} triggers)")

        _info("Collecting data model information ...")
        data_model_data = self._client.get_data_model_data()
        _ok(f"Data model collected "
            f"({data_model_data.get('total_custom_objects', 0)} custom objects)")

        _info("Collecting Apex code data ...")
        apex_data = self._client.get_apex_code_data()
        _ok(f"Apex data collected "
            f"({apex_data.get('total_classes', 0)} classes, "
            f"{apex_data.get('total_triggers', 0)} triggers)")

        _info("Collecting inactive users ...")
        inactive_users_data = self._client.get_inactive_users()
        _ok(f"Inactive users: {inactive_users_data.get('count', 0)} "
            f"(>{inactive_users_data.get('threshold_days', 90)} days since last login)")

        _info("Collecting users without role ...")
        users_no_role_data = self._client.get_users_without_role()
        _ok(f"Users without role: {users_no_role_data.get('count', 0)}")

        _info("Collecting org limits ...")
        org_limits_data = self._client.get_org_limits()
        _ok(f"Org limits retrieved ({len(org_limits_data)} limit types)")

        _info("Collecting org-wide Apex test coverage ...")
        org_coverage_data = self._client.get_org_wide_coverage()
        pct = org_coverage_data.get("percent_covered")
        _ok(f"Org-wide coverage: {pct}%" if pct is not None else "Org-wide coverage: unavailable")

        _info("Collecting guest user data ...")
        guest_users_data = self._client.get_guest_users()
        _ok(f"Guest users: {guest_users_data.get('count', 0)}")

        _info("Collecting duplicate rules ...")
        duplicate_rules_data = self._client.get_duplicate_rules()
        _ok(f"Active duplicate rules: {duplicate_rules_data.get('total_active', 0)}")

        _info("Collecting record-triggered flows ...")
        record_triggered_data = self._client.get_record_triggered_flows()
        _ok(f"Record-triggered flows: {record_triggered_data.get('count', 0)}")

        _info("Collecting scheduled Apex jobs ...")
        scheduled_jobs_data = self._client.get_scheduled_jobs()
        _ok(f"Scheduled jobs waiting: {scheduled_jobs_data.get('count', 0)}")

        _info("Collecting Multi-Select Picklist fields ...")
        multiselect_fields_data = self._client.get_multiselect_picklist_fields()
        _ok(f"Multi-select picklist fields: {multiselect_fields_data.get('count', 0)}")

        _info("Collecting Connected Apps ...")
        connected_apps_data = self._client.get_connected_apps()
        _ok(f"Connected apps: {connected_apps_data.get('count', 0)}")

        _info("Collecting Remote Site Settings ...")
        remote_sites_data = self._client.get_remote_site_settings()
        _ok(f"Active remote site settings: {remote_sites_data.get('count', 0)}")

        # ── Step 4: Bundle collected data ────────────────────────────
        org_data = {
            "security":                   security_data,
            "owd":                        owd_data,
            "permissions":                perm_data,
            "automation":                 automation_data,
            "data_model":                 data_model_data,
            "apex":                       apex_data,
            "inactive_users":             inactive_users_data,
            "users_without_role":         users_no_role_data,
            "org_limits":                 org_limits_data,
            "org_coverage":               org_coverage_data,
            "guest_users":                guest_users_data,
            "duplicate_rules":            duplicate_rules_data,
            "record_triggered_flows":     record_triggered_data,
            "scheduled_jobs":             scheduled_jobs_data,
            "multiselect_picklist_fields": multiselect_fields_data,
            "connected_apps":             connected_apps_data,
            "remote_site_settings":       remote_sites_data,
        }

        # ── Step 5: Run AI analysis ──────────────────────────────────
        _banner("Step 4 — Running Health Checks & AI Analysis")
        _info("Evaluating checks against org data ...")
        _info("Enriching FAIL findings with Claude AI recommendations ...")

        report_data = self._analyser.analyse(org_data)

        summary = report_data["summary"]
        _ok(f"Analysis complete — "
            f"{summary.get('total_findings', 0)} issue(s) found")

        # ── Step 6: Generate HTML report ─────────────────────────────
        _banner("Step 5 — Generating HTML Report")
        _info("Rendering interactive report ...")

        report_path = self._generator.generate(
            report_data=report_data,
            org_info=org_info,
            output_dir=self._reports_dir,
            open_browser=True,
        )

        # ── Step 7: Print terminal summary ───────────────────────────
        elapsed = round(time.time() - start_time, 1)

        print(f"\n{'=' * 55}")
        print(f"{_BOLD}{_GREEN}  Analysis Complete{_RESET}")
        print(f"{'=' * 55}")

        crit   = summary.get("critical_count", 0)
        high   = summary.get("high_count", 0)
        medium = summary.get("medium_count", 0)
        low    = summary.get("low_count", 0)
        score  = summary.get("health_score", 0)

        print(f"\n  Findings:")
        if crit:
            print(f"    {_RED}{_BOLD}  Critical : {crit}{_RESET}")
        print(f"    {_YELLOW}  High     : {high}{_RESET}")
        print(f"       Medium   : {medium}")
        print(f"    {_GREEN}  Low      : {low}{_RESET}")

        # Health score colour
        if score >= 71:
            score_color = _GREEN
            score_label = "Healthy"
        elif score >= 41:
            score_color = _YELLOW
            score_label = "Needs Attention"
        else:
            score_color = _RED
            score_label = "Critical Risk"

        print(f"\n  Health Score : "
              f"{score_color}{_BOLD}{score}/100{_RESET} "
              f"({score_label})")
        print(f"  Report saved : {report_path}")
        print(f"  Completed in : {elapsed}s")
        print(f"{'=' * 55}\n")

        return report_path


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Salesforce Org Health Analyser — AI-powered org audit tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables (set in .env):\n"
            "  SALESFORCE_USERNAME        Salesforce login username\n"
            "  SALESFORCE_PASSWORD        Salesforce login password\n"
            "  SALESFORCE_SECURITY_TOKEN  Security token (append to password)\n"
            "  ANTHROPIC_API_KEY          Anthropic API key for AI analysis\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config/checks_config.yaml",
        help="Path to checks config YAML (default: config/checks_config.yaml)",
    )
    args = parser.parse_args()

    agent = OrgHealthAgent()
    agent.run()
