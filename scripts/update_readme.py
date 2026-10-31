#!/usr/bin/env python3
"""Organization README updater.

Purpose
-------
Regenerate the dynamic project and stats sections in the organization profile
README using live GitHub REST API data: repository metadata, topics, languages,
licenses, README data, and repository statistics.

Required environment variables
------------------------------
- GH_TOKEN: fine-grained PAT with Metadata: Read-only and Contents: Read-only
- ORG: GitHub organization login (default: JalaU-Capstones)
- README_PATH: path to the README file to update (default: profile/README.md)

PAT creation
------------
Create a fine-grained PAT for the JalaU-Capstones organization:
- Resource owner: JalaU-Capstones
- Repository access: All repositories
- Permissions: Metadata: Read-only, Contents: Read-only

Exit codes
----------
- 0: success
- 1: configuration or API failure

Local testing
-------------
    export GH_TOKEN="<fine-grained PAT>"
    export ORG="JalaU-Capstones"
    export README_PATH="profile/README.md"
    python scripts/update_readme.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://api.github.com"
DEFAULT_ORG = "JalaU-Capstones"
DEFAULT_README_PATH = "profile/README.md"
EXCLUDED_REPOS = {".github"}

MARKER_PROJECTS_START = "<!-- PROJECTS_START -->"
MARKER_PROJECTS_END = "<!-- PROJECTS_END -->"
MARKER_STATS_START = "<!-- STATS_START -->"
MARKER_STATS_END = "<!-- STATS_END -->"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("update_readme")


class GitHubApiError(RuntimeError):
    """Raised when the GitHub API cannot complete a required request."""


def create_session(token: str) -> requests.Session:
    """Create a reusable requests session with retries and GitHub headers."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "JalaU-Capstones-README-Updater",
        }
    )

    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def resolve_rate_limit_reset(resp: requests.Response) -> str:
    """Return a human-readable reset timestamp from an API response."""
    epoch = resp.headers.get("X-RateLimit-Reset")
    if not epoch:
        return "unknown"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return epoch


def guard_rate_limit(resp: requests.Response, label: str) -> None:
    """Warn and sleep when GitHub is close to the rate limit, or fail when exhausted."""
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is None:
        return

    try:
        remaining_int = int(remaining)
    except ValueError:
        return

    if remaining_int == 0:
        reset_time = resolve_rate_limit_reset(resp)
        raise GitHubApiError(
            f"GitHub API rate limit reached while calling {label}. Reset time: {reset_time}."
        )

    if remaining_int < 50:
        logger.warning(
            "GitHub rate limit remaining for %s is %s; sleeping 2 seconds before the next request.",
            label,
            remaining,
        )
        time.sleep(2)


def handle_api_response(resp: requests.Response, label: str) -> Any:
    """Raise helpful errors for authentication, rate-limit, and generic API failures."""
    if resp.status_code == 401:
        msg = (
            "GitHub rejected the token used for GH_TOKEN. Verify ORG_READ_TOKEN is valid, "
            "not expired, and has Metadata: Read-only and Contents: Read-only for JalaU-Capstones."
        )
        logger.error(msg)
        raise GitHubApiError(msg)

    if resp.status_code == 403:
        payload = resp.json() if "json" in (resp.headers.get("Content-Type", "") or "").lower() else {}
        message = str(payload.get("message", "")).lower()
        if "rate limit" in message or resp.headers.get("X-RateLimit-Remaining") == "0":
            reset_time = resolve_rate_limit_reset(resp)
            msg = f"GitHub API rate limit reached while calling {label}. Reset time: {reset_time}."
            logger.error(msg)
            raise GitHubApiError(msg)

        msg = (
            "GitHub rejected the token used for GH_TOKEN. Verify ORG_READ_TOKEN is valid, "
            "not expired, and has Metadata: Read-only and Contents: Read-only for JalaU-Capstones."
        )
        logger.error(msg)
        raise GitHubApiError(msg)

    if resp.status_code == 429:
        reset_time = resolve_rate_limit_reset(resp)
        msg = f"GitHub API rate limit reached while calling {label}. Reset time: {reset_time}."
        logger.error(msg)
        raise GitHubApiError(msg)

    if resp.status_code >= 400:
        payload = resp.json() if "json" in (resp.headers.get("Content-Type", "") or "").lower() else {}
        message = payload.get("message", f"HTTP {resp.status_code}")
        msg = f"GitHub API error for {label}: HTTP {resp.status_code} - {message}"
        logger.error(msg)
        raise GitHubApiError(msg)

    guard_rate_limit(resp, label)
    return resp.json() if resp.content else None


