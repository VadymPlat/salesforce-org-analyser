"""
app.py
------
SF Org Analyser — Streamlit web application.

Replaces the CLI interface with a professional web UI that uses
Salesforce OAuth 2.0 for authentication.

Run:
    streamlit run app.py

Required environment variables (in .env):
    SALESFORCE_CLIENT_ID      Connected App consumer key
    SALESFORCE_CLIENT_SECRET  Connected App consumer secret
    SALESFORCE_REDIRECT_URI   e.g. http://localhost:8501
    ANTHROPIC_API_KEY         Anthropic API key
"""

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests
import streamlit as st
from dotenv import load_dotenv

# Ensure project root is on sys.path so `src.*` imports resolve
sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

# ---------------------------------------------------------------------------
# Page config — must be the very first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SF Org Analyser",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

_PROD_BASE   = "https://login.salesforce.com"
_SANDBOX_BASE = "https://test.salesforce.com"

CLIENT_ID     = os.getenv("SALESFORCE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("SALESFORCE_REDIRECT_URI", "http://localhost:8501")


def _base_url(org_type: str, custom_domain: str = "") -> str:
    """Return the correct Salesforce base URL for the given org type."""
    if org_type == "Sandbox":
        return _SANDBOX_BASE
    if org_type == "Custom Domain":
        domain = custom_domain.strip().rstrip("/")
        if not domain.startswith("https://"):
            domain = "https://" + domain.lstrip("http://")
        return domain
    return _PROD_BASE


def _generate_pkce() -> tuple[str, str]:
    """
    Generate a PKCE code_verifier and its SHA-256 code_challenge.

    Returns:
        (code_verifier, code_challenge) — both URL-safe base64, no padding.
    """
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32))
        .rstrip(b"=")
        .decode("utf-8")
    )
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .rstrip(b"=")
        .decode("utf-8")
    )
    return code_verifier, code_challenge


def get_auth_url(org_type: str, custom_domain: str = "") -> str:
    """
    Build the Salesforce OAuth 2.0 authorization URL with PKCE.

    Generates a fresh PKCE pair and encodes the code_verifier (along with
    org_type and custom_domain) inside the OAuth `state` parameter as
    base64-encoded JSON. This survives the redirect regardless of whether
    Streamlit session state persists across the browser redirect.
    """
    base = _base_url(org_type, custom_domain)

    code_verifier, code_challenge = _generate_pkce()

    state_data = {
        "code_verifier": code_verifier,
        "org_type":      org_type,
        "custom_domain": custom_domain or "",
    }
    state = base64.urlsafe_b64encode(
        json.dumps(state_data).encode()
    ).decode()

    params = {
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 "full refresh_token",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{base}/services/oauth2/authorize?{urlencode(params)}"


def exchange_code_for_token(
    code: str,
    org_type: str,
    custom_domain: str = "",
    code_verifier: str = "",
) -> dict:
    """
    Exchange an authorization code for an access token.

    Includes the PKCE code_verifier (decoded from the OAuth state parameter
    by the callback handler) in the POST body so Salesforce can verify the
    challenge.

    Returns a dict with keys: access_token, instance_url, token_type.
    Raises RuntimeError on failure.
    """
    base = _base_url(org_type, custom_domain)
    token_url = f"{base}/services/oauth2/token"

    payload = {
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": code_verifier,
    }

    resp = requests.post(token_url, data=payload, timeout=30)

    if not resp.ok:
        try:
            detail = resp.json().get("error_description", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Token exchange failed: {detail}")

    data = resp.json()
    return {
        "access_token": data["access_token"],
        "instance_url": data["instance_url"],
        "token_type":   data.get("token_type", "Bearer"),
    }


def get_org_info(access_token: str, instance_url: str) -> dict:
    """
    Fetch basic org name and edition via the REST API.
    Returns dict with org_name and org_edition.
    """
    url = f"{instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "q": (
            "SELECT Id, Name, OrganizationType, IsSandbox "
            "FROM Organization LIMIT 1"
        )
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if records:
            return {
                "org_name":    records[0].get("Name", "Unknown Org"),
                "org_edition": records[0].get("OrganizationType", ""),
                "is_sandbox":  records[0].get("IsSandbox", False),
            }
    except Exception:
        pass
    return {"org_name": "Your Org", "org_edition": "", "is_sandbox": False}


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "authenticated":      False,
    "access_token":       "",
    "instance_url":       "",
    "org_name":           "",
    "org_type":           "Production",
    "analysis_complete":  False,
    "analysis_running":   False,
    "report_path":        "",
    "report_data":        None,
    "selected_categories": [
        "Security", "Automation", "Data Model", "Integrations", "Governance"
    ],
    "oauth_error":        "",
}

