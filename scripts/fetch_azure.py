"""
Step 1b: Fetch Azure Status incident history.

Scrapes azure.status.microsoft/status/history/ for Post Incident Reviews (PIRs).
Extracts tracking IDs, dates, affected services, affected regions, and summaries.
Outputs raw/azure.json for the generator to merge with M365 data.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
SITE_DIR = SCRIPT_DIR / ".." / "site"
RAW_DIR = SITE_DIR / "raw"
OUTPUT_FILE = RAW_DIR / "azure.json"

HISTORY_URL = "https://azure.status.microsoft/en-us/status/history/"
REQUEST_TIMEOUT = 60

# Known Azure regions to extract
AZURE_REGIONS = [
    "West US", "West US 2", "West US 3", "East US", "East US 2",
    "Central US", "North Central US", "South Central US", "West Central US",
    "Canada Central", "Canada East",
    "North Europe", "West Europe", "UK South", "UK West",
    "France Central", "France South", "Germany West Central",
    "Switzerland North", "Norway East", "Sweden Central", "Italy North", "Poland Central", "Spain Central",
    "Australia East", "Australia Southeast", "Australia Central",
    "East Asia", "Southeast Asia", "Japan East", "Japan West",
    "Korea Central", "Korea South", "Central India", "South India", "West India",
    "Brazil South", "South Africa North", "UAE North",
    "Qatar Central", "Israel Central",
]

REGION_PATTERN = re.compile(
    "|".join(re.escape(r) for r in sorted(AZURE_REGIONS, key=len, reverse=True)),
    re.IGNORECASE,
)

# Month name → number
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def extract_tracking_id(text):
    """Extract tracking ID from PIR text (e.g., 8GCS-858, _SVS-5_G, FNJ8-VQZ).
    Only uses aka.ms/air/ links (video URLs) which reliably contain the PIR ID.
    Falls back to aka.ms/AzPIR/ survey links (also contain PIR ID).
    """
    # Primary: aka.ms/air/{id} — incident retrospective video (most reliable)
    matches = re.findall(r'aka\.ms/air/([A-Za-z0-9_\-]{5,})', text)
    if matches:
        return matches[0]
    # Fallback: aka.ms/AzPIR/{id} — survey link at end of PIR
    # Filter out generic doc links (WAF, Monitoring, Alerts)
    generic = {"WAF", "Monitoring", "Alerts", "monitoring"}
    for match in re.findall(r'aka\.ms/AzPIR/([A-Za-z0-9_\-]{5,})', text):
        if match not in generic:
            return match
    return None


def extract_regions(text):
    """Extract Azure region names from text."""
    found = set()
    for match in REGION_PATTERN.finditer(text):
        found.add(match.group(0))
    return sorted(found)


def extract_azure_services(text):
    """Extract Azure service names from the 'What happened' section.
    
    In plain text (from get_text()), bold markers are stripped.
    Look for known Azure service name patterns instead.
    """
    known_services = [
        "Azure AI Search", "Azure App Service", "Azure Backup",
        "Azure Cache for Redis", "Azure Container Registry",
        "Azure Cosmos DB", "Azure Data Factory",
        "Azure Database for MySQL", "Azure Database for PostgreSQL",
        "Azure Databricks", "Azure DevOps", "Azure Event Hubs",
        "Azure IoT Hub", "Azure Kubernetes Service", "Azure Key Vault",
        "Azure Monitor", "Azure OpenAI Service", "Azure OpenAI",
        "Azure Service Bus", "Azure SQL Database", "Azure Storage",
        "Azure Stream Analytics", "Azure Virtual Machines",
        "Azure Front Door", "Azure Functions", "Azure Logic Apps",
        "Azure Container Apps", "Azure SignalR", "Azure Firewall",
        "Azure Load Balancer", "Azure Application Gateway",
        "Azure Virtual Network", "Azure ExpressRoute",
        "Azure Active Directory", "Microsoft Entra",
        "Microsoft Defender for Cloud", "GitHub Actions",
        "Application Insights", "Power BI",
    ]
    found = []
    for svc in known_services:
        if svc.lower() in text.lower():
            found.append(svc)
    return found


def extract_dates(text, month, year):
    """Extract start and end dates from the 'How did we respond' timeline."""
    # Look for UTC timestamps
    timestamps = re.findall(
        r'(\d{2}:\d{2})\s+UTC\s+on\s+(\d{1,2})\s+(\w+)\s+(\d{4})',
        text,
    )
    if not timestamps:
        return None, None

    parsed = []
    for time_str, day, month_name, yr in timestamps:
        m = MONTH_MAP.get(month_name)
        if m:
            try:
                dt = datetime(int(yr), m, int(day),
                              int(time_str[:2]), int(time_str[3:5]),
                              tzinfo=timezone.utc)
                parsed.append(dt)
            except ValueError:
                pass

    if parsed:
        return parsed[0].isoformat(), parsed[-1].isoformat()
    return None, None


def parse_history_page(html):
    """Parse the Azure Status history HTML into structured incidents."""
    soup = BeautifulSoup(html, "html.parser")
    incidents = []

    # Find the history section
    history = soup.find("section", id="history-section")
    if not history:
        print("  WARNING: Could not find history-section")
        # Try the whole page
        history = soup

    # Get the text content for parsing
    # The page structure uses divs/sections for each incident
    # Let's get all text and parse by month/day patterns
    content = history.get_text(separator="\n")

    # Split by month headers
    # Pattern: "March 2026", "February 2026", etc.
    month_pattern = re.compile(
        r'^(' + '|'.join(MONTH_MAP.keys()) + r')\s+(\d{4})\s*$',
        re.MULTILINE,
    )

    # Find all month positions
    month_matches = list(month_pattern.finditer(content))

    for i, month_match in enumerate(month_matches):
        month_name = month_match.group(1)
        year = int(month_match.group(2))
        month_num = MONTH_MAP[month_name]

        # Get content until next month
        start = month_match.end()
        end = month_matches[i + 1].start() if i + 1 < len(month_matches) else len(content)
        month_content = content[start:end]

        # Split by "What happened?" sections (each is an incident)
        incident_chunks = re.split(r'(?=What happened\?)', month_content)

        for chunk in incident_chunks:
            if "What happened?" not in chunk:
                continue

            text = chunk.strip()
            tracking_id = extract_tracking_id(text)

            # Extract the day number (usually right before "Watch our" or at start)
            day_match = re.search(r'(?:^|\n)\s*(\d{1,2})\s*\n', text[:200])
            day = int(day_match.group(1)) if day_match else 1

            # Extract "What happened?" section
            what_match = re.search(
                r'What happened\?\s*\n(.*?)(?=What went wrong|How did we respond|$)',
                text, re.DOTALL,
            )
            what_happened = what_match.group(1).strip() if what_match else ""

            # First paragraph as summary
            paragraphs = [p.strip() for p in what_happened.split("\n\n") if p.strip()]
            summary = paragraphs[0] if paragraphs else what_happened[:500]

            # Extract services and regions from full text
            azure_services = extract_azure_services(text)
            regions = extract_regions(text)

            # Extract dates from timeline
            start_time, end_time = extract_dates(text, month_num, year)

            # Fallback date from month/day
            if not start_time:
                try:
                    start_time = datetime(year, month_num, day, tzinfo=timezone.utc).isoformat()
                except ValueError:
                    start_time = datetime(year, month_num, 1, tzinfo=timezone.utc).isoformat()

            # Generate a unique ID (deduplicate if needed)
            seen_ids = {inc["id"] for inc in incidents}
            if tracking_id:
                incident_id = f"AZ-{tracking_id}"
                if incident_id in seen_ids:
                    incident_id = f"AZ-{year}{month_num:02d}{day:02d}-{len(incidents) + 1}"
            else:
                incident_id = f"AZ-{year}{month_num:02d}{day:02d}-{len(incidents) + 1}"

            incidents.append({
                "id": incident_id,
                "source": "azure-status",
                "tracking_id": tracking_id,
                "title": f"Azure incident affecting {', '.join(regions[:3]) or 'multiple regions'}" if regions else f"Azure incident — {month_name} {day}, {year}",
                "classification": "incident",
                "status": "postIncidentReviewPublished",
                "service": "Azure",
                "feature": "",
                "feature_group": "",
                "affected_services": azure_services,
                "affected_regions": regions,
                "start_time": start_time,
                "end_time": end_time,
                "last_modified": end_time or start_time,
                "impact_description": summary[:500],
                "is_resolved": True,
                "has_updates": False,
                "update_count": 0,
                "updates": [],
                "pir_url": f"https://azure.status.microsoft/status/history/?trackingId={tracking_id}" if tracking_id else None,
                "video_url": None,
            })

            # Try to extract video URL
            video_match = re.search(r'(https://aka\.ms/air/[A-Za-z0-9_\-]+)', text)
            if video_match:
                incidents[-1]["video_url"] = video_match.group(1)

    return incidents


def main():
    print("Azure Status History Scraper")
    print("=" * 50)

    print("\nFetching Azure Status history page...")
    try:
        resp = requests.get(HISTORY_URL, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": "ServiceHealthTracker/1.0 (aguidetocloud.com)",
        })
        resp.raise_for_status()
        print(f"  Page fetched: {len(resp.text)} bytes")
    except requests.RequestException as e:
        print(f"  ERROR: Failed to fetch: {e}")
        # Don't fail the pipeline — Azure data is supplementary
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "incidents": []}, f)
        print(f"  Saved empty azure.json (non-blocking)")
        return

    print("\nParsing incidents...")
    incidents = parse_history_page(resp.text)
    print(f"  Found {len(incidents)} Azure incidents")

    if incidents:
        # Show summary
        for inc in incidents[:5]:
            regions_str = ", ".join(inc["affected_regions"][:3]) or "N/A"
            services_count = len(inc["affected_services"])
            print(f"    {inc['id']}: {inc['start_time'][:10]} | {regions_str} | {services_count} services")
        if len(incidents) > 5:
            print(f"    ... and {len(incidents) - 5} more")

    # Save
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": HISTORY_URL,
        "total_incidents": len(incidents),
        "incidents": incidents,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(incidents)} incidents -> {OUTPUT_FILE}")
    print("Done!")


if __name__ == "__main__":
    main()