def validate_token(session: requests.Session) -> str:
    """Validate the token and return the authenticated user login."""
    resp = session.get(f"{API_BASE}/user", timeout=30)
    payload = handle_api_response(resp, "GET /user")
    login = (payload or {}).get("login")
    if not login:
        raise GitHubApiError("GitHub API did not return a valid login for the configured token.")
    logger.info("Authenticated as %s", login)
    return login


def fetch_org_repos(session: requests.Session, org: str) -> list[dict[str, Any]]:
    """Return the public non-fork repositories for the organization."""
    repos: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"{API_BASE}/orgs/{org}/repos"
        resp = session.get(url, params={"type": "public", "per_page": 100, "page": page}, timeout=30)
        data = handle_api_response(resp, f"GET /orgs/{org}/repos?page={page}")
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1

    filtered = [
        repo for repo in repos if repo.get("name") not in EXCLUDED_REPOS and not repo.get("fork", False)
    ]
    logger.info("Fetched %d repositories from %s; %d retained after filtering.", len(repos), org, len(filtered))
    return filtered


def normalize_languages(raw: Any) -> dict[str, int]:
    """Normalize GitHub language payloads to dict[str, int]."""
    if not isinstance(raw, dict):
        return {}

    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            out[str(key)] = value
            continue

        if isinstance(value, dict):
            for nested_key in ("bytes", "count", "value"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, int) and not isinstance(nested_value, bool):
                    out[str(key)] = nested_value
                    break
    return out


def fetch_repo_languages(session: requests.Session, org: str, repo_name: str) -> dict[str, int]:
    """Return the language byte totals for one repository."""
    url = f"{API_BASE}/repos/{org}/{repo_name}/languages"
    resp = session.get(url, timeout=30)
    if resp.status_code == 404:
        logger.warning("Languages are unavailable for %s/%s; skipping language data.", org, repo_name)
        return {}
    payload = handle_api_response(resp, f"GET /repos/{org}/{repo_name}/languages")
    if not payload:
        return {}
    return normalize_languages(payload)


def parse_iso8601(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp returned by the GitHub API."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def format_date(raw: str | None) -> str:
    """Render a GitHub date as YYYY-MM-DD."""
    dt = parse_iso8601(raw)
    if dt is None:
        return "Unknown"
    return dt.strftime("%Y-%m-%d")


def humanize_repo_name(name: str) -> str:
    """Convert a repository slug into a readable title."""
    if "-" not in name and "_" not in name:
        return name
    cleaned = re.sub(r"[-_]+", " ", name).strip()
    return " ".join(part.title() for part in cleaned.split())


def truncate_text(value: str, max_chars: int = 220) -> str:
    """Truncate text to a readable width, preserving word boundaries."""
    text = " ".join((value or "").split())
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars].rsplit(" ", 1)[0] if " " in text[:max_chars] else text[: max_chars - 1]
    return trimmed.rstrip(".,;:") + "..."


def sanitized_topics(topics: Iterable[str]) -> list[str]:
    """Normalize topics to lowercase and drop empty values."""
    values: list[str] = []
    for topic in topics or []:
        value = str(topic).strip().lower()
        if value:
            values.append(value)
    return values


