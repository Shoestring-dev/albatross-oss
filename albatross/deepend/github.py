"""GitHub (Deep End): the public repos behind the tokens we watch. AI-agent and dev-led memecoins move on GitHub
activity, so for every hot or warm token (deepend/watch.py) whose links include a github.com URL this follows the
repo's public metadata over time and turns it into plain-English flags for the dossier (section()).

Hard rule: never download a repo. Metadata only, through the REST API: every request goes
through url(), which builds api.github.com URLs from an allowlist of metadata paths (the repo, its commit and
contributor lists, releases, stargazers, an account's repo list, the stats endpoints, our own quota) and raises
Forbidden on anything else: no clone, no archive (tarball, zipball), no file (contents, readme, git blobs and trees,
raw.githubusercontent.com, a single commit with its patches), no release asset. Redirects are never followed blindly:
a Location goes through url() again.

Links: Jupiter's stored token object (chain.jupiter_intel_many: the on-chain metadata, so pump.fun's own links),
discovery's DexScreener links (de_watch) and, every LINKS_EVERY per token, DexScreener's token info with all its
websites and socials (discovery keeps only one website, and drops one labelled "GitHub"). A link to a repo, or to a
file in one, names that repo (the file is never fetched); a link to an account stands for its most-starred public
repo (its repo list: metadata only). Only link fields are read: Jupiter's logos live on raw.githubusercontent.com.

Snapshots (job deep_github, every 30 minutes): a hot token's repo every run, a warm one's once its last snapshot is
2 hours old. Each reads the repo object (stars, forks, watchers, open issues, last push, archived/disabled, license,
language, topics, fork and parent); commits in the last 7 and 30 days (the commit list with `since`: one page of 100,
and per_page=1's last page when there are more), contributors (anon=1, per_page=1's last page) and the latest
release are re-read after a push (at most hourly) or every ACTIVITY_EVERY, and carried forward in between. Changes
over 24 h and 7 d come from our own snapshots.

Limits: without GITHUB_TOKEN (data.keys, optional) GitHub allows 60 requests an hour per IP, with it 5,000. Every
response's X-RateLimit-Remaining and -Reset are read; we use at most half of each hour's limit, split over the hour's
two runs (15 requests a run unauthenticated), and a rate-limit answer (403 or 429) holds every request until its
reset or Retry-After. Requests per UTC day are counted in de_api_usage (provider 'github'). Measured 2026-10-01 on
staging: a new repo costs 4 requests (repo, commits, contributors, releases; 5 past 100 commits a month), a refresh
without a push 1.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit

import asyncpg
import httpx
import orjson

from albatross import db
from albatross.deepend import watch
from albatross.deepend.cfg import env_str
from albatross.http import client
from albatross.venues import dexscreener

log = logging.getLogger(__name__)

JOB = "deep_github"
CADENCE_S = 1800
API = "https://api.github.com"
HOT_EVERY = timedelta(minutes=25)       # a hot token's repo: every run (the job runs every 30 minutes)
WARM_EVERY = timedelta(hours=2)
ACTIVITY_EVERY = timedelta(hours=6)     # commits, contributors and the latest release, unless pushed since
ACTIVITY_MIN = timedelta(hours=1)       # ... and after a push at most this often (a busy repo pushes all day)
LINKS_EVERY = timedelta(hours=6)        # a token's DexScreener links, re-read this often
RESOLVE_EVERY = timedelta(hours=24)     # an account link's most-starred repo, and a dead link, are asked again daily
FRESH = timedelta(hours=3)
SHARE = 0.5                             # of each hour's rate limit, at most
MIN_GAP_S = 0.5                         # one request at a time, as GitHub asks
MAX_PAGES = 3                           # an account's repo list: its 300 latest-pushed repos at most
MAX_REDIRECTS = 2
DAY = timedelta(days=1)
YOUNG = timedelta(days=7)               # a repo created this little before the token's launch was made for the coin
ESTABLISHED = timedelta(days=180)
KNOWN_STARS = 1000                      # a parent (or a linked repo) this starred is a known project
ABANDONED_DAYS = 90
STAR_JUMP = 20                          # stars in 24 h worth a flag (and at least a quarter more than the day before)


# ─── the gate: metadata only ─────────────────────────────────────────────────
class Forbidden(ValueError):
    """A request outside the metadata allowlist: a bug to fix, never something to work around."""


class Paced(RuntimeError):
    """Our share of GitHub's hour is used, or GitHub said wait: the run stops here."""


# Every path this module may request. None of them returns a file, an archive, a diff or a release asset.
PATHS = {
    "rate_limit": "/rate_limit",                                 # our own quota; free
    "repo": "/repos/{owner}/{repo}",
    "repo_id": "/repositories/{id}",                             # by GitHub's id: where a renamed repo redirects
    "commits": "/repos/{owner}/{repo}/commits",                  # the list (shas, dates, messages), never one commit
    "contributors": "/repos/{owner}/{repo}/contributors",
    "releases": "/repos/{owner}/{repo}/releases",                # tags and dates; the assets are never fetched
    "stargazers": "/repos/{owner}/{repo}/stargazers",
    "user_repos": "/users/{owner}/repos",
    "org_repos": "/orgs/{owner}/repos",
    "commit_activity": "/repos/{owner}/{repo}/stats/commit_activity",
    "participation": "/repos/{owner}/{repo}/stats/participation",
    "contributor_stats": "/repos/{owner}/{repo}/stats/contributors",
}
PARAMS = {"per_page", "page", "since", "until", "sort", "direction", "type", "anon"}
OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
NAME = r"(?!\.\.?(?:/|$))[A-Za-z0-9._-]{1,100}"
_PART = {"owner": OWNER, "repo": NAME, "id": r"[0-9]{1,12}"}
_ALLOWED = [re.compile("^" + re.sub(r"\{(\w+)\}", lambda m: _PART[m.group(1)], p) + "$") for p in PATHS.values()]


