#!/usr/bin/env nix-shell
#!nix-shell -I nixpkgs=flake:nixpkgs -i python3 -p "python3.withPackages (ps: [ ps.matplotlib ])"
"""Fetch and visualize GitHub contributions over time.

Usage:
    scripts/contributions.py all                 # fetch + plot
    scripts/contributions.py fetch               # only update contributions.json
    scripts/contributions.py plot                # only re-plot from the cache

Needs a GitHub token, resolved in this order:
    1. $GITHUB_TOKEN or $GH_TOKEN
    2. `pass show Account/github-token` (override with --pass-entry)
A classic PAT without any scopes suffices to read your own *public*
contribution data; add `read:user` to also include private contributions.

Without nix, `pip install matplotlib` + `python3 scripts/contributions.py ...`
works the same.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.github.com/graphql"
GREEN = "#40c463"  # GitHub contribution green

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    createdAt
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""

VIEWER_QUERY = "query { viewer { login } }"


def api(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "User-Agent": "contributions.py",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"error: GitHub API returned HTTP {e.code}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach api.github.com: {e.reason}")
    if "errors" in payload:
        sys.exit(f"error: GitHub API: {json.dumps(payload['errors'], indent=2)[:800]}")
    return payload["data"]


def get_token(entry: str) -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(var):
            return token
    try:
        r = subprocess.run(["pass", "show", "-p", entry], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        sys.exit(f"error: set GITHUB_TOKEN or make a token available via `pass insert {entry}` (`pass` not on PATH)")
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(f"error: set GITHUB_TOKEN or fix the pass entry: `pass show {entry}` failed: {r.stderr.strip()[:200]}")
    print(f"using token from `pass show {entry}`")
    return r.stdout.splitlines()[0].strip()


def fetch(args) -> None:
    token = get_token(args.pass_entry)

    login = args.login
    if not login:
        login = api(token, VIEWER_QUERY, {})["viewer"]["login"]
        print(f"fetched login from token: {login}")

    # one query per calendar year (API limit: <= 1 year per range)
    now = datetime.now(timezone.utc)
    days: dict[str, int] = {}
    if os.path.exists(args.cache):  # resume from cache
        with open(args.cache) as f:
            days = {d["date"]: d["count"] for d in json.load(f)["days"]}

    created: str | None = None
    for year in range(args.since or now.year - 20, now.year + 1):
        start = date(year, 1, 1)
        end = min(date(year + 1, 1, 1), now.date() + timedelta(days=1))
        if start >= end:
            continue
        print(f"fetching {year} ...")
        user = api(
            token, QUERY, {"login": login, "from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T00:00:00Z"}
        )["user"]
        if user is None:
            sys.exit(f"error: no such GitHub user: {login!r}")
        created = user["createdAt"] or created
        for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = day["contributionCount"]

    days = {d: c for d, c in days.items() if d[:4] >= str(args.since or 0)}
    out = {
        "login": login,
        "fetchedAt": now.isoformat(timespec="seconds"),
        "createdAt": (created or "")[:4],
        "days": [{"date": d, "count": c} for d, c in sorted(days.items())],
    }
    with open(args.cache, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {args.cache}: {len(days)} days, {sum(days.values())} contributions")


def streaks(counts: list[int]) -> tuple[int, int]:
    longest = cur = 0
    for c in counts:
        cur = cur + 1 if c else 0
        longest = max(longest, cur)
    # current streak: walk back from the end, tolerating a quiet today
    i, current = len(counts) - 1, 0
    if counts and counts[-1] == 0:
        i -= 1
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    return longest, current


def plot(args) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    with open(args.cache) as f:
        data = json.load(f)
    login = data["login"]
    days = [(date.fromisoformat(d["date"]), d["count"]) for d in data["days"]]
    dates = [d for d, _ in days]
    counts = [c for _, c in days]
    if not dates:
        sys.exit("error: no data in cache; run `fetch` first")

    window = 28
    smooth = [sum(counts[max(0, i - window + 1) : i + 1]) / window for i in range(len(counts))]
    years = list(range(dates[0].year, dates[-1].year + 1))
    yearly = [sum(c for d, c in days if d.year == y) for y in years]
    total = sum(counts)
    active = sum(1 for c in counts if c)
    longest, current = streaks(counts)

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "axes.edgecolor": "#8b949e",
            "axes.labelcolor": "#57606a",
            "xtick.color": "#57606a",
            "ytick.color": "#57606a",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 5.5), height_ratios=[3, 1.4], sharex=False,
        gridspec_kw={"hspace": 0.45},
    )

    fig.suptitle(
        f"GitHub contributions over time — @{login}   "
        f"({total:,} total on {active:,} active days · longest streak {longest} days · current streak {current} days)",
        fontsize=12, fontweight="bold", color="#24292f", x=0.02, ha="left",
    )

    # top: smoothed daily activity
    ax1.fill_between(dates, smooth, color=GREEN, alpha=0.25, linewidth=0)
    ax1.plot(dates, smooth, color=GREEN, linewidth=1.5)
    ax1.set_ylabel(f"contributions/day\n({window}d avg)", fontsize=9)
    ax1.set_xlim(dates[0], dates[-1])
    ax1.set_ylim(bottom=0)
    ax1.xaxis.set_major_locator(mdates.YearLocator())
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.grid(axis="y", color="#e1e4e8", linewidth=0.8)
    ax1.spines[["top", "right"]].set_visible(False)
    ax1.tick_params(labelsize=9)

    # bottom: yearly totals
    bars = ax2.bar(years, yearly, color=GREEN, alpha=0.8, width=0.7)
    for bar, val in zip(bars, yearly):
        ax2.text(bar.get_x() + bar.get_width() / 2, val, f"{val:,}".replace(",", " "),
                 ha="center", va="bottom", fontsize=8, color="#57606a")
    ax2.set_ylim(top=max(yearly) * 1.15)
    ax2.set_ylabel("per year", fontsize=9)
    ax2.set_xticks(years)
    ax2.set_xticklabels([str(y) for y in years], rotation=45 if len(years) > 12 else 0)
    ax2.yaxis.set_major_locator(MaxNLocator(3))
    ax2.grid(axis="y", color="#e1e4e8", linewidth=0.8)
    ax2.spines[["top", "right"]].set_visible(False)
    ax2.tick_params(labelsize=9)
    ax2.set_xlim(min(years) - 0.6, max(years) + 0.6)

    out_stem = args.out
    for fmt in ("svg", "png"):
        path = f"{out_stem}.{fmt}"
        fig.savefig(path, format=fmt, bbox_inches="tight", dpi=150)
        print(f"wrote {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["fetch", "plot", "all"])
    p.add_argument("--login", help="GitHub login (default: login of the token owner)")
    p.add_argument("--pass-entry", default=os.environ.get("PASS_GITHUB_TOKEN_ENTRY", "Account/github-pat-read"),
                   help="pass entry holding the GitHub token (default: %(default)s, env: PASS_GITHUB_TOKEN_ENTRY)")
    p.add_argument("--since", type=int, metavar="YEAR", help="ignore contributions before this year")
    p.add_argument("--cache", default="contributions.json", help="JSON data cache (default: %(default)s)")
    p.add_argument("--out", default="contributions", help="output file stem for plots (default: %(default)s)")
    args = p.parse_args()

    if args.command in ("fetch", "all"):
        fetch(args)
    if args.command in ("plot", "all"):
        plot(args)


if __name__ == "__main__":
    main()
