# Runs each health check defined in checks_config.yaml against the data
# returned by salesforce_client.py and produces structured findings.

"""
analyser.py
-----------
OrgAnalyser evaluates a Salesforce org against the checks defined in
config/checks_config.yaml and enriches each finding with AI commentary
from Claude (claude-sonnet-4-6).

Usage:
    from src.salesforce_client import SalesforceClient
    from src.analyser import OrgAnalyser

    client = SalesforceClient()
    client.connect()
    org_data = {
        "security":    client.get_user_security_data(),
        "permissions": client.get_permission_sets_data(),
        "automation":  client.get_automation_data(),
        "data_model":  client.get_data_model_data(),
        "apex":        client.get_apex_code_data(),
    }
    analyser = OrgAnalyser()
    report = analyser.analyse(org_data)
"""

import os
from pathlib import Path
from typing import Any

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()

# Path resolution: works regardless of where the script is invoked from
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "checks_config.yaml"

# Terminal colour codes for check result log lines
_TERM_COLORS = {
    "FAIL":  "\033[91m",   # red
    "PASS":  "\033[92m",   # green
    "INFO":  "\033[93m",   # yellow
    "RESET": "\033[0m",
}

# Target column at which the dots end (left-pad to this width before status)
_LOG_DOT_COLUMN = 68


def _log_check_result(finding: dict) -> None:
    """
    Print a formatted one-line status line for a completed check.

    Format:
        [SEC-001] System Administrators with Active Sessions ...... FAIL  8 found, threshold 5
    """
    check_id = finding["id"]
    name     = finding["name"]
    status   = finding["status"]
    details  = finding.get("details", "")

    # Take the first sentence (up to ". ") as the brief metric — cap at 60 chars
    first_sentence = details.split(". ")[0] if ". " in details else details
    metric = first_sentence if len(first_sentence) <= 60 else first_sentence[:57] + "..."

    left      = f"[{check_id}] {name} "
    dots      = "." * max(3, _LOG_DOT_COLUMN - len(left))
    color     = _TERM_COLORS.get(status, "")
    reset     = _TERM_COLORS["RESET"]

    print(f"  {left}{dots} {color}{status}{reset}  {metric}")