def url(path: str, params: dict | None = None) -> str:
    """The only way a GitHub URL is made here: an allowlisted metadata path on api.github.com, else Forbidden."""
    if not isinstance(path, str) or not any(rx.match(path) for rx in _ALLOWED):
        raise Forbidden(f"not an allowlisted GitHub metadata path: {path!r}")
    if extra := set(params or {}) - PARAMS:
        raise Forbidden(f"parameters outside the allowlist: {sorted(extra)}")
    return API + path


def path(name: str, **parts: str) -> str:
    """A path from its template (url() still checks every request)."""
    return PATHS[name].format(**parts)


def redirect(location: str) -> tuple[str, dict]:
    """(url, params) for a redirect GitHub sent, through the same gate."""
    u = urlsplit(location)
    if u.scheme != "https" or u.hostname != "api.github.com":
        raise Forbidden(f"redirect off the metadata API: {location!r}")
    params = dict(parse_qsl(u.query))
    return url(u.path, params), params


# ─── quota ───────────────────────────────────────────────────────────────────
@dataclass
class Quota:
    """GitHub's hourly window as its headers describe it, and how much of it is ours."""
    limit: int | None = None            # requests an hour: 60 without a token, 5,000 with one
    remaining: int | None = None
    reset: float = 0.0                  # epoch seconds
    hold_until: float = 0.0             # a rate-limit answer: nothing before this

    def see(self, headers) -> None:
        if (headers.get("x-ratelimit-resource") or "core") != "core":
            return
        try:
            limit, remaining = int(headers["x-ratelimit-limit"]), int(headers["x-ratelimit-remaining"])
            reset = float(headers["x-ratelimit-reset"])
        except (KeyError, TypeError, ValueError):
            return
        self.limit, self.remaining, self.reset = limit, remaining, reset

    def room(self, now: float) -> int:
        """Requests we may still make this hour, leaving the other half unused (1 when unknown: its answer tells)."""
        if now < self.hold_until:
            return 0
        if self.limit is None or self.remaining is None:
            return 1
        if now >= self.reset:
            return int(self.limit * SHARE)
        return max(0, self.remaining - (self.limit - int(self.limit * SHARE)))


def per_run(limit: int) -> int:
    """A run's share: half the hour's limit, over the runs in an hour."""
    return max(1, int(limit * SHARE * CADENCE_S / 3600))


def hold(status: int, headers, body, now: float) -> float | None:
    """Until when GitHub wants us to stop (a primary or secondary rate limit), or None for any other refusal."""
    if status not in (403, 429):
        return None
    retry = headers.get("retry-after") or ""
    if retry.isdigit():
        return now + max(1, int(retry))
    if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset"):
        return float(headers["x-ratelimit-reset"])
    message = str(body.get("message") if isinstance(body, dict) else "").lower()
    if status == 429 or "rate limit" in message or "abuse" in message:
        return now + 60                 # a secondary limit without Retry-After: GitHub says wait at least a minute
    return None


LAST = re.compile(r'<([^>]+)>;\s*rel="last"')


def last_page(link: str | None) -> int | None:
    m = LAST.search(link or "")
    page = dict(parse_qsl(urlsplit(m.group(1)).query)).get("page") if m else None
    return int(page) if page and page.isdigit() else None


@dataclass
class Reply:
    status: int
    body: object
    last: int | None = None             # the Link header's last page, when there is more than one


