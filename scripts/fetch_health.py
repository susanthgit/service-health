"""
Step 1: Fetch Microsoft 365 service health data from Microsoft Graph.

Uses the Service Communications API to retrieve:
- Current health status of all subscribed services
- All historical service health issues with update posts

Outputs raw/health.json for the generator.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import re


SCRIPT_DIR = Path(__file__).parent
SITE_DIR = SCRIPT_DIR / ".." / "site"
RAW_DIR = SITE_DIR / "raw"
OUTPUT_FILE = RAW_DIR / "health.json"
PREVIOUS_STATE_FILE = SITE_DIR / "previous_state.json"
SERVICES_FILE = SCRIPT_DIR / "services.json"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT = 30
PAGE_SIZE = 100


def get_graph_token():
    """Get a Graph API token. Supports multiple auth methods:
    1. GRAPH_TOKEN env var (pre-acquired token)
    2. Client credentials (AZURE_CLIENT_ID + AZURE_CLIENT_SECRET + AZURE_TENANT_ID)
    3. Azure CLI (az account get-access-token) — works in Actions with OIDC login
    """
    # Option 1: Token passed directly
    token = os.environ.get("GRAPH_TOKEN")
    if token:
        print("  🔑 Using GRAPH_TOKEN from environment")
        return token

    # Option 2: Client credentials flow (local dev or service principal)
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    if client_id and client_secret and tenant_id:
        print("  🔑 Using client credentials flow")
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    # Option 3: Azure CLI token (works in GitHub Actions after OIDC az login)
    try:
        import subprocess
        result = subprocess.run(
            ["az", "account", "get-access-token",
             "--resource", "https://graph.microsoft.com",
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("  🔑 Using token from Azure CLI")
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("❌ No Graph API token available.")
    print("   Set GRAPH_TOKEN, or AZURE_CLIENT_ID + AZURE_CLIENT_SECRET + AZURE_TENANT_ID")
    sys.exit(1)


def graph_get(endpoint, token, params=None):
    """Make a GET request to the Graph API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{GRAPH_BASE}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def graph_get_all(endpoint, token, params=None):
    """Fetch all pages from a paginated Graph API endpoint."""
    all_items = []
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{GRAPH_BASE}{endpoint}"

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        params = None  # nextLink already contains params

    return all_items


