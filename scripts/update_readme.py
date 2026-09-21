#!/usr/bin/env python3
"""
JalaU-Capstones Organization README Updater
============================================

Fetch all public repositories of a GitHub organization, extract metadata and
language breakdowns, and regenerate the dynamic sections of the organization's
profile README (``profile/README.md``).

The generated content replaces whatever lives between these markers:

    <!-- PROJECTS_START --> ... <!-- PROJECTS_END -->
    <!-- STATS_START -->    ... <!-- STATS_END -->

Repositories listed in ``EXCLUDED_REPOS`` (e.g. ``.github``) and forks are
ignored. Repositories without a description simply omit the description line.

Environment variables:
    GH_TOKEN     GitHub token with ``Metadata: Read`` on the org's repositories.
    ORG          GitHub organization login (default: ``JalaU-Capstones``).
    README_PATH  Path to the README to update (default: ``profile/README.md``).

Exit codes:
    0  Success (README updated or unchanged).
    1  Configuration or API error.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("update_readme")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api.github.com"
DEFAULT_ORG = "JalaU-Capstones"
DEFAULT_README_PATH = "profile/README.md"

EXCLUDED_REPOS: set[str] = {".github"}

MARKER_PROJECTS_START = "<!-- PROJECTS_START -->"
MARKER_PROJECTS_END = "<!-- PROJECTS_END -->"
MARKER_STATS_START = "<!-- STATS_START -->"
MARKER_STATS_END = "<!-- STATS_END -->"

# ---------------------------------------------------------------------------
# Categories: define order, heading and grid width for each section
# ---------------------------------------------------------------------------

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "enterprise": {
        "heading": "### 🏆 **Enterprise Architecture Showcase**",
        "columns": 2,
        "default_emoji": "🏗️",
    },
    "scientific": {
        "heading": "### 🔬 **Scientific & Analytical Computing**",
        "columns": 2,
        "default_emoji": "🔬",
    },
    "games": {
        "heading": "### 🎮 **Game Development Portfolio**",
        "columns": 3,
        "default_emoji": "🎮",
    },
    "business": {
        "heading": "### 💼 **Business Applications & Productivity**",
        "columns": 2,
        "default_emoji": "💼",
    },
    "other": {
        "heading": "### 📦 **Other Projects**",
        "columns": 2,
        "default_emoji": "📦",
    },
}

# ---------------------------------------------------------------------------
# Per-repository presentation metadata.
#
# Only curated repos need an entry here. Any repository that is NOT listed
# will be auto-classified into the ``other`` category using only data from
# the GitHub API (description, languages).
#
# Keys:
#   category    One of the keys in CATEGORIES.
#   emoji       Emoji shown before the repo title.
#   title       Human-readable title (falls back to repo name).
#   subtitle    Bold tagline under the title (optional).
#   highlights  List of bullet points (optional).
#   stack       List of stack tags shown as ``code`` (falls back to languages).
# ---------------------------------------------------------------------------

REPO_METADATA: Dict[str, Dict[str, Any]] = {
    # ----- Enterprise / API -------------------------------------------------
    "cis-phase2-crowdsourced-ideation": {
        "category": "enterprise",
        "emoji": "🏗️",
        "title": "CIS Phase 2: Crowdsourced Ideation",
        "subtitle": "Modern .NET 8 Minimal API",
        "highlights": [
            "📐 Vertical Slice Architecture (ADR-004)",
            "🗄️ EF Core 8 with best practices (ADR-003)",
            "🧪 xUnit + FluentAssertions + Moq (ADR-005)",
            "🚀 Production-ready API design",
        ],
        "stack": ["C#", ".NET 8", "EF Core", "Minimal APIs", "Vertical Slice"],
    },
    "CIS-Fase1-User-Management-API": {
        "category": "enterprise",
        "emoji": "🔧",
        "title": "CIS Phase 1: User Management API",
        "subtitle": "Legacy Integration REST API",
        "highlights": [
            "🔗 RESTful API with legacy system coexistence",
            "👥 Employee user management domain",
            "🔄 Migration path from CLI to API architecture",
            "📊 Enterprise-grade error handling",
        ],
        "stack": ["Java", "Spring Boot", "REST API", "JPA"],
    },
    "userscli": {
        "category": "enterprise",
        "emoji": "🎓",
        "title": "Users CLI",
        "subtitle": "Software Development 3 Companion",
        "highlights": [
            "💻 CLI interface design patterns",
            "🔄 Complete CRUD functionality",
            "🏗️ Clean separation of concerns",
            "📚 Educational reference implementation",
        ],
        "stack": ["Java", "Console App", "OOP Principles"],
    },
    # ----- Scientific -------------------------------------------------------
    "topovision": {
        "category": "scientific",
        "emoji": "🛰️",
        "title": "TopoVision",
        "subtitle": "3D Topographic Analysis System",
        "highlights": [
            "📦 Available on PyPI (`pip install topovision`)",
            "🎥 Real-time video capture & OpenCV processing",
            "🧮 Mathematical gradient computation",
            "🗺️ Interactive 3D terrain visualizations",
        ],
        "stack": ["Python", "OpenCV", "NumPy", "Matplotlib", "Tkinter"],
    },
    "math-solver-chatbot": {
        "category": "scientific",
        "emoji": "🧮",
        "title": "Math Solver Chatbot",
        "subtitle": "AI-Powered Educational Tool",
        "highlights": [
            "🤖 AI-powered step-by-step solutions",
            "🎓 Educational focus with explanations",
            "🌐 Web-based interface",
            "📊 Multiple mathematical domains",
        ],
        "stack": ["JavaScript", "AI/ML", "Web App"],
    },
    # ----- Games ------------------------------------------------------------
    "arcade-maze-chomper": {
        "category": "games",
        "emoji": "🎮",
        "title": "Arcade Maze Chomper",
        "subtitle": "Cross-Platform Game Development",
        "highlights": [
            "🎯 Cross-platform desktop (Windows/Linux/Mac)",
            "🎨 Avalonia UI modern interface",
            "🧠 Game loop architecture & state management",
        ],
        "stack": ["C#", ".NET 9", "Avalonia UI", "Game Dev"],
    },
    "march-of-the-Legion": {
        "category": "games",
        "emoji": "⚔️",
        "title": "March of the Legion",
        "subtitle": "Algorithm Visualization",
        "stack": ["Java", "JavaFX", "Algorithms", "SOLID"],
    },
    "El_Secreto_de_la_Mansion_Oscura": {
        "category": "games",
        "emoji": "🏚️",
        "title": "El Secreto de la Mansión Oscura",
        "subtitle": "Mystery Adventure Game",
        "stack": ["Python", "Pygame", "Z3-Solver", "Game Logic"],
    },
    "gravity-shift": {
        "category": "games",
        "emoji": "🌀",
        "title": "Gravity Shift",
        "subtitle": "Physics-Based Platformer",
        "stack": ["Python", "Physics Engine", "Pygame"],
    },
    "snake-lineal": {
        "category": "games",
        "emoji": "🐍",
        "title": "Snake Lineal",
        "subtitle": "Mathematical Game Dev",
        "stack": ["JavaScript", "Linear Algebra", "Canvas API"],
    },
    "GameOfLife": {
        "category": "games",
        "emoji": "🧬",
        "title": "Game of Life",
        "subtitle": "Cellular Automata Simulation",
        "stack": ["Java", "Cellular Automata", "Concurrency"],
    },
    # ----- Business ---------------------------------------------------------
    "credit-card-module": {
        "category": "business",
        "emoji": "💳",
        "title": "Credit Card Module",
        "subtitle": "Financial Benefits Tracker",
        "highlights": [
            "💰 Benefit optimization algorithms",
            "📈 Spending analytics & visualizations",
            "🎯 Personalized recommendations",
        ],
        "stack": ["Java", "JavaFX", "SQLite", "Analytics"],
    },
    "NotoFlow": {
        "category": "business",
        "emoji": "📝",
        "title": "NotoFlow",
        "subtitle": "Real-Time Task Management",
        "highlights": [
            "☁️ Firebase Firestore real-time sync",
            "🔐 Authentication & security rules",
            "📱 Responsive JavaFX interface",
        ],
        "stack": ["Java", "JavaFX", "Firebase", "Firestore"],
    },
    "unitutor": {
        "category": "business",
        "emoji": "🎓",
        "title": "UniTutor",
        "subtitle": "Academic Tutoring Platform",
        "stack": ["Java", "MySQL", "Console App", "Agile"],
    },
}

# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------

def create_session(token: str) -> requests.Session:
    """Build a ``requests.Session`` with auth, retries and sane timeouts."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "JalaU-Capstones-README-Updater",
        }
    )
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def get_org_repos(session: requests.Session, org: str) -> List[Dict[str, Any]]:
    """Return all public, non-fork repositories of ``org`` (paginated)."""
    repos: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = f"{API_BASE}/orgs/{org}/repos"
        params = {"type": "public", "per_page": 100, "page": page}
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        repos.extend(chunk)
        page += 1

    filtered = [
        r
        for r in repos
        if r["name"] not in EXCLUDED_REPOS and not r.get("fork", False)
    ]
    logger.info("Fetched %d repositories (%d after filtering).", len(repos), len(filtered))
    return filtered