def count(r: Reply) -> int:
    """Items behind a per_page=1 request: the last page's number, or what came back."""
    return r.last if r.last is not None else len(r.body or [])


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := env_str("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


async def _record(units: int) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units)
           VALUES ('github', (now() AT TIME ZONE 'UTC')::date, 1, $1)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + $1""",
        units)


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M:%S UTC")


@dataclass
class Client:
    """Serial requests through the gate, paced to our share of the hour."""
    http: httpx.AsyncClient | None = None
    quota: Quota = field(default_factory=Quota)
    budget: int | None = None           # requests left this run (None: no run started)
    calls: int = 0
    last: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        """A run's start: read the quota (free) and take this run's share of the hour."""
        self.budget, self.calls = None, 0
        await self.get(path("rate_limit"))
        self.budget = min(per_run(self.quota.limit or 60), self.quota.room(time.time()))

    async def get(self, p: str, params: dict | None = None) -> Reply:
        """GET an allowlisted metadata path. Raises Forbidden off the allowlist, Paced past our share."""
        full = url(p, params)
        for _ in range(MAX_REDIRECTS + 1):
            r = await self._send(full, params)
            if r.status_code in (301, 302, 307, 308) and r.headers.get("location"):
                full, params = redirect(r.headers["location"])
                continue
            if r.status_code == 401:
                raise RuntimeError("GitHub rejected GITHUB_TOKEN (HTTP 401)")
            if r.status_code >= 500:
                r.raise_for_status()
            body = r.json() if r.status_code == 200 and r.content else [] if r.status_code == 204 else None
            return Reply(r.status_code, body, last_page(r.headers.get("link")))
        raise RuntimeError(f"github: more than {MAX_REDIRECTS} redirects from {p}")

    async def _send(self, full: str, params: dict | None) -> httpx.Response:
        async with self.lock:
            now = time.time()
            if self.budget is not None and self.budget <= 0:
                raise Paced(f"this run's share of the hour is used ({self.calls} requests)")
            if self.quota.room(now) <= 0:
                until = max(self.quota.hold_until, self.quota.reset)
                raise Paced(f"half of the hour's {self.quota.limit} requests used; waiting until {_when(until)}")
            if (wait := MIN_GAP_S - (time.monotonic() - self.last)) > 0:
                await asyncio.sleep(wait)
            r = await (self.http or client()).get(full, params=params, headers=_headers(), timeout=20,
                                                  follow_redirects=False)
            self.last = time.monotonic()
            self.quota.see(r.headers)
            self.calls += 1
            if self.budget is not None:
                self.budget -= 1
            try:
                body = r.json() if r.status_code in (403, 429) and r.content else None
            except ValueError:
                body = None
            if until := hold(r.status_code, r.headers, body, time.time()):
                self.quota.hold_until = until
        await _record(0 if full.endswith("/rate_limit") else 1)
        if until:
            raise Paced(f"GitHub said wait (HTTP {r.status_code}) until {_when(until)}")
        return r


gh = Client()


# ─── links ───────────────────────────────────────────────────────────────────
class Ref(NamedTuple):
    owner: str
    name: str | None                    # None: the link names an account

    @property
    def key(self) -> str:
        return (f"{self.owner}/{self.name}" if self.name else self.owner).lower()

    @property
    def kind(self) -> str:
        return "repo" if self.name else "account"


# github.com/<first segment> pages that are GitHub's own, not an account
RESERVED = {"about", "account", "apps", "blog", "codespaces", "collections", "contact", "copilot", "customer-stories",
            "dashboard", "enterprise", "events", "explore", "features", "git-guides", "github", "home", "issues",
            "join", "login", "logout", "marketplace", "mobile", "new", "nonprofit", "notifications", "organizations",
            "premium-support", "pricing", "pulls", "readme", "resources", "search", "security", "sessions", "settings",
            "signup", "site", "solutions", "sponsors", "stars", "team", "topics", "trending", "user-attachments",
            "users", "watching"}
_OWNER, _NAME = re.compile(f"^{OWNER}$"), re.compile(f"^{NAME}$")


def parse_link(link) -> Ref | None:
    """The account and repo a github.com link names. A deep link (a file, a commit, an issue) names its repo, an
    account or org link the account; gists, GitHub's own pages, raw files and other hosts name nothing."""
    s = link.strip() if isinstance(link, str) else ""
    if not s:
        return None
    try:
        u = urlsplit(s if "://" in s else "https://" + s)
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or host not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts[:1] == ["orgs"]:
        parts = parts[1:2]
    if not parts or parts[0].lower() in RESERVED or not _OWNER.match(parts[0]):
        return None
    if len(parts) == 1:
        return Ref(parts[0], None)
    name = parts[1].removesuffix(".git")
    return Ref(parts[0], name) if _NAME.match(name) else None


LINK_KEYS = ("website", "twitter", "telegram", "discord", "github", "otherUrl")


def jupiter_urls(j: dict) -> list:
    """The link fields of a Jupiter token object (never its icon: logos are often raw GitHub files)."""
    links = j.get("links") if isinstance(j.get("links"), dict) else {}
    return [j.get(k) for k in LINK_KEYS] + list(links.values())


def dexscreener_urls(info: dict) -> list:
    return [w.get("url") for w in info.get("websites") or [] if isinstance(w, dict)] + \
           [s.get("url") for s in info.get("socials") or [] if isinstance(s, dict)]


_checked: dict[tuple[str, str], datetime] = {}         # when each token's DexScreener links were last read
DEX_CHAINS = {"solana", "base", "bsc", "ethereum", "robinhood"}
WATCH_SQL = """SELECT chain, address, meta->'links' AS links FROM de_watch
               WHERE (chain, address) IN (SELECT * FROM unnest($1::text[], $2::text[]))"""


async def _dexscreener(keys: list[tuple[str, str]], now: datetime) -> dict[tuple[str, str], list]:
    """Every website and social on DexScreener's token info, for tokens not read in LINKS_EVERY (30 a call)."""
    for k in [k for k, at in _checked.items() if now - at >= LINKS_EVERY]:
        del _checked[k]
    due = [k for k in keys if k[0] in DEX_CHAINS and k not in _checked]
    out: dict[tuple[str, str], list] = {}
    for chain in sorted({k[0] for k in due}):
        addrs = [a for c, a in due if c == chain]
        for i in range(0, len(addrs), dexscreener.MAX_ADDRESSES):
            batch = addrs[i:i + dexscreener.MAX_ADDRESSES]
            try:
                pairs, _ = await dexscreener.tokens(chain, batch)
            except Exception as e:  # noqa: BLE001 - the stored links still count; these wait for the next run
                log.info("%s: DexScreener links for %d %s tokens: %s", JOB, len(batch), chain, e)
                continue
            mine = {a.lower(): a for a in batch}
            for p in pairs:
                if a := mine.get(((p.get("baseToken") or {}).get("address") or "").lower()):
                    out.setdefault((chain, a), []).extend(dexscreener_urls(p.get("info") or {}))
            _checked.update({(chain, a): now for a in batch})
    return out


