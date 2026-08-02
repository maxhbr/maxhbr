#!/usr/bin/env nix-shell
#!nix-shell -I nixpkgs=flake:nixpkgs -i python3 -p "python3.withPackages (ps: [ ps.matplotlib ])"
"""Fetch and visualize GitHub repository stars: growth over time + top repos.

Usage:
    scripts/repo_stars.py all      # fetch + plot
    scripts/repo_stars.py fetch    # only update repo_stars.json
    scripts/repo_stars.py plot     # only re-plot from the cache

Token resolution (same as contributions.py):
    1. $GITHUB_TOKEN or $GH_TOKEN
    2. `pass show Account/github-token` (override with --pass-entry)
A classic PAT without scopes suffices for public repositories.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

API = "https://api.github.com/graphql"

REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER,
                 isFork: false, orderBy: {field: STARGAZERS, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { name stargazerCount url }
    }
  }
}
"""

STARGAZERS_QUERY = """
query($login: String!, $name: String!, $cursor: String) {
  repository(owner: $login, name: $name) {
    stargazers(first: 100, after: $cursor, orderBy: {field: STARRED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      edges { starredAt }
    }
  }
}
"""

VIEWER_QUERY = "query { viewer { login } }"


def api(token: str, query: str, variables: dict, tolerate: bool = False) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "User-Agent": "repo_stars.py",
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
        # A token may lack permission for some fields (e.g. stargazers of other
        # repos with the default Actions GITHUB_TOKEN). Keep partial data if we can.
        if tolerate and payload.get("data") is not None:
            return payload["data"]
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