def get_repo_languages(
    session: requests.Session, org: str, repo_name: str
) -> Dict[str, int]:
    """Return the language-byte breakdown for a single repository."""
    url = f"{API_BASE}/repos/{org}/{repo_name}/languages"
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        logger.warning("Languages not available for %s/%s (HTTP %s).",
                       org, repo_name, resp.status_code)
        return {}
    return resp.json()


def enrich_with_languages(
    session: requests.Session, org: str, repos: List[Dict[str, Any]]
) -> None:
    """Attach a ``languages`` dict to each repo (mutates in place)."""
    for repo in repos:
        repo["languages"] = get_repo_languages(session, org, repo["name"])

# ---------------------------------------------------------------------------
# Data processing helpers
# ---------------------------------------------------------------------------

def humanize_repo_name(name: str) -> str:
    """Turn ``my-cool-repo`` into ``My Cool Repo`` for display."""
    cleaned = re.sub(r"[-_]+", " ", name).strip()
    return " ".join(word.capitalize() for word in cleaned.split())


def top_languages(languages: Dict[str, int], limit: int = 4) -> List[str]:
    """Return the top N language names sorted by bytes descending."""
    if not languages:
        return []
    ordered = sorted(languages.items(), key=lambda kv: kv[1], reverse=True)
    return [lang for lang, _ in ordered[:limit]]