async def links(keys: list[tuple[str, str]], now: datetime) -> list[tuple]:
    """The GitHub links among the tokens' own links: [(chain, address, ref, kind, link, source)], one per ref."""
    found: dict[tuple, tuple] = {}

    def add(key: tuple[str, str], link, source: str) -> None:
        if ref := parse_link(link):
            found.setdefault((*key, ref.key), (*key, ref.key, ref.kind, link.strip(), source))

    try:
        from albatross.deepend import chain as chain_intel
        for key, j in (await chain_intel.jupiter_intel_many(keys)).items():
            for link in jupiter_urls(j):
                add(key, link, "jupiter")
    except Exception as e:  # noqa: BLE001 - another builder's table: its absence costs one source
        log.info("%s: Jupiter links: %s", JOB, e)
    pool = await db.pool()
    try:
        for r in await pool.fetch(WATCH_SQL, [k[0] for k in keys], [k[1] for k in keys]):
            got = orjson.loads(r["links"]) if isinstance(r["links"], (str, bytes)) else r["links"]
            for link in (got or {}).values() if isinstance(got, dict) else []:
                add((r["chain"], r["address"]), link, "discovery")
    except asyncpg.UndefinedTableError:
        pass
    for key, urls in (await _dexscreener(keys, now)).items():
        for link in urls:
            add(key, link, "dexscreener")
    return list(found.values())


async def remember(found: list[tuple], now: datetime) -> int:
    """New links become pending rows; known ones are marked seen."""
    return await db.upsert("de_token_repos", ["chain", "address", "ref", "kind", "link", "source", "status",
                                              "first_seen", "seen_at"],
                           [(*f, "pending", now, now) for f in found], ["chain", "address", "ref"], update=["seen_at"])


# ─── reading GitHub ──────────────────────────────────────────────────────────
GONE = (403, 404, 410, 451)             # 403/451 here: access blocked (rate limits never get this far)


