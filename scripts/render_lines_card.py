#!/usr/bin/env python3
import argparse
import base64
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = json.loads((ROOT / "scripts" / "fonts.json").read_text())
OUT_DIR = ROOT / "assets"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
USER = os.environ.get("GITHUB_REPOSITORY_OWNER", "")

CODE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs", ".php", ".dart", ".hs", ".pl", ".go", ".rs", ".swift", ".kt", ".css", ".scss",
    ".html", ".sh", ".lua", ".vue", ".svelte", ".m", ".razor", ".cshtml", ".jmm", ".zsh",
    ".fish", ".rb", ".ml", ".scala", ".r",
}
EXCLUDE_PATH = re.compile(
    r"(^|/)(node_modules|obj|bin|dist|build|vendor|venv|\.venv|target|docs?|output|html|latex)/"
    r"|cmake-build|CMakeFiles|demoData|\.min\.|pixmap|xpm", re.I)

WIDTH, HEIGHT = 840, 270
THEMES = {
    "dark": {"headline": "#F0F6FC", "number": "#C9D1D9", "muted": "#8B949E",
             "accent": "#58A6FF", "pulse": "#A5D6FF"},
    "light": {"headline": "#1F2328", "number": "#3D444D", "muted": "#6E7781",
              "accent": "#0969DA", "pulse": "#54AEFF"},
}


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.github.com" + path, data=data, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "lines-card"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def paginate(path):
    items, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        batch = api(f"{path}{sep}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def owned_repos(user):
    try:
        repos = paginate("/user/repos?affiliation=owner")
    except Exception:
        repos = paginate(f"/users/{user}/repos?type=owner")
    return [r["full_name"] for r in repos]


def contributed_repos(user, since_year):
    if not TOKEN:
        return []
    years = " ".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y}-12-31T23:59:59Z") {{'
        f" commitContributionsByRepository(maxRepositories: 100) {{ repository {{ nameWithOwner }} }}"
        f" pullRequestContributionsByRepository(maxRepositories: 100) {{ repository {{ nameWithOwner }} }} }}"
        for y in range(since_year, dt.date.today().year + 1))
    query = ("query($login: String!) { user(login: $login) {"
             " repositoriesContributedTo(first: 100, includeUserRepositories: true,"
             " contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { nodes { nameWithOwner } }"
             f" {years} }} }}")
    res = api("/graphql", {"query": query, "variables": {"login": user}})
    if res.get("errors"):
        print(f"graphql: {res['errors'][0].get('message')}", file=sys.stderr)
    data = (res.get("data") or {}).get("user") or {}
    repos = [n["nameWithOwner"] for n in (data.get("repositoriesContributedTo") or {}).get("nodes", [])]
    for key, coll in data.items():
        if key.startswith("y") and coll:
            for kind in ("commitContributionsByRepository", "pullRequestContributionsByRepository"):
                repos += [c["repository"]["nameWithOwner"] for c in coll[kind]]
    return repos


def author_emails(user, uid, repos):
    emails = {f"{user}@users.noreply.github.com", f"{uid}+{user}@users.noreply.github.com"}
    try:
        return emails | {e["email"] for e in api("/user/emails")}
    except Exception:
        pass
    for repo in repos:
        try:
            emails.update(c["commit"]["author"]["email"]
                          for c in paginate(f"/repos/{repo}/commits?author={user}"))
        except Exception:
            continue
    return emails


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def sync_repo(full_name, workdir, fetch=True):
    path = workdir / (full_name.replace("/", "_") + ".git")
    url = f"https://github.com/{full_name}.git"
    auth = []
    if TOKEN:
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        auth = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    err = None
    for extra in ([], auth) if auth else ([],):
        try:
            if not path.exists():
                git(*extra, "clone", "--quiet", "--bare", url, str(path))
            elif fetch:
                git(*extra, "fetch", "--quiet", "--prune", "origin",
                    "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", cwd=path)
            return path
        except subprocess.CalledProcessError as e:
            err = e
    print(f"skip {full_name}: {err.stderr.strip()[:200]}", file=sys.stderr)
    return None