def format_updated_date(iso_ts: Optional[str]) -> str:
    """Format an ISO timestamp as ``YYYY-MM-DD`` (or ``—`` if missing)."""
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return iso_ts[:10] if len(iso_ts) >= 10 else iso_ts

# ---------------------------------------------------------------------------
# HTML card generation
# ---------------------------------------------------------------------------

def build_card_cell(repo: Dict[str, Any]) -> str:
    """Render a single repository as an HTML table cell (card)."""
    name = repo["name"]
    meta = REPO_METADATA.get(name, {})

    emoji = meta.get("emoji", "📦")
    title = meta.get("title") or humanize_repo_name(name)
    url = repo["html_url"]
    subtitle: Optional[str] = meta.get("subtitle")
    description: Optional[str] = (repo.get("description") or "").strip() or None
    highlights: List[str] = meta.get("highlights", []) or []
    stack: List[str] = meta.get("stack") or top_languages(repo.get("languages", {}))

    parts: List[str] = [f"#### {emoji} [{title}]({url})"]

    if subtitle:
        parts.append(f"**{subtitle}**")

    # Only include description if the repo actually has one
    if description:
        parts.append(description)

    if highlights:
        parts.append("**Highlights:**")
        parts.extend(f"- {item}" for item in highlights)

    if stack:
        rendered_stack = " ".join(f"`{tag}`" for tag in stack)
        parts.append(f"**Stack:** {rendered_stack}")

    body = "\n\n".join(parts)
    return f"<td width=\"{100 // 1}%\">\n\n{body}\n\n</td>"


def build_category_block(category_key: str, repos: List[Dict[str, Any]]) -> str:
    """Render a category heading followed by a responsive HTML grid."""
    category = CATEGORIES[category_key]
    heading = category["heading"]
    columns = max(1, int(category["columns"]))

    if not repos:
        return ""

    # Sort repos: curated order first (by title), then auto ones alphabetically
    def sort_key(r: Dict[str, Any]) -> str:
        meta = REPO_METADATA.get(r["name"], {})
        return (meta.get("title") or r["name"]).lower()

    ordered = sorted(repos, key=sort_key)

    rows_md: List[str] = []
    for i in range(0, len(ordered), columns):
        chunk = ordered[i : i + columns]
        cells = "\n".join(build_card_cell(r) for r in chunk)
        # Pad the last row so the table stays balanced
        missing = columns - len(chunk)
        if missing:
            filler = "\n".join(["<td width=\"33%\"></td>"] * missing)
            cells = f"{cells}\n{filler}"
        rows_md.append(f"<tr>\n{cells}\n</tr>")

    table = "<table>\n" + "\n".join(rows_md) + "\n</table>"
    return f"{heading}\n\n{table}"

# ---------------------------------------------------------------------------
# Stats & projects sections
# ---------------------------------------------------------------------------

