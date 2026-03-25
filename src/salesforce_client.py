"""
salesforce_client.py
--------------------
Handles authentication and all data collection calls to the Salesforce org.
Uses the REST API (v59.0) with username/password/security token auth.

Usage:
    client = SalesforceClient()
    client.connect()                    # authenticate first
    client.test_connection()            # verify and print org info
    data = client.get_user_security_data()
"""

import os
import re
import sys
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# Salesforce API version used for all requests
API_VERSION = "v59.0"

# Salesforce SOAP login endpoint — no Connected App required
SOAP_LOGIN_URL = "https://login.salesforce.com/services/Soap/u/59.0"

SOAP_LOGIN_BODY = """<?xml version="1.0" encoding="utf-8"?>
<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
  <env:Body>
    <n1:login xmlns:n1="urn:partner.soap.sforce.com">
      <n1:username>{username}</n1:username>
      <n1:password>{password}</n1:password>
    </n1:login>
  </env:Body>
</env:Envelope>"""

# Standard objects whose OWD sharing model we always inspect
STANDARD_OBJECTS_FOR_OWD = ["Account", "Contact", "Opportunity", "Lead", "Case"]

# Keywords that suggest a user account is an integration/API account
INTEGRATION_USER_KEYWORDS = ["integration", "api", "int", "system", "svc", "service", "sync", "etl", "mule"]

