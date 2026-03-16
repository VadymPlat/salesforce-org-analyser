"""
test_connection.py
------------------
Quick sanity check — verifies that credentials in .env are correct
and that the Salesforce REST API is reachable.

Usage:
    python3 test_connection.py
"""

import os
import sys
from dotenv import load_dotenv
from src.salesforce_client import SalesforceClient

load_dotenv()

print("=" * 50)
print("  Salesforce Connection Test")
print("=" * 50)

client = SalesforceClient()
connected = client.connect()

if not connected:
    print("\nStatus : FAILED")
    print("Check your credentials in .env and try again.")
    sys.exit(1)

org_info = client.test_connection()

if "error" in org_info:
    print(f"\nStatus : FAILED — {org_info['error']}")
    sys.exit(1)

print("\n" + "-" * 50)
print(f"  Status      : SUCCESS")
print(f"  Org Name    : {org_info.get('org_name')}")
print(f"  Org ID      : {org_info.get('org_id')}")
print(f"  Org Type    : {org_info.get('org_type')}")
print(f"  Instance    : {org_info.get('instance')}")
print(f"  Sandbox     : {org_info.get('is_sandbox')}")
print(f"  API Version : {org_info.get('api_version')}")
print(f"  Username    : {os.getenv('SALESFORCE_USERNAME')}")
print("-" * 50)