def group_repos_by_category(
    repos: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket repos by their category (auto-assigned to ``other`` if unknown)."""
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in CATEGORIES}
    for repo in repos:
        meta = REPO_METADATA.get(repo["name"], {})
        category = meta.get("category", "other")
        if category not in grouped:
            category = "other"
        grouped[category].append(repo)
    return grouped


def generate_projects_section(repos: List[Dict[str, Any]]) -> str:
    """Build the full ``Featured Projects`` block."""
    grouped = group_repos_by_category(repos)
    blocks: List[str] = []
    for key in CATEGORIES:
        block = build_category_block(key, grouped[key])
        if block:
            blocks.append(block)
    if not blocks:
        return "_No projects available yet._"
    return "\n\n".join(blocks)


def generate_stats_section(repos: List[Dict[str, Any]]) -> str:
    """Build a markdown summary table with aggregated statistics."""
    total_repos = len(repos)
    total_stars = sum(r.get("stargazers_count", 0) for r in repos)
    total_forks = sum(r.get("forks_count", 0) for r in repos)
    total_issues = sum(r.get("open_issues_count", 0) for r in repos)

    # Language aggregation across all repos
    lang_bytes: Dict[str, int] = {}
    for repo in repos:
        for lang, count in repo.get("languages", {}).items():
            lang_bytes[lang] = lang_bytes.get(lang, 0) + count
    top_langs = top_languages(lang_bytes, limit=5)
    top_langs_str = ", ".join(top_langs) if top_langs else "—"

    # Category counts
    grouped = group_repos_by_category(repos)
    category_labels = {
        "enterprise": "🏗️ Enterprise / API",
        "scientific": "🔬 Scientific",
        "games": "🎮 Game Dev",
        "business": "💼 Business Apps",
        "other": "📦 Other",
    }

    lines: List[str] = []
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| **Total repositories** | {total_repos} |")
    lines.append(f"| **Total stars** | ⭐ {total_stars} |")
    lines.append(f"| **Total forks** | 🍴 {total_forks} |")
    lines.append(f"| **Open issues** | 🐛 {total_issues} |")
    lines.append(f"| **Top languages** | {top_langs_str} |")
    lines.append(f"| **Last updated** | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} |")
    lines.append("")
    lines.append("**Projects per category**")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    for key, label in category_labels.items():
        lines.append(f"| {label} | {len(grouped.get(key, []))} |")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# README manipulation
# ---------------------------------------------------------------------------

def replace_between_markers(
    text: str, start_marker: str, end_marker: str, new_content: str
) -> str:
    """Replace content between two HTML comment markers (idempotent)."""
    pattern = re.compile(
        rf"({re.escape(start_marker)})(.*?)({re.escape(end_marker)})",
        re.DOTALL,
    )
    if not pattern.search(text):
        logger.warning("Markers not found: %s ... %s", start_marker, end_marker)
        return text
    return pattern.sub(
        lambda m: f"{m.group(1)}\n{new_content}\n{m.group(3)}", text
    )


def update_readme_file(
    readme_path: str, projects_md: str, stats_md: str
) -> bool:
    """Update the README in place. Return True if the file changed."""
    try:
        with open(readme_path, "r", encoding="utf-8") as fh:
            original = fh.read()
    except FileNotFoundError:
        logger.error("README not found at %s", readme_path)
        sys.exit(1)

    updated = replace_between_markers(
        original, MARKER_PROJECTS_START, MARKER_PROJECTS_END, projects_md
    )
    updated = replace_between_markers(
        updated, MARKER_STATS_START, MARKER_STATS_END, stats_md
    )

    if updated == original:
        logger.info("README unchanged — nothing to write.")
        return False

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(updated)
    logger.info("README updated at %s", readme_path)
    return True

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("GH_TOKEN")
    org = os.environ.get("ORG", DEFAULT_ORG)
    readme_path = os.environ.get("README_PATH", DEFAULT_README_PATH)

    if not token:
        logger.error("GH_TOKEN environment variable is not set.")
        sys.exit(1)

    logger.info("Starting README update for org=%s, path=%s", org, readme_path)

    session = create_session(token)

    try:
        repos = get_org_repos(session, org)
        enrich_with_languages(session, org, repos)
    except requests.HTTPError as exc:
        logger.error("GitHub API request failed: %s", exc)
        sys.exit(1)

    projects_md = generate_projects_section(repos)
    stats_md = generate_stats_section(repos)

    update_readme_file(readme_path, projects_md, stats_md)


if __name__ == "__main__":
    main()