def ts(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def repo(owner: str | None = None, name: str | None = None, *, repo_id: int | None = None) -> dict | None:
    """The repo object by name or by GitHub's id; None when GitHub answers that it is gone or blocked."""
    r = await gh.get(path("repo_id", id=str(repo_id)) if repo_id else path("repo", owner=owner, repo=name))
    if r.status in GONE:
        return None
    if r.status != 200 or not isinstance(r.body, dict) or "id" not in r.body:
        raise RuntimeError(f"github: HTTP {r.status} for repo {repo_id or f'{owner}/{name}'}")
    return r.body


async def most_starred(owner: str) -> tuple[str, dict | None]:
    """An account link's repo: its most-starred public repo (a tie: the latest push), from its repo list.
    -> ('ok', list item) | ('empty', None) | ('missing', None)."""
    best, page = None, 1
    while page <= MAX_PAGES:
        r = await gh.get(path("user_repos", owner=owner), {"per_page": 100, "page": page, "sort": "pushed"})
        if r.status in GONE:
            return "missing", None
        if r.status != 200 or not isinstance(r.body, list):
            raise RuntimeError(f"github: HTTP {r.status} for {owner}'s repos")
        for it in r.body:
            if best is None or (it.get("stargazers_count") or 0, it.get("pushed_at") or "") > \
                    (best.get("stargazers_count") or 0, best.get("pushed_at") or ""):
                best = it
        if r.last is None or page >= r.last:
            break
        page += 1
    return ("ok", best) if best else ("empty", None)


async def commits(owner: str, name: str, now: datetime) -> tuple[int, int] | None:
    """(commits in the last 7 days, in the last 30) on the default branch, or None when GitHub won't say."""
    p, since7, since30 = path("commits", owner=owner, repo=name), now - timedelta(days=7), now - timedelta(days=30)
    r = await gh.get(p, {"since": iso(since30), "per_page": 100})
    if r.status == 409:                 # an empty repository
        return 0, 0
    if r.status != 200 or not isinstance(r.body, list):
        return None
    dates = [ts(((c.get("commit") or {}).get("committer") or {}).get("date")) for c in r.body]
    recent = sum(1 for d in dates if d and d >= since7)
    if r.last is None:
        return recent, len(r.body)
    n30 = count(await gh.get(p, {"since": iso(since30), "per_page": 1}))
    if any(d and d < since7 for d in dates):          # the page reaches past 7 days: it holds all of them
        return recent, n30
    return count(await gh.get(p, {"since": iso(since7), "per_page": 1})), n30


async def contributors(owner: str, name: str) -> int | None:
    """Commit authors: GitHub accounts plus emails linked to none (anon=1; memecoin repos are often committed from
    bare emails, and an AI agent's commits always are)."""
    r = await gh.get(path("contributors", owner=owner, repo=name), {"per_page": 1, "anon": 1})
    if r.status == 204:                 # an empty repository
        return 0
    return count(r) if r.status == 200 else None       # 403: a history too large for GitHub to list


async def release(owner: str, name: str) -> tuple[str | None, datetime | None] | None:
    """The latest release's tag and date ((None, None): no release; None: GitHub won't say)."""
    r = await gh.get(path("releases", owner=owner, repo=name), {"per_page": 1})
    if r.status != 200 or not isinstance(r.body, list):
        return None
    if not r.body:
        return None, None
    return r.body[0].get("tag_name"), ts(r.body[0].get("published_at") or r.body[0].get("created_at"))


# ─── snapshots ───────────────────────────────────────────────────────────────
ACTIVITY = ("commits_7d", "commits_30d", "contributors", "release_tag", "release_at", "activity_at")
SNAP_COLS = ["repo_id", "ts", "stars", "forks", "watchers", "open_issues", "pushed_at", "archived", "disabled",
             *ACTIVITY]
REPO_COLS = ["repo_id", "full_name", "owner_type", "description", "homepage", "language", "license", "topics",
             "created_at", "fork", "parent", "parent_stars", "first_seen", "updated_at", "gone_at"]


def activity_due(prev: dict | None, pushed_at: datetime | None, now: datetime) -> bool:
    """Re-read commits, contributors and the release: never read, older than ACTIVITY_EVERY, or pushed since and
    older than ACTIVITY_MIN (a hot repo that pushes all day would otherwise cost 4-5 requests every run)."""
    at = (prev or {}).get("activity_at")
    return at is None or now - at >= ACTIVITY_EVERY or (
        pushed_at is not None and pushed_at > at and now - at >= ACTIVITY_MIN)


def repo_row(r: dict, now: datetime) -> tuple:
    lic, parent = r.get("license") or {}, r.get("parent") or {}
    spdx = lic.get("spdx_id")
    return (r["id"], r["full_name"], (r.get("owner") or {}).get("type"), r.get("description"),
            r.get("homepage") or None, r.get("language"),
            (lic.get("name") if spdx in (None, "NOASSERTION") else spdx) if lic else None,
            list(r.get("topics") or []), ts(r.get("created_at")), bool(r.get("fork")), parent.get("full_name"),
            parent.get("stargazers_count"), now, now, None)


async def snapshot(r: dict, prev: dict | None, now: datetime) -> tuple[dict, bool]:
    """One snapshot from the repo object, plus the activity when due (else carried forward from `prev`).
    -> (row, paced): a run out of requests mid-way still stores what it read."""
    row = {"repo_id": r["id"], "ts": now, "stars": r.get("stargazers_count"), "forks": r.get("forks_count"),
           "watchers": r.get("subscribers_count"), "open_issues": r.get("open_issues_count"),
           "pushed_at": ts(r.get("pushed_at")), "archived": r.get("archived"), "disabled": r.get("disabled"),
           **{k: (prev or {}).get(k) for k in ACTIVITY}}
    if not activity_due(prev, row["pushed_at"], now):
        return row, False
    owner, name = r["full_name"].split("/", 1)
    try:
        c, n, rel = await commits(owner, name, now), await contributors(owner, name), await release(owner, name)
    except Paced:
        return row, True
    except (httpx.HTTPError, RuntimeError) as e:       # GitHub failing: the counts wait for the next run
        log.info("%s: activity of %s: %s", JOB, r["full_name"], e)
        return row, False
    row.update(commits_7d=c[0] if c else None, commits_30d=c[1] if c else None, contributors=n,
               release_tag=rel[0] if rel else None, release_at=rel[1] if rel else None, activity_at=now)
    return row, False


async def store(r: dict, row: dict, now: datetime) -> None:
    await db.upsert("de_github_repos", REPO_COLS, [repo_row(r, now)], ["repo_id"],
                    update=[c for c in REPO_COLS if c not in ("repo_id", "first_seen")])
    await db.upsert("de_github_snapshots", SNAP_COLS, [tuple(row[c] for c in SNAP_COLS)], ["repo_id", "ts"])


LATEST_SQL = "SELECT * FROM de_github_snapshots WHERE repo_id = $1 ORDER BY ts DESC LIMIT 1"


async def refresh(r: dict, now: datetime) -> bool:
    """Snapshot a repo object we hold. -> paced."""
    pool = await db.pool()
    prev = await pool.fetchrow(LATEST_SQL, r["id"])
    row, paced = await snapshot(r, dict(prev) if prev else None, now)
    await store(r, row, now)
    return paced


async def resolve(item: dict, now: datetime) -> tuple[str, dict | None]:
    """A link's repo: ('ok', repo object) | ('missing', None) | ('empty', None), recorded on every token with it."""
    owner, _, name = item["ref"].partition("/")
    if name:
        r = await repo(owner, name)
        status = "ok" if r else "missing"
    else:
        status, it = await most_starred(owner)
        r = await repo(repo_id=it["id"]) if it else None    # the full object: watchers and the parent
        status = "missing" if status == "ok" and r is None else status
    pool = await db.pool()
    await pool.execute("UPDATE de_token_repos SET status = $2, repo_id = coalesce($3, repo_id), resolved_at = $4 "
                       "WHERE ref = $1", item["ref"], status, r["id"] if r else None, now)
    return status, r


async def gone(repo_id: int, now: datetime) -> None:
    """GitHub answers 404 for a repo we followed: deleted or made private."""
    pool = await db.pool()
    await pool.execute("UPDATE de_github_repos SET gone_at = coalesce(gone_at, $2) WHERE repo_id = $1", repo_id, now)
    await pool.execute("UPDATE de_token_repos SET status = 'missing', resolved_at = $2 WHERE repo_id = $1",
                       repo_id, now)


# ─── the job ─────────────────────────────────────────────────────────────────
def resolve_due(r: dict, now: datetime) -> bool:
    """Never asked; or an account link (its most-starred repo can change), a dead link or an empty account, daily."""
    if r["status"] == "pending" or r.get("resolved_at") is None:
        return True
    return (r["kind"] == "account" or r["status"] != "ok") and now - r["resolved_at"] >= RESOLVE_EVERY


def plan(rows: list[dict], tiers: dict[tuple[str, str], str], now: datetime) -> list[dict]:
    """This run's work, most urgent first: hot before warm, links to resolve before snapshots, never-snapshotted
    repos first, then the stalest. One item per link (shared by every token with it) and per repo."""
    refs: dict[str, dict] = {}
    repos: dict[int, dict] = {}
    for r in rows:
        hot = tiers.get((r["chain"], r["address"])) == "hot"
        if resolve_due(r, now):
            it = refs.setdefault(r["ref"], {"do": "resolve", "ref": r["ref"], "hot": False, "last": r.get("last_ts")})
        elif r["status"] == "ok" and r.get("repo_id"):
            it = repos.setdefault(r["repo_id"], {"do": "snapshot", "repo_id": r["repo_id"], "hot": False,
                                                 "last": r.get("last_ts")})
        else:
            continue
        it["hot"] = it["hot"] or hot
    due = [it for it in repos.values()
           if it["last"] is None or now - it["last"] >= (HOT_EVERY if it["hot"] else WARM_EVERY)]
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted([*refs.values(), *due], key=lambda it: (not it["hot"], it["do"] != "resolve",
                                                          it["last"] is not None, it["last"] or epoch))


MAPPED_SQL = """
SELECT m.chain, m.address, m.ref, m.kind, m.status, m.repo_id, m.resolved_at, s.ts AS last_ts
FROM de_token_repos m
LEFT JOIN LATERAL (SELECT ts FROM de_github_snapshots s WHERE s.repo_id = m.repo_id ORDER BY ts DESC LIMIT 1) s ON TRUE
WHERE (m.chain, m.address) IN (SELECT * FROM unnest($1::text[], $2::text[]))"""


async def run(tokens: list[dict] | None = None) -> dict:
    """deep_github: the GitHub links of hot and warm tokens (watch.tokens(), or the tokens given), resolved, and the
    repos that are due snapshotted, within our share of the hour."""
    now = datetime.now(UTC)
    tokens = await watch.tokens() if tokens is None else tokens
    tiers = {(t["chain"], t["address"]): t["tier"] for t in tokens if t.get("tier") in ("hot", "warm")}
    out = {"rows": 0, "tokens": len(tiers), "links": 0, "resolved": 0, "requests": 0, "paced": False}
    if not tiers:
        return out
    keys = list(tiers)
    found = await links(keys, now)
    await remember(found, now)
    out["links"] = len(found)
    pool = await db.pool()
    work = plan([dict(r) for r in await pool.fetch(MAPPED_SQL, [k[0] for k in keys], [k[1] for k in keys])],
                tiers, now)
    if not work:
        return out
    done: set[int] = set()
    try:
        await gh.start()
        for it in work:
            if it.get("repo_id") in done:
                continue
            try:
                if it["do"] == "resolve":
                    _, r = await resolve(it, now)
                    out["resolved"] += 1
                else:
                    r = await repo(repo_id=it["repo_id"])
                    if r is None:
                        await gone(it["repo_id"], now)
                if r is None or r["id"] in done:
                    continue
                done.add(r["id"])
                paced = await refresh(r, now)
                out["rows"] += 1
                if paced:
                    raise Paced("this run's share of the hour is used")
            except (Paced, Forbidden):
                raise
            except Exception:  # noqa: BLE001 - one repo's failure must not stop the others
                log.exception("%s: %s failed", JOB, it.get("ref") or it.get("repo_id"))
    except Paced as e:
        out["paced"] = True
        log.info("%s: %s; %d of %d items done", JOB, e, len(done), len(work))
    out["requests"] = gh.calls
    log.info("%s: %d tokens, %d GitHub links, %d resolved, %d snapshots, %d requests%s", JOB, out["tokens"],
             out["links"], out["resolved"], out["rows"], out["requests"],
             "" if env_str("GITHUB_TOKEN") else " (no GITHUB_TOKEN: 60 requests an hour)")
    return out


# ─── what the dossier reads ──────────────────────────────────────────────────
SECTION_SQL = """
SELECT m.ref, m.kind, m.link, m.status, m.repo_id, r.full_name, r.owner_type, r.description, r.homepage, r.language,
       r.license, r.topics, r.created_at, r.fork, r.parent, r.parent_stars, r.gone_at,
       s.ts, s.stars, s.forks, s.watchers, s.open_issues, s.pushed_at, s.archived, s.disabled, s.commits_7d,
       s.commits_30d, s.contributors, s.release_tag, s.release_at, s.activity_at,
       (SELECT count(DISTINCT o.chain || ':' || o.address) FROM de_token_repos o
        WHERE o.repo_id = m.repo_id AND (o.chain, o.address) <> (m.chain, m.address)) AS shared
FROM de_token_repos m
LEFT JOIN de_github_repos r ON r.repo_id = m.repo_id
LEFT JOIN LATERAL (SELECT * FROM de_github_snapshots s WHERE s.repo_id = m.repo_id ORDER BY ts DESC LIMIT 1) s ON TRUE
WHERE m.chain = $1 AND m.address = $2"""
HISTORY_SQL = """SELECT ts, stars, forks FROM de_github_snapshots WHERE repo_id = $1 AND ts > $2 ORDER BY ts"""
NAMES_SQL = """SELECT coalesce(s.symbol, w.meta->>'symbol') AS symbol, coalesce(s.name, w.meta->>'name') AS name
FROM (SELECT $1::text AS chain, $2::text AS address) k
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN de_watch w ON w.chain = k.chain AND w.address = k.address"""
LAUNCH_SQL = """
SELECT least((SELECT created_at FROM micro_state WHERE chain = $1 AND token = $2),
             (SELECT min(first_seen) FROM micro_tokens WHERE address = $2 AND coalesce(resolved_chain, chain) = $1),
             (SELECT (meta->'pair'->>'created_at')::timestamptz FROM de_watch WHERE chain = $1 AND address = $2))"""


def change(snaps: list[dict], window: timedelta) -> dict | None:
    """Stars and forks gained since the snapshot nearest `window` before the latest (0.9 to 1.5 windows back), with
    the hours it actually spans; None until our history reaches that far."""
    if not snaps:
        return None
    end = snaps[-1]
    base = [s for s in snaps if window * 0.9 <= end["ts"] - s["ts"] <= window * 1.5]
    if not base:
        return None
    b = min(base, key=lambda s: abs(end["ts"] - s["ts"] - window))
    diff = {k: end[k] - b[k] if end[k] is not None and b[k] is not None else None for k in ("stars", "forks")}
    return diff | {"hours": round((end["ts"] - b["ts"]) / timedelta(hours=1), 1)}


def velocity(c7: int | None, c30: int | None, age_days: float | None) -> str | None:
    """This week's commits against the 30-day pace: rising, steady, slowing or idle (new: too young to compare)."""
    if c7 is None or c30 is None:
        return None
    if c30 == 0:
        return "idle"
    if age_days is not None and age_days < 14:
        return "new"
    ratio = c7 / (c30 * 7 / 30)
    return "rising" if ratio >= 1.5 else "slowing" if ratio <= 0.5 else "steady"


def _k(n: int | None) -> str:
    return "?" if n is None else f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def _s(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _span(td: timedelta) -> str:
    m = abs(td) / timedelta(minutes=1)
    if m < 90:
        return _s(max(1, round(m)), "minute")
    if m < 48 * 60:
        return _s(round(m / 60), "hour")
    return _s(round(m / 1440), "day") if m < 730 * 1440 else f"{m / 1440 / 365.25:.1f} years"


LEVELS = ("bad", "warn", "good", "info")


def flags(m: dict, h24: dict | None, launch: datetime | None, now: datetime) -> list[dict]:
    """Plain-English flags for the dossier, worst first: {key, level (good | warn | bad | info), text}."""
    out: list[dict] = []

    def add(key: str, level: str, text: str) -> None:
        out.append({"key": key, "level": level, "text": text})

    has_snap, created = m.get("ts") is not None, m.get("created_at")
    if m["status"] == "missing" and has_snap:
        add("gone", "bad", f"The repo is gone: GitHub answers 404 for {m['full_name']} (deleted or made private"
            f"{', since ' + m['gone_at'].strftime('%Y-%m-%d') if m.get('gone_at') else ''}); last seen with "
            f"{_k(m['stars'])} stars.")
    elif m["status"] == "missing":
        add("dead_link", "bad", f"The GitHub link is dead: GitHub answers 404 for {m['ref']} (deleted, private or "
            "never public).")
    elif m["status"] == "empty":
        add("empty", "warn", f"The GitHub account {m['ref']} has no public repositories.")
    if not has_snap or m["status"] != "ok":
        return out
    if m["kind"] == "account":
        add("account", "info", f"The link names the account {m['ref'].split('/')[0]}, not a repo: this is its "
            "most-starred repo.")
    if m.get("disabled"):
        add("disabled", "bad", "Disabled by GitHub.")
    if m.get("archived"):
        add("archived", "bad", "Archived by its owner: read-only, no more work on it.")
    idle = now - m["pushed_at"] if m.get("pushed_at") else None
    if idle is not None and idle.days >= ABANDONED_DAYS and not m.get("archived"):
        add("abandoned", "bad", f"No push in {_span(idle)}: abandoned.")
    elif m.get("commits_30d") == 0 and not m.get("archived"):
        add("quiet", "warn", "No commits in the last 30 days.")
    if launch and created:
        lead = launch - created
        if lead < timedelta(0):
            add("after_launch", "warn", f"Repo created {_span(lead)} after the token launched.")
        elif lead < YOUNG:
            add("young", "warn", f"Repo created only {_span(lead)} before the token launched.")
        elif lead >= ESTABLISHED and (m.get("stars") or 0) >= KNOWN_STARS and not m.get("fork"):
            add("established", "warn", f"An established project ({_span(lead)} older than the token, "
                f"{_k(m['stars'])} stars): any coin can link a famous repo, so check the project itself names "
                "this coin.")
    if m.get("fork") and m.get("parent"):
        known = (m.get("parent_stars") or 0) >= KNOWN_STARS
        add("fork", "warn" if known else "info", f"A fork of {m['parent']}"
            + (f" ({_k(m['parent_stars'])} stars): most of its code is theirs." if known else "."))
    if m.get("shared"):
        add("shared", "warn", f"{_s(m['shared'], 'other token')} we track link{'s' if m['shared'] == 1 else ''} "
            "the same repo.")
    if m.get("commits_7d"):
        who = f", {_s(m['contributors'], 'contributor')}" if m.get("contributors") else ""
        add("active", "good", f"{_s(m['commits_7d'], 'commit')} in the last 7 days ({m['commits_30d']} in 30){who}.")
    if h24 and h24.get("stars") and m.get("stars") is not None:
        before = m["stars"] - h24["stars"]
        if h24["stars"] >= STAR_JUMP and h24["stars"] >= 0.25 * max(before, 1):
            add("stars_jump", "info", f"Stars up {h24['stars']}"
                + (f" (+{h24['stars'] / before:.0%})" if before > 0 else "") + f" in {h24['hours']:g} h.")
    return sorted(out, key=lambda f: LEVELS.index(f["level"]))


def primary(rows: list[dict]) -> dict:
    """The link the section is about: a resolved one with a snapshot, a repo link over an account link, the most
    starred."""
    return min(rows, key=lambda r: (r["status"] != "ok", r.get("ts") is None, r["kind"] != "repo",
                                    -(r.get("stars") or 0)))


OWN_LEAD = timedelta(days=30)            # a repo this much older than the coin is its own only if it names the coin


def own(m: dict, launch: datetime | None, names: tuple = ()) -> tuple[bool, str | None]:
    """Is the repo the coin's own? Not when the link named only an account (the repo is the account's most-starred,
    our pick), nor when the repo is OWN_LEAD older than the coin and never names it (its name, description, topics or
    homepage carry none of the coin's symbol, name or address). Any coin can link a famous repo. -> (own, why not).
    The repo's text is matched in code only; it never reaches a model."""
    if m.get("kind") == "account":
        return False, "account"
    created = m.get("created_at")
    if not (launch and created and launch - created > OWN_LEAD):
        return True, None                        # made around the coin's launch (or we can't tell): its own
    # Older than the coin: a shared word proves nothing (Archify linked a 75,781-star "archify" repo 135 days older),
    # so it must carry the contract address or the coin's $TICKER.
    text = " ".join(str(x) for x in (m.get("full_name"), m.get("description"), m.get("homepage"),
                                       *(m.get("topics") or [])) if x).lower()
    symbol, _name, address = (*names, None, None, None)[:3]
    marks = [address.lower()] if isinstance(address, str) and len(address) >= 20 else []
    sym = symbol.strip().lstrip("$").lower() if isinstance(symbol, str) else ""
    if len(sym) >= 2:
        marks.append("$" + sym)                      # "$LLD"
    if len(sym) >= 3:
        marks.append(f"({sym})")                     # "Lucky Legend (LLD)"; a 2-letter "(ai)" or "(ui)" is any text
    return (True, None) if any(mk in text for mk in marks) else (False, "older")


def shape(rows: list[dict], snaps: list[dict], launch: datetime | None, now: datetime, names: tuple = ()) -> dict:
    """The dossier's GitHub block from the token's links (SECTION_SQL rows), the primary repo's recent snapshots
    (oldest first), the token's launch and its (symbol, name, address)."""
    m = primary(rows)
    is_own, why_not = own(m, launch, names) if m.get("full_name") else (True, None)
    has_snap, has_repo = m.get("ts") is not None, m.get("full_name") is not None
    status = {"pending": "pending", "missing": "missing", "empty": "empty"}.get(m["status"]) or (
        "pending" if not has_snap else "fresh" if now - m["ts"] < FRESH else "stale")
    created = m.get("created_at")
    age_days = (now - created) / DAY if created else None
    h24, d7 = change(snaps, DAY), change(snaps, 7 * DAY)
    return {
        "status": status, "as_of": m.get("ts"), "link": m["link"], "via": m["kind"], "own": is_own,
        "not_own": why_not,
        "repo": {"name": m["full_name"], "url": f"https://github.com/{m['full_name']}", "description": m["description"],
                 "homepage": m["homepage"], "language": m["language"], "license": m["license"],
                 "topics": list(m["topics"] or []), "owner_type": m["owner_type"], "created_at": created,
                 "fork": m["fork"], "parent": m["parent"], "parent_stars": m["parent_stars"],
                 "gone_at": m["gone_at"]} if has_repo else None,
        "metrics": {k: m.get(k) for k in ("stars", "forks", "watchers", "open_issues", "pushed_at", "archived",
                                          "disabled", "commits_7d", "commits_30d", "contributors")}
        | {"release": {"tag": m["release_tag"], "at": m["release_at"]} if m.get("release_tag") else None,
           "activity_as_of": m.get("activity_at")} if has_snap else None,
        "change": {"h24": h24, "d7": d7},
        "signals": {
            "age_at_launch_days": round((launch - created) / DAY, 1) if launch and created else None,
            "days_since_push": round((now - m["pushed_at"]) / DAY, 1) if has_snap and m.get("pushed_at") else None,
            "stars_per_day": round(m["stars"] / max(age_days, 1.0), 1)
            if has_snap and m.get("stars") is not None and age_days is not None else None,
            "velocity": velocity(m.get("commits_7d"), m.get("commits_30d"), age_days),
            "shared_with": m.get("shared") or 0},
        "flags": flags(m, h24, launch, now),
        "links": [{"link": r["link"], "repo": r.get("full_name"), "status": r["status"]} for r in rows],
    }


async def section(chain: str, address: str) -> dict | None:
    """The dossier's GitHub block: the repo the token links, its latest metrics, its star and fork changes over 24 h
    and 7 d, and plain-English flags. None when the token links no GitHub (or before migration 030)."""
    pool = await db.pool()
    try:
        rows = [dict(r) for r in await pool.fetch(SECTION_SQL, chain, address)]
    except asyncpg.UndefinedTableError:
        return None
    if not rows:
        return None
    now, m = datetime.now(UTC), primary(rows)
    snaps = [dict(r) for r in await pool.fetch(HISTORY_SQL, m["repo_id"], now - 11 * DAY)] if m.get("ts") else []
    try:
        launch = await pool.fetchval(LAUNCH_SQL, chain, address)
    except asyncpg.PostgresError:
        launch = None
    names = await pool.fetchrow(NAMES_SQL, chain, address)
    return shape(rows, snaps, launch, now, (*(tuple(names) if names else ()), address))


# ─── registry (albatross/deepend/registry.py) ────────────────────────────────
JOBS: dict = {JOB: run}
SCHEDULE: list = [(JOB, {"minute": "7,37", "second": 20})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE: dict = {JOB: CADENCE_S}