def _safe_int(value: Any, default: int = 0) -> int:
    """
    Safely coerce a value from an API response to int.

    Salesforce REST/Tooling API responses occasionally return numeric
    fields as strings (e.g. "65" instead of 65). This helper prevents
    '<' not supported between str and int errors at comparison sites.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce a value from an API response to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Scoring weights per severity
_SEVERITY_WEIGHTS = {
    "critical": 15,
    "high":     8,
    "medium":   3,
    "low":      1,
    "info":     0,
}

# Claude model used for AI analysis
_ANALYSIS_MODEL = "claude-sonnet-4-6"


class OrgAnalyser:
    """
    Runs health checks against raw Salesforce org data and produces a
    structured report enriched with AI recommendations.
    """

    def __init__(self):
        self._checks: list[dict] = self._load_checks()
        self._client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse(self, org_data: dict) -> dict:
        """
        Run all enabled health checks against the collected org data.

        Args:
            org_data: Dict produced by collecting all SalesforceClient data
                      methods. Expected keys: security, permissions,
                      automation, data_model, apex.

        Returns:
            {
                "summary": {
                    "health_score": int (0-100),
                    "total_findings": int,
                    "critical_count": int,
                    "high_count":     int,
                    "medium_count":   int,
                    "low_count":      int,
                    "info_count":     int,
                },
                "findings": [
                    {
                        "id":             str,
                        "category":       str,
                        "name":           str,
                        "severity":       str,
                        "status":         "FAIL" | "PASS" | "INFO",
                        "details":        str,
                        "ai_analysis":    str,
                        "recommendation": str,
                    },
                    ...
                ]
            }
        """
        print("\nRunning org health checks ...")
        raw_findings: list[dict] = []

        raw_findings.extend(self._evaluate_security(org_data))
        raw_findings.extend(self._evaluate_automations(org_data))
        raw_findings.extend(self._evaluate_data_model(org_data))
        raw_findings.extend(self._evaluate_governance(org_data))
        raw_findings.extend(self._evaluate_integrations(org_data))

        # Only FAIL findings contribute to the score
        failed = [f for f in raw_findings if f["status"] == "FAIL"]

        pass_count = sum(1 for f in raw_findings if f["status"] == "PASS")
        info_count = sum(1 for f in raw_findings if f["status"] == "INFO")
        print(f"  Checks completed: {len(raw_findings)} "
              f"(FAIL={len(failed)}, PASS={pass_count}, INFO={info_count})")
        print("Enriching findings with AI analysis ...")

        enriched_findings = []
        for finding in raw_findings:
            if finding["status"] == "FAIL":
                ai_result = self._get_ai_analysis(finding)
                finding["ai_analysis"]    = ai_result.get("analysis", "")
                finding["recommendation"] = ai_result.get("recommendation", "")
            else:
                finding["ai_analysis"]    = ""
                finding["recommendation"] = ""
            enriched_findings.append(finding)

        # Count by severity among FAIL findings only
        severity_counts = {s: 0 for s in _SEVERITY_WEIGHTS}
        for f in failed:
            sev = f.get("severity", "info").lower()
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        health_score = self._calculate_health_score(severity_counts)

        return {
            "summary": {
                "health_score":   health_score,
                "total_findings": len(failed),
                "critical_count": severity_counts["critical"],
                "high_count":     severity_counts["high"],
                "medium_count":   severity_counts["medium"],
                "low_count":      severity_counts["low"],
                "info_count":     severity_counts["info"],
            },
            "findings": enriched_findings,
        }

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_checks(self) -> list[dict]:
        """Load and parse checks_config.yaml."""
        with open(_CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
        checks = config.get("checks", [])
        enabled = [c for c in checks if c.get("enabled", True)]
        print(f"Loaded {len(enabled)} enabled checks from {_CONFIG_PATH.name}")
        return enabled

    def _get_check(self, check_id: str) -> dict | None:
        """Return a check config by its ID, or None if not found / disabled."""
        for c in self._checks:
            if c["id"] == check_id:
                return c
        return None

    # ------------------------------------------------------------------
    # Security checks
    # ------------------------------------------------------------------

    def _evaluate_security(self, org_data: dict) -> list[dict]:
        """Run all enabled Security checks and return raw findings."""
        findings = []
        security  = org_data.get("security", {})
        perms     = org_data.get("permissions", {})

        # SEC-001 — System Administrators with Active Sessions
        check = self._get_check("SEC-001")
        if check:
            sys_admins  = security.get("sys_admin_users", [])
            admin_count = len(sys_admins)
            threshold   = check.get("threshold", {}).get("max_count", 5)
            status      = "FAIL" if admin_count > threshold else "PASS"
            admin_names = [f"{a['name']} ({a['username']})" for a in sys_admins[:10]]
            details = (
                f"{admin_count} active System Administrator user(s) found "
                f"(threshold: {threshold}). "
                + (f"Admins: {', '.join(admin_names)}" if admin_names else "")
            )
            findings.append(self._make_finding(check, status, details))

        # SEC-002 — Users with Modify All Data Permission
        check = self._get_check("SEC-002")
        if check:
            dangerous = perms.get("dangerous_perm_sets", [])
            modify_all = [ps for ps in dangerous if ps.get("modify_all")]
            threshold  = check.get("threshold", {}).get("max_count", 3)
            status     = "FAIL" if len(modify_all) > threshold else "PASS"
            names = [ps["label"] for ps in modify_all[:10]]
            details = (
                f"{len(modify_all)} permission set(s) grant Modify All Data "
                f"(threshold: {threshold}). "
                + (f"Sets: {', '.join(names)}" if names else "")
            )
            findings.append(self._make_finding(check, status, details))

        # SEC-003 — Profiles with View All Data Permission
        check = self._get_check("SEC-003")
        if check:
            view_all  = [ps for ps in perms.get("dangerous_perm_sets", []) if ps.get("view_all")]
            threshold = check.get("threshold", {}).get("max_count", 2)
            status    = "FAIL" if len(view_all) > threshold else "PASS"
            names = [ps["label"] for ps in view_all[:10]]
            details = (
                f"{len(view_all)} permission set(s) grant View All Data "
                f"(threshold: {threshold}). "
                + (f"Sets: {', '.join(names)}" if names else "")
            )
            findings.append(self._make_finding(check, status, details))

        # SEC-005 — MFA Enforcement (informational — requires metadata API to verify)
        check = self._get_check("SEC-005")
        if check:
            details = (
                "MFA enforcement status could not be automatically verified via the REST API. "
                "Manual review of Setup > Identity > Identity Verification is recommended."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # SEC-006 — Login IP Range Restrictions (informational summary)
        check = self._get_check("SEC-006")
        if check:
            total_users = security.get("total_active_users", 0)
            details = (
                f"The org has {total_users} active internal users. "
                "Profile-level IP range restrictions cannot be queried via SOQL REST API alone. "
                "Review Login IP Ranges in Setup > Profiles for sensitive profiles."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # SEC-010 — API-Only Users Without IP Restrictions
        check = self._get_check("SEC-010")
        if check:
            api_users = security.get("integration_users", [])
            if api_users:
                names    = [f"{u['name']} ({u['username']})" for u in api_users[:10]]
                status   = "FAIL"
                details  = (
                    f"{len(api_users)} likely integration/API user(s) detected "
                    f"that may lack IP restrictions: {', '.join(names)}. "
                    "Verify each user's profile has Login IP Ranges configured."
                )
            else:
                status  = "PASS"
                details = "No obvious integration/API user accounts detected."
            findings.append(self._make_finding(check, status, details))

        # SEC-011 — Permission Sets Granting Admin-Level Access
        check = self._get_check("SEC-011")
        if check:
            all_dangerous = perms.get("dangerous_perm_sets", [])
            # Admin-equivalent = both manage_users AND modify_all
            admin_equiv = [
                ps for ps in all_dangerous
                if ps.get("modify_all") and ps.get("manage_users")
            ]
            if admin_equiv:
                names   = [ps["label"] for ps in admin_equiv[:10]]
                status  = "FAIL"
                details = (
                    f"{len(admin_equiv)} permission set(s) combine Modify All Data "
                    f"+ Manage Users (admin-equivalent): {', '.join(names)}."
                )
            else:
                status  = "PASS"
                details = "No permission sets found combining Modify All Data and Manage Users."
            findings.append(self._make_finding(check, status, details))

        # SEC-004 — Password Policy Strength (Security Health Check score)
        check = self._get_check("SEC-004")
        if check:
            health_check = org_data.get("security_health_check", {})
            raw_score    = health_check.get("score")
            score        = _safe_int(raw_score, -1) if raw_score is not None else None
            if score is not None and score >= 0:
                threshold = check.get("threshold", {}).get("min_score", 70)
                if score < threshold:
                    status  = "FAIL"
                    details = (
                        f"Salesforce Security Health Check score is {score}/100 "
                        f"(threshold: {threshold}). "
                        "Review and remediate items in Setup > Security > Health Check."
                    )
                else:
                    status  = "PASS"
                    details = (
                        f"Salesforce Security Health Check score is {score}/100 "
                        f"(meets threshold of {threshold})."
                    )
            else:
                status  = "INFO"
                details = (
                    "Security Health Check score could not be retrieved via the Tooling API "
                    "on this org edition. "
                    "Review manually in Setup > Security > Health Check."
                )
            findings.append(self._make_finding(check, status, details))

        # SEC-007 — Guest User Profile Permissions
        check = self._get_check("SEC-007")
        if check:
            guest        = org_data.get("guest_users", {})
            guest_users  = guest.get("users", [])
            if guest_users:
                status  = "FAIL"
                profiles = list({u["profile"] for u in guest_users})
                details  = (
                    f"{len(guest_users)} active Guest User(s) found "
                    f"on profile(s): {', '.join(profiles[:10])}. "
                    "Review permissions granted to guest profiles in Setup > Guest User."
                )
            else:
                status  = "PASS"
                details = "No active Guest User accounts found."
            findings.append(self._make_finding(check, status, details))

        # SEC-012 — Inactive Users with Active Licenses
        check = self._get_check("SEC-012")
        if check:
            inactive      = org_data.get("inactive_users", {})
            users         = inactive.get("users", [])
            threshold     = check.get("threshold", {}).get("inactive_days", 90)
            if users:
                status = "FAIL"
                names  = [f"{u['name']} ({u['username']})" for u in users[:10]]
                details = (
                    f"{len(users)} active user(s) have not logged in for {threshold}+ days: "
                    f"{', '.join(names)}"
                    + (" ..." if len(users) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = f"No active users found with login inactivity exceeding {threshold} days."
            findings.append(self._make_finding(check, status, details))

        # SEC-008 — Named Credentials for External Callouts
        check = self._get_check("SEC-008")
        if check:
            nc       = org_data.get("named_credentials", {})
            creds    = nc.get("credentials", [])
            if creds:
                names   = [c["name"] for c in creds[:10]]
                status  = "PASS"
                details = (
                    f"{len(creds)} Named Credential(s) found: {', '.join(names)}"
                    + (" ..." if len(creds) > 10 else ".")
                    + " The org is using Named Credentials for callout management."
                )
            else:
                status  = "INFO"
                details = (
                    "No Named Credentials found in this org. "
                    "Adopt Named Credentials for all external callouts to avoid "
                    "hardcoded endpoints and credentials in Apex or Custom Settings."
                )
            findings.append(self._make_finding(check, status, details))

        # SEC-009 — Field-Level Security on Sensitive Fields
        check = self._get_check("SEC-009")
        if check:
            fls            = org_data.get("sensitive_field_permissions", {})
            readable_by    = fls.get("fields_readable_by", {})
            threshold      = check.get("threshold", {}).get("max_reader_count", 3)
            overexposed    = {
                field: readers
                for field, readers in readable_by.items()
                if len(readers) > threshold
            }
            if overexposed:
                status  = "FAIL"
                examples = [
                    f"{field} ({len(readers)} readers)"
                    for field, readers in list(overexposed.items())[:8]
                ]
                details = (
                    f"{len(overexposed)} sensitive field(s) are readable by more than "
                    f"{threshold} profile(s)/permission set(s): {', '.join(examples)}"
                    + (" ..." if len(overexposed) > 8 else ".")
                )
            elif readable_by:
                status  = "PASS"
                details = (
                    f"{len(readable_by)} sensitive field(s) found; all are restricted "
                    f"to {threshold} or fewer profile(s)/permission set(s)."
                )
            else:
                status  = "PASS"
                details = (
                    "No sensitive fields (SSN, Salary, Credit Card, etc.) detected, "
                    "or all are properly restricted."
                )
            findings.append(self._make_finding(check, status, details))

        return findings

    # ------------------------------------------------------------------
    # Automation checks
    # ------------------------------------------------------------------

    def _evaluate_automations(self, org_data: dict) -> list[dict]:
        """Run all enabled Automation checks and return raw findings."""
        findings = []
        automation = org_data.get("automation", {})
        apex       = org_data.get("apex", {})

        # AUTO-001 — Active Process Builders (Legacy)
        check = self._get_check("AUTO-001")
        if check:
            pb_count  = automation.get("legacy_process_builders", 0)
            threshold = check.get("threshold", {}).get("max_count", 0)
            status    = "FAIL" if pb_count > threshold else "PASS"
            details   = (
                f"{pb_count} active Process Builder automation(s) found "
                f"(threshold: {threshold}). Salesforce has deprecated Process Builder."
            )
            findings.append(self._make_finding(check, status, details))

        # AUTO-002 — Active Workflow Rules (Legacy)
        check = self._get_check("AUTO-002")
        if check:
            wf_count  = automation.get("legacy_workflow_rules", 0)
            threshold = check.get("threshold", {}).get("max_count", 0)
            status    = "FAIL" if wf_count > threshold else "PASS"
            details   = (
                f"{wf_count} active Workflow Rule(s) found "
                f"(threshold: {threshold}). Salesforce recommends migration to Flow."
            )
            findings.append(self._make_finding(check, status, details))

        # AUTO-003 — Multiple Triggers on the Same Object
        check = self._get_check("AUTO-003")
        if check:
            multi_trigger_objects = automation.get("objects_multi_triggers", [])
            threshold = check.get("threshold", {}).get("max_triggers_per_object", 1)
            if multi_trigger_objects:
                status  = "FAIL"
                details = (
                    f"{len(multi_trigger_objects)} object(s) have more than "
                    f"{threshold} active Apex Trigger(s): "
                    f"{', '.join(multi_trigger_objects[:10])}."
                )
            else:
                status  = "PASS"
                details = "All objects have at most one active Apex Trigger."
            findings.append(self._make_finding(check, status, details))

        # AUTO-004 — Flows Without Error Handling
        check = self._get_check("AUTO-004")
        if check:
            record_triggered = org_data.get("record_triggered_flows", {})
            flows     = record_triggered.get("flows", [])
            threshold = 5
            if len(flows) > threshold:
                status = "FAIL"
                names  = [f["label"] for f in flows[:10]]
                details = (
                    f"{len(flows)} active record-triggered flow(s) found "
                    f"(threshold: {threshold}). Verify each has a fault path configured: "
                    f"{', '.join(names)}"
                    + (" ..." if len(flows) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = (
                    f"{len(flows)} active record-triggered flow(s) found — "
                    f"within threshold of {threshold}. "
                    "Verify each has a fault path configured."
                )
            findings.append(self._make_finding(check, status, details))

        # AUTO-006 — Apex Triggers Without Test Coverage (informational — needs ApexCodeCoverage)
        check = self._get_check("AUTO-006")
        if check:
            total_triggers = apex.get("total_triggers", 0)
            details = (
                f"The org has {total_triggers} Apex Trigger(s). "
                "Detailed per-trigger test coverage requires running tests via the Tooling API "
                "and is not collected in this snapshot. "
                "Verify coverage via Setup > Apex Test Execution."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # AUTO-007 — Overlapping Automation (Flow + Trigger on same object/event)
        check = self._get_check("AUTO-007")
        if check:
            trigger_objects = set(automation.get("triggers_by_object", {}).keys())
            flow_objects    = set()
            for flow in automation.get("active_flows", []):
                if flow.get("process_type") in ("AutoLaunchedFlow", "Workflow") and flow.get("object"):
                    flow_objects.add(flow["object"])
            overlap = trigger_objects & flow_objects
            if overlap:
                status  = "FAIL"
                details = (
                    f"{len(overlap)} object(s) have both an active Apex Trigger "
                    f"and an active Flow: {', '.join(sorted(overlap)[:10])}. "
                    "Review for potential double-execution or conflicts."
                )
            else:
                status  = "PASS"
                details = "No objects detected with overlapping Trigger and Flow automation."
            findings.append(self._make_finding(check, status, details))

        # AUTO-009 — Active Flows With No Description
        check = self._get_check("AUTO-009")
        if check:
            total_flows = automation.get("total_active_flows", 0)
            # FlowDefinitionView doesn't return Description via SOQL — surface as informational
            details = (
                f"The org has {total_flows} active Flow(s). "
                "Flow descriptions cannot be retrieved via SOQL; "
                "review via Setup > Flows to ensure all flows are documented."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # AUTO-010 — High-Volume Scheduled Jobs
        check = self._get_check("AUTO-010")
        if check:
            scheduled = org_data.get("scheduled_jobs", {})
            jobs      = scheduled.get("jobs", [])
            names     = [j["name"] for j in jobs[:10]]
            details = (
                f"{len(jobs)} scheduled Apex job(s) currently waiting to execute: "
                + (", ".join(names) if names else "none")
                + (" ..." if len(jobs) > 10 else ".")
                + " Review scheduling frequency and verify there are no conflicts or load spikes."
            )
            findings.append(self._make_finding(check, "INFO", details))

        return findings

    # ------------------------------------------------------------------
    # Data model checks
    # ------------------------------------------------------------------

    def _evaluate_data_model(self, org_data: dict) -> list[dict]:
        """Run all enabled Data Model and Governance checks and return raw findings."""
        findings = []
        data_model = org_data.get("data_model", {})
        apex       = org_data.get("apex", {})

        # DATA-001 — Custom Objects Without Description
        check = self._get_check("DATA-001")
        if check:
            no_desc   = data_model.get("objects_without_description", [])
            total     = data_model.get("total_custom_objects", 0)
            if no_desc:
                status  = "FAIL"
                pct     = round(len(no_desc) / total * 100) if total else 0
                details = (
                    f"{len(no_desc)} of {total} custom object(s) ({pct}%) "
                    f"have no description: {', '.join(no_desc[:10])}"
                    + (" ..." if len(no_desc) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = f"All {total} custom object(s) have descriptions."
            findings.append(self._make_finding(check, status, details))

        # DATA-002 — Custom Fields Without Description
        check = self._get_check("DATA-002")
        if check:
            objects_over = data_model.get("objects_over_field_limit", [])
            total_objs   = data_model.get("total_custom_objects", 0)
            threshold    = check.get("threshold", {}).get("min_coverage_percent", 80)
            # We use objects_over_field_limit as a proxy for objects needing field-level attention
            status  = "INFO"
            details = (
                f"{len(objects_over)} custom object(s) have more than 50 custom fields "
                f"(potential field sprawl): "
                + (', '.join(objects_over[:10]) if objects_over else "none")
                + ". Full field-description coverage requires per-field metadata API inspection."
            )
            findings.append(self._make_finding(check, status, details))

        # DATA-003 — Objects Approaching Record Limit
        check = self._get_check("DATA-003")
        if check:
            details = (
                "Record counts per object require the sforce-limit-info header or "
                "per-object COUNT() queries. This check is captured as informational; "
                "review record counts in Setup > Storage Usage."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # DATA-004 — Multi-Select Picklist Fields
        check = self._get_check("DATA-004")
        if check:
            mspl      = org_data.get("multiselect_picklist_fields", {})
            fields    = mspl.get("fields", [])
            threshold = check.get("threshold", {}).get("max_count", 10)
            if len(fields) > threshold:
                status = "FAIL"
                field_names = [f"{f['object']}.{f['field']}" for f in fields[:10]]
                details = (
                    f"{len(fields)} Multi-Select Picklist field(s) found "
                    f"(threshold: {threshold}): {', '.join(field_names)}"
                    + (" ..." if len(fields) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = (
                    f"{len(fields)} Multi-Select Picklist field(s) found — "
                    f"within the threshold of {threshold}."
                )
            findings.append(self._make_finding(check, status, details))

        # DATA-006 — Duplicate Rules Configuration
        check = self._get_check("DATA-006")
        if check:
            dup_rules    = org_data.get("duplicate_rules", {})
            rules_by_obj = dup_rules.get("rules_by_object", {})
            required     = check.get("required_objects", ["Account", "Contact", "Lead"])
            missing      = [obj for obj in required if obj not in rules_by_obj]
            if missing:
                status  = "FAIL"
                details = (
                    f"{len(missing)} of {len(required)} required object(s) have no active "
                    f"Duplicate Rule: {', '.join(missing)}. "
                    f"Covered: {', '.join(obj for obj in required if obj in rules_by_obj) or 'none'}."
                )
            else:
                status  = "PASS"
                details = (
                    f"All {len(required)} required object(s) have at least one active "
                    f"Duplicate Rule ({', '.join(required)})."
                )
            findings.append(self._make_finding(check, status, details))

        # DATA-005 — Objects With More Than 2 Master-Detail Relationships
        check = self._get_check("DATA-005")
        if check:
            md        = org_data.get("master_detail_fields", {})
            by_object = md.get("fields_by_object", {})
            at_limit  = {
                obj: fields
                for obj, fields in by_object.items()
                if len(fields) >= 2
            }
            if at_limit:
                status  = "FAIL"
                examples = [
                    f"{obj} ({len(fields)} master-detail)"
                    for obj, fields in list(at_limit.items())[:10]
                ]
                details = (
                    f"{len(at_limit)} object(s) are at or approaching the "
                    f"Salesforce maximum of 2 Master-Detail relationships: "
                    f"{', '.join(examples)}."
                )
            else:
                status  = "PASS"
                details = (
                    "No objects found with 2 or more Master-Detail relationships."
                )
            findings.append(self._make_finding(check, status, details))

        # DATA-007 — Custom Objects Without External ID Fields
        check = self._get_check("DATA-007")
        if check:
            ext_id       = org_data.get("external_id_fields", {})
            with_ext_id  = set(ext_id.get("objects_with_external_id", []))
            all_custom   = [
                obj["name"]
                for obj in data_model.get("custom_objects", [])
            ]
            missing      = [obj for obj in all_custom if obj not in with_ext_id]
            if missing:
                status  = "FAIL"
                details = (
                    f"{len(missing)} of {len(all_custom)} custom object(s) have no "
                    f"External ID field: {', '.join(missing[:10])}"
                    + (" ..." if len(missing) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = (
                    f"All {len(all_custom)} custom object(s) have at least one "
                    "External ID field."
                )
            findings.append(self._make_finding(check, status, details))

        # DATA-009 — Validation Rules Without Error Message Details
        check = self._get_check("DATA-009")
        if check:
            vr_data   = org_data.get("validation_rules", {})
            rules     = vr_data.get("rules", [])
            threshold = check.get("threshold", {}).get("min_message_length", 20)
            short_msg = [
                r for r in rules
                if len(r.get("error_message", "")) < threshold
            ]
            if short_msg:
                status  = "FAIL"
                examples = [
                    f"{r['object']}: \"{r['error_message'][:40]}\""
                    for r in short_msg[:8]
                ]
                details = (
                    f"{len(short_msg)} of {len(rules)} active Validation Rule(s) "
                    f"have error messages shorter than {threshold} characters: "
                    f"{', '.join(examples)}"
                    + (" ..." if len(short_msg) > 8 else ".")
                )
            else:
                status  = "PASS"
                details = (
                    f"All {len(rules)} active Validation Rule(s) have error messages "
                    f"of at least {threshold} characters."
                )
            findings.append(self._make_finding(check, status, details))

        return findings

    # ------------------------------------------------------------------
    # Governance checks
    # ------------------------------------------------------------------

    def _evaluate_governance(self, org_data: dict) -> list[dict]:
        """Run all enabled Governance checks and return raw findings."""
        findings = []
        apex              = org_data.get("apex", {})
        users_no_role     = org_data.get("users_without_role", {})
        org_limits        = org_data.get("org_limits", {})

        # GOV-002 — Apex Test Coverage (informational)
        check = self._get_check("GOV-002")
        if check:
            total_classes = apex.get("total_classes", 0)
            details = (
                f"The org has {total_classes} Apex class(es). "
                "Per-class test coverage requires running tests via the Tooling API. "
                "Verify overall coverage via Setup > Apex Test Execution (must be ≥ 75%)."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # GOV-003 — Overall Org Test Coverage
        check = self._get_check("GOV-003")
        if check:
            org_coverage = org_data.get("org_coverage", {})
            raw_pct      = org_coverage.get("percent_covered")
            pct_covered  = _safe_int(raw_pct) if raw_pct is not None else None
            threshold    = check.get("threshold", {}).get("min_coverage_percent", 75)
            if pct_covered is None:
                status  = "INFO"
                details = (
                    "Org-wide Apex test coverage could not be retrieved from the Tooling API. "
                    "Verify coverage via Setup > Apex Test Execution (minimum required: 75%)."
                )
            elif pct_covered < threshold:
                status  = "FAIL"
                details = (
                    f"Org-wide Apex test coverage is {pct_covered}%, "
                    f"below the required minimum of {threshold}%. "
                    "Salesforce blocks production deployments below this threshold."
                )
            else:
                status  = "PASS"
                details = (
                    f"Org-wide Apex test coverage is {pct_covered}%, "
                    f"meeting the required minimum of {threshold}%."
                )
            findings.append(self._make_finding(check, status, details))

        # GOV-004 — Storage Usage (Data and File)
        check = self._get_check("GOV-004")
        if check:
            warning_pct  = check.get("threshold", {}).get("warning_percent", 75)
            critical_pct = check.get("threshold", {}).get("critical_percent", 90)
            data_limit   = org_limits.get("DataStorageMB", {})
            file_limit   = org_limits.get("FileStorageMB", {})

            if data_limit and file_limit:
                data_max       = _safe_int(data_limit.get("Max", 0))
                data_remaining = _safe_int(data_limit.get("Remaining", 0))
                file_max       = _safe_int(file_limit.get("Max", 0))
                file_remaining = _safe_int(file_limit.get("Remaining", 0))

                data_used_pct = round((data_max - data_remaining) / data_max * 100) if data_max else 0
                file_used_pct = round((file_max - file_remaining) / file_max * 100) if file_max else 0
                max_pct       = max(data_used_pct, file_used_pct)

                if max_pct >= critical_pct:
                    status = "FAIL"
                elif max_pct >= warning_pct:
                    status = "FAIL"
                else:
                    status = "PASS"

                details = (
                    f"Data storage: {data_used_pct}% used "
                    f"({data_max - data_remaining} MB of {data_max} MB). "
                    f"File storage: {file_used_pct}% used "
                    f"({file_max - file_remaining} MB of {file_max} MB). "
                    f"Thresholds: warning {warning_pct}%, critical {critical_pct}%."
                )
            else:
                status  = "INFO"
                details = (
                    "Storage usage data unavailable from the Limits API. "
                    "Review storage in Setup > Storage Usage."
                )
            findings.append(self._make_finding(check, status, details))

        # GOV-005 — Hardcoded IDs in Apex (remapped from debug logs slot)
        check = self._get_check("GOV-005")
        if check:
            classes_with_ids  = apex.get("classes_with_hardcoded_ids", [])
            triggers_with_ids = apex.get("triggers_with_hardcoded_ids", [])
            all_with_ids      = classes_with_ids + triggers_with_ids
            if all_with_ids:
                status  = "FAIL"
                details = (
                    f"{len(all_with_ids)} Apex file(s) contain potential hardcoded Salesforce IDs "
                    f"(classes: {len(classes_with_ids)}, triggers: {len(triggers_with_ids)}). "
                    f"Examples: {', '.join(all_with_ids[:8])}"
                    + (" ..." if len(all_with_ids) > 8 else ".")
                )
            else:
                status  = "PASS"
                details = "No hardcoded Salesforce IDs detected in Apex classes or triggers."
            # Copy to avoid mutating the config
            check = dict(check)
            check["name"]        = "Hardcoded Salesforce IDs in Apex Code"
            check["severity"]    = "high"
            check["description"] = (
                "Hardcoded Salesforce record IDs in Apex break when deployed between orgs "
                "and are a maintenance liability."
            )
            findings.append(self._make_finding(check, status, details))

        # GOV-007 — Deprecated API Versions in Use
        check = self._get_check("GOV-007")
        if check:
            below_min = apex.get("classes_below_min_api", [])
            threshold = check.get("threshold", {}).get("min_api_version", 50.0)
            if below_min:
                status = "FAIL"
                names  = [f"{c['name']} (v{c['api_version']})" for c in below_min[:10]]
                details = (
                    f"{len(below_min)} Apex class(es) use an API version below {threshold}: "
                    f"{', '.join(names)}"
                    + (" ..." if len(below_min) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = f"All Apex classes use API version {threshold} or higher."
            findings.append(self._make_finding(check, status, details))

        # GOV-010 — Active Users Without Role Assignment
        check = self._get_check("GOV-010")
        if check:
            users     = users_no_role.get("users", [])
            threshold = check.get("threshold", {}).get("max_count", 0)
            if users:
                status = "FAIL"
                names  = [f"{u['name']} ({u['username']})" for u in users[:10]]
                details = (
                    f"{len(users)} active user(s) have no Role assigned: "
                    f"{', '.join(names)}"
                    + (" ..." if len(users) > 10 else ".")
                )
            else:
                status  = "PASS"
                details = "All active internal users have a Role assigned."
            findings.append(self._make_finding(check, status, details))

        # GOV-006 — Custom Labels Count
        check = self._get_check("GOV-006")
        if check:
            labels    = org_data.get("custom_labels", {})
            count     = _safe_int(labels.get("count", 0))
            threshold = check.get("threshold", {}).get("warning_count", 5000)
            if count > threshold:
                status  = "FAIL"
                details = (
                    f"{count:,} Custom Label(s) found — exceeds the warning threshold "
                    f"of {threshold:,}. Review and retire unused labels to reduce sprawl."
                )
            else:
                status  = "PASS"
                details = (
                    f"{count:,} Custom Label(s) found — within the threshold of {threshold:,}."
                )
            findings.append(self._make_finding(check, status, details))

        return findings

    # ------------------------------------------------------------------
    # Integrations checks
    # ------------------------------------------------------------------

    def _evaluate_integrations(self, org_data: dict) -> list[dict]:
        """Run all enabled Integrations checks and return raw findings."""
        findings   = []
        org_limits = org_data.get("org_limits", {})

        # INT-001 — Connected Apps With Excessive OAuth Scopes
        check = self._get_check("INT-001")
        if check:
            connected = org_data.get("connected_apps", {})
            apps      = connected.get("apps", [])
            names     = [a["name"] for a in apps[:10]]
            details = (
                f"{len(apps)} Connected App(s) found in this org: "
                + (", ".join(names) if names else "none")
                + (" ..." if len(apps) > 10 else ".")
                + " Review OAuth scopes for each app in Setup > App Manager — "
                "restrict to the minimum required scopes."
            )
            findings.append(self._make_finding(check, "INFO", details))

        # INT-005 — REST API Daily Request Limit Usage
        check = self._get_check("INT-005")
        if check:
            warning_pct  = check.get("threshold", {}).get("warning_percent", 70)
            critical_pct = check.get("threshold", {}).get("critical_percent", 90)
            api_limit    = org_limits.get("DailyApiRequests", {})

            if api_limit:
                max_requests       = _safe_int(api_limit.get("Max", 0))
                remaining_requests = _safe_int(api_limit.get("Remaining", 0))
                used_requests      = max_requests - remaining_requests
                used_pct           = round(used_requests / max_requests * 100) if max_requests else 0

                if used_pct >= critical_pct:
                    status = "FAIL"
                elif used_pct >= warning_pct:
                    status = "FAIL"
                else:
                    status = "PASS"

                details = (
                    f"Daily API request usage: {used_pct}% "
                    f"({used_requests:,} of {max_requests:,} requests used today). "
                    f"Thresholds: warning {warning_pct}%, critical {critical_pct}%."
                )
            else:
                status  = "INFO"
                details = (
                    "API request limit data unavailable from the Limits API. "
                    "Review usage in Setup > Company Information."
                )
            findings.append(self._make_finding(check, status, details))

        # INT-008 — Remote Site Settings With Wildcards
        check = self._get_check("INT-008")
        if check:
            from urllib.parse import urlparse
            remote_sites = org_data.get("remote_site_settings", {})
            sites        = remote_sites.get("sites", [])
            broad_sites  = []
            for s in sites:
                url    = s.get("endpoint_url", "")
                parsed = urlparse(url)
                path   = parsed.path.rstrip("/")
                if "*" in url or path == "":
                    broad_sites.append(s)
            if broad_sites:
                status = "FAIL"
                names  = [f"{s['name']} ({s['endpoint_url']})" for s in broad_sites[:10]]
                details = (
                    f"{len(broad_sites)} of {len(sites)} active Remote Site Setting(s) "
                    "have overly broad endpoints (top-level domain only or wildcard): "
                    f"{', '.join(names)}."
                )
            else:
                status  = "PASS"
                details = (
                    f"All {len(sites)} active Remote Site Setting(s) have "
                    "specific endpoint URLs."
                )
            findings.append(self._make_finding(check, status, details))

        return findings

    # ------------------------------------------------------------------
    # AI analysis
    # ------------------------------------------------------------------

    def _get_ai_analysis(self, finding: dict) -> dict:
        """
        Call Claude to produce a concise analysis and actionable recommendation
        for a single FAIL finding.

        Returns:
            {"analysis": str, "recommendation": str}
        """
        system_prompt = (
            "You are a Senior Salesforce Solutions Architect with deep expertise in "
            "org health, security best practices, automation governance, and technical debt. "
            "You are reviewing automated health check findings for a client's Salesforce org. "
            "Be concise, practical, and prioritise actionable guidance. "
            "Use plain English — no markdown headers or bullet points in your response."
        )

        user_prompt = (
            f"Health Check Finding\n"
            f"--------------------\n"
            f"Check ID:   {finding['id']}\n"
            f"Category:   {finding['category']}\n"
            f"Check Name: {finding['name']}\n"
            f"Severity:   {finding['severity'].upper()}\n"
            f"Details:    {finding['details']}\n\n"
            "Provide two short paragraphs:\n"
            "1. Analysis — explain the business/technical risk this finding represents.\n"
            "2. Recommendation — the specific steps the org should take to remediate or improve."
        )

        try:
            response = self._client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=400,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            full_text = response.content[0].text.strip()

            # Split at the second paragraph boundary
            paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]
            if len(paragraphs) >= 2:
                analysis       = paragraphs[0]
                recommendation = "\n\n".join(paragraphs[1:])
            else:
                analysis       = full_text
                recommendation = ""

            return {"analysis": analysis, "recommendation": recommendation}

        except anthropic.APIError as e:
            print(f"  [WARNING] AI analysis failed for {finding['id']}: {e}")
            return {
                "analysis":       "AI analysis unavailable.",
                "recommendation": "Please review this finding manually.",
            }

    # ------------------------------------------------------------------
    # Health score
    # ------------------------------------------------------------------

    def _calculate_health_score(self, severity_counts: dict) -> int:
        """
        Calculate a 0-100 health score by deducting points per severity.

        Deductions:
            Critical : 15 pts each
            High     : 8 pts each
            Medium   : 3 pts each
            Low      : 1 pt each
            Info     : 0 pts
        """
        deductions = sum(
            count * _SEVERITY_WEIGHTS.get(severity, 0)
            for severity, count in severity_counts.items()
        )
        return max(0, 100 - deductions)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_finding(check: dict, status: str, details: str) -> dict:
        """
        Build a standardised finding dict from a check config entry.

        Args:
            check:   The check config dict from checks_config.yaml.
            status:  "FAIL", "PASS", or "INFO".
            details: Human-readable description of what was found.

        Returns:
            A finding dict ready to be included in the report.
        """
        finding = {
            "id":             check["id"],
            "category":       check["category"],
            "name":           check["name"],
            "severity":       check.get("severity", "info"),
            "status":         status,
            "details":        details,
            "description":    str(check.get("description", "")).strip(),
            "ai_analysis":    "",   # populated later by _get_ai_analysis
            "recommendation": "",  # populated later by _get_ai_analysis
        }
        _log_check_result(finding)
        return finding