def paginate(token, query, variables, path, tolerate=False):
    """Yield nodes/edges from a paginated connection reached via `path` (list of keys).

    Stops silently if the connection is missing/None (e.g. a forbidden field).
    """
    cursor = None
    while True:
        conn = api(token, query, {**variables, "cursor": cursor}, tolerate=tolerate)
        for key in path:
            conn = conn.get(key) if isinstance(conn, dict) else None
            if conn is None:
                return
        yield from conn["nodes" if "nodes" in conn else "edges"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        cursor = conn["pageInfo"]["endCursor"]


def fetch(args) -> None:
    token = get_token(args.pass_entry)
    login = args.login or api(token, VIEWER_QUERY, {})["viewer"]["login"]
    if not args.login:
        print(f"fetched login from token: {login}")

    print("listing repositories ...")
    repos = list(paginate(token, REPOS_QUERY, {"login": login}, ["user", "repositories"]))

    out = []
    missing_history = 0
    for r in repos:
        name, stars = r["name"], r["stargazerCount"]
        starred = []
        if stars:
            print(f"  {name}: {stars} stars ...")
            starred = [e["starredAt"] for e in
                       paginate(token, STARGAZERS_QUERY, {"login": login, "name": name},
                                ["repository", "stargazers"], tolerate=True)]
            if not starred:
                missing_history += 1
        out.append({"name": name, "stars": stars, "url": r["url"], "starredAt": starred})

    if missing_history:
        print(f"note: star history was unavailable for {missing_history} repo(s). The default\n"
              f"      Actions GITHUB_TOKEN cannot read other repos' stargazers; set a PAT\n"
              f"      (e.g. the PROFILE_TOKEN secret) to populate the stars-over-time chart.",
              file=sys.stderr)

    payload = {
        "login": login,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repos": out,
    }
    with open(args.cache, "w") as f:
        json.dump(payload, f, indent=1)
    total = sum(r["stars"] for r in out)
    print(f"wrote {args.cache}: {len(out)} repos, {total} stars")


def _cumulative(dates: list[date]):
    dates = sorted(dates)
    return dates, list(range(1, len(dates) + 1))


def plot(args) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    with open(args.cache) as f:
        data = json.load(f)
    login = data["login"]
    repos = [r for r in data["repos"] if r["stars"] > 0]
    repos.sort(key=lambda r: r["stars"], reverse=True)
    if not repos:
        sys.exit("error: no starred repositories in cache; run `fetch` first")

    total = sum(r["stars"] for r in data["repos"])
    all_dates = sorted(date.fromisoformat(s[:10]) for r in repos for s in r["starredAt"])
    top = repos[: args.top]

    plt.rcParams.update({
        "font.family": "sans-serif",
        "axes.edgecolor": "#8b949e", "axes.labelcolor": "#57606a",
        "xtick.color": "#57606a", "ytick.color": "#57606a",
        "figure.facecolor": "white", "axes.facecolor": "white",
    })
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), height_ratios=[1.35, 1],
        gridspec_kw={"hspace": 0.32},
    )
    fig.suptitle(
        f"GitHub stars — @{login}   ({total:,} stars across {len(repos)} starred repos)",
        fontsize=13, fontweight="bold", color="#24292f", x=0.02, ha="left",
    )

    # top panel: cumulative stars over time (total + per-repo for the top ones)
    if all_dates:
        xs, ys = _cumulative(all_dates)
        ax1.fill_between(xs, ys, color="#f0b429", alpha=0.18, linewidth=0)
        ax1.plot(xs, ys, color="#d99e00", linewidth=2, label="all repos")
        cmap = plt.get_cmap("tab10")
        for i, r in enumerate(top):
            rd = [date.fromisoformat(s[:10]) for s in r["starredAt"]]
            if rd:
                rx, ry = _cumulative(rd)
                ax1.plot(rx, ry, linewidth=1.3, color=cmap(i % 10), label=r["name"])
        ax1.legend(loc="upper left", fontsize=8, frameon=False, ncols=2)
        ax1.xaxis.set_major_locator(mdates.YearLocator())
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax1.set_xlim(xs[0], xs[-1])
        ax1.set_ylabel("cumulative stars", fontsize=9)
        ax1.set_ylim(bottom=0)
        ax1.grid(axis="y", color="#e1e4e8", linewidth=0.8)
    else:
        ax1.text(0.5, 0.5, "star history unavailable\n(run with a PAT to populate this chart)",
                 ha="center", va="center", fontsize=11, color="#8b949e", transform=ax1.transAxes)
        ax1.set_xticks([])
        ax1.set_yticks([])
    ax1.spines[["top", "right"]].set_visible(False)
    ax1.tick_params(labelsize=9)

    # bottom panel: top repos by current stars
    bar_repos = top[::-1]
    names = [r["name"] for r in bar_repos]
    stars = [r["stars"] for r in bar_repos]
    bars = ax2.barh(names, stars, color="#40c463", alpha=0.85)
    for bar, val in zip(bars, stars):
        ax2.text(val, bar.get_y() + bar.get_height() / 2, f" {val:,}",
                 va="center", ha="left", fontsize=8, color="#57606a")
    ax2.set_xlabel("stars", fontsize=9)
    ax2.set_xlim(right=max(stars) * 1.12)
    ax2.grid(axis="x", color="#e1e4e8", linewidth=0.8)
    ax2.spines[["top", "right"]].set_visible(False)
    ax2.tick_params(labelsize=9)

    for fmt in ("svg", "png"):
        path = f"{args.out}.{fmt}"
        fig.savefig(path, format=fmt, bbox_inches="tight", dpi=150)
        print(f"wrote {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["fetch", "plot", "all"])
    p.add_argument("--login", help="GitHub login (default: login of the token owner)")
    p.add_argument("--pass-entry", default=os.environ.get("PASS_GITHUB_TOKEN_ENTRY", "Account/github-pat-read"),
                   help="pass entry holding the GitHub token (default: %(default)s, env: PASS_GITHUB_TOKEN_ENTRY)")
    p.add_argument("--top", type=int, default=10, help="number of repos to highlight (default: %(default)s)")
    p.add_argument("--cache", default="repo_stars.json", help="JSON data cache (default: %(default)s)")
    p.add_argument("--out", default="repo-stars", help="output file stem for plots (default: %(default)s)")
    args = p.parse_args()

    if args.command in ("fetch", "all"):
        fetch(args)
    if args.command in ("plot", "all"):
        plot(args)


if __name__ == "__main__":
    main()
