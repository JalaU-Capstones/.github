#!/usr/bin/env python3
"""
generate_readme.py — Reactive README generator for JalaU-Capstones
==================================================================
Fetches the organization's public repositories from the GitHub API and
regenerates every section between the <!-- AUTO:...:START/END --> markers
inside profile/README.md.

Sections regenerated:
  * STATS      -> org KPI line (repos, languages, stars, last update)
  * LANGUAGES  -> language distribution table (byte-level when available)
  * REPOS      -> full repository table w/ descriptions from the GitHub API

Run locally :  python .github/scripts/generate_readme.py
Run in CI   :  see .github/workflows/update-readme.yml
No third-party dependencies — stdlib only.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ORG = "JalaU-Capstones"
README_PATH = os.path.join("profile", "README.md")

# Repos that are infrastructure, not capstone projects
EXCLUDE = {".github"}

# Human-friendly fallbacks for repos without a GitHub description.
# To override any auto description permanently, just add an entry here.
DESCRIPTION_OVERRIDES = {
    "gameapi": "Game management API service — the newest addition to the capstone ecosystem.",
    "capstone-algorithms": "Algorithm study & experimentation sandbox for capstone coursework.",
    "credit-card-module": "Financial benefits tracker — smart recommendations to maximize credit card perks.",
}

# Language -> shields.io badge color
LANG_COLORS = {
    "Java": "ED8B00", "Python": "3776AB", "JavaScript": "F7DF1E",
    "C#": "239120", "Vue": "4FC08D", "TypeScript": "3178C6",
    "HTML": "E34F26", "CSS": "563D7C", "Shell": "89E051",
    "Jupyter Notebook": "DA5B0B", "Go": "00ADD8", "Rust": "DEA584",
    "Kotlin": "7F52FF", "C++": "00599C", "C": "555555",
}
DEFAULT_COLOR = "0077B5"

# Keyword -> (emoji, category) for automatic repo classification
CATEGORIES = [
    ("🎮", "Game Dev",     ["game", "arcade", "snake", "gravity", "mansion", "legion", "chomper", "maze", "pac"]),
    ("🏗️", "API & Backend",["api", "backend", "rest", "service", "cli", "migration"]),
    ("🌐", "Frontend",     ["frontend", "web", "ui", "vite", "vue"]),
    ("🔬", "Scientific",   ["math", "topo", "algorithm", "calculus", "vision", "solver", "chatbot", "pathfinder", "visualizer"]),
    ("💼", "Business Apps",["credit", "noto", "tutor", "task", "finance"]),
]


def gh_get(url: str, token: str | None = None) -> object:
    """Minimal GitHub API GET with pagination-safe single call."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{ORG}-readme-generator",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def badge(label: str, value: str, color: str, logo: str | None = None) -> str:
    logo_part = f"&logo={logo}&logoColor=white" if logo else ""
    return f"![{label}](https://img.shields.io/badge/{label.replace(' ', '_')}-{value}-{color}?style=for-the-badge{logo_part})"


def lang_badge(lang: str) -> str:
    color = LANG_COLORS.get(lang, DEFAULT_COLOR)
    logo = lang.lower().replace("#", "sharp").replace(" ", "")
    slug = lang.replace(" ", "_").replace("#", "%23")
    return f"![{lang}](https://img.shields.io/badge/{slug}-{color}?style=flat-square&logo={logo}&logoColor=white)"


def categorize(name: str, description: str) -> tuple[str, str]:
    text = f"{name} {description}".lower()
    for emoji, cat, keywords in CATEGORIES:
        if any(k in text for k in keywords):
            return emoji, cat
    return "📦", "Other"


