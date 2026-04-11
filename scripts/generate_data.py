"""
Step 2: Generate data files for the Hugo frontend.

Reads raw/health.json and produces:
- latest.json      — current service health + recent incidents (for main page)
- archive/YYYY-MM.json — monthly incident archives (loaded on demand)
- incidents/{id}.json  — per-incident detail with update posts
- stats.json       — incident metrics per service
- feed.xml         — RSS feed for subscribers
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

SCRIPT_DIR = Path(__file__).parent
SITE_DIR = SCRIPT_DIR / ".." / "site"
RAW_FILE = SITE_DIR / "raw" / "health.json"
SERVICES_FILE = SCRIPT_DIR / "services.json"
ARCHIVE_DIR = SITE_DIR / "archive"
INCIDENTS_DIR = SITE_DIR / "incidents"

LATEST_INCIDENTS_COUNT = 150
RSS_ITEMS_COUNT = 50


def load_services_config():
    """Load service display configuration."""
    with open(SERVICES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_dt(date_str):
    """Parse ISO datetime string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def duration_str(start_str, end_str):
    """Calculate human-readable duration between two datetimes."""
    start = parse_dt(start_str)
    end = parse_dt(end_str)
    if not start or not end:
        return None
    delta = end - start
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}d {remaining_hours}h" if remaining_hours else f"{days}d"


def slim_issue(issue, config):
    """Strip heavy fields for the frontend JSON."""
    svc_name = issue.get("service", "")
    display = config.get("display_config", {}).get(svc_name, config.get("default_display", {}))
    status_info = config.get("status_map", {}).get(issue.get("status", ""), {})

    return {
        "id": issue["id"],
        "title": issue.get("title", ""),
        "classification": issue.get("classification", ""),
        "status": issue.get("status", ""),
        "status_label": status_info.get("label", issue.get("status", "")),
        "status_icon": status_info.get("icon", "⚙️"),
        "status_color": status_info.get("color", "#6B7280"),
        "severity": status_info.get("severity", 0),
        "service": svc_name,
        "service_short": display.get("short_name", svc_name),
        "service_icon": display.get("icon", "⚙️"),
        "service_color": display.get("color", "#6B7280"),
        "feature": issue.get("feature", ""),
        "start_time": issue.get("start_time"),
        "end_time": issue.get("end_time"),
        "duration": duration_str(issue.get("start_time"), issue.get("end_time")),
        "last_modified": issue.get("last_modified"),
        "impact": issue.get("impact_description", ""),
        "is_resolved": issue.get("is_resolved", False),
        "update_count": issue.get("update_count", 0),
        "change_type": issue.get("change_type"),
    }