for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------------------------------------------------------------------
# OAuth callback handler — runs on every page load
# ---------------------------------------------------------------------------

def _handle_oauth_callback() -> None:
    """
    Detect an OAuth redirect (query params contain `code` or `error`)
    and process it before rendering any UI.
    """
    params = st.query_params

    error = params.get("error")
    if error:
        desc = params.get("error_description", error)
        st.session_state.oauth_error = f"Salesforce denied access: {desc}"
        st.query_params.clear()
        return

    code = params.get("code")
    if not code:
        return

    # Decode state: base64-encoded JSON carrying code_verifier, org_type,
    # and custom_domain — all survive the browser redirect inside the URL.
    raw_state = params.get("state", "")
    try:
        # Pad to a multiple of 4 before decoding (base64 requires it)
        state_data    = json.loads(base64.urlsafe_b64decode(raw_state + "==").decode())
        org_type      = state_data.get("org_type", "Production")
        custom_domain = state_data.get("custom_domain", "")
        code_verifier = state_data.get("code_verifier", "")
    except Exception:
        st.session_state.oauth_error = "Invalid OAuth state parameter. Please try connecting again."
        st.query_params.clear()
        return

    # Clear query params immediately — prevents re-processing on re-render
    st.query_params.clear()

    try:
        token_data = exchange_code_for_token(code, org_type, custom_domain, code_verifier)
    except RuntimeError as exc:
        st.session_state.oauth_error = str(exc)
        return

    access_token = token_data["access_token"]
    instance_url = token_data["instance_url"]

    org_info = get_org_info(access_token, instance_url)

    st.session_state.authenticated  = True
    st.session_state.access_token   = access_token
    st.session_state.instance_url   = instance_url
    st.session_state.org_name       = org_info["org_name"]
    st.session_state.org_type       = org_type
    st.session_state.oauth_error    = ""


_handle_oauth_callback()


# ---------------------------------------------------------------------------
# Custom CSS
# ---------------------------------------------------------------------------