def parse_datetime(dt_str):
    """Parse Graph API datetime string."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, TypeError):
        return dt_str


# Region patterns to detect in incident text
REGION_PATTERNS = {
    "North America": r"\bNorth America\b",
    "South America": r"\bSouth America\b|Brazil",
    "Europe": r"\bEurope\b|EMEA\b|European\b",
    "Asia Pacific": r"\bAsia\b|APAC\b|Asia Pacific\b",
    "Middle East": r"\bMiddle East\b",
    "Africa": r"\bAfrica\b",
    "Australia": r"\bAustralia\b|ANZ\b|Oceania\b",
    "India": r"\bIndia\b",
    "Japan": r"\bJapan\b",
    "China": r"\bChina\b",
    "UK": r"\bUnited Kingdom\b|\bUK\b",
    "Canada": r"\bCanada\b",
    "Germany": r"\bGermany\b",
    "France": r"\bFrance\b",
    "Worldwide": r"\bworldwide\b|\bglobally\b|\ball regions\b|\ball users\b",
}


def extract_regions(impact_text, post_texts):
    """Extract mentioned regions from incident text using regex patterns."""
    all_text = (impact_text or "") + " " + " ".join(post_texts or [])
    if not all_text.strip():
        return []

    found = []
    for region_name, pattern in REGION_PATTERNS.items():
        if re.search(pattern, all_text, re.IGNORECASE):
            found.append(region_name)

    return found


def normalize_issue(issue):
    """Transform a Graph API issue into our unified format."""
    posts = []
    for post in issue.get("posts", []):
        content = ""
        desc = post.get("description", {})
        if isinstance(desc, dict):
            content = desc.get("content", "")
        elif isinstance(desc, str):
            content = desc

        posts.append({
            "timestamp": parse_datetime(post.get("createdDateTime")),
            "type": post.get("postType", "regular").lower(),
            "content": content,
        })

    # Sort posts chronologically
    posts.sort(key=lambda p: p["timestamp"] or "")

    # Extract regions from text (impactDescription + posts)
    regions = extract_regions(
        issue.get("impactDescription", ""),
        [p["content"] for p in posts],
    )

    return {
        "id": issue.get("id", ""),
        "source": "graph",
        "title": issue.get("title", ""),
        "classification": (issue.get("classification") or "").lower(),
        "status": issue.get("status", ""),
        "service": issue.get("service", ""),
        "feature": issue.get("feature", ""),
        "feature_group": issue.get("featureGroup", ""),
        "affected_services": [issue.get("service", "")] if issue.get("service") else [],
        "affected_regions": regions,
        "start_time": parse_datetime(issue.get("startDateTime")),
        "end_time": parse_datetime(issue.get("endDateTime")),
        "last_modified": parse_datetime(issue.get("lastModifiedDateTime")),
        "impact_description": issue.get("impactDescription", ""),
        "is_resolved": issue.get("isResolved", False),
        "has_updates": len(posts) > 0,
        "update_count": len(posts),
        "updates": posts,
    }


def content_hash(issue):
    """Hash issue content for change detection."""
    fields = (
        str(issue.get("status", ""))
        + str(issue.get("is_resolved", ""))
        + str(issue.get("update_count", ""))
        + str(issue.get("last_modified", ""))
    )
    return hashlib.md5(fields.encode()).hexdigest()


def load_previous_state():
    """Load previous state for change detection."""
    if not PREVIOUS_STATE_FILE.exists():
        return {}
    try:
        with open(PREVIOUS_STATE_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
        return {item["id"]: item for item in items}
    except (json.JSONDecodeError, KeyError):
        return {}


def detect_changes(issues, previous_state):
    """Compare current issues against previous state."""
    changes = {"new": 0, "updated": 0, "resolved": 0, "unchanged": 0}

    for issue in issues:
        prev = previous_state.get(issue["id"])
        if prev is None:
            issue["change_type"] = "new"
            changes["new"] += 1
        elif not prev.get("is_resolved") and issue.get("is_resolved"):
            issue["change_type"] = "resolved"
            changes["resolved"] += 1
        elif content_hash(issue) != content_hash(prev):
            issue["change_type"] = "updated"
            changes["updated"] += 1
        else:
            issue["change_type"] = None
            changes["unchanged"] += 1

    return issues, changes


def main():
    print("🏥 Microsoft Service Health Fetcher")
    print("=" * 50)

    # Get auth token
    print("\n🔑 Authentication:")
    token = get_graph_token()

    # Fetch service health overviews (current status)
    print("\n📡 Fetching service health overviews...")
    overviews = graph_get_all("/admin/serviceAnnouncement/healthOverviews", token)
    print(f"  ✅ {len(overviews)} services found")

    # Build service status summary
    services_status = []
    for svc in overviews:
        services_status.append({
            "id": svc.get("id", ""),
            "service": svc.get("service", ""),
            "status": svc.get("status", ""),
        })

    for svc in sorted(services_status, key=lambda s: s["service"]):
        status_icon = "✅" if svc["status"] == "serviceOperational" else "⚠️"
        print(f"    {status_icon} {svc['service']} — {svc['status']}")

    # Fetch all issues (paginated)
    print(f"\n📡 Fetching service health issues...")
    raw_issues = graph_get_all(
        "/admin/serviceAnnouncement/issues",
        token,
        params={"$top": str(PAGE_SIZE), "$orderby": "startDateTime desc"},
    )
    print(f"  ✅ {len(raw_issues)} issues fetched")

    # Normalize issues
    print("\n🔧 Normalizing issues...")
    issues = [normalize_issue(issue) for issue in raw_issues]

    # Change detection
    previous_state = load_previous_state()
    is_first_run = len(previous_state) == 0

    if is_first_run:
        print("\n🆕 First run — skipping change annotations")
        for issue in issues:
            issue["change_type"] = None
        changes = {"new": 0, "updated": 0, "resolved": 0, "unchanged": len(issues)}
    else:
        print(f"\n🔄 Change detection (previous: {len(previous_state)} issues):")
        issues, changes = detect_changes(issues, previous_state)
        print(f"  🆕 New: {changes['new']}")
        print(f"  🔄 Updated: {changes['updated']}")
        print(f"  ✅ Resolved: {changes['resolved']}")
        print(f"  ⏸️  Unchanged: {changes['unchanged']}")

    # Count by service
    service_counts = {}
    for issue in issues:
        svc = issue["service"]
        service_counts[svc] = service_counts.get(svc, 0) + 1

    # Count active (unresolved) issues
    active_issues = [i for i in issues if not i["is_resolved"]]

    print(f"\n📊 Summary:")
    print(f"  Total issues: {len(issues)}")
    print(f"  Active (unresolved): {len(active_issues)}")
    print(f"  Resolved: {len(issues) - len(active_issues)}")
    print(f"  Services with issues:")
    for svc, count in sorted(service_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"    {svc}: {count}")

    # Save output
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "services_status": services_status,
        "total_issues": len(issues),
        "active_issues": len(active_issues),
        "changes": changes,
        "service_counts": service_counts,
        "issues": issues,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Saved raw data → {OUTPUT_FILE}")

    # Save state for next run's diff
    state_items = []
    for issue in issues:
        state_items.append({
            "id": issue["id"],
            "status": issue["status"],
            "is_resolved": issue["is_resolved"],
            "update_count": issue["update_count"],
            "last_modified": issue["last_modified"],
        })

    with open(PREVIOUS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state_items, f, indent=2, ensure_ascii=False)
    print(f"💾 Saved state ({len(state_items)} items) → {PREVIOUS_STATE_FILE}")

    print("\n✅ Fetch complete!")


if __name__ == "__main__":
    main()
