# Salesforce Org Health Analyser — Claude Code Instructions

## Project Overview
An AI-powered Salesforce org health assessment tool that:
- Connects to any Salesforce org via REST/Metadata/Tooling APIs
- Runs 50+ automated health checks across 5 categories
- Uses Claude AI to generate intelligent findings and recommendations
- Produces an interactive HTML report with severity scoring

## Architecture
src/salesforce_client.py  — Salesforce API connection and data collection
src/analyser.py           — Claude AI analysis engine, loads checks_config.yaml
src/report_generator.py   — Jinja2 HTML report generation
src/agent.py              — Main orchestrator (connects all components)
config/checks_config.yaml — All check definitions, severity, thresholds
templates/report_template.html — HTML report template
reports/                  — Generated reports saved here (gitignored)

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
- NEVER commit Salesforce credentials
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
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in real credentials

# Test connection
python3 test_connection.py

# Run full analysis
python3 agent.py --org myorg

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
- Authentication: username/password for dev, OAuth JWT for production
- Vector store: numpy-based (no ChromaDB — Python 3.14 incompatible)
- Config-driven: all check severity and thresholds in checks_config.yaml
- Report format: single self-contained HTML file, no external dependencies
- API version: v59.0 for all Salesforce API calls