# Salesforce record ID pattern: 15 or 18 alphanumeric characters
SF_ID_PATTERN = re.compile(r"\b[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _soql_encode(query: str) -> str:
    """URL-encode a SOQL query string for use in a GET request."""
    return requests.utils.quote(query)


# ---------------------------------------------------------------------------
# SalesforceClient
# ---------------------------------------------------------------------------

class SalesforceClient:
    """
    Authenticates with a Salesforce org and exposes methods that collect
    raw data for each health-check category.

    Call connect() before any data method.
    """

    def __init__(self):
        self._access_token: str | None = None
        self._instance_url: str | None = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict:
        """Standard headers for authenticated REST calls."""
        if not self._access_token:
            raise RuntimeError("Not connected. Call connect() first.")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _rest_url(self, path: str) -> str:
        """Build a full REST API URL from a relative path."""
        return f"{self._instance_url}/services/data/{API_VERSION}/{path}"

    def _soql_query(self, soql: str, tooling: bool = False) -> dict:
        """
        Execute a SOQL query via the REST or Tooling API.

        Args:
            soql:    The SOQL query string.
            tooling: If True, routes through the Tooling API endpoint.

        Returns:
            The parsed JSON response dict, or {"records": [], "error": "..."} on failure.
        """
        endpoint = "tooling/query" if tooling else "query"
        url = self._rest_url(f"{endpoint}?q={_soql_encode(soql)}")
        try:
            response = self._session.get(url, headers=self._headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP {e.response.status_code} for query: {soql[:80]}..."
            try:
                detail = e.response.json()
                error_msg += f" — {detail}"
            except Exception:
                pass
            print(f"  [WARNING] {error_msg}")
            return {"records": [], "error": error_msg}
        except requests.exceptions.RequestException as e:
            print(f"  [WARNING] Request failed: {e}")
            return {"records": [], "error": str(e)}

    def _get_all_records(self, soql: str, tooling: bool = False) -> list[dict]:
        """
        Paginate through all query results and return a flat list of records.
        Handles the nextRecordsUrl pattern for result sets > 2000 rows.
        """
        all_records = []
        result = self._soql_query(soql, tooling=tooling)

        if "error" in result:
            return []

        all_records.extend(result.get("records", []))

        # Follow pagination links if results are truncated
        while not result.get("done", True):
            next_url = self._instance_url + result["nextRecordsUrl"]
            try:
                response = self._session.get(next_url, headers=self._headers, timeout=30)
                response.raise_for_status()
                result = response.json()
                all_records.extend(result.get("records", []))
            except requests.exceptions.RequestException as e:
                print(f"  [WARNING] Pagination failed: {e}")
                break

        return all_records

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Authenticate with Salesforce using the SOAP login API.
        No Connected App is required — only username, password, and security token.

        Required env vars:
            SALESFORCE_USERNAME
            SALESFORCE_PASSWORD
            SALESFORCE_SECURITY_TOKEN  (append to password; leave blank if org has no IP restrictions)

        Returns:
            True on success, False on failure.
        """
        import xml.etree.ElementTree as ET

        username = os.getenv("SALESFORCE_USERNAME")
        password = os.getenv("SALESFORCE_PASSWORD")
        token    = os.getenv("SALESFORCE_SECURITY_TOKEN", "")

        if not username or not password:
            print("[ERROR] SALESFORCE_USERNAME and SALESFORCE_PASSWORD must be set in .env")
            return False

        print(f"Connecting to Salesforce as {username} ...")

        import html as _html
        soap_body = SOAP_LOGIN_BODY.format(
            username=_html.escape(username),
            password=_html.escape(password + token),
        )

        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction":   "login",
        }

        try:
            response = self._session.post(
                SOAP_LOGIN_URL, data=soap_body, headers=headers, timeout=30
            )
            # Note: SOAP uses HTTP 500 to signal a login fault — don't raise_for_status here.
            # We parse the XML body to detect success vs fault in all cases.
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Connection error: {e}")
            return False

        # Parse the SOAP XML response to extract sessionId and serverUrl
        try:
            root = ET.fromstring(response.text)
            ns = {
                "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
                "sf":      "urn:partner.soap.sforce.com",
            }

            # Check for SOAP fault (login error)
            fault = root.find(".//soapenv:Body/soapenv:Fault", ns)
            if fault is not None:
                fault_string = fault.findtext("faultstring", default="Unknown error")
                print(f"[ERROR] Authentication failed: {fault_string}")
                return False

            session_id  = root.findtext(".//sf:sessionId",  namespaces=ns)
            server_url  = root.findtext(".//sf:serverUrl",  namespaces=ns)

            if not session_id or not server_url:
                print("[ERROR] Could not parse sessionId or serverUrl from SOAP response")
                return False

            # Extract instance URL from the serverUrl
            # e.g. https://na1.salesforce.com/services/Soap/u/59.0/... → https://na1.salesforce.com
            from urllib.parse import urlparse
            parsed = urlparse(server_url)
            self._instance_url = f"{parsed.scheme}://{parsed.netloc}"
            self._access_token = session_id

        except ET.ParseError as e:
            print(f"[ERROR] Failed to parse SOAP response: {e}")
            return False

        print(f"  Connected. Instance: {self._instance_url}")
        return True

    def connect_with_token(self, access_token: str, instance_url: str) -> None:
        """
        Set credentials from an existing OAuth access token.

        Used by the Streamlit web UI after the OAuth callback — bypasses the
        SOAP login flow entirely.

        Args:
            access_token: Salesforce OAuth Bearer token.
            instance_url: Salesforce instance URL (e.g. https://na1.salesforce.com).
        """
        self._access_token = access_token
        self._instance_url = instance_url.rstrip("/")

    def test_connection(self) -> dict:
        """
        Verify the connection is working and return basic org information.

        Returns:
            Dict with org name, id, edition, and API version info.
        """
        print("Testing connection ...")
        url = self._rest_url("sobjects/Organization")
        try:
            # Query the Organization object for key org metadata
            result = self._soql_query(
                "SELECT Id, Name, OrganizationType, InstanceName, IsSandbox "
                "FROM Organization LIMIT 1"
            )
            records = result.get("records", [])
            if not records:
                return {"error": "Could not retrieve org info"}

            org = records[0]
            info = {
                "org_id":            org.get("Id"),
                "org_name":          org.get("Name"),
                "org_type":          org.get("OrganizationType"),
                "instance":          org.get("InstanceName"),
                "is_sandbox":        org.get("IsSandbox"),
                "instance_url":      self._instance_url,
                "api_version":       API_VERSION,
            }
            print(f"  Org: {info['org_name']} ({info['org_type']}) "
                  f"| Instance: {info['instance']} "
                  f"| Sandbox: {info['is_sandbox']}")
            return info
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 1. User & Security Data
    # ------------------------------------------------------------------

    def get_user_security_data(self) -> dict:
        """
        Collect active user data relevant to security checks.

        Returns a dict with:
            - total_active_users:   int
            - sys_admin_users:      list of {Id, Name, Username, LastLoginDate}
            - integration_users:    list of {Id, Name, Username, Profile}
            - users_without_mfa:    list of {Id, Name, Username}
            - profiles_summary:     list of {Name, UserCount}
        """
        print("Collecting user and security data ...")

        # All active internal users with their profile name
        users = self._get_all_records(
            "SELECT Id, Name, Username, IsActive, LastLoginDate, "
            "Profile.Name, Profile.PermissionsModifyAllData, "
            "Profile.PermissionsViewAllData "
            "FROM User "
            "WHERE IsActive = true AND UserType = 'Standard' "
            "ORDER BY Name"
        )

        # Identify System Administrators
        sys_admins = [
            {
                "id":            u["Id"],
                "name":          u["Name"],
                "username":      u["Username"],
                "last_login":    u.get("LastLoginDate"),
                "profile":       u["Profile"]["Name"],
            }
            for u in users
            if u.get("Profile", {}).get("Name") == "System Administrator"
        ]

        # Identify likely integration/API users by name keywords
        integration_users = [
            {
                "id":       u["Id"],
                "name":     u["Name"],
                "username": u["Username"],
                "profile":  u["Profile"]["Name"],
            }
            for u in users
            if any(kw in u["Name"].lower() or kw in u["Username"].lower()
                   for kw in INTEGRATION_USER_KEYWORDS)
        ]

        # Profile summary: count users per profile
        profile_counts: dict[str, int] = {}
        for u in users:
            profile_name = u.get("Profile", {}).get("Name", "Unknown")
            profile_counts[profile_name] = profile_counts.get(profile_name, 0) + 1

        profiles_summary = sorted(
            [{"name": k, "user_count": v} for k, v in profile_counts.items()],
            key=lambda x: x["user_count"],
            reverse=True,
        )

        result = {
            "total_active_users":  len(users),
            "sys_admin_users":     sys_admins,
            "sys_admin_count":     len(sys_admins),
            "integration_users":   integration_users,
            "profiles_summary":    profiles_summary,
        }

        print(f"  Active users: {result['total_active_users']} | "
              f"Sys Admins: {result['sys_admin_count']} | "
              f"Likely integration users: {len(integration_users)}")
        return result

    # ------------------------------------------------------------------
    # 2. OWD Settings
    # ------------------------------------------------------------------

    def get_owd_settings(self) -> dict:
        """
        Retrieve Organisation-Wide Default sharing settings for standard
        and custom objects.

        Returns:
            - standard_objects: list of {name, sharing_model}
            - custom_objects:   list of {name, sharing_model}
            - private_objects:  list of object names where OWD is Private
        """
        print("Collecting OWD sharing settings ...")

        # Standard objects OWD
        std_soql = (
            "SELECT QualifiedApiName, InternalSharingModel, ExternalSharingModel "
            "FROM EntityDefinition "
            f"WHERE QualifiedApiName IN ({', '.join(repr(o) for o in STANDARD_OBJECTS_FOR_OWD)}) "
            "ORDER BY QualifiedApiName"
        )
        std_records = self._get_all_records(std_soql)

        # Custom objects OWD
        cust_soql = (
            "SELECT QualifiedApiName, InternalSharingModel, ExternalSharingModel "
            "FROM EntityDefinition "
            "WHERE QualifiedApiName LIKE '%__c' "
            "AND IsCustomizable = true "
            "ORDER BY QualifiedApiName"
        )
        cust_records = self._get_all_records(cust_soql)

        def _parse(records: list) -> list[dict]:
            return [
                {
                    "name":            r["QualifiedApiName"],
                    "internal_sharing": r.get("InternalSharingModel", "Unknown"),
                    "external_sharing": r.get("ExternalSharingModel", "Unknown"),
                }
                for r in records
            ]

        standard = _parse(std_records)
        custom   = _parse(cust_records)
        all_objs = standard + custom

        private_objects = [o["name"] for o in all_objs if o["internal_sharing"] == "Private"]

        print(f"  Standard objects checked: {len(standard)} | "
              f"Custom objects: {len(custom)} | "
              f"Private OWD: {len(private_objects)}")

        return {
            "standard_objects": standard,
            "custom_objects":   custom,
            "private_objects":  private_objects,
        }

    # ------------------------------------------------------------------
    # 3. Permission Sets Data
    # ------------------------------------------------------------------

    def get_permission_sets_data(self) -> dict:
        """
        Retrieve permission sets and flag dangerous permissions.

        Returns:
            - total_count:          int
            - dangerous_perm_sets:  list of {name, modify_all, view_all, assignee_count}
            - all_perm_sets:        list of {name, label, is_custom}
        """
        print("Collecting permission set data ...")

        # Exclude permission sets that are actually profile "shadows"
        perm_sets = self._get_all_records(
            "SELECT Id, Name, Label, IsCustom, "
            "PermissionsModifyAllData, PermissionsViewAllData, "
            "PermissionsManageUsers, PermissionsAuthorApex "
            "FROM PermissionSet "
            "WHERE IsOwnedByProfile = false "
            "ORDER BY Name"
        )

        dangerous = [
            {
                "id":           ps["Id"],
                "name":         ps["Name"],
                "label":        ps["Label"],
                "modify_all":   ps.get("PermissionsModifyAllData", False),
                "view_all":     ps.get("PermissionsViewAllData", False),
                "manage_users": ps.get("PermissionsManageUsers", False),
                "author_apex":  ps.get("PermissionsAuthorApex", False),
            }
            for ps in perm_sets
            if ps.get("PermissionsModifyAllData") or ps.get("PermissionsViewAllData")
        ]

        print(f"  Permission sets: {len(perm_sets)} | "
              f"With dangerous permissions: {len(dangerous)}")

        return {
            "total_count":          len(perm_sets),
            "dangerous_perm_sets":  dangerous,
            "all_perm_sets": [
                {"name": ps["Name"], "label": ps["Label"], "is_custom": ps["IsCustom"]}
                for ps in perm_sets
            ],
        }

    # ------------------------------------------------------------------
    # 4. Automation Data
    # ------------------------------------------------------------------

    def get_automation_data(self) -> dict:
        """
        Collect data on active Flows, Apex Triggers, and legacy automation.

        Returns:
            - flows_by_type:          dict of {ProcessType: count}
            - active_flows:           list of {label, process_type, object, last_modified}
            - triggers_by_object:     dict of {object_name: [trigger_names]}
            - objects_multi_triggers: list of object names with > 1 trigger
            - legacy_process_builders: int count
            - legacy_workflow_rules:   int count
        """
        print("Collecting automation data ...")

        # Active flows via FlowDefinitionView
        flows = self._get_all_records(
            "SELECT Id, Label, ProcessType, TriggerType, "
            "TriggerObjectOrEventLabel, LastModifiedDate "
            "FROM FlowDefinitionView "
            "WHERE IsActive = true "
            "ORDER BY ProcessType, Label"
        )

        # Group flows by ProcessType
        flows_by_type: dict[str, int] = {}
        for f in flows:
            pt = f.get("ProcessType", "Unknown")
            flows_by_type[pt] = flows_by_type.get(pt, 0) + 1

        active_flows = [
            {
                "label":         f["Label"],
                "process_type":  f.get("ProcessType"),
                "trigger_type":  f.get("TriggerType"),
                "object":        f.get("TriggerObjectOrEventLabel"),
                "last_modified": f.get("LastModifiedDate"),
            }
            for f in flows
        ]

        # Legacy counts
        legacy_pb = flows_by_type.get("Workflow", 0)          # Process Builder type is "Workflow"
        legacy_wf = self._get_all_records(
            "SELECT COUNT() FROM WorkflowRule WHERE Active = true"
        )
        # COUNT() queries return totalSize, not records
        legacy_wf_count = self._soql_query(
            "SELECT COUNT() FROM WorkflowRule WHERE Active = true"
        ).get("totalSize", 0)

        # Active Apex Triggers
        triggers = self._get_all_records(
            "SELECT Id, Name, TableEnumOrId, Status "
            "FROM ApexTrigger "
            "WHERE Status = 'Active' "
            "ORDER BY TableEnumOrId, Name"
        )

        # Group triggers by object name
        triggers_by_object: dict[str, list] = {}
        for t in triggers:
            obj = t.get("TableEnumOrId", "Unknown")
            triggers_by_object.setdefault(obj, []).append(t["Name"])

        objects_multi_triggers = [
            obj for obj, names in triggers_by_object.items() if len(names) > 1
        ]

        print(f"  Active flows: {len(flows)} | "
              f"Active triggers: {len(triggers)} | "
              f"Legacy Process Builders: {legacy_pb} | "
              f"Legacy Workflow Rules: {legacy_wf_count}")

        return {
            "flows_by_type":           flows_by_type,
            "active_flows":            active_flows,
            "total_active_flows":      len(flows),
            "triggers_by_object":      triggers_by_object,
            "total_active_triggers":   len(triggers),
            "objects_multi_triggers":  objects_multi_triggers,
            "legacy_process_builders": legacy_pb,
            "legacy_workflow_rules":   legacy_wf_count,
        }

    # ------------------------------------------------------------------
    # 5. Data Model Data
    # ------------------------------------------------------------------

    def get_data_model_data(self) -> dict:
        """
        Collect information about the org's custom object schema.

        Returns:
            - custom_objects:          list of {name, label, field_count, flagged}
            - objects_over_field_limit: list of object names with > 50 custom fields
            - total_custom_objects:    int
            - objects_without_description: list of names with no description
        """
        print("Collecting data model information ...")

        # All custom objects
        custom_objects = self._get_all_records(
            "SELECT QualifiedApiName, Label, Description, IsCustomizable "
            "FROM EntityDefinition "
            "WHERE QualifiedApiName LIKE '%__c' "
            "AND IsCustomizable = true "
            "ORDER BY QualifiedApiName"
        )

        objects_detail = []
        objects_over_limit = []
        objects_without_description = []

        for obj in custom_objects:
            obj_api_name = obj["QualifiedApiName"]

            # Count custom fields on this object
            field_result = self._soql_query(
                f"SELECT COUNT() FROM FieldDefinition "
                f"WHERE EntityDefinition.QualifiedApiName = '{obj_api_name}' "
                f"AND IsCustom = true"
            )
            custom_field_count = field_result.get("totalSize", 0)

            flagged = custom_field_count > 50
            if flagged:
                objects_over_limit.append(obj_api_name)

            if not obj.get("Description"):
                objects_without_description.append(obj_api_name)

            objects_detail.append({
                "name":          obj_api_name,
                "label":         obj.get("Label"),
                "description":   obj.get("Description") or "",
                "field_count":   custom_field_count,
                "over_50_fields": flagged,
            })

        print(f"  Custom objects: {len(custom_objects)} | "
              f"Over 50 fields: {len(objects_over_limit)} | "
              f"Missing description: {len(objects_without_description)}")

        return {
            "custom_objects":              objects_detail,
            "total_custom_objects":        len(custom_objects),
            "objects_over_field_limit":    objects_over_limit,
            "objects_without_description": objects_without_description,
        }

    # ------------------------------------------------------------------
    # 6. Inactive Users
    # ------------------------------------------------------------------

    def get_inactive_users(self, inactive_days: int = 90) -> dict:
        """
        Query active internal users who have not logged in within the threshold period.

        Args:
            inactive_days: Days of inactivity to flag (default: 90).

        Returns:
            - users:          list of {id, name, username, last_login}
            - count:          int
            - threshold_days: int
        """
        from datetime import datetime, timedelta, timezone

        print(f"Collecting inactive users (no login in {inactive_days}+ days) ...")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=inactive_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        users = self._get_all_records(
            "SELECT Id, Name, Username, LastLoginDate "
            "FROM User "
            "WHERE IsActive = true "
            "AND UserType = 'Standard' "
            f"AND (LastLoginDate < {cutoff} OR LastLoginDate = null) "
            "ORDER BY LastLoginDate ASC NULLS FIRST"
        )

        result_users = [
            {
                "id":         u["Id"],
                "name":       u["Name"],
                "username":   u["Username"],
                "last_login": u.get("LastLoginDate"),
            }
            for u in users
        ]

        print(f"  Inactive users (>{inactive_days} days): {len(result_users)}")
        return {
            "users":          result_users,
            "count":          len(result_users),
            "threshold_days": inactive_days,
        }

    # ------------------------------------------------------------------
    # 7. Users Without Role
    # ------------------------------------------------------------------

    def get_users_without_role(self) -> dict:
        """
        Query active internal users who have no Role assigned.

        Users without a role do not participate in the Role Hierarchy for
        record sharing, which can cause unintended data access gaps.

        Returns:
            - users: list of {id, name, username, profile}
            - count: int
        """
        print("Collecting users without role assignment ...")

        users = self._get_all_records(
            "SELECT Id, Name, Username, Profile.Name "
            "FROM User "
            "WHERE IsActive = true "
            "AND UserType = 'Standard' "
            "AND UserRoleId = null "
            "ORDER BY Name"
        )

        result_users = [
            {
                "id":       u["Id"],
                "name":     u["Name"],
                "username": u["Username"],
                "profile":  u.get("Profile", {}).get("Name", "Unknown"),
            }
            for u in users
        ]

        print(f"  Users without role: {len(result_users)}")
        return {
            "users": result_users,
            "count": len(result_users),
        }

    # ------------------------------------------------------------------
    # 8. Org Limits
    # ------------------------------------------------------------------

    def get_org_limits(self) -> dict:
        """
        Retrieve current org limit usage from the Salesforce Limits API.

        Calls /services/data/v59.0/limits/ which returns entries of the form:
            {"DailyApiRequests": {"Max": 15000, "Remaining": 14000}, ...}

        Returns:
            The raw limits dict keyed by limit name, or {} on failure.
        """
        print("Collecting org limits data ...")
        url = self._rest_url("limits/")
        try:
            response = self._session.get(url, headers=self._headers, timeout=30)
            response.raise_for_status()
            limits = response.json()
            print(f"  Org limits retrieved ({len(limits)} limit types)")
            return limits
        except requests.exceptions.RequestException as e:
            print(f"  [WARNING] Could not retrieve org limits: {e}")
            return {}

    # ------------------------------------------------------------------
    # 9. Org-Wide Apex Test Coverage
    # ------------------------------------------------------------------

    def get_org_wide_coverage(self) -> dict:
        """
        Query the org-wide Apex test coverage percentage via the Tooling API.

        Uses ApexOrgWideCoverage which returns a single row with the
        aggregate coverage across all Apex classes.

        Returns:
            - percent_covered: int (0–100), or None if unavailable
        """
        print("Collecting org-wide Apex test coverage ...")
        result = self._soql_query(
            "SELECT PercentCovered FROM ApexOrgWideCoverage",
            tooling=True,
        )
        records = result.get("records", [])
        if records:
            pct = records[0].get("PercentCovered")
            print(f"  Org-wide Apex coverage: {pct}%")
            return {"percent_covered": pct}
        print("  [WARNING] ApexOrgWideCoverage returned no records")
        return {"percent_covered": None}

    # ------------------------------------------------------------------
    # 10. Guest Users
    # ------------------------------------------------------------------

    def get_guest_users(self) -> dict:
        """
        Query active Guest User accounts (Experience Cloud site users).

        Guest users are a security concern if they hold broad permissions,
        as they represent unauthenticated public access to the org.

        Returns:
            - users: list of {id, name, profile}
            - count: int
        """
        print("Collecting guest user data ...")
        users = self._get_all_records(
            "SELECT Id, Name, Profile.Name "
            "FROM User "
            "WHERE UserType = 'Guest' "
            "AND IsActive = true "
            "ORDER BY Name"
        )
        result_users = [
            {
                "id":      u["Id"],
                "name":    u["Name"],
                "profile": u.get("Profile", {}).get("Name", "Unknown"),
            }
            for u in users
        ]
        print(f"  Active guest users: {len(result_users)}")
        return {
            "users": result_users,
            "count": len(result_users),
        }

    # ------------------------------------------------------------------
    # 11. Duplicate Rules
    # ------------------------------------------------------------------

    def get_duplicate_rules(self) -> dict:
        """
        Query active Duplicate Rules and group them by object type.

        Returns:
            - rules_by_object: dict of {SobjectType: count}
            - total_active:    int
        """
        print("Collecting duplicate rules data ...")
        rules = self._get_all_records(
            "SELECT Id, SobjectType, IsActive "
            "FROM DuplicateRule "
            "WHERE IsActive = true "
            "ORDER BY SobjectType"
        )
        rules_by_object: dict[str, int] = {}
        for r in rules:
            obj = r.get("SobjectType", "Unknown")
            rules_by_object[obj] = rules_by_object.get(obj, 0) + 1

        print(f"  Active duplicate rules: {len(rules)} across {len(rules_by_object)} object(s)")
        return {
            "rules_by_object": rules_by_object,
            "total_active":    len(rules),
        }

    # ------------------------------------------------------------------
    # 12. Record-Triggered Flows
    # ------------------------------------------------------------------

    def get_record_triggered_flows(self) -> dict:
        """
        Query active record-triggered flows via the Tooling API.

        Used for AUTO-004 (Flows Without Error Handling). Since fault
        connector presence cannot be detected via SOQL, the count of
        active record-triggered flows is returned as a proxy.

        Returns:
            - flows: list of {id, api_name, label, process_type}
            - count: int
        """
        print("Collecting active record-triggered flows ...")
        flows = self._get_all_records(
            "SELECT Id, ApiName, Label, ProcessType FROM Flow "
            "WHERE Status = 'Active' "
            "AND ProcessType IN ('AutoLaunchedFlow', 'RecordAfterSave', 'RecordBeforeSave')",
            tooling=True,
        )
        result_flows = [
            {
                "id":           f["Id"],
                "api_name":     f.get("ApiName", ""),
                "label":        f.get("Label", ""),
                "process_type": f.get("ProcessType", ""),
            }
            for f in flows
        ]
        print(f"  Active record-triggered flows: {len(result_flows)}")
        return {"flows": result_flows, "count": len(result_flows)}

    # ------------------------------------------------------------------
    # 13. Scheduled Jobs
    # ------------------------------------------------------------------

    def get_scheduled_jobs(self) -> dict:
        """
        Query Apex Scheduled Jobs currently in WAITING state.

        Used for AUTO-010 (High-Volume Scheduled Jobs). Returns all jobs
        waiting to fire so the analyser can report on scheduling load.

        Returns:
            - jobs: list of {id, name, next_fire_time, cron_expression}
            - count: int
        """
        print("Collecting scheduled Apex jobs ...")
        jobs = self._get_all_records(
            "SELECT Id, CronJobDetail.Name, State, NextFireTime, CronExpression "
            "FROM CronTrigger "
            "WHERE State = 'WAITING'"
        )
        result_jobs = [
            {
                "id":              j["Id"],
                "name":            j.get("CronJobDetail", {}).get("Name", "Unknown"),
                "state":           j.get("State", ""),
                "next_fire_time":  j.get("NextFireTime"),
                "cron_expression": j.get("CronExpression", ""),
            }
            for j in jobs
        ]
        print(f"  Scheduled jobs waiting: {len(result_jobs)}")
        return {"jobs": result_jobs, "count": len(result_jobs)}

    # ------------------------------------------------------------------
    # 14. Multi-Select Picklist Fields
    # ------------------------------------------------------------------

    def get_multiselect_picklist_fields(self) -> dict:
        """
        Query all Multi-Select Picklist fields across the org via Tooling API.

        Used for DATA-004. Multi-select picklists are hard to report on
        and filter; a high count signals data model quality issues.

        Returns:
            - fields: list of {object, field}
            - count:  int
        """
        print("Collecting Multi-Select Picklist fields ...")
        fields = self._get_all_records(
            "SELECT EntityDefinition.QualifiedApiName, QualifiedApiName, DataType "
            "FROM FieldDefinition "
            "WHERE DataType = 'MultiselectPicklist'",
            tooling=True,
        )
        result_fields = [
            {
                "object": f.get("EntityDefinition", {}).get("QualifiedApiName", ""),
                "field":  f.get("QualifiedApiName", ""),
            }
            for f in fields
        ]
        print(f"  Multi-select picklist fields: {len(result_fields)}")
        return {"fields": result_fields, "count": len(result_fields)}

    # ------------------------------------------------------------------
    # 15. Connected Apps
    # ------------------------------------------------------------------

    def get_connected_apps(self) -> dict:
        """
        Query Connected Apps defined in the org via the Tooling API.

        Used for INT-001 (Connected Apps With Excessive OAuth Scopes).
        Full scope details are not queryable via SOQL, so the check
        surfaces all apps for manual review.

        Returns:
            - apps:  list of {id, name, admin_approved_only}
            - count: int
        """
        print("Collecting Connected Apps ...")
        apps = self._get_all_records(
            "SELECT Id, Name, OptionsAllowAdminApprovedUsersOnly "
            "FROM ConnectedApplication",
            tooling=True,
        )
        result_apps = [
            {
                "id":                 a["Id"],
                "name":               a.get("Name", ""),
                "admin_approved_only": a.get("OptionsAllowAdminApprovedUsersOnly", False),
            }
            for a in apps
        ]
        print(f"  Connected apps: {len(result_apps)}")
        return {"apps": result_apps, "count": len(result_apps)}

    # ------------------------------------------------------------------
    # 16. Remote Site Settings
    # ------------------------------------------------------------------

    def get_remote_site_settings(self) -> dict:
        """
        Query active Remote Site Settings (RemoteProxy).

        Used for INT-008. Flags any endpoint that is overly broad
        (top-level domain only, no path) or contains a wildcard.

        Returns:
            - sites: list of {id, name, endpoint_url}
            - count: int
        """
        print("Collecting Remote Site Settings ...")
        sites = self._get_all_records(
            "SELECT Id, SiteName, EndpointUrl, IsActive "
            "FROM RemoteProxy "
            "WHERE IsActive = true"
        )
        result_sites = [
            {
                "id":           s["Id"],
                "name":         s.get("SiteName", ""),
                "endpoint_url": s.get("EndpointUrl", ""),
            }
            for s in sites
        ]
        print(f"  Active remote site settings: {len(result_sites)}")
        return {"sites": result_sites, "count": len(result_sites)}

    # ------------------------------------------------------------------
    # 17. Apex Code Data
    # ------------------------------------------------------------------

    def get_apex_code_data(self) -> dict:
        """
        Analyse Apex classes and triggers for quality and security issues.

        Checks performed:
            - API version currency
            - Presence of hardcoded Salesforce IDs (18-char alphanumeric)
            - Body length as a rough complexity indicator

        Returns:
            - classes:             list of {name, api_version, body_length, has_hardcoded_ids}
            - triggers:            list of {name, object, api_version, has_hardcoded_ids}
            - classes_with_hardcoded_ids: list of class names
            - triggers_with_hardcoded_ids: list of trigger names
            - classes_below_min_api:  list of {name, api_version}
            - total_classes:       int
            - total_triggers:      int
        """
        print("Collecting Apex code data ...")

        MIN_API_VERSION = 50.0  # matches checks_config.yaml GOV-007 threshold

        # Apex Classes (body retrieved via Tooling API for pattern matching)
        classes_raw = self._get_all_records(
            "SELECT Id, Name, ApiVersion, LengthWithoutComments, Body "
            "FROM ApexClass "
            "ORDER BY Name",
            tooling=True,
        )

        classes = []
        classes_with_hardcoded_ids = []

        for cls in classes_raw:
            body = cls.get("Body") or ""
            # Find candidate 15/18-char SF IDs in source code
            # Filter out common false positives (all-lowercase, all-uppercase constants)
            id_matches = [
                m for m in SF_ID_PATTERN.findall(body)
                if not m.islower() and not m.isupper() and m[0].isalpha()
            ]
            has_ids = len(id_matches) > 0

            if has_ids:
                classes_with_hardcoded_ids.append(cls["Name"])

            classes.append({
                "name":              cls["Name"],
                "api_version":       float(cls.get("ApiVersion", 0)),
                "body_length":       cls.get("LengthWithoutComments", 0),
                "has_hardcoded_ids": has_ids,
                "hardcoded_id_count": len(id_matches),
            })

        # Apex Triggers (also via Tooling API for body access)
        triggers_raw = self._get_all_records(
            "SELECT Id, Name, TableEnumOrId, ApiVersion, Body "
            "FROM ApexTrigger "
            "ORDER BY Name",
            tooling=True,
        )

        triggers = []
        triggers_with_hardcoded_ids = []

        for trig in triggers_raw:
            body = trig.get("Body") or ""
            id_matches = [
                m for m in SF_ID_PATTERN.findall(body)
                if not m.islower() and not m.isupper() and m[0].isalpha()
            ]
            has_ids = len(id_matches) > 0

            if has_ids:
                triggers_with_hardcoded_ids.append(trig["Name"])

            triggers.append({
                "name":              trig["Name"],
                "object":            trig.get("TableEnumOrId"),
                "api_version":       float(trig.get("ApiVersion", 0)),
                "has_hardcoded_ids": has_ids,
                "hardcoded_id_count": len(id_matches),
            })

        # Classes and triggers using deprecated API versions
        classes_below_min = [
            {"name": c["name"], "api_version": c["api_version"]}
            for c in classes
            if c["api_version"] < MIN_API_VERSION
        ]

        print(f"  Apex classes: {len(classes)} | "
              f"Triggers: {len(triggers)} | "
              f"Classes with hardcoded IDs: {len(classes_with_hardcoded_ids)} | "
              f"Below min API version: {len(classes_below_min)}")

        return {
            "classes":                      classes,
            "triggers":                     triggers,
            "total_classes":                len(classes),
            "total_triggers":               len(triggers),
            "classes_with_hardcoded_ids":   classes_with_hardcoded_ids,
            "triggers_with_hardcoded_ids":  triggers_with_hardcoded_ids,
            "classes_below_min_api":        classes_below_min,
        }