def render_topics_line(repo: dict[str, Any]) -> str:
    """Render topic badges as inline code tokens."""
    topics = sanitized_topics(repo.get("topics") or [])
    if not topics:
        return ""
    return "**Topics:** " + ", ".join(f"`{topic}`" for topic in topics[:10])


def render_repo_stats(repo: dict[str, Any]) -> str:
    """Render repository statistics without Unicode icons."""
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    issues = int(repo.get("open_issues_count") or 0)

    parts: list[str] = []
    if stars > 0 or not any((stars, forks, issues)):
        parts.append(f"Stars: {stars}")
    if forks > 0:
        parts.append(f"Forks: {forks}")
    if issues > 0:
        parts.append(f"Issues: {issues}")

    return " · ".join(parts) if parts else "Stars: 0"


def top_languages(languages: dict[str, int], limit: int = 4) -> list[str]:
    """Return top language names, sorted by bytes descending."""
    normalized = normalize_languages(languages)
    if not normalized:
        return []
    ordered = sorted(normalized.items(), key=lambda item: (-int(item[1]), str(item[0]).lower()))
    return [language for language, _ in ordered[:limit]]


def primary_language(repo: dict[str, Any]) -> str | None:
    """Return the primary language by byte count."""
    languages = normalize_languages(repo.get("languages"))
    if not languages:
        return None
    return max(languages.items(), key=lambda item: int(item[1]))[0]


def build_card_cell(repo: dict[str, Any]) -> str:
    """Render a single repository card in table-cell Markdown."""
    name = str(repo.get("name") or "")
    pretty_name = humanize_repo_name(name)
    html_url = repo.get("html_url") or ""
    description = (repo.get("description") or "").strip()
    homepage = (repo.get("homepage") or "").strip()
    license_info = repo.get("license") or {}
    license_url = license_info.get("url") if isinstance(license_info, dict) else None
    languages = normalize_languages(repo.get("languages"))
    stack = top_languages(languages, limit=4)

    parts: list[str] = [f"#### [{pretty_name}]({html_url})"]

    if description:
        parts.append(truncate_text(description, 220))

    topics_line = render_topics_line(repo)
    if topics_line:
        parts.append(topics_line)

    if stack:
        parts.append("**Stack:** " + " ".join(f"`{lang}`" for lang in stack))

    links: list[str] = [f"[Repository]({html_url})"] if html_url else []
    if homepage and homepage != html_url:
        links.append(f"[Homepage]({homepage})")
    if license_url:
        links.append(f"[License]({license_url})")
    if links:
        parts.append(" · ".join(links))

    parts.append(render_repo_stats(repo))
    parts.append(f"Updated: {format_date(repo.get('pushed_at'))}")

    body = "\n\n".join(parts)
    return f"<td width=\"50%\">\n\n{body}\n\n</td>"