st.markdown("""
<style>
/* ── Feature cards ─────────────────────────────────────── */
.feature-card {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 24px 20px;
    margin-bottom: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
    min-height: 120px;
}
.feature-card h4 {
    margin: 0 0 8px 0;
    font-size: 1rem;
    color: #1a202c;
}
.feature-card p {
    margin: 0;
    font-size: 0.85rem;
    color: #4a5568;
    line-height: 1.5;
}

/* ── Metric cards ──────────────────────────────────────── */
.metric-card {
    background: #fff;
    border-radius: 10px;
    padding: 20px;
    text-align: center;
    border-top: 4px solid;
    box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.metric-card .count  { font-size: 2.4rem; font-weight: 700; line-height: 1; }
.metric-card .label  { font-size: 0.8rem; color: #4a5568; margin-top: 4px; }
.card-critical { border-color: #e53e3e; }
.card-critical .count { color: #e53e3e; }
.card-high     { border-color: #dd6b20; }
.card-high     .count { color: #dd6b20; }
.card-medium   { border-color: #d69e2e; }
.card-medium   .count { color: #d69e2e; }
.card-low      { border-color: #38a169; }
.card-low      .count { color: #38a169; }

/* ── Health score ──────────────────────────────────────── */
.score-display {
    text-align: center;
    padding: 32px;
    background: #f8fafc;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
}
.score-number {
    font-size: 4rem;
    font-weight: 800;
    line-height: 1;
}
.score-label  { font-size: 1.1rem; margin-top: 8px; }

/* ── Connected badge ───────────────────────────────────── */
.connected-badge {
    background: #f0fff4;
    border: 1px solid #9ae6b4;
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 0.95rem;
    color: #276749;
    display: inline-block;
}

/* ── Hero ──────────────────────────────────────────────── */
.hero-title  { font-size: 2.2rem; font-weight: 700; color: #1a202c; }
.hero-sub    { font-size: 1.1rem; color: #4a5568; margin-top: 8px; }

/* ── Security note ─────────────────────────────────────── */
.security-note {
    font-size: 0.8rem;
    color: #718096;
    margin-top: 12px;
}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("## 🔍 SF Org Analyser")
    st.caption("v2.0")
    st.divider()

    st.markdown("**Built with:**")
    st.markdown("🤖 Claude AI (Anthropic)")
    st.markdown("☁️ Salesforce APIs")
    st.markdown("🐍 Python + Streamlit")
    st.divider()

    st.markdown(
        "[![GitHub](https://img.shields.io/badge/GitHub-VadymPlat-181717?logo=github)]"
        "(https://github.com/VadymPlat/salesforce-org-analyser)"
    )
    st.divider()

    st.caption(
        "⚠️ This tool requires read access to your Salesforce org. "
        "Always review OAuth permissions before connecting."
    )


# ---------------------------------------------------------------------------
# Header (always visible)
# ---------------------------------------------------------------------------

st.markdown("# 🔍 SF Org Analyser")
st.caption("AI-powered Salesforce org health assessment")
st.divider()


# ---------------------------------------------------------------------------
# OAuth error banner (shown if Salesforce returned an error)
# ---------------------------------------------------------------------------

if st.session_state.oauth_error:
    st.error(st.session_state.oauth_error)
    if st.button("🔄 Try again"):
        st.session_state.oauth_error = ""
        st.rerun()
    st.stop()


# ---------------------------------------------------------------------------
# Page: RESULTS
# ---------------------------------------------------------------------------

def show_results() -> None:
    """Render the post-analysis results page."""
    report_data  = st.session_state.report_data
    report_path  = st.session_state.report_path
    summary      = report_data.get("summary", {}) if report_data else {}

    critical = summary.get("critical_count", 0)
    high     = summary.get("high_count", 0)
    medium   = summary.get("medium_count", 0)
    low      = summary.get("low_count", 0)
    score    = summary.get("health_score", 0)

    st.markdown("## Analysis Complete! 🎉")

    # Severity metric cards
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            f'<div class="metric-card card-critical">'
            f'<div class="count">{critical}</div>'
            f'<div class="label">🔴 CRITICAL</div></div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f'<div class="metric-card card-high">'
            f'<div class="count">{high}</div>'
            f'<div class="label">🟠 HIGH</div></div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div class="metric-card card-medium">'
            f'<div class="count">{medium}</div>'
            f'<div class="label">🟡 MEDIUM</div></div>',
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f'<div class="metric-card card-low">'
            f'<div class="count">{low}</div>'
            f'<div class="label">🟢 LOW</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("")

    # Health score + action buttons
    score_col, actions_col = st.columns([1, 2])

    with score_col:
        if score >= 71:
            score_color = "#38a169"
            score_label = "🟢 Healthy"
        elif score >= 41:
            score_color = "#dd6b20"
            score_label = "🟠 Needs Attention"
        else:
            score_color = "#e53e3e"
            score_label = "🔴 Critical Risk"

        st.markdown(
            f'<div class="score-display">'
            f'<div class="score-number" style="color:{score_color}">{score}/100</div>'
            f'<div class="score-label" style="color:{score_color}">{score_label}</div>'
            f"</div>",
            unsafe_allow_html=True,
        )

    with actions_col:
        st.markdown("### Actions")

        btn1, btn2 = st.columns(2)

        with btn1:
            if report_path and Path(report_path).exists():
                with open(report_path, "rb") as fh:
                    report_bytes = fh.read()
                st.download_button(
                    label="⬇️ Download Report",
                    data=report_bytes,
                    file_name=Path(report_path).name,
                    mime="text/html",
                    type="primary",
                    use_container_width=True,
                )

        with btn2:
            if st.button("🔄 Analyse Another Org", use_container_width=True):
                for key, default in _DEFAULTS.items():
                    st.session_state[key] = default
                st.rerun()

        st.markdown("")
        st.caption(
            "Download the full interactive report with AI recommendations. "
            "The report is self-contained — open it in any browser, share via email or Slack."
        )


# ---------------------------------------------------------------------------
# Page: ANALYSIS RUNNING
# ---------------------------------------------------------------------------

def show_analysis_running() -> None:
    """Run the analysis pipeline with a live progress bar."""
    st.markdown("## 🔄 Analysing your org...")
    st.warning("Please do not close this tab while the analysis is running.")

    progress_bar  = st.progress(0, text="🔄 Starting analysis...")
    status_text   = st.empty()

    def update_progress(pct: int, message: str) -> None:
        progress_bar.progress(pct, text=message)
        status_text.markdown(f"**{message}**")

    try:
        from src.agent import OrgHealthAgent
        agent = OrgHealthAgent()

        report_data, report_path = agent.run_with_token(
            access_token=st.session_state.access_token,
            instance_url=st.session_state.instance_url,
            selected_categories=st.session_state.selected_categories,
            progress_callback=update_progress,
        )

        st.session_state.report_data       = report_data
        st.session_state.report_path       = report_path
        st.session_state.analysis_complete = True
        st.session_state.analysis_running  = False

        st.rerun()

    except Exception as exc:
        st.session_state.analysis_running = False
        st.error(f"Analysis failed: {exc}")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 Retry analysis"):
                st.session_state.analysis_running = True
                st.rerun()
        with col2:
            if st.button("↩ Connect a different org"):
                for key, default in _DEFAULTS.items():
                    st.session_state[key] = default
                st.rerun()


# ---------------------------------------------------------------------------
# Page: CONNECTED (pre-analysis)
# ---------------------------------------------------------------------------

def show_connected_page() -> None:
    """Render the authenticated state — org summary + run button."""
    org_name = st.session_state.org_name
    org_type = st.session_state.org_type

    st.success(f"✅ Successfully connected to **{org_name}**")
    st.markdown(f"**Org type:** {org_type}")
    st.markdown("")

    # What will be analysed
    st.markdown("### This analysis will check:")
    check_cols = st.columns(5)
    checks = [
        ("🔒", "Security", "Profiles, OWD, Permission Sets"),
        ("⚡", "Automation", "Flows, Triggers, Apex"),
        ("📦", "Data Model", "Objects, Fields, Naming"),
        ("🔌", "Integrations", "APIs, Endpoints, Users"),
        ("📋", "Governance", "Deployment, Coverage, Docs"),
    ]
    for col, (icon, name, detail) in zip(check_cols, checks):
        with col:
            st.markdown(
                f'<div class="feature-card">'
                f"<h4>{icon} {name}</h4>"
                f"<p>{detail}</p>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown("")

    # Advanced config
    with st.expander("⚙️ Customise checks"):
        st.markdown("Select which categories to include:")
        selected = []
        cat_cols = st.columns(5)
        for col, (_, name, _) in zip(cat_cols, checks):
            with col:
                if st.checkbox(name, value=True, key=f"cat_{name}"):
                    selected.append(name)
        if selected:
            st.session_state.selected_categories = selected
        else:
            st.warning("Select at least one category.")

    st.caption("⏱ Analysis takes approximately 2–3 minutes depending on org size.")
    st.markdown("")

    run_col, reset_col = st.columns([2, 1])

    with run_col:
        cats = st.session_state.selected_categories
        if st.button(
            "🚀 Run Analysis",
            type="primary",
            use_container_width=True,
            disabled=not cats,
        ):
            st.session_state.analysis_running = True
            st.rerun()

    with reset_col:
        if st.button("🔄 Connect a different org", use_container_width=True):
            for key, default in _DEFAULTS.items():
                st.session_state[key] = default
            st.rerun()


# ---------------------------------------------------------------------------
# Page: LANDING (unauthenticated)
# ---------------------------------------------------------------------------

def show_landing_page() -> None:
    """Render the landing page with hero, feature cards, and OAuth connect."""
    # Validate that OAuth is configured
    if not CLIENT_ID or not CLIENT_SECRET:
        st.error(
            "**OAuth not configured.** "
            "Set `SALESFORCE_CLIENT_ID` and `SALESFORCE_CLIENT_SECRET` in your `.env` file, "
            "then restart the app."
        )
        with st.expander("ℹ️ How to create a Salesforce Connected App"):
            st.markdown(
                """
1. In Salesforce Setup, go to **App Manager → New Connected App**
2. Enable **OAuth Settings**
3. Set **Callback URL** to `http://localhost:8501`
4. Add scopes: **Full access (full)** and **Perform requests at any time (refresh_token)**
5. Save — copy the **Consumer Key** and **Consumer Secret** to your `.env`
                """
            )
        return

    # Hero
    st.markdown(
        '<div class="hero-title">Salesforce Org Health Analyser</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="hero-sub">50+ automated checks powered by Claude AI.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("")

    # Connect section — primary action first
    st.markdown("### Connect to Salesforce")

    # Org type selector
    org_type = st.radio(
        "Connect to:",
        options=["🏢 Production / Developer Edition", "🧪 Sandbox", "🔗 Custom Domain"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # Map display label → internal name
    _ORG_MAP = {
        "🏢 Production / Developer Edition": "Production",
        "🧪 Sandbox":                        "Sandbox",
        "🔗 Custom Domain":                  "Custom Domain",
    }
    org_type_key = _ORG_MAP[org_type]

    custom_domain = ""
    custom_domain_valid = True

    if org_type_key == "Custom Domain":
        custom_domain = st.text_input(
            "Enter your org URL",
            placeholder="https://mycompany.my.salesforce.com",
        )
        if custom_domain:
            if not custom_domain.startswith(("https://", "http://")):
                st.error("URL must start with `https://`")
                custom_domain_valid = False
        else:
            custom_domain_valid = False

    st.markdown("")

    connect_disabled = (org_type_key == "Custom Domain" and not custom_domain_valid)

    if st.button(
        "Connect to Salesforce →",
        type="primary",
        disabled=connect_disabled,
        use_container_width=False,
    ):
        auth_url = get_auth_url(org_type_key, custom_domain)
        # JavaScript redirect — works in all browsers
        st.markdown(
            f'<meta http-equiv="refresh" content="0; url={auth_url}">',
            unsafe_allow_html=True,
        )
        st.markdown(
            f"↗ [Click here if not redirected automatically]({auth_url})"
        )

    st.markdown(
        '<div class="security-note">'
        "🔐 Read-only access via Salesforce OAuth. "
        "No passwords stored. No data leaves your browser session."
        "</div>",
        unsafe_allow_html=True,
    )

    # Feature cards — supporting info below the primary action
    st.markdown("")
    f1, f2, f3 = st.columns(3)
    with f1:
        st.markdown(
            '<div class="feature-card">'
            "<h4>🔒 Security Analysis</h4>"
            "<p>Profiles, Permission Sets, OWD, Sharing Rules, "
            "MFA enforcement, and admin count.</p>"
            "</div>",
            unsafe_allow_html=True,
        )
    with f2:
        st.markdown(
            '<div class="feature-card">'
            "<h4>⚡ Automation Health</h4>"
            "<p>Flows, Triggers, Apex class quality, "
            "Order of Execution conflicts, and bulk-safety patterns.</p>"
            "</div>",
            unsafe_allow_html=True,
        )
    with f3:
        st.markdown(
            '<div class="feature-card">'
            "<h4>📊 AI Recommendations</h4>"
            "<p>Claude AI explains every finding and prioritises fixes "
            "with actionable, org-specific recommendations.</p>"
            "</div>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

if st.session_state.analysis_complete:
    show_results()
elif st.session_state.analysis_running:
    show_analysis_running()
elif st.session_state.authenticated:
    show_connected_page()
else:
    show_landing_page()
