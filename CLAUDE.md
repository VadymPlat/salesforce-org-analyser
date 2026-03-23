# Salesforce Org Health Analyser — Claude Code Instructions

## Project Overview
An AI-powered Salesforce org health assessment tool that:
- Connects to any Salesforce org via REST/Metadata/Tooling APIs
- Runs 50+ automated health checks across 5 categories
- Uses Claude AI to generate intelligent findings and recommendations
- Produces an interactive HTML report with severity scoring

## Architecture

### Entry Points (two interfaces, one shared engine)
app.py                    — PRIMARY: Streamlit web UI (deployed to Streamlit Cloud)
                            Uses OAuth 2.0 Authorization Code + PKCE
                            Imports OrgHealthAgent from src/agent.py
src/agent.py              — SECONDARY: CLI entry point + orchestrator
                            Uses SOAP login (username/password/token)

### Core Modules (shared by both entry points)
src/salesforce_client.py  — Salesforce API connection and data collection
src/analyser.py           — Claude AI analysis engine, loads checks_config.yaml
src/report_generator.py   — Jinja2 HTML report generation

### Configuration and Templates
config/checks_config.yaml — All check definitions, severity, thresholds
templates/report_template.html — HTML report template
reports/                  — Generated reports saved here (gitignored)

### How It Fits Together
app.py (OAuth 2.0 + PKCE) ──┐
                             ├──→ OrgHealthAgent.run_with_token()
src/agent.py (SOAP login) ──┘         │
                                      ├── salesforce_client.py (REST + Tooling APIs)
                                      ├── analyser.py (50+ checks + Claude AI)
                                      └── report_generator.py (Jinja2 HTML)

## Authentication — Two Flows

### Web UI (app.py) — OAuth 2.0 with PKCE
- Production-grade: Connected App + Authorization Code + PKCE
- PKCE code_verifier packed inside OAuth `state` param as base64 JSON
- Survives browser redirect on Streamlit Cloud iframe
- Credentials: SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET in .env

### CLI (src/agent.py) — SOAP Login
- Dev/testing only: username + password + security token
- Faster setup, no Connected App required
- Credentials: SALESFORCE_USERNAME, SALESFORCE_PASSWORD, SALESFORCE_SECURITY_TOKEN in .env

## Development Workflow
ALWAYS follow this branching strategy:
- main = production, never commit directly
- dev = integration branch, never commit directly
- feature/xxx = where all development happens

For every new feature:
1. git checkout dev && git pull origin dev
2. git checkout -b feature/your-feature-name
3. Build and test
4. git push origin feature/your-feature-name
5. Open PR to dev on GitHub
6. Never merge to main directly — only from dev via PR

## Security Rules — NON NEGOTIABLE
- NEVER commit .env file — it is gitignored, keep it that way
- NEVER hardcode API keys, passwords, or tokens in code
- NEVER log or print credentials anywhere
- NEVER commit Salesforce credentials or OAuth client secrets
- All secrets go in .env file only
- If you accidentally expose a credential — stop and rotate it immediately

## Coding Standards
- Python: follow PEP 8 conventions
- All methods must have docstrings
- Error handling: never let one failed API call crash the entire agent
- Always use environment variables for configuration
- Print progress messages so user knows what's happening
- Use API version v59.0 for all Salesforce calls

## Running the Project

### Web UI (primary — this is what's deployed)
source venv/bin/activate
streamlit run app.py
# Opens at http://localhost:8501
# Live deployment: https://salesforce-org-analyser.streamlit.app/

### CLI (secondary — for local dev/testing)
source venv/bin/activate
PYTHONPATH=. python3 src/agent.py

### Setup from scratch
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in real credentials

### Test Salesforce connection
PYTHONPATH=. python3 test_connection.py

## Workflow Principles

### 1. Verify Before Done
- Never mark a task complete without proving it works
- Run the actual code, check actual output
- Ask yourself: "Would a senior engineer approve this?"
- Show evidence of working: output, logs, test results

### 2. Autonomous Bug Fixing
- When encountering a bug: investigate and fix it directly
- Point at logs, errors, tracebacks — then resolve them
- Don't ask for hand-holding on standard errors
- Fix the root cause, not just the symptom

### 3. Plan Before Building
- For any task with 3+ steps: write the plan first
- State what files will be changed and why
- Identify risks before touching code
- If something goes wrong mid-task: stop and re-plan

### 4. Minimal Impact
- Make changes as small as possible
- Only touch files directly relevant to the task
- Never refactor code that isn't broken
- Avoid introducing new dependencies unless necessary

### 5. Self-Correction
- After any correction from the user: understand why
- Apply the same standard going forward
- Don't repeat the same mistake twice

## Environment
Project root: /Users/vadymaus/dev/salesforce-org-analyser
Always use this absolute path when running commands.
Personal AI projects live in ~/dev/ — separate from client work in ~/projects/

## Key Design Decisions (don't change without discussion)
- Primary interface: Streamlit web UI (app.py) with OAuth 2.0 + PKCE
- Secondary interface: CLI (src/agent.py) with SOAP login
- Both interfaces share OrgHealthAgent — no code duplication
- Vector store: numpy-based (no ChromaDB — Python 3.14 incompatible)
- Config-driven: all check severity and thresholds in checks_config.yaml
- Report format: single self-contained HTML file, no external dependencies
- API version: v59.0 for all Salesforce API calls