def collect(repo_paths, emails):
    emails = {e.lower() for e in emails}
    seen = set()
    monthly = collections.Counter()
    total = 0
    repos = set()
    for path in repo_paths:
        out = git("log", "--all", "--no-merges", "--numstat", "--date=short",
                  "--format=@@%H|%ae|%ad", cwd=path)
        sha = month = None
        mine = False
        for line in out.splitlines():
            if line.startswith("@@"):
                sha, email, date = line[2:].split("|", 2)
                mine = email.lower() in emails
                month = date[:7]
                if mine:
                    repos.add(path.name)
                continue
            parts = line.split("\t")
            if not mine or len(parts) != 3 or parts[0] == "-":
                continue
            added, _, fname = parts
            if (sha, fname) in seen:
                continue
            seen.add((sha, fname))
            if os.path.splitext(fname)[1].lower() not in CODE_EXTENSIONS or EXCLUDE_PATH.search(fname):
                continue
            total += int(added)
            monthly[month] += int(added)
    return total, monthly, len(repos)


def month_series(monthly):
    if not monthly:
        return []
    y, m = map(int, min(monthly).split("-"))
    today = dt.date.today()
    series = []
    while (y, m) <= (today.year, today.month):
        series.append(monthly.get(f"{y:04d}-{m:02d}", 0))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return series


def monotone_path(pts):
    n = len(pts)
    f = lambda v: f"{v:.2f}".rstrip("0").rstrip(".")
    if n < 2:
        return f"M{f(pts[0][0])} {f(pts[0][1])}"
    dx = [pts[i + 1][0] - pts[i][0] for i in range(n - 1)]
    s = [(pts[i + 1][1] - pts[i][1]) / dx[i] for i in range(n - 1)]
    m = [s[0]]
    for i in range(1, n - 1):
        if s[i - 1] * s[i] <= 0:
            m.append(0)
        else:
            w1, w2 = 2 * dx[i] + dx[i - 1], dx[i] + 2 * dx[i - 1]
            m.append((w1 + w2) / (w1 / s[i - 1] + w2 / s[i]))
    m.append(s[-1])
    d = f"M{f(pts[0][0])} {f(pts[0][1])}"
    for i in range(n - 1):
        (x0, y0), (x1, y1), h = pts[i], pts[i + 1], dx[i] / 3
        d += (f"C{f(x0 + h)} {f(y0 + m[i] * h)} "
              f"{f(x1 - h)} {f(y1 - m[i + 1] * h)} {f(x1)} {f(y1)}")
    return d


def hairline(series):
    baseline, start, end = 168, 190, WIDTH - 190
    data_start, amplitude = start + 56, 19
    if len(series) < 2:
        return f"M{start} {baseline}H{end}", end, baseline
    smooth = [((series[i - 1] if i else v) + 2 * v + (series[i + 1] if i + 1 < len(series) else v)) / 4
              for i, v in enumerate(series)]
    peak = max(smooth) or 1
    pts = [(start, baseline), (data_start - 22, baseline)]
    for i, v in enumerate(smooth):
        x = data_start + (end - data_start) * i / (len(smooth) - 1)
        pts.append((x, baseline - (v / peak) ** 0.75 * amplitude))
    return monotone_path(pts), pts[-1][0], pts[-1][1]