def generate_latest(data, config):
    """Generate latest.json — main data file for the frontend."""
    issues = data.get("issues", [])
    services_status = data.get("services_status", [])

    # Enrich service status with display info
    enriched_services = []
    for svc in services_status:
        svc_name = svc.get("service", "")
        display = config.get("display_config", {}).get(svc_name, config.get("default_display", {}))
        status_info = config.get("status_map", {}).get(svc.get("status", ""), {})
        enriched_services.append({
            "service": svc_name,
            "short_name": display.get("short_name", svc_name),
            "icon": display.get("icon", "⚙️"),
            "color": display.get("color", "#6B7280"),
            "status": svc.get("status", ""),
            "status_label": status_info.get("label", svc.get("status", "")),
            "status_icon": status_info.get("icon", "⚙️"),
            "status_color": status_info.get("color", "#6B7280"),
            "severity": status_info.get("severity", 0),
        })

    # Sort services: degraded/outage first, then alphabetical
    enriched_services.sort(key=lambda s: (-(s.get("severity", 0)), s["service"]))

    # Sort issues: active first (by severity desc), then resolved (by start_time desc)
    issues.sort(key=lambda i: (
        0 if not i.get("is_resolved") else 1,
        -(config.get("status_map", {}).get(i.get("status", ""), {}).get("severity", 0)),
        -(parse_dt(i.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
    ))

    # Slim down for frontend
    slim_issues = [slim_issue(i, config) for i in issues[:LATEST_INCIDENTS_COUNT]]

    # Count unique services with active issues
    active_services = set()
    for i in issues:
        if not i.get("is_resolved"):
            active_services.add(i.get("service", ""))

    output = {
        "generated_at": data.get("fetched_at", datetime.now(timezone.utc).isoformat()),
        "total_issues": len(issues),
        "active_issues": data.get("active_issues", 0),
        "active_services_count": len(active_services),
        "total_services": len(enriched_services),
        "changes": data.get("changes", {}),
        "services": enriched_services,
        "issues": slim_issues,
    }

    output_path = SITE_DIR / "latest.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✅ latest.json ({len(slim_issues)} issues, {size_kb:.1f} KB) → {output_path}")


def generate_archives(data, config):
    """Generate monthly archive files."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    issues = data.get("issues", [])

    # Group by month
    by_month = {}
    for issue in issues:
        start = parse_dt(issue.get("start_time"))
        if not start:
            continue
        month_key = start.strftime("%Y-%m")
        if month_key not in by_month:
            by_month[month_key] = []
        by_month[month_key].append(issue)

    for month_key, month_issues in sorted(by_month.items()):
        month_issues.sort(
            key=lambda i: parse_dt(i.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        archive = {
            "month": month_key,
            "total_issues": len(month_issues),
            "issues": [slim_issue(i, config) for i in month_issues],
        }

        path = ARCHIVE_DIR / f"{month_key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False)

    print(f"  ✅ {len(by_month)} monthly archives → {ARCHIVE_DIR}")


def generate_incident_details(data, config):
    """Generate per-incident detail files with full update posts."""
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    issues = data.get("issues", [])
    count = 0

    for issue in issues:
        issue_id = issue.get("id", "")
        if not issue_id:
            continue

        svc_name = issue.get("service", "")
        display = config.get("display_config", {}).get(svc_name, config.get("default_display", {}))
        status_info = config.get("status_map", {}).get(issue.get("status", ""), {})

        detail = {
            "id": issue_id,
            "title": issue.get("title", ""),
            "classification": issue.get("classification", ""),
            "status": issue.get("status", ""),
            "status_label": status_info.get("label", ""),
            "service": svc_name,
            "service_short": display.get("short_name", svc_name),
            "service_icon": display.get("icon", "⚙️"),
            "feature": issue.get("feature", ""),
            "feature_group": issue.get("feature_group", ""),
            "start_time": issue.get("start_time"),
            "end_time": issue.get("end_time"),
            "duration": duration_str(issue.get("start_time"), issue.get("end_time")),
            "last_modified": issue.get("last_modified"),
            "impact": issue.get("impact_description", ""),
            "is_resolved": issue.get("is_resolved", False),
            "updates": issue.get("updates", []),
        }

        path = INCIDENTS_DIR / f"{issue_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False)
        count += 1

    print(f"  ✅ {count} incident detail files → {INCIDENTS_DIR}")


def generate_stats(data, config):
    """Generate stats.json — incident metrics per service."""
    issues = data.get("issues", [])

    # Metrics per service
    service_metrics = {}
    for issue in issues:
        svc = issue.get("service", "")
        if svc not in service_metrics:
            service_metrics[svc] = {
                "total_incidents": 0,
                "active_incidents": 0,
                "avg_duration_minutes": 0,
                "durations": [],
                "last_incident": None,
                "classifications": {},
            }

        m = service_metrics[svc]
        m["total_incidents"] += 1

        if not issue.get("is_resolved"):
            m["active_incidents"] += 1

        # Track duration
        start = parse_dt(issue.get("start_time"))
        end = parse_dt(issue.get("end_time"))
        if start and end:
            minutes = (end - start).total_seconds() / 60
            m["durations"].append(minutes)

        # Track latest incident
        if not m["last_incident"] or (issue.get("start_time") or "") > (m["last_incident"] or ""):
            m["last_incident"] = issue.get("start_time")

        # Count classifications
        cls = issue.get("classification", "unknown")
        m["classifications"][cls] = m["classifications"].get(cls, 0) + 1

    # Compute averages and clean up
    stats = []
    for svc, m in service_metrics.items():
        display = config.get("display_config", {}).get(svc, config.get("default_display", {}))
        avg_dur = sum(m["durations"]) / len(m["durations"]) if m["durations"] else 0

        stats.append({
            "service": svc,
            "short_name": display.get("short_name", svc),
            "icon": display.get("icon", "⚙️"),
            "total_incidents": m["total_incidents"],
            "active_incidents": m["active_incidents"],
            "avg_duration_minutes": round(avg_dur),
            "last_incident": m["last_incident"],
            "classifications": m["classifications"],
        })

    stats.sort(key=lambda s: -s["total_incidents"])

    # Monthly summary
    monthly = {}
    for issue in issues:
        start = parse_dt(issue.get("start_time"))
        if not start:
            continue
        month_key = start.strftime("%Y-%m")
        if month_key not in monthly:
            monthly[month_key] = {"month": month_key, "incident_count": 0, "services_affected": set()}
        monthly[month_key]["incident_count"] += 1
        monthly[month_key]["services_affected"].add(issue.get("service", ""))

    monthly_list = []
    for month_key in sorted(monthly.keys(), reverse=True):
        m = monthly[month_key]
        monthly_list.append({
            "month": m["month"],
            "incident_count": m["incident_count"],
            "services_affected": len(m["services_affected"]),
        })

    output = {
        "generated_at": data.get("fetched_at", datetime.now(timezone.utc).isoformat()),
        "total_incidents_tracked": len(issues),
        "services": stats,
        "monthly": monthly_list,
    }

    output_path = SITE_DIR / "stats.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"  ✅ stats.json ({len(stats)} services) → {output_path}")


def generate_rss(data, config):
    """Generate RSS feed of recent incidents."""
    issues = data.get("issues", [])

    # Recent incidents, sorted by start time desc
    recent = sorted(
        issues,
        key=lambda i: i.get("start_time") or "",
        reverse=True,
    )[:RSS_ITEMS_COUNT]

    xml_items = ""
    for issue in recent:
        title = xml_escape(issue.get("title", ""))
        svc = xml_escape(issue.get("service", ""))
        issue_id = xml_escape(issue.get("id", ""))
        status = xml_escape(issue.get("status", ""))
        impact = xml_escape(issue.get("impact_description", "")[:500])
        start = issue.get("start_time", "")

        # RFC 822 date for RSS
        pub_date = ""
        dt = parse_dt(start)
        if dt:
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

        desc = f"[{svc}] {impact}" if impact else f"[{svc}] {title}"

        xml_items += f"""    <item>
      <title>[{svc}] {title}</title>
      <link>https://www.aguidetocloud.com/service-health/#{issue_id}</link>
      <description>{xml_escape(desc)}</description>
      <category>{svc}</category>
      <category>{xml_escape(status)}</category>
      <guid isPermaLink="false">{issue_id}</guid>
      <pubDate>{pub_date}</pubDate>
    </item>\n"""

    now_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Microsoft Service Health Tracker — A Guide to Cloud &amp; AI</title>
    <link>https://www.aguidetocloud.com/service-health/</link>
    <description>Microsoft 365 service health incidents — tracked and searchable</description>
    <language>en</language>
    <lastBuildDate>{now_str}</lastBuildDate>
    <atom:link href="https://www.aguidetocloud.com/data/service-health/feed.xml" rel="self" type="application/rss+xml"/>
{xml_items}  </channel>
</rss>"""

    rss_path = SITE_DIR / "feed.xml"
    with open(rss_path, "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"  ✅ RSS feed ({len(recent)} items) → {rss_path}")


def main():
    print("📊 Service Health Data Generator")
    print("=" * 50)

    if not RAW_FILE.exists():
        print(f"❌ No raw data at {RAW_FILE}")
        print("   Run fetch_health.py first.")
        sys.exit(1)

    with open(RAW_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    config = load_services_config()

    issues = data.get("issues", [])
    print(f"📋 {len(issues)} issues to process")

    # 1. Generate latest.json
    print("\n📅 Latest:")
    generate_latest(data, config)

    # 2. Generate monthly archives
    print("\n📦 Archives:")
    generate_archives(data, config)

    # 3. Generate per-incident detail files
    print("\n📄 Incident details:")
    generate_incident_details(data, config)

    # 4. Generate stats
    print("\n📊 Stats:")
    generate_stats(data, config)

    # 5. Generate RSS feed
    print("\n📡 RSS:")
    generate_rss(data, config)

    print("\n🎉 Data generation complete!")


if __name__ == "__main__":
    import sys
    main()