def clip(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def replace_block(content: str, marker: str, new_body: str) -> str:
    start, end = f"<!-- AUTO:{marker}:START -->", f"<!-- AUTO:{marker}:END -->"
    pre, rest = content.split(start, 1)
    _, post = rest.split(end, 1)
    return f"{pre}{start}\n{new_body}\n{end}{post}"


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    org = gh_get(f"https://api.github.com/orgs/{ORG}", token)
    repos = gh_get(f"https://api.github.com/orgs/{ORG}/repos?per_page=100&type=public", token)
    repos = [r for r in repos if r["name"] not in EXCLUDE and not r.get("archived")]
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)  # freshest first

    # --- Aggregates -------------------------------------------------------
    langs_bytes: dict[str, int] = {}
    for r in repos:
        primary = r.get("language")
        if primary:
            langs_bytes[primary] = langs_bytes.get(primary, 0) + 1
        try:  # byte-level enrichment (falls back silently on rate limit)
            for lang, nbytes in gh_get(r["languages_url"], token).items():
                langs_bytes[f"{lang}"] = langs_bytes.get(f"{lang}", 0)  # ensure key exists
                langs_bytes[lang] = langs_bytes.get(lang, 0) + nbytes
        except Exception:
            pass
    # If byte data came in, byte counts dominate; rebuild ranking safely:
    byte_total = sum(v for k, v in langs_bytes.items() if v > len(repos) * 10)
    if byte_total:
        ranking = sorted(
            ((k, v) for k, v in langs_bytes.items() if v > len(repos) * 10),
            key=lambda kv: kv[1], reverse=True,
        )
    else:
        total = sum(langs_bytes.values())
        ranking = sorted(langs_bytes.items(), key=lambda kv: kv[1], reverse=True)
        byte_total = total

    total_stars = sum(r["stargazers_count"] for r in repos)
    total_forks = sum(r["forks_count"] for r in repos)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- STATS section -----------------------------------------------------
    stats = (
        f"<div align=\"center\">\n\n"
        f"{badge('Public Repos', org['public_repos'], '4CAF50', 'github')}\n"
        f"{badge('Projects', len(repos), '00D4FF')}\n"
        f"{badge('Languages', len(ranking), 'FF6B35')}\n"
        f"{badge('Stars', total_stars, 'FFD700')}\n"
        f"{badge('Forks', total_forks, '9E9E9E')}\n\n"
        f"*{badge('Last Sync', now.replace(' ', '_').replace(':', '%3A'), '0077B5')}*\n\n"
        f"</div>"
    )

    # --- LANGUAGES section -------------------------------------------------
    lines = ["<div align=\"center\">", "", "| Language | Share |", "|---|---|"]
    for lang, val in ranking[:8]:
        pct = 100.0 * val / byte_total
        bar_len = max(1, round(pct / 5))
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"| {lang_badge(lang)} | `{bar}` **{pct:.1f}%** |")
    lines += ["", "*Byte-level shares pulled live from the GitHub Linguist API.*", "", "</div>"]
    languages = "\n".join(lines)

    # --- REPOS section ------------------------------------------------------
    lines = [
        "<div align=\"center\">",
        "",
        "*Every public capstone repo — description pulled live from the GitHub API. "
        "Sorted by last push. New repos appear here automatically.* 🤖",
        "",
        "</div>",
        "",
        "| | Repository | Description | Language | ⭐ | Last Push |",
        "|---|---|---|---|---|---|",
    ]
    for r in repos:
        desc = DESCRIPTION_OVERRIDES.get(r["name"]) or r.get("description") or "🚧 _No description yet — check the repo README!_"
        desc = clip(str(desc)).replace("|", "\\|")
        emoji, _ = categorize(r["name"], desc)
        lang = lang_badge(r["language"]) if r.get("language") else "—"
        pushed = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00")).strftime("%b %d, %Y")
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).days
        name_cell = f"[{r['name']}]({r['html_url']})" + (" 🆕" if age_days <= 90 else "")
        stars = r["stargazers_count"] if r["stargazers_count"] else "—"
        lines.append(f"| {emoji} | {name_cell} | {desc} | {lang} | {stars} | {pushed} |")
    lines += [
        "",
        f"**{len(repos)} project repositories** · synced from the GitHub API on {now}",
    ]
    repos_md = "\n".join(lines)

    # --- Write back ---------------------------------------------------------
    with open(README_PATH, encoding="utf-8") as f:
        content = f.read()
    content = replace_block(content, "STATS", stats)
    content = replace_block(content, "LANGUAGES", languages)
    content = replace_block(content, "REPOS", repos_md)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"✅ README regenerated: {len(repos)} repos, {len(ranking)} languages @ {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