def render(total, n_repos, series, theme, name):
    t = THEMES[theme]
    path, ex, ey = hairline(series)
    total_text = f"{total:,} lines of code written"
    repos_text = f" · {n_repos} repos"
    faces = "\n".join(
        f"    @font-face {{ font-family: '{fc['family']}'; font-weight: {fc['weight']}; "
        f"src: url(data:font/woff2;base64,{fc['b64']}) format('woff2'); }}"
        for fc in FONTS["faces"])
    return f"""<svg width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
  <title id="title">Hello, I am {name}</title>
  <desc id="desc">Hello, I am {name}. {total_text}{repos_text} across my GitHub history. The divider is every month since my first commit.</desc>
  <defs>
    <linearGradient id="rule" x1="190" y1="0" x2="{WIDTH - 190}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{t['accent']}" stop-opacity="0"/>
      <stop offset=".16" stop-color="{t['accent']}" stop-opacity=".55"/>
      <stop offset=".5" stop-color="{t['accent']}"/>
      <stop offset="1" stop-color="{t['accent']}"/>
    </linearGradient>
    <linearGradient id="pulseGrad" x1="190" y1="0" x2="{WIDTH - 190}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{t['pulse']}" stop-opacity="0"/>
      <stop offset=".16" stop-color="{t['pulse']}" stop-opacity=".6"/>
      <stop offset=".5" stop-color="{t['pulse']}"/>
      <stop offset="1" stop-color="{t['pulse']}"/>
    </linearGradient>
    <filter id="soften" x="-5%" y="-300%" width="110%" height="700%">
      <feGaussianBlur stdDeviation=".6"/>
    </filter>
  </defs>
  <style>
{faces}
    .headline {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; animation: rise .9s cubic-bezier(.2,.6,.2,1) both; }}
    .counter {{ font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; animation: fade .8s ease-out 1.5s both; }}
    .hairline {{ stroke-dasharray: 1 2; animation: draw 2.4s cubic-bezier(.45,0,.2,1) .4s both; }}
    .pulse {{ animation: comet 9s linear 4s infinite; }}
    .now {{ transform-box: fill-box; transform-origin: center; animation: fade .7s ease-out 2.5s both, beat 9s ease-in-out 4s infinite; }}
    @keyframes rise {{ from {{ opacity: 0; transform: translateY(7px); }} to {{ opacity: 1; transform: none; }} }}
    @keyframes draw {{ from {{ stroke-dashoffset: 1.35; }} to {{ stroke-dashoffset: 0; }} }}
    @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    @keyframes comet {{
      0% {{ stroke-dashoffset: .2; animation-timing-function: cubic-bezier(.5,.05,.55,.95); }}
      26% {{ stroke-dashoffset: -.93; animation-timing-function: linear; }}
      30%, 100% {{ stroke-dashoffset: -1.14; }}
    }}
    @keyframes beat {{ 0%, 26% {{ transform: none; }} 31% {{ transform: scale(1.5); }} 43%, 100% {{ transform: none; }} }}
    @media (prefers-reduced-motion: reduce) {{ .headline, .hairline, .pulse, .now, .counter {{ animation: none; }} }}
  </style>
  <text class="headline" x="{WIDTH / 2:g}" y="102" text-anchor="middle" fill="{t['headline']}" font-size="42" font-weight="600" letter-spacing="-.4">Hello, I am {name}</text>
  <path class="hairline" pathLength="1" d="{path}" stroke="url(#rule)" stroke-width="1.5"/>
  <path class="pulse" pathLength="1" d="{path}" stroke="url(#pulseGrad)" stroke-width="1.8" stroke-dasharray=".07 1.35" stroke-dashoffset=".2" filter="url(#soften)" opacity=".85"/>
  <circle class="now" cx="{ex:.2f}" cy="{ey:.2f}" r="2.4" fill="{t['accent']}"/>
  <text class="counter" x="{WIDTH / 2:g}" y="219" text-anchor="middle" font-size="14.5" letter-spacing=".2" ><tspan fill="{t['number']}">{total_text}</tspan><tspan fill="{t['muted']}">{repos_text}</tspan></text>
</svg>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=str(ROOT / ".cache" / "repos"))
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if not USER:
        sys.exit("GITHUB_REPOSITORY_OWNER is not set")
    try:
        profile = api(f"/users/{USER}")
        repos = owned_repos(USER) + contributed_repos(USER, int(profile["created_at"][:4]))
    except Exception as e:
        sys.exit(f"GitHub API failed ({e}); keeping existing cards")
    repos = list(dict.fromkeys(repos))
    print(f"{len(repos)} candidate repos")
    emails = author_emails(USER, profile["id"], repos)
    name = (profile.get("name") or USER).split()[0]
    paths = [p for p in (sync_repo(r, workdir, not args.no_fetch) for r in repos) if p]
    if not paths:
        sys.exit("no repositories could be read; keeping existing cards")
    total, monthly, n_repos = collect(paths, emails)
    series = month_series(monthly)
    print(f"{n_repos} repos with my commits, {total:,} lines, {len(series)} months")
    OUT_DIR.mkdir(exist_ok=True)
    for theme in THEMES:
        (OUT_DIR / f"lines-{theme}.svg").write_text(render(total, n_repos, series, theme, name))


if __name__ == "__main__":
    main()
