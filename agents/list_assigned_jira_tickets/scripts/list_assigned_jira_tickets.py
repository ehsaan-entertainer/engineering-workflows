#!/usr/bin/env python3
"""
List Jira tickets assigned to users, grouped first by User, then by Project.

Uses the modern Atlassian Cloud v3 endpoint (/rest/api/3/search/jql) with
nextPageToken pagination.

Install dependencies first:
    pip install -r requirements.txt

Usage:
    python list_assigned_jira_tickets.py
    python list_assigned_jira_tickets.py --user currentUser() "john.doe@example.com"
    python list_assigned_jira_tickets.py --status "To Do" "In Progress" "In Review"
    python list_assigned_jira_tickets.py --project PROJ INFRA --days 14 --highlight-days 1
    python list_assigned_jira_tickets.py --json

Options:
    --user KEY ...        One or more user account IDs/emails/names to filter.
                          Default: ["currentUser()"]
    --status NAME ...     One or more statuses to filter. Pass "all" for no status filter.
                          Default: ["To Do", "In Progress"]
    --project KEY ...     One or more project keys to filter (e.g. PROJ INFRA).
                          Default: All projects
    --days INT            Number of days back to search by assignment/update activity.
                          Default: 30
    --highlight-days INT  Number of days to look back for the "Recently Updated" spotlight.
                          Default: 1
    --json                Print raw results as nested JSON instead of table format.

Exit codes:
    0  – success
    1  – auth error, missing credentials, or Jira API failure
"""

import argparse
import io
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

# Add repo root to path so shared package is importable regardless of CWD
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from shared.env import load_env_file, require
from shared.jira_client import get_jira_client, get_ticket_url, JiraError

# Jira priority order (lowest index = highest priority)
_PRIORITY_ORDER = {
    "Highest": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "Lowest": 4,
    "None": 5,
}


def _priority_rank(ticket: dict) -> int:
    name = ticket.get("fields", {}).get("priority", {}).get("name", "None")
    return _PRIORITY_ORDER.get(name, 99)


def fetch_assigned_tickets(
    client,
    days: int,
    users: list[str],
    statuses: list[str],
    projects: list[str]
) -> list[dict]:
    """
    Query Jira for tickets assigned to specified users within the last `days` days,
    optionally filtered by project keys and statuses.

    Uses POST /rest/api/3/search/jql with nextPageToken handling.
    """
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    # Build User clause
    formatted_users = []
    for u in users:
        if u.lower() == "currentuser()":
            formatted_users.append("currentUser()")
        else:
            formatted_users.append(f'"{u}"')
    user_clause = f"assignee in ({', '.join(formatted_users)})"

    # Build Project clause
    project_clause = ""
    if projects:
        keys = ", ".join(f'"{p.upper()}"' for p in projects)
        project_clause = f" AND project in ({keys})"

    # Build Status clause
    status_clause = ""
    if statuses and "all" not in [s.lower() for s in statuses]:
        status_names = ", ".join(f'"{s}"' for s in statuses)
        status_clause = f" AND status in ({status_names})"

    jql = (
        f"{user_clause}"
        f"{project_clause}"
        f"{status_clause}"
        f' AND updated >= "{since_date}"'
        f" ORDER BY priority ASC, updated DESC"
    )

    fields = ["summary", "status", "issuetype", "priority", "project", "assignee", "updated", "created"]

    all_issues = []
    next_page_token = None

    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": fields,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        try:
            results = client.post("rest/api/3/search/jql", json=payload)
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                raise JiraError("Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN.")
            if "403" in msg:
                raise JiraError("No permission to execute this JQL query.")
            raise JiraError(f"Jira API error executing JQL: {e}")

        if isinstance(results, str):
            raise JiraError(f"Jira API returned unexpected response string: {results[:200]}")

        issues = results.get("issues", [])
        all_issues.extend(issues)

        # Atlassian Cloud v3 search/jql uses nextPageToken for pagination
        next_page_token = results.get("nextPageToken")
        if not next_page_token or not issues:
            break

    return all_issues