def aggregate_language_totals(repos: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate language byte totals across the organization."""
    totals: dict[str, int] = defaultdict(int)
    for repo in repos:
        for language, value in normalize_languages(repo.get("languages")).items():
            totals[str(language)] += int(value)
    return dict(totals)


def build_top_languages_table(repos: list[dict[str, Any]]) -> str:
    """Render the top language table for the markdown stats block."""
    totals = aggregate_language_totals(repos)
    total_bytes = sum(totals.values())
    if total_bytes == 0:
        return "| Language | Share | Repos |\n|----------|-------|-------|\n| N/A | 0% | 0 |"

    rows = []
    for language, byte_count in sorted(totals.items(), key=lambda item: (-item[1], item[0].lower()))[:10]:
        share = (byte_count / total_bytes) * 100
        repo_count = sum(1 for repo in repos if language in normalize_languages(repo.get("languages")))
        rows.append(f"| {language} | {share:.0f}% | {repo_count} |")

    return "| Language | Share | Repos |\n|----------|-------|-------|\n" + "\n".join(rows)


def build_skills_table(repos: list[dict[str, Any]]) -> str:
    """Render the language and topic summary table."""
    totals = aggregate_language_totals(repos)
    languages = [f"`{lang}`" for lang in sorted(totals.keys(), key=lambda item: (-totals[item], item.lower()))]

    topic_counter: Counter[str] = Counter()
    for repo in repos:
        for topic in sanitized_topics(repo.get("topics") or []):
            topic_counter[topic] += 1

    popular_topics = [f"`{topic}`" for topic, _ in topic_counter.most_common(15)]

    rows = ["| Category | Technologies (from detected data) |", "|----------|-----------------------------------|"]
    if languages:
        rows.append(f"| Languages | {', '.join(languages[:20])} |")
    if popular_topics:
        rows.append(f"| Popular topics | {', '.join(popular_topics)} |")

    return "\n".join(rows)


def freshness_counts(repos: list[dict[str, Any]]) -> dict[str, int]:
    """Bucket repositories by recency and archive state."""
    now = datetime.now(timezone.utc)
    counts = {
        "Active": 0,
        "Recent": 0,
        "Stale": 0,
        "Archived": 0,
    }

    for repo in repos:
        pushed_at = parse_iso8601(repo.get("pushed_at") or repo.get("updated_at"))
        if pushed_at is None:
            counts["Archived"] += 1
            continue

        age = now - pushed_at
        if repo.get("archived") or age > timedelta(days=365):
            counts["Archived"] += 1
        elif age <= timedelta(days=30):
            counts["Active"] += 1
        elif age <= timedelta(days=90):
            counts["Recent"] += 1
        else:
            counts["Stale"] += 1

    return counts


def build_freshness_table(repos: list[dict[str, Any]]) -> str:
    """Render the repository freshness table."""
    counts = freshness_counts(repos)
    return "| Bucket | Count |\n|--------|-------|\n" + "\n".join(
        [
            f"| Active | {counts['Active']} |",
            f"| Recent | {counts['Recent']} |",
            f"| Stale | {counts['Stale']} |",
            f"| Archived | {counts['Archived']} |",
        ]
    )


def build_quickchart_url(config: dict[str, Any]) -> str:
    """Encode a Chart.js config into a quickchart.io SVG URL."""
    encoded = quote(json.dumps(config, separators=(",", ":")), safe="")
    return f"https://quickchart.io/chart?c={encoded}&w=800&h=300&backgroundColor=white"


def build_shields_badge(label: str, value: str, color: str = "blue") -> str:
    """Return a shields.io badge URL for inline statistics."""
    label_text = quote(label.replace(" ", "_"), safe="")
    value_text = quote(str(value).replace(" ", "_"), safe="")
    color_text = quote(color, safe="")
    return f"https://img.shields.io/badge/{label_text}-{value_text}-{color_text}?style=flat-square"


def aggregate_commits_last_year(repo_stats_list: list[dict[str, Any]]) -> int:
    """Total commits in the last year across all repos."""
    total = 0
    for stats in repo_stats_list:
        if not stats:
            continue
        participation = stats.get("participation") or {}
        if isinstance(participation.get("all"), list):
            total += sum(int(value) for value in participation.get("all", []) if value is not None)
            continue
        activity = stats.get("commit_activity") or []
        if isinstance(activity, list):
            total += sum(int(item.get("total") or 0) for item in activity)
    return total


def aggregate_commit_activity(repo_stats_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate commit activity across repos into weekly totals."""
    weekly_totals: dict[int, int] = defaultdict(int)
    for stats in repo_stats_list:
        if not stats:
            continue
        for item in stats.get("commit_activity") or []:
            if not isinstance(item, dict):
                continue
            week = int(item.get("week") or 0)
            weekly_totals[week] += int(item.get("total") or 0)

    if not weekly_totals:
        return {"labels": [], "values": []}

    labels = [datetime.fromtimestamp(week, tz=timezone.utc).strftime("%Y-%m-%d") for week in sorted(weekly_totals)]
    values = [weekly_totals[week] for week in sorted(weekly_totals)]
    return {"labels": labels, "values": values}


def aggregate_code_frequency(repo_stats_list: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Aggregate added and deleted lines across repos."""
    additions: list[int] = []
    deletions: list[int] = []
    labels: list[str] = []
    seen: dict[int, list[int]] = defaultdict(lambda: [0, 0])

    for stats in repo_stats_list:
        if not stats:
            continue
        for item in stats.get("code_frequency") or []:
            if not isinstance(item, list) or len(item) < 3:
                continue
            week = int(item[0])
            seen[week][0] += int(item[1] or 0)
            seen[week][1] += int(item[2] or 0)

    for week in sorted(seen):
        labels.append(datetime.fromtimestamp(week, tz=timezone.utc).strftime("%Y-%m-%d"))
        additions.append(seen[week][0])
        deletions.append(seen[week][1])

    return {"labels": labels, "additions": additions, "deletions": deletions}


def aggregate_contributors(repo_stats_list: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate contributors by login across the organization."""
    totals: dict[str, int] = defaultdict(int)
    repos_by_contributor: dict[str, set[str]] = defaultdict(set)

    for stats in repo_stats_list:
        if not stats:
            continue
        for item in stats.get("contributors") or []:
            if not isinstance(item, dict):
                continue
            login = str(item.get("login") or item.get("name") or "unknown")
            contributions = int(item.get("contributions") or item.get("total") or 0)
            totals[login] += contributions
            repos_by_contributor[login].add(str(stats.get("repo_name") or "unknown"))

    return dict(totals), repos_by_contributor


def get_repo_stats(session: requests.Session, org: str, repo: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch repository stats for a single repo, returning None when unavailable."""
    repo_name = str(repo.get("name") or "")
    if repo.get("archived") or int(repo.get("size") or 0) == 0:
        logger.info("Skipping stats for %s/%s because it is archived or has no content.", org, repo_name)
        return None

    stats: dict[str, Any] = {"repo_name": repo_name}
    skipped_202 = 0
    failed = 0

    for kind in ("participation", "commit_activity", "code_frequency", "contributors"):
        url = f"{API_BASE}/repos/{org}/{repo_name}/stats/{kind}"
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as exc:
            logger.warning("Request failed for %s/%s stats/%s: %s", org, repo_name, kind, exc)
            failed += 1
            continue

        if resp.status_code == 202:
            logger.warning("Stats for %s/%s are being computed, skipping this run.", org, repo_name)
            skipped_202 += 1
            repo["_stats_status"] = "pending"
            return None

        if resp.status_code in (401, 403, 404):
            logger.warning("Stats unavailable for %s/%s (%s): HTTP %s", org, repo_name, kind, resp.status_code)
            failed += 1
            repo["_stats_status"] = "failed"
            continue

        if resp.status_code >= 400:
            logger.warning("Stats request failed for %s/%s (%s): HTTP %s", org, repo_name, kind, resp.status_code)
            failed += 1
            repo["_stats_status"] = "failed"
            continue

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("No JSON payload returned for %s/%s stats/%s", org, repo_name, kind)
            failed += 1
            repo["_stats_status"] = "failed"
            continue

        if not payload:
            logger.warning("Empty stats payload for %s/%s stats/%s", org, repo_name, kind)
            failed += 1
            repo["_stats_status"] = "failed"
            continue

        stats[kind] = payload

    if not stats or len(stats) == 1:
        logger.info("Stats summary: 0 ok, %s skipped (202), %s failed", skipped_202, failed)
        repo["_stats_status"] = "skipped" if skipped_202 == 0 else "pending"
        return None

    logger.info("Stats summary: 1 ok, %s skipped (202), %s failed", skipped_202, failed)
    repo["_stats_status"] = "ok"
    return stats


def project_grouping(repos: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group repositories by primary language, with Uncategorized as the fallback."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repo in repos:
        language = primary_language(repo)
        groups[language or "Uncategorized"].append(repo)
    return groups


def build_projects_section(repos: list[dict[str, Any]]) -> str:
    """Render the projects section group-by-language, without emojis."""
    groups = project_grouping(repos)
    ordered_names = sorted(
        groups.keys(),
        key=lambda name: (
            -sum(normalize_languages(repo.get("languages")).get(name, 0) for repo in groups[name]),
            name.lower(),
        ) if name != "Uncategorized" else (0, "zzzzzz"),
    )

    sections: list[str] = []
    for name in ordered_names:
        items = sorted(
            groups[name],
            key=lambda repo: (
                -(int(repo.get("stargazers_count") or 0)),
                -(int(parse_iso8601(repo.get("updated_at")).timestamp()) if parse_iso8601(repo.get("updated_at")) else 0),
                str(repo.get("name") or "").lower(),
            ),
        )
        if not items:
            continue
        heading = f"### {name if name != 'Uncategorized' else 'Uncategorized'} Projects"
        rows: list[str] = []
        for index in range(0, len(items), 2):
            chunk = items[index : index + 2]
            cells = "\n".join(build_card_cell(repo) for repo in chunk)
            if len(chunk) == 1:
                cells += "\n<td width=\"50%\"></td>"
            rows.append(f"<tr>\n{cells}\n</tr>")
        table = "<table>\n" + "\n".join(rows) + "\n</table>"
        sections.append(f"{heading}\n\n{table}")

    return "\n\n".join(sections) if sections else "_No public repositories available._"


def generate_stats_charts(repo_stats: list[dict[str, Any]]) -> dict[str, str]:
    """Generate chart URLs for the stats section when data is available."""
    charts: dict[str, str] = {}

    activity = aggregate_commit_activity(repo_stats)
    if activity["labels"] and activity["values"]:
        labels = activity["labels"][-52:]
        values = activity["values"][-52:]
        config = {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [
                    {"label": "Weekly commits", "data": values, "borderColor": "#2f81f7", "backgroundColor": "rgba(47,129,247,0.2)", "fill": False, "tension": 0.2}
                ],
            },
            "options": {"plugins": {"legend": {"display": False}}, "scales": {"y": {"beginAtZero": False}}},
        }
        charts["commit_activity"] = build_quickchart_url(config)

    code_freq = aggregate_code_frequency(repo_stats)
    if code_freq["labels"] and code_freq["additions"]:
        config = {
            "type": "bar",
            "data": {
                "labels": code_freq["labels"][-52:],
                "datasets": [
                    {"label": "Additions", "data": code_freq["additions"][-52:], "backgroundColor": "#2f81f7"},
                    {"label": "Deletions", "data": code_freq["deletions"][-52:], "backgroundColor": "#d9485f"},
                ],
            },
            "options": {"plugins": {"legend": {"display": True}}, "scales": {"y": {"beginAtZero": True}}},
        }
        charts["code_frequency"] = build_quickchart_url(config)

    contributor_totals = aggregate_contributors(repo_stats)[0]
    if contributor_totals:
        top_contributors = sorted(contributor_totals.items(), key=lambda item: (-item[1], item[0].lower()))[:10]
        labels = [name for name, _ in top_contributors]
        values = [count for _, count in top_contributors]
        config = {
            "type": "bar",
            "data": {"labels": labels, "datasets": [{"label": "Commits", "data": values, "backgroundColor": "#3fb950"}]},
            "options": {
                "indexAxis": "y",
                "plugins": {"legend": {"display": False}},
                "scales": {"x": {"beginAtZero": True}},
            },
        }
        charts["contributors"] = build_quickchart_url(config)

    totals = aggregate_language_totals([repo for repo in []])
    # The aggregate_language_totals call above is placeholder only; actual totals are handled in main.

    return charts


def generate_stats_section(repos: list[dict[str, Any]], repo_stats: list[dict[str, Any]]) -> str:
    """Render the markdown statistics block, including charts when available."""
    totals = aggregate_language_totals(repos)
    total_repos = len(repos)
    total_stars = sum(int(repo.get("stargazers_count") or 0) for repo in repos)
    total_forks = sum(int(repo.get("forks_count") or 0) for repo in repos)
    total_issues = sum(int(repo.get("open_issues_count") or 0) for repo in repos)
    total_commits = aggregate_commits_last_year(repo_stats)
    contributor_totals = aggregate_contributors(repo_stats)[0]
    total_contributors = len(contributor_totals)
    last_updated = max((parse_iso8601(repo.get("pushed_at") or repo.get("updated_at")) for repo in repos), default=None)
    last_updated_str = last_updated.strftime("%Y-%m-%d %H:%M UTC") if last_updated else "N/A"

    lines = [
        "### Global stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total repositories | {total_repos} |",
        f"| Total stars | {total_stars} |",
        f"| Total forks | {total_forks} |",
        f"| Open issues | {total_issues} |",
        f"| Total commits | {total_commits} |",
        f"| Total contributors | {total_contributors} |",
        f"| Total languages | {len(totals)} |",
        f"| Last updated | {last_updated_str} |",
        "",
        "### Top languages",
        build_top_languages_table(repos),
        "",
        "### Skills / tech stack",
        build_skills_table(repos),
        "",
    ]

    if contributor_totals:
        top = sorted(contributor_totals.items(), key=lambda item: (-item[1], item[0].lower()))[:10]
        lines.extend(["### Top contributors", "| Contributor | Commits | Repositories |", "|-------------|--------|--------------|"])
        for name, commits in top:
            repo_count = len({repo_name for repo_name, stats in [(stats.get("repo_name"), stats) for stats in repo_stats if stats] if repo_name})
            lines.append(f"| {name} | {commits} | {repo_count} |")
        lines.append("")

    lines.extend(["### Repository freshness", build_freshness_table(repos), ""])

    charts = generate_stats_charts(repo_stats)
    if not charts:
        lines.append(
            "_Commit activity charts will appear once GitHub finishes computing stats for this organization. The next scheduled run will include them._"
        )
        return "\n".join(lines)

    if "commit_activity" in charts:
        lines.append("### Weekly commit activity")
        lines.append(f"[![Weekly commit activity]({charts['commit_activity']})](https://github.com/orgs/JalaU-Capstones)")
        lines.append("Weekly commit activity over the last 52 weeks.")
        lines.append("")

    if "code_frequency" in charts:
        lines.append("### Code frequency")
        lines.append(f"[![Code frequency]({charts['code_frequency']})](https://github.com/orgs/JalaU-Capstones)")
        lines.append("Aggregate additions and deletions across the last 52 weeks.")
        lines.append("")

    if "contributors" in charts:
        lines.append("### Top contributors chart")
        lines.append(f"[![Top contributors]({charts['contributors']})](https://github.com/orgs/JalaU-Capstones)")
        lines.append("Top contributors by commit count.")
        lines.append("")

    if totals:
        labels = list(totals.keys())
        values = list(totals.values())
        config = {
            "type": "doughnut",
            "data": {"labels": labels, "datasets": [{"data": values, "backgroundColor": ["#2f81f7", "#3fb950", "#d9485f", "#f59e0b", "#8b5cf6", "#0ea5e9", "#f97316", "#10b981", "#eab308", "#64748b"]}]},
            "options": {"plugins": {"legend": {"position": "bottom"}}},
        }
        lines.append("### Language distribution")
        lines.append(f"[![Language distribution]({build_quickchart_url(config)})](https://github.com/orgs/JalaU-Capstones)")
        lines.append("Repository language distribution by total bytes.")
        lines.append("")

    freshness = freshness_counts(repos)
    if any(freshness.values()):
        labels = ["Active", "Recent", "Stale", "Archived"]
        values = [freshness["Active"], freshness["Recent"], freshness["Stale"], freshness["Archived"]]
        config = {
            "type": "bar",
            "data": {"labels": labels, "datasets": [{"label": "Repositories", "data": values, "backgroundColor": ["#2f81f7", "#3fb950", "#f59e0b", "#d9485f"]}]},
            "options": {"indexAxis": "y", "plugins": {"legend": {"display": False}}, "scales": {"x": {"beginAtZero": True}}},
        }
        lines.append("### Repository freshness chart")
        lines.append(f"[![Repository freshness]({build_quickchart_url(config)})](https://github.com/orgs/JalaU-Capstones)")
        lines.append("Repository age distribution.")
        lines.append("")

    return "\n".join(lines)


def replace_between_markers(readme: str, start_marker: str, end_marker: str, new_block: str) -> str:
    """Replace content strictly between markers, preserving the marker comments."""
    start_index = readme.find(start_marker)
    end_index = readme.find(end_marker, start_index)
    if start_index == -1 or end_index == -1:
        raise RuntimeError(f"README marker pair missing: {start_marker} / {end_marker}")
    head = readme[: start_index + len(start_marker)]
    tail = readme[end_index:]
    block = f"\n{new_block.strip()}\n"
    return head + block + tail


def update_readme_file(readme_path: str, projects_md: str, stats_md: str) -> bool:
    """Write the README if the generated sections differ from the current content."""
    readme_file = Path(readme_path)
    if not readme_file.exists():
        raise RuntimeError(f"README file not found: {readme_file}")

    original = readme_file.read_text(encoding="utf-8")
    updated = replace_between_markers(original, MARKER_PROJECTS_START, MARKER_PROJECTS_END, projects_md)
    updated = replace_between_markers(updated, MARKER_STATS_START, MARKER_STATS_END, stats_md)

    if updated == original:
        logger.info("README content already matches; no rewrite required.")
        return False

    readme_file.write_text(updated, encoding="utf-8")
    logger.info("Updated %s", readme_path)
    return True


def _self_test() -> int:
    """Run a small regression check for the language normalizer."""
    bad = {"Python": {"bytes": 100, "count": 2}, "Java": 50, "Weird": "x"}
    expected = {"Python": 100, "Java": 50}
    actual = normalize_languages(bad)
    if actual != expected:
        logger.error("Self-test failed: normalize_languages(%s) returned %s, expected %s", bad, actual, expected)
        return 1
    logger.info("Self-test passed: normalize_languages() returned %s", actual)
    return 0


def main() -> int:
    """Run the README generation workflow."""
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return _self_test()

    org = os.getenv("ORG", DEFAULT_ORG)
    readme_path = os.getenv("README_PATH", DEFAULT_README_PATH)
    token = os.getenv("GH_TOKEN") or os.getenv("ORG_READ_TOKEN")

    if not token:
        logger.error("Missing GH_TOKEN. Export the fine-grained PAT before running the updater.")
        return 1

    session = create_session(token)

    try:
        validate_token(session)
        repos = fetch_org_repos(session, org)

        for repo in repos:
            repo["languages"] = normalize_languages(fetch_repo_languages(session, org, repo["name"]))
            repo["primary_language"] = primary_language(repo)

        repo_stats: list[dict[str, Any]] = []
        pending = 0
        failed = 0
        skipped = 0
        for repo in repos:
            repo["_stats_status"] = "skipped"
            stats = get_repo_stats(session, org, repo)
            if stats is not None:
                repo_stats.append(stats)
                repo["_stats_status"] = "ok"
                continue
            status = repo.get("_stats_status") or "skipped"
            if status == "pending":
                pending += 1
            elif status == "failed":
                failed += 1
            else:
                skipped += 1

        projects_md = build_projects_section(repos)
        stats_md = generate_stats_section(repos, repo_stats)
        update_readme_file(readme_path, projects_md, stats_md)
        logger.info("Stats summary: %s ok, %s pending (202), %s failed, %s skipped.", len(repo_stats), pending, failed, skipped)
        return 0
    except GitHubApiError as exc:
        logger.error(str(exc))
        return 1
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error while updating README: %s", exc)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        raise SystemExit(_self_test())
    raise SystemExit(main())
