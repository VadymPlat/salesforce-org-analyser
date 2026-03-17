# Salesforce Org Health Analyser

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Claude AI](https://img.shields.io/badge/Claude-claude--sonnet--4--6-blueviolet?logo=anthropic)
![Salesforce](https://img.shields.io/badge/Salesforce-API%20v59.0-00A1E0?logo=salesforce)
![Checks](https://img.shields.io/badge/Health%20Checks-50%2B-success)
![License](https://img.shields.io/badge/License-MIT-green)

An AI-powered Salesforce org auditing tool that connects via REST/Metadata/Tooling APIs, runs 50+ automated health checks across 5 categories, enriches every finding with Claude AI analysis, and delivers a self-contained interactive HTML report with a 0–100 health score.

Built as a portfolio project demonstrating AI-augmented cloud architecture tooling.

---

## Demo

```
╔══════════════════════════════════════════════╗
║     SALESFORCE ORG HEALTH ANALYSER v1.0      ║
╚══════════════════════════════════════════════╝

[1/5] Connecting to Salesforce...
  ✓ Connected — CTAdmin@customertimes.com.ctdev98
  ✓ Org: Customertimes DEV 98 (Developer Edition)
  ✓ API version: 59.0

[2/5] Collecting org data...
  ✓ Users           4 active
  ✓ Profiles        6
  ✓ Permission Sets 12
  ✓ Apex Classes    47
  ✓ Triggers        3
  ✓ Objects         2 custom

[3/5] Running health checks...
  ✓ Security checks    (12 checks)
  ✓ Automation checks  (8 checks)
  ✓ Data model checks  (6 checks)

[4/5] AI enrichment via Claude...
  ✓ 4 findings enriched

[5/5] Generating HTML report...
  ✓ Report saved → reports/org_health_20260316_143022.html

══════════════════════════════════════════════
  HEALTH SCORE:  92 / 100  ● GOOD
  Critical: 0  High: 0  Medium: 1  Low: 3
  Elapsed:  21.4s
══════════════════════════════════════════════
```

---

## Architecture

```
agent.py  (orchestrator / CLI entry point)
    │
    ├── salesforce_client.py   ← Salesforce REST + Tooling + Metadata APIs
    │       SOAP login (username/password/token)
    │       Auto-detects sandbox vs production
    │       Collects: users, profiles, perm sets, apex, triggers, objects
    │
    ├── analyser.py            ← Health check engine + AI enrichment
    │       Loads checks_config.yaml (50 checks, thresholds, severity)
    │       Evaluates Security / Automation / Data Model categories
    │       Calls Claude claude-sonnet-4-6 per FAIL finding
    │       Returns structured findings + 0–100 health score
    │
    └── report_generator.py   ← Jinja2 HTML report
            Self-contained HTML (zero external dependencies)
            SVG circular gauge with animation
            Severity-grouped expandable finding cards
            Print-ready CSS
```

Config: `config/checks_config.yaml` — all check definitions, severity, and thresholds
Template: `templates/report_template.html` — single-file self-contained HTML

---

## Health Checks (50+)

### Security (12 checks)

| Check | What It Tests | Severity |
|-------|--------------|----------|
| MFA Enforcement | Is MFA required for all users? | Critical |
| Admin Count | Too many system administrators? | High |
| Profile Proliferation | Excessive custom profiles? | Medium |
| Guest User Access | Guest user enabled with broad access? | High |
| Password Policy | Minimum password age/complexity set? | High |
| Login IP Ranges | Profiles with unrestricted IP ranges? | Medium |
| Session Timeout | Idle session timeout configured? | Medium |
| Apex Without Tests | Apex classes lacking test coverage? | High |
| Connected App Permissions | Overly permissive connected apps? | High |
| Permission Set Overuse | Too many users on a single perm set? | Low |
| Inactive Users Licensed | Inactive users consuming licences? | Low |
| Public Groups | Open public groups without owners? | Info |

### Automation (8 checks)

| Check | What It Tests | Severity |
|-------|--------------|----------|
| Trigger per Object | More than 1 trigger per object? | High |
| Apex CPU Limits | Triggers likely to hit CPU limits? | High |
| Flow Proliferation | Too many active flows? | Medium |
| Workflow Rules | Deprecated workflow rules still active? | Medium |
| Process Builder | Process Builder usage (deprecated)? | Medium |
| Recursive Triggers | Trigger logic that may recurse? | High |
| Before/After Conflicts | Before and after triggers on same event? | Medium |
| Scheduled Jobs Failing | Any scheduled Apex in failed state? | Critical |

### Data Model (6 checks)

| Check | What It Tests | Severity |
|-------|--------------|----------|
| Object Field Count | Custom objects with excessive fields? | Medium |
| Required Field Abuse | Too many required fields on an object? | Low |
| Missing Descriptions | Custom objects/fields without descriptions? | Low |
| Lookup vs Master-Detail | Incorrect relationship types used? | Info |
| Deprecated Fields | Fields marked deprecated still in use? | Medium |
| Object Without Triggers | High-volume objects with no automation? | Info |

### Integrations (12 checks)

| Check | What It Tests | Severity |
|-------|--------------|----------|
| Named Credentials | Hardcoded endpoints instead of named creds? | High |
| REST API Usage | External REST APIs called from Apex? | Info |
| Callout Limits | Risk of hitting callout limits? | High |
| Integration Users | Dedicated integration user accounts? | Medium |
| OAuth Apps | Connected apps with broad scopes? | High |
| Platform Events | Platform event consumers healthy? | Medium |
| Change Data Capture | CDC configured for critical objects? | Info |
| Middleware Patterns | Anti-patterns in integration Apex? | Medium |
| API Version Usage | Outdated API versions in connections? | Low |
| Bulk API Usage | Large integrations using Bulk API? | Info |
| Error Handling | Integration Apex has try/catch? | High |
| Retry Logic | Integration Apex has retry patterns? | Medium |

### Governance (12 checks)

| Check | What It Tests | Severity |
|-------|--------------|----------|
| Sandbox Strategy | Sandbox types and refresh cadence? | Medium |
| Change Sets | Change sets as deployment mechanism? | Medium |
| Deployment Automation | CI/CD tooling in use? | Info |
| Apex Test Coverage | Org-wide test coverage ≥ 75%? | Critical |
| Code Review Process | Code review enforced? | High |
| Release Notes | Release documentation maintained? | Info |
| Monitoring Setup | Event monitoring/Shield configured? | High |
| Audit Trail Usage | Field history tracking overused? | Medium |
| Debug Log Retention | Debug logs leaving sensitive data? | Medium |
| GDPR Compliance | Data retention policies in place? | High |
| Backup Strategy | Regular org backup configured? | High |
| Documentation Coverage | Object/field documentation completeness? | Low |

---

## Health Score Algorithm

The health score starts at 100 and deducts points per failed check:

| Severity | Deduction per Failing Check |
|----------|-----------------------------|
| Critical | −15 points |
| High | −8 points |
| Medium | −3 points |
| Low | −1 point |
| Info | 0 points |

Minimum score is 0. Score bands:

| Score | Label | Colour |
|-------|-------|--------|
| 80–100 | GOOD | Green |
| 60–79 | FAIR | Yellow |
| 40–59 | NEEDS ATTENTION | Orange |
| 0–39 | CRITICAL | Red |

---

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.11+ | Broad Salesforce ecosystem, rapid development |
| AI Engine | Anthropic Claude (`claude-sonnet-4-6`) | Best-in-class analysis quality |
| Salesforce Auth | SOAP Login API | Works without a Connected App (zero org setup) |
| Salesforce Data | REST API + Tooling API v59.0 | Full access to metadata and configuration |
| Config | YAML (`checks_config.yaml`) | Non-developer-friendly check management |
| Templating | Jinja2 | Clean separation of logic and presentation |
| Report | Self-contained HTML | Zero dependencies, shareable via email/Slack |
| Vector Store | NumPy-based (no ChromaDB) | Python 3.14 compatibility |
| HTTP | requests + urllib3 | Simple, well-tested |

---

## Project Structure

```
salesforce-org-analyser/
├── src/
│   ├── agent.py              # CLI entry point + orchestrator
│   ├── salesforce_client.py  # Salesforce API client
│   ├── analyser.py           # Health check engine + AI
│   └── report_generator.py  # HTML report generator
├── config/
│   └── checks_config.yaml    # All check definitions
├── templates/
│   └── report_template.html  # Jinja2 HTML template
├── reports/                  # Generated reports (gitignored)
├── tests/                    # Unit and integration tests
├── .env.example              # Environment variable template
├── requirements.txt
├── CLAUDE.md                 # Claude Code project instructions
└── README.md
```

---

## Setup

### Prerequisites

- Python 3.11+
- Salesforce org credentials (username + password + security token)
- Anthropic API key

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/vadymplatoshyn/salesforce-org-analyser.git
cd salesforce-org-analyser

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
# venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
```

Edit `.env`:

```dotenv
# Salesforce credentials
SALESFORCE_USERNAME=your@email.com
SALESFORCE_PASSWORD=yourpassword
SALESFORCE_SECURITY_TOKEN=yourtoken
SALESFORCE_INSTANCE_URL=https://your-instance.salesforce.com

# For sandboxes — set instance URL containing "sandbox" OR:
# SALESFORCE_IS_SANDBOX=true

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
# 5. Test the Salesforce connection
PYTHONPATH=. python3 test_connection.py

# 6. Run the full analysis
PYTHONPATH=. python3 src/agent.py
```

The report opens automatically in your browser. Reports are saved to `reports/`.

---

## Customising Checks

All checks are defined in `config/checks_config.yaml`. No Python code changes needed to adjust thresholds or severity.

```yaml
security:
  admin_count:
    name: "Administrator Count"
    description: "Checks for excessive system administrator users"
    severity: high
    threshold: 5          # FAIL if more than 5 admins
    enabled: true

automation:
  trigger_per_object:
    name: "Multiple Triggers Per Object"
    description: "Multiple triggers on one object cause unpredictable execution order"
    severity: high
    enabled: true
```

To disable a check, set `enabled: false`.
To change severity, edit the `severity` field: `critical` / `high` / `medium` / `low` / `info`.

---

## Real-World Test Results

Tested against two live Salesforce orgs:

| Metric | Customertimes DEV 98 | COOPERATION PHARMA (Sandbox) |
|--------|---------------------|------------------------------|
| Edition | Developer Edition | Unlimited Edition |
| Active Users | 4 | 213 |
| Custom Objects | 2 | 159 |
| Apex Classes | 47 | 1,023 |
| Triggers | 3 | 86 |
| Health Score | **92 / 100** | **50 / 100** |
| Findings | 4 (0 critical) | 18 (2 critical) |
| Analysis Time | 21.4s | 89.8s |

The pharmaceutical sandbox correctly flagged: no MFA enforcement, 47 admin users, triggers without bulk-safe patterns, and Apex test coverage below threshold.

---

## Security Considerations

| Risk | Mitigation |
|------|-----------|
| Credentials in code | All credentials in `.env`, never committed (gitignored) |
| Salesforce password with special chars | XML `html.escape()` applied to SOAP body |
| Sandbox vs production login | Auto-detected from `SALESFORCE_INSTANCE_URL` |
| API key exposure | Loaded only via `os.getenv()`, never logged or printed |
| Read-only access | Tool only reads data — no writes to the org |
| Report contents | Reports saved locally, not transmitted anywhere |

---

## Known Limitations

| Limitation | Impact |
|-----------|--------|
| WorkflowRule not queryable on Developer Edition orgs | Workflow check returns 0 (no false positives) |
| `EntityDefinition.Description` unavailable in some editions | Field description coverage check may under-count |
| No OAuth JWT flow (dev only) | Production deployments should implement JWT |
| Single-org analysis | No cross-org comparison (planned for v2) |
| English only | Claude analysis output is in English |

---

## Roadmap (v2)

- [ ] OAuth JWT Bearer flow for production deployments
- [ ] Cross-org comparison mode
- [ ] Trend tracking (compare reports over time)
- [ ] Slack / Teams notification integration
- [ ] GitHub Actions workflow for scheduled org health checks
- [ ] Expanded check library (Integrations + Governance categories)
- [ ] Executive PDF export
- [ ] Multi-language report support

---

## What I Learned

1. **Salesforce SOAP login is the fastest path to zero-setup auth** — no Connected App, no OAuth flow, works against any org with username + password + token. The gotcha: passwords with special characters (`&`, `<`) break the XML body unless escaped with `html.escape()`.

2. **AI enrichment changes the value proposition** — without Claude, the tool outputs a list of config flags. With Claude, each finding gets a *why this matters* and a *how to fix it* tailored to the specific org's context. The difference in actionability is significant.

3. **Config-driven architecture pays off immediately** — by putting all check definitions in YAML, I could tune severity and thresholds without touching Python. A non-developer Salesforce Admin could own the check library.

4. **Self-contained HTML reports are underrated** — a single file with inline CSS and JS can be emailed, Slacked, committed to a repo, or opened offline. No server required, no broken CDN links.

5. **PYTHONPATH discipline matters early** — Python's module resolution is unforgiving when your entry point lives inside a `src/` directory. Setting `PYTHONPATH=.` at the run command (or in a Makefile) is a cleaner solution than relative imports or installing the package in editable mode.

---

## Author

**Vadym Platoshyn**
Salesforce Solutions Architect → AI/Cloud Architect
18-month transition project — building AI-augmented tools for the Salesforce ecosystem.

- GitHub: [@vadymplatoshyn](https://github.com/vadymplatoshyn)
- LinkedIn: [linkedin.com/in/vadymplatoshyn](https://linkedin.com/in/vadymplatoshyn)

---

## License

MIT — see [LICENSE](LICENSE) for details.