def group_by_user_and_project(tickets: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """Group tickets by User (DisplayName), then by Project Key."""
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for ticket in tickets:
        assignee_obj = ticket["fields"].get("assignee")
        assignee_name = assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned"
        project_key = ticket["fields"]["project"]["key"]

        groups[assignee_name][project_key].append(ticket)

    # Sort tickets inside each project by priority
    sorted_groups = {}
    for user in sorted(groups.keys()):
        sorted_groups[user] = {}
        for proj in sorted(groups[user].keys()):
            groups[user][proj].sort(key=_priority_rank)
            sorted_groups[user][proj] = groups[user][proj]

    return sorted_groups


def filter_recently_updated(tickets: list[dict], highlight_days: int) -> list[dict]:
    """Return a sorted list of tickets updated within the last `highlight_days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=highlight_days)
    recent = []

    for t in tickets:
        updated_str = t.get("fields", {}).get("updated")
        if not updated_str:
            continue
        try:
            # Jira timestamps typically look like '2026-07-05T18:30:00.000+0000' or ISO 8601
            # Replace +0000 format to +00:00 for python fromisoformat compatibility
            clean_str = updated_str
            if len(clean_str) > 5 and clean_str[-5] in ("+", "-") and ":" not in clean_str[-5:]:
                clean_str = clean_str[:-2] + ":" + clean_str[-2:]
            updated_dt = datetime.fromisoformat(clean_str)
            if updated_dt.astimezone(timezone.utc) >= cutoff:
                recent.append(t)
        except ValueError:
            # Fallback string comparison against date prefix if parsing fails
            cutoff_prefix = cutoff.strftime("%Y-%m-%d")
            if updated_str[:10] >= cutoff_prefix:
                recent.append(t)

    # Sort spotlight list by assignee name, then priority
    def _spotlight_sort_key(t: dict):
        assignee_obj = t["fields"].get("assignee")
        assignee_name = assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned"
        return (assignee_name, _priority_rank(t))

    recent.sort(key=_spotlight_sort_key)
    return recent


def _col(value: str, width: int) -> str:
    """Left-pad a string to a fixed column width, truncating if needed."""
    value = value or ""
    if len(value) > width:
        value = value[: width - 1] + "…"
    return value.ljust(width)


def print_table(
    groups: dict[str, dict[str, list[dict]]],
    recent_tickets: list[dict],
    domain: str,
    highlight_days: int
) -> None:
    """Print tickets grouped hierarchy: User -> Project -> Tickets + Spotlight."""
    total_tickets = 0
    total_projects = set()

    for user, projects in groups.items():
        user_ticket_count = sum(len(t) for t in projects.values())
        total_tickets += user_ticket_count

        print()
        print("█" * 74)
        print(f" USER: {user}  ({user_ticket_count} ticket{'s' if user_ticket_count != 1 else ''})")
        print("█" * 74)

        for project_key, tickets in projects.items():
            total_projects.add(project_key)
            count = len(tickets)
            print()
            print(f"  ── PROJECT: {project_key} ({count} ticket{'s' if count != 1 else ''}) " + "─" * (48 - len(project_key)))

            # Header
            print(f"  {'Priority':<10}  {'Key':<14}  {'Status':<14}  {'Type':<10}  {'Summary'}")
            print(f"  {'-'*10}  {'-'*14}  {'-'*14}  {'-'*10}  {'-'*34}")

            for t in tickets:
                f = t["fields"]
                key = t["key"]
                priority = f.get("priority", {}).get("name", "-")
                status = f.get("status", {}).get("name", "-")
                issue_type = f.get("issuetype", {}).get("name", "-")
                summary = f.get("summary", "(no summary)")
                url = get_ticket_url(domain, key)

                print(
                    f"  {_col(priority, 10)}  "
                    f"{_col(key, 14)}  "
                    f"{_col(status, 14)}  "
                    f"{_col(issue_type, 10)}  "
                    f"{summary}"
                )
                print(f"  {'':10}  {'':14}  {'':14}  {'':10}  └─ {url}")

    print()
    print("═" * 74)
    print(
        f"Total: {total_tickets} ticket{'s' if total_tickets != 1 else ''} "
        f"across {len(groups)} user{'s' if len(groups) != 1 else ''} and "
        f"{len(total_projects)} project{'s' if len(total_projects) != 1 else ''}."
    )
    print("═" * 74)

    # Print Recent Activity Spotlight
    if recent_tickets:
        count = len(recent_tickets)
        day_label = f"{highlight_days} Day{'s' if highlight_days != 1 else ''}"
        print()
        print(f"⚡ RECENT ACTIVITY SPOTLIGHT: Tickets Updated in Last {day_label} ({count} ticket{'s' if count != 1 else ''})")
        print("─" * 74)
        print(f"  {'User':<10}  {'Key':<14}  {'Status':<14}  {'Type':<10}  {'Summary'}")
        print(f"  {'-'*10}  {'-'*14}  {'-'*14}  {'-'*10}  {'-'*34}")

        for t in recent_tickets:
            f = t["fields"]
            key = t["key"]
            assignee_obj = f.get("assignee")
            assignee_name = assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned"
            status = f.get("status", {}).get("name", "-")
            issue_type = f.get("issuetype", {}).get("name", "-")
            summary = f.get("summary", "(no summary)")

            print(
                f"  {_col(assignee_name, 10)}  "
                f"{_col(key, 14)}  "
                f"{_col(status, 14)}  "
                f"{_col(issue_type, 10)}  "
                f"{summary}"
            )
        print("─" * 74)


def build_json_output(
    groups: dict[str, dict[str, list[dict]]],
    recent_tickets: list[dict],
    domain: str
) -> dict:
    """Serialize hierarchy and spotlight list to nested JSON output."""
    output = {"grouped_tickets": {}, "recently_updated": []}
    for user, projects in groups.items():
        output["grouped_tickets"][user] = {}
        for project_key, tickets in projects.items():
            output["grouped_tickets"][user][project_key] = [
                {
                    "key": t["key"],
                    "url": get_ticket_url(domain, t["key"]),
                    "summary": t["fields"].get("summary", ""),
                    "status": t["fields"].get("status", {}).get("name", ""),
                    "type": t["fields"].get("issuetype", {}).get("name", ""),
                    "priority": t["fields"].get("priority", {}).get("name", ""),
                    "updated": t["fields"].get("updated", ""),
                }
                for t in tickets
            ]

    for t in recent_tickets:
        assignee_obj = t["fields"].get("assignee")
        assignee_name = assignee_obj.get("displayName", "Unassigned") if assignee_obj else "Unassigned"
        output["recently_updated"].append(
            {
                "user": assignee_name,
                "key": t["key"],
                "url": get_ticket_url(domain, t["key"]),
                "summary": t["fields"].get("summary", ""),
                "status": t["fields"].get("status", {}).get("name", ""),
                "type": t["fields"].get("issuetype", {}).get("name", ""),
                "priority": t["fields"].get("priority", {}).get("name", ""),
                "updated": t["fields"].get("updated", ""),
            }
        )

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List Jira tickets grouped first by User, then by Project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--user",
        nargs="+",
        metavar="USER",
        default=["currentUser()"],
        help='Filter by user account ID, email, or name (default: ["currentUser()"]).',
    )
    parser.add_argument(
        "--status",
        nargs="+",
        metavar="STATUS",
        default=["To Do", "In Progress"],
        help='Filter by statuses (default: ["To Do", "In Progress"]). Pass "all" for all.',
    )
    parser.add_argument(
        "--project",
        nargs="+",
        metavar="KEY",
        default=None,
        help="Filter by one or more project keys, e.g. --project PROJ INFRA.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        metavar="N",
        help="Return tickets updated within the last N days (default: 30).",
    )
    parser.add_argument(
        "--highlight-days",
        type=int,
        default=1,
        metavar="N",
        help="Number of days to look back for the Recent Activity Spotlight (default: 1).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Print output as JSON instead of a formatted table.",
    )
    parser.add_argument(
        "--save-to",
        metavar="DIR",
        dest="save_to",
        default=None,
        help=(
            "Directory to save output into. Creates the directory if it does not exist. "
            "File is named jira_tickets_YYYY-MM-DD.txt (or .json when --json is also set). "
            "Output is still printed to stdout as well."
        ),
    )
    args = parser.parse_args()

    env = load_env_file()
    domain, email, token = require(env, "JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN")

    try:
        client = get_jira_client(domain, email, token)
        tickets = fetch_assigned_tickets(
            client,
            days=args.days,
            users=args.user,
            statuses=args.status,
            projects=args.project or []
        )
    except JiraError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not tickets:
        print("No matching tickets found for the given criteria.")
        sys.exit(0)

    groups = group_by_user_and_project(tickets)
    recent_tickets = filter_recently_updated(tickets, args.highlight_days)

    # Build the output string
    buf = io.StringIO()

    if args.output_json:
        output_str = json.dumps(build_json_output(groups, recent_tickets, domain), indent=2)
    else:
        # Redirect print_table output into the buffer
        _orig_stdout = sys.stdout
        sys.stdout = buf
        print_table(groups, recent_tickets, domain, args.highlight_days)
        sys.stdout = _orig_stdout
        output_str = buf.getvalue()

    # Always print to stdout
    print(output_str)

    # Optionally save to a dated file in the specified directory
    if args.save_to:
        os.makedirs(args.save_to, exist_ok=True)
        ext = "json" if args.output_json else "txt"
        filename = f"jira_tickets_{date.today().isoformat()}.{ext}"
        filepath = os.path.join(args.save_to, filename)
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write("─" * 74 + "\n")
            fh.write(output_str)
        print(f"\nOutput saved to: {filepath}", file=sys.stderr)


if __name__ == "__main__":
    main()
