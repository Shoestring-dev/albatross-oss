"""GitHub (albatross/deepend/github.py): the metadata-only gate, link parsing,
pacing to half the hour's limit, the commit and contributor counts, deltas, flags and the section, on the repo object
and commit-list Link header recorded 2026-10-01 (tests/fixtures/deep_github). No test touches the network or a DB."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from albatross.deepend import github as gh

REC = json.loads((Path(__file__).parent / "fixtures" / "deep_github" / "recorded.json").read_text())
NOW = datetime(2026, 10, 1, 19, 30, tzinfo=UTC)
LAUNCH = datetime(2026, 9, 30, 10, 33, 23, tzinfo=UTC)          # terrafying's pool, the earlier of the two tokens
H, D = timedelta(hours=1), timedelta(days=1)


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No pause between requests and no usage rows: the tests' transports are local and there is no DB."""
    async def record(units):
        return None
    monkeypatch.setattr(gh, "MIN_GAP_S", 0.0)
    monkeypatch.setattr(gh, "_record", record)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh, "env_str", lambda name, default=None: None)


class Net:
    """A fake api.github.com: answers by path, records every request that reached it."""

    def __init__(self, routes: dict, headers: dict | None = None):
        self.routes, self.headers, self.seen = routes, headers or {}, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path not in self.routes:
            raise AssertionError(f"unexpected request {request.url}")
        answer = self.routes[request.url.path]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        status, body, extra = (answer + ({},))[:3] if isinstance(answer, tuple) else (200, answer, {})
        return httpx.Response(status, json=body, headers={**self.headers, **extra})

    def client(self) -> gh.Client:
        return gh.Client(http=httpx.AsyncClient(transport=httpx.MockTransport(self)))


QUOTA = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "59", "x-ratelimit-reset": "4102444800",
         "x-ratelimit-resource": "core"}


# ─── links ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("link, ref, kind", [
    ("https://github.com/terrafying/ai-torture-chamber", "terrafying/ai-torture-chamber", "repo"),
    # the docspider token's website, as stored in staging: a file in the repo names the repo; nothing is fetched
    ("https://github.com/terrafying/docspider/blob/386a5395c970c750414c5996da64dc035114c00b/docspider/settings.py#L10",
     "terrafying/docspider", "repo"),
    ("github.com/elizaOS/eliza.git", "elizaos/eliza", "repo"),
    ("http://www.github.com/elizaOS/eliza/tree/develop?tab=readme-ov-file#readme", "elizaos/eliza", "repo"),
    ("https://github.com/elizaOS/eliza/releases/download/v1.0.0/eliza.zip", "elizaos/eliza", "repo"),
    ("https://github.com/elizaOS", "elizaos", "account"),
    ("https://github.com/elizaOS/", "elizaos", "account"),
    ("https://github.com/orgs/elizaOS/repositories", "elizaos", "account"),
    ("  https://github.com/ai16z?tab=repositories ", "ai16z", "account"),
])
def test_a_link_names_its_repo_or_account(link, ref, kind):
    r = gh.parse_link(link)
    assert (r.key, r.kind) == (ref, kind)


@pytest.mark.parametrize("link", [
    # Jupiter's logo for RAY, as stored in staging: a raw file is not a project link
    ("https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/"
     "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R/logo.png"),
    "https://gist.github.com/someone/0123abcd", "https://terrafying.github.io/docs", "https://github.com",
    "https://github.com/features/copilot", "https://github.com/user-attachments/assets/123", "https://github.com/login",
    "https://github.com/topics/ai-agents", "https://github.com.evil.example/elizaOS/eliza",
    "https://evil.example/github.com/elizaOS/eliza", "ftp://github.com/a/b", "https://github.com/-bad/repo",
    "https://github.com/a/..", "https://x.com/elizaOS", "", None, 42,
])
def test_links_that_name_no_repo(link):
    assert gh.parse_link(link) is None


def test_only_link_fields_are_read_never_logos():
    jup = {"icon": "https://raw.githubusercontent.com/a/b/main/logo.png", "website": "https://github.com/a/b",
           "twitter": "https://x.com/a", "links": {"github": "https://github.com/a"}}
    urls = [u for u in gh.jupiter_urls(jup) if u]
    assert "https://raw.githubusercontent.com/a/b/main/logo.png" not in urls
    assert {gh.parse_link(u).key for u in urls if gh.parse_link(u)} == {"a/b", "a"}
    info = {"imageUrl": "https://raw.githubusercontent.com/x/y/z.png",
            "websites": [{"label": "Website", "url": "https://a.fun"},
                         {"label": "GitHub", "url": "https://github.com/a/b"}],
            "socials": [{"type": "twitter", "url": "https://x.com/a"}]}
    assert gh.dexscreener_urls(info) == ["https://a.fun", "https://github.com/a/b", "https://x.com/a"]


def test_links_come_from_jupiter_discovery_and_dexscreener(monkeypatch):
    from albatross import db
    from albatross.deepend import chain as chain_intel
    from albatross.venues import dexscreener
    qwen = ("solana", "A3W3ujLaYiEHCUWJgRNkmoTTokBVg7A7jBXmdAo8pump")
    doc = ("robinhood", "0x9259b34b4ff1efe75591e262f009b8b367b57205")
    evm = ("base", "0xabc0000000000000000000000000000000000001")
    deep = "https://github.com/terrafying/docspider/blob/386a5395/docspider/settings.py#L10"

    async def jupiter(keys):                    # as staging stores QWEN's: the logo is not a link
        return {qwen: {"icon": "https://raw.githubusercontent.com/a/b/main/logo.png",
                       "website": "https://github.com/terrafying/ai-torture-chamber", "twitter": "https://x.com/q"}}

    class Pool:
        async def fetch(self, sql, *args):
            assert sql is gh.WATCH_SQL
            return [{"chain": doc[0], "address": doc[1], "links": json.dumps({"x": "https://x.com/grok",
                                                                               "website": deep, "telegram": None})}]

    async def pool():
        return Pool()
    asked = []

    async def tokens(chain, addresses):
        asked.append(chain)
        pair = {"baseToken": {"address": "0xABC0000000000000000000000000000000000001"},     # checksummed
                "info": {"websites": [{"label": "GitHub", "url": "https://github.com/someorg"}],
                         "socials": [{"type": "twitter", "url": "https://x.com/someorg"}]}}
        return ([pair] if chain == "base" else []), NOW
    monkeypatch.setattr(chain_intel, "jupiter_intel_many", jupiter)
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setattr(dexscreener, "tokens", tokens)
    monkeypatch.setattr(gh, "_checked", {})
    found = go(gh.links([qwen, doc, evm], NOW))
    assert sorted(found) == sorted([
        (*qwen, "terrafying/ai-torture-chamber", "repo", "https://github.com/terrafying/ai-torture-chamber", "jupiter"),
        (*doc, "terrafying/docspider", "repo", deep, "discovery"),
        (*evm, "someorg", "account", "https://github.com/someorg", "dexscreener")])
    assert sorted(asked) == ["base", "robinhood", "solana"]
    go(gh.links([qwen, doc, evm], NOW + H))
    assert len(asked) == 3                      # DexScreener is asked again only after LINKS_EVERY
    go(gh.links([qwen, doc, evm], NOW + 7 * H))
    assert len(asked) == 6


# ─── the gate ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", [
    "/rate_limit", "/repos/elizaOS/eliza", "/repositories/1385768572", "/repos/elizaOS/eliza/commits",
    "/repos/elizaOS/eliza/contributors", "/repos/elizaOS/eliza/releases", "/repos/elizaOS/eliza/stargazers",
    "/users/elizaOS/repos", "/orgs/elizaOS/repos", "/repos/elizaOS/eliza/stats/commit_activity",
    "/repos/elizaOS/eliza/stats/participation", "/repos/elizaOS/eliza/stats/contributors", "/repos/a/b.js/commits",
])
def test_metadata_paths_are_allowed(path):
    assert gh.url(path, {"per_page": 1, "since": "2026-09-01T00:00:00Z"}) == "https://api.github.com" + path


@pytest.mark.parametrize("path", [
    "/repos/elizaOS/eliza/contents", "/repos/elizaOS/eliza/contents/README.md", "/repos/elizaOS/eliza/readme",
    "/repos/elizaOS/eliza/readme/docs", "/repos/elizaOS/eliza/tarball", "/repos/elizaOS/eliza/tarball/main",
    "/repos/elizaOS/eliza/zipball", "/repos/elizaOS/eliza/zipball/main", "/repos/elizaOS/eliza/git/blobs/0a1b2c",
    "/repos/elizaOS/eliza/git/trees/main", "/repos/elizaOS/eliza/git/trees/main?recursive=1",
    "/repos/elizaOS/eliza/releases/assets/12345", "/repos/elizaOS/eliza/releases/1/assets",
    "/repos/elizaOS/eliza/commits/0a1b2c3d",            # one commit comes with its files' patches
    "/repos/elizaOS/eliza/compare/main...develop", "/repos/elizaOS/eliza/archive/refs/heads/main.zip",
    "https://raw.githubusercontent.com/elizaOS/eliza/main/README.md", "raw.githubusercontent.com/elizaOS/eliza/main/x",
    "https://codeload.github.com/elizaOS/eliza/tar.gz/main", "https://github.com/elizaOS/eliza/archive/main.zip",
    "https://api.github.com/repos/elizaOS/eliza", "/repos/elizaOS/eliza/../eliza/contents", "/repos/elizaOS/..",
    "/repos/elizaOS/eliza%2Fcontents", "/repos/elizaOS/eliza/commits?path=README.md", "/repos/elizaOS/eliza/",
    "/gists/0a1b2c", "/repos/elizaOS/eliza/git/refs", "", None,
])
def test_anything_that_could_download_the_repo_is_forbidden(path):
    with pytest.raises(gh.Forbidden):
        gh.url(path)


def test_parameters_outside_the_allowlist_are_forbidden():
    with pytest.raises(gh.Forbidden):
        gh.url("/repos/a/b/commits", {"path": "README.md"})
    with pytest.raises(gh.Forbidden):
        gh.url("/repos/a/b/commits", {"sha": "main", "per_page": 1})


def test_a_forbidden_request_never_reaches_the_network():
    net = Net({})
    c = net.client()
    for p in ("/repos/a/b/contents/x.py", "/repos/a/b/tarball", "https://raw.githubusercontent.com/a/b/main/x"):
        with pytest.raises(gh.Forbidden):
            go(c.get(p))
    assert net.seen == []


def test_redirects_go_through_the_gate():
    renamed = Net({"/repos/old/name": (301, {"message": "Moved Permanently"},
                                       {"location": "https://api.github.com/repositories/1385768572"}),
                   "/repositories/1385768572": REC["repo"]}, QUOTA)
    r = go(renamed.client().get("/repos/old/name"))
    assert r.status == 200 and r.body["full_name"] == "terrafying/ai-torture-chamber"
    for location in ("https://codeload.github.com/a/b/legacy.tar.gz/main",
                     "https://api.github.com/repos/a/b/tarball/main", "https://raw.githubusercontent.com/a/b/main/x"):
        net = Net({"/repos/a/b": (302, {}, {"location": location})}, QUOTA)
        with pytest.raises(gh.Forbidden):
            go(net.client().get("/repos/a/b"))
        assert [str(q.url) for q in net.seen] == ["https://api.github.com/repos/a/b"]     # never the target


# ─── pacing ──────────────────────────────────────────────────────────────────
def test_we_use_at_most_half_the_hour():
    q, now = gh.Quota(), 1_000.0
    assert q.room(now) == 1                                     # unknown: one request, whose headers tell
    q.see({"x-ratelimit-limit": "60", "x-ratelimit-remaining": "60", "x-ratelimit-reset": "4600"})
    assert q.room(now) == 30
    q.see({"x-ratelimit-limit": "60", "x-ratelimit-remaining": "31", "x-ratelimit-reset": "4600"})
    assert q.room(now) == 1
    q.see({"x-ratelimit-limit": "60", "x-ratelimit-remaining": "30", "x-ratelimit-reset": "4600"})
    assert q.room(now) == 0
    assert q.room(4600.0) == 30                                 # the window reset
    q.see({"x-ratelimit-limit": "30", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "9",
           "x-ratelimit-resource": "search"})
    assert q.limit == 60                                        # another resource's window is not ours
    q.see({"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990", "x-ratelimit-reset": "4600"})
    assert q.room(now) == 2490
    assert gh.per_run(60) == 15 and gh.per_run(5000) == 1250    # two runs an hour share the half


def test_a_run_stops_at_its_share_before_sending():
    net = Net({"/rate_limit": {"resources": {}}, "/repos/a/b": REC["repo"]},
              {**QUOTA, "x-ratelimit-remaining": "44"})
    c = net.client()
    go(c.start())
    assert c.budget == 14                                       # 44 left - 30 kept = 14, under the run's 15
    for _ in range(14):
        go(c.get("/repos/a/b"))
    with pytest.raises(gh.Paced):
        go(c.get("/repos/a/b"))
    assert len(net.seen) == 15                                  # the quota read and 14 requests, nothing more


def test_a_rate_limit_answer_holds_every_request_until_reset():
    reset = "4102444800"
    for status, extra, body in [
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}, {"message": "API rate limit exceeded"}),
        (429, {"retry-after": "120"}, {"message": "Too many requests"}),
        (403, {}, {"message": "You have exceeded a secondary rate limit."}),
    ]:
        net = Net({"/repos/a/b": (status, body, extra)})
        c = net.client()
        with pytest.raises(gh.Paced):
            go(c.get("/repos/a/b"))
        assert c.quota.hold_until > 0
        with pytest.raises(gh.Paced):
            go(c.get("/repos/a/b"))
        assert len(net.seen) == 1                               # the hold stops the next request at home
    blocked = Net({"/repos/a/b": (403, {"message": "Repository access blocked"})}, QUOTA)
    c = blocked.client()
    assert go(c.get("/repos/a/b")).status == 403 and c.quota.hold_until == 0     # not a rate limit: no hold
    assert gh.hold(404, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}, None, 0.0) is None


def test_the_token_is_sent_only_when_set(monkeypatch):
    net = Net({"/repos/a/b": REC["repo"]}, QUOTA)
    go(net.client().get("/repos/a/b"))
    assert "authorization" not in net.seen[-1].headers
    monkeypatch.setattr(gh, "env_str", lambda name, default=None: "ghp_test" if name == "GITHUB_TOKEN" else default)
    go(net.client().get("/repos/a/b"))
    assert net.seen[-1].headers["authorization"] == "Bearer ghp_test"
    assert net.seen[-1].headers["accept"] == "application/vnd.github+json"


def test_a_rejected_token_fails_loudly_instead_of_marking_repos_missing():
    net = Net({"/repos/a/b": (401, {"message": "Bad credentials"})}, QUOTA)
    with pytest.raises(RuntimeError, match="401"):
        go(net.client().get("/repos/a/b"))


# ─── counts ──────────────────────────────────────────────────────────────────
def test_the_last_page_of_a_per_page_1_list_is_its_count():
    assert gh.last_page(REC["commits_link"]) == 106             # 106 commits since 2026-09-01, in one request
    assert gh.last_page(None) is None and gh.last_page('<https://api.github.com/x?page=1>; rel="first"') is None
    assert gh.count(gh.Reply(200, [{}], 106)) == 106 and gh.count(gh.Reply(200, [{}])) == 1
    assert gh.count(gh.Reply(200, []))== 0


def _commit(at: datetime) -> dict:
    return {"sha": "x", "commit": {"committer": {"date": gh.iso(at)}}}


def test_commits_under_a_page_are_counted_by_date(monkeypatch):
    net = Net({"/repos/a/b/commits": [_commit(NOW - k * D) for k in (0, 1, 6, 8, 20, 29)]}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    assert go(gh.commits("a", "b", NOW)) == (3, 6)
    assert len(net.seen) == 1
    assert net.seen[0].url.params["since"] == gh.iso(NOW - 30 * D)


def test_commits_past_a_page_are_counted_by_the_last_page(monkeypatch):
    since7 = gh.iso(NOW - 7 * D)

    def commits(req):
        if req.url.params["per_page"] == "100":                 # a full page, all inside the week
            return 200, [_commit(NOW - H)] * 100, {"link": REC["commits_link"]}
        n = "140" if req.url.params["since"] == since7 else "306"
        return 200, [_commit(NOW - H)], {"link": f'<https://api.github.com/x?per_page=1&page={n}>; rel="last"'}
    net = Net({"/repos/a/b/commits": commits}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    assert go(gh.commits("a", "b", NOW)) == (140, 306)
    assert len(net.seen) == 3


def test_contributors_count_bare_emails_too(monkeypatch):
    net = Net({"/repos/terrafying/ai-torture-chamber/contributors":
               (200, [{"email": "anon@clanker.church", "name": "E", "type": "Anonymous", "contributions": 81}],
                {"link": REC["contributors_link"]})}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    assert go(gh.contributors("terrafying", "ai-torture-chamber")) == 3        # 1 without anon=1, recorded
    assert dict(net.seen[0].url.params) == {"per_page": "1", "anon": "1"}


def test_an_empty_repo_has_no_commits_or_contributors(monkeypatch):
    net = Net({"/repos/a/b/commits": (409, {"message": "Git Repository is empty."}),
               "/repos/a/b/contributors": lambda req: httpx.Response(204, headers=QUOTA),
               "/repos/a/b/releases": []}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    assert go(gh.commits("a", "b", NOW)) == (0, 0)
    assert go(gh.contributors("a", "b")) == 0
    assert go(gh.release("a", "b")) == (None, None)


def test_an_account_link_stands_for_its_most_starred_repo(monkeypatch):
    repos = [{"id": 1, "full_name": "dev/fork-of-eliza", "stargazers_count": 3, "pushed_at": "2026-09-30T00:00:00Z"},
             {"id": 2, "full_name": "dev/agent", "stargazers_count": 41, "pushed_at": "2026-09-20T00:00:00Z"},
             {"id": 3, "full_name": "dev/dotfiles", "stargazers_count": 41, "pushed_at": "2026-09-29T00:00:00Z"}]
    net = Net({"/users/dev/repos": repos, "/users/nobody/repos": [], "/users/gone/repos": (404, {})}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    assert go(gh.most_starred("dev")) == ("ok", repos[2])           # a tie goes to the latest push
    assert go(gh.most_starred("nobody")) == ("empty", None)
    assert go(gh.most_starred("gone")) == ("missing", None)


# ─── snapshots ───────────────────────────────────────────────────────────────
def test_activity_is_read_after_a_push_or_every_six_hours():
    pushed = NOW - 3 * H
    assert gh.activity_due(None, pushed, NOW)
    assert not gh.activity_due({"activity_at": NOW - 2 * H}, pushed, NOW)
    assert gh.activity_due({"activity_at": NOW - 4 * H}, pushed, NOW)         # pushed since the last read
    assert gh.activity_due({"activity_at": NOW - 7 * H}, NOW - 8 * H, NOW)    # six hours old
    m = timedelta(minutes=1)                    # a repo pushing all day: its counts at most hourly
    assert not gh.activity_due({"activity_at": NOW - 30 * m}, NOW - 5 * m, NOW)
    assert gh.activity_due({"activity_at": NOW - 61 * m}, NOW - 5 * m, NOW)


def test_a_refresh_without_a_push_costs_one_request(monkeypatch):
    net = Net({}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    prev = {"commits_7d": 40, "commits_30d": 106, "contributors": 1, "release_tag": None, "release_at": None,
            "activity_at": NOW - H}
    row, paced = go(gh.snapshot(REC["repo"] | {"pushed_at": gh.iso(NOW - 2 * H)}, prev, NOW))
    assert not paced and net.seen == []                         # the repo object was the request
    assert (row["stars"], row["forks"], row["watchers"], row["open_issues"]) == (207, 64, 0, 30)
    assert (row["commits_30d"], row["activity_at"]) == (106, NOW - H)         # carried forward with its own time


def test_a_run_out_of_requests_mid_snapshot_keeps_what_it_read(monkeypatch):
    c = Net({}, QUOTA).client()
    c.budget = 0
    monkeypatch.setattr(gh, "gh", c)
    row, paced = go(gh.snapshot(REC["repo"], None, NOW))
    assert paced and row["stars"] == 207 and row["activity_at"] is None


def test_github_failing_mid_activity_keeps_the_snapshot(monkeypatch):
    net = Net({"/repos/terrafying/ai-torture-chamber/commits": (502, {"message": "Server Error"})}, QUOTA)
    monkeypatch.setattr(gh, "gh", net.client())
    prev = {"commits_7d": 5, "commits_30d": 9, "contributors": 2, "release_tag": "v1", "release_at": NOW - D,
            "activity_at": NOW - 7 * H}
    row, paced = go(gh.snapshot(REC["repo"], prev, NOW))
    assert not paced and row["stars"] == 207
    assert (row["commits_30d"], row["release_tag"], row["activity_at"]) == (9, "v1", NOW - 7 * H)


def test_the_repo_row_keeps_identity_and_parent():
    fork = REC["repo"] | {"fork": True, "parent": {"full_name": "elizaOS/eliza", "stargazers_count": 17213},
                          "license": {"spdx_id": "MIT", "name": "MIT License"}, "topics": ["ai", "agents"]}
    row = dict(zip(gh.REPO_COLS, gh.repo_row(fork, NOW)))
    assert (row["repo_id"], row["full_name"], row["owner_type"], row["license"]) == (
        1385768572, "terrafying/ai-torture-chamber", "User", "MIT")
    assert (row["fork"], row["parent"], row["parent_stars"], row["topics"]) == (True, "elizaOS/eliza", 17213,
                                                                                ["ai", "agents"])
    assert row["created_at"] == datetime(2026, 9, 24, 15, 10, 21, tzinfo=UTC)
    assert dict(zip(gh.REPO_COLS, gh.repo_row(REC["repo"], NOW)))["license"] == "Other"   # NOASSERTION


# ─── the job's plan ──────────────────────────────────────────────────────────
def _map(chain, address, ref, status="ok", repo_id=1, last=None, resolved=NOW - H, kind=None):
    return {"chain": chain, "address": address, "ref": ref, "kind": kind or ("repo" if "/" in ref else "account"),
            "status": status, "repo_id": repo_id, "resolved_at": resolved, "last_ts": last}


def test_hot_repos_every_run_warm_ones_after_two_hours_new_links_first():
    tiers = {("solana", "HOT"): "hot", ("solana", "WARM"): "warm", ("solana", "WARM2"): "warm",
             ("solana", "NEW"): "warm"}
    rows = [_map("solana", "HOT", "a/hot", repo_id=1, last=NOW - 26 * timedelta(minutes=1)),
            _map("solana", "WARM", "a/warm", repo_id=2, last=NOW - H),                 # not due yet
            _map("solana", "WARM2", "a/stale", repo_id=3, last=NOW - 3 * H),
            _map("solana", "WARM", "a/hot", repo_id=1, last=NOW - 26 * timedelta(minutes=1)),   # same repo
            _map("solana", "NEW", "a/new", status="pending", repo_id=None, resolved=None),
            _map("solana", "NEW", "dead/link", status="missing", repo_id=None, resolved=NOW - 2 * H)]
    work = gh.plan(rows, tiers, NOW)
    assert [(w["do"], w.get("ref") or w.get("repo_id")) for w in work] == [
        ("snapshot", 1), ("resolve", "a/new"), ("snapshot", 3)]
    rows[-1]["resolved_at"] = NOW - 25 * H                     # a dead link is asked again daily
    assert ("resolve", "dead/link") in [(w["do"], w.get("ref")) for w in gh.plan(rows, tiers, NOW)]


def test_account_links_are_resolved_again_daily():
    acct = _map("solana", "T", "dev", kind="account", repo_id=7, last=NOW - 3 * H, resolved=NOW - 25 * H)
    assert [w["do"] for w in gh.plan([acct], {("solana", "T"): "warm"}, NOW)] == ["resolve"]
    acct["resolved_at"] = NOW - 2 * H
    assert [w["do"] for w in gh.plan([acct], {("solana", "T"): "warm"}, NOW)] == ["snapshot"]


# ─── deltas and flags ────────────────────────────────────────────────────────
def snaps(*points) -> list[dict]:
    return [{"ts": NOW - ago, "stars": s, "forks": f} for ago, s, f in points]


def test_changes_need_history_that_reaches_back():
    assert gh.change([], D) is None
    assert gh.change(snaps((20 * H, 150, 50), (0 * H, 207, 64)), D) is None          # 20 h is not a day
    s = snaps((8 * D, 10, 1), (26 * H, 150, 50), (23 * H, 160, 52), (0 * H, 207, 64))
    assert gh.change(s, D) == {"stars": 47, "forks": 12, "hours": 23.0}                # the nearest to 24 h
    assert gh.change(s, 7 * D) == {"stars": 197, "forks": 63, "hours": 192.0}
    assert gh.change(snaps((12 * D, 1, 1), (0 * H, 207, 64)), 7 * D) is None          # too far back to be "7 d"


def test_velocity():
    assert gh.velocity(None, 4, 100) is None and gh.velocity(0, 0, 100) == "idle"
    assert gh.velocity(40, 106, 7) == "new"                     # a week-old repo: nothing to compare with
    assert gh.velocity(20, 30, 100) == "rising" and gh.velocity(7, 30, 100) == "steady"
    assert gh.velocity(1, 30, 100) == "slowing"


def row(**kw) -> dict:
    """A SECTION_SQL row for the recorded repo, two QWEN-like tokens on it."""
    r = REC["repo"]
    base = {"ref": "terrafying/ai-torture-chamber", "kind": "repo",
            "link": "https://github.com/terrafying/ai-torture-chamber", "status": "ok", "repo_id": r["id"],
            "full_name": r["full_name"], "owner_type": "User",
            "description": None, "homepage": r["homepage"], "language": "Python", "license": "Other", "topics": [],
            "created_at": gh.ts(r["created_at"]), "fork": False, "parent": None, "parent_stars": None,
            "gone_at": None, "ts": NOW - 10 * timedelta(minutes=1), "stars": 207, "forks": 64, "watchers": 0,
            "open_issues": 30, "pushed_at": gh.ts(r["pushed_at"]), "archived": False, "disabled": False,
            "commits_7d": 106, "commits_30d": 106, "contributors": 1, "release_tag": None, "release_at": None,
            "activity_at": NOW - 10 * timedelta(minutes=1), "shared": 1}
    return base | kw


def keys(fl: list[dict]) -> dict:
    return {f["key"]: f["level"] for f in fl}


def test_the_recorded_repo_was_made_days_before_the_coin():
    fl = gh.flags(row(), None, LAUNCH, NOW)
    assert keys(fl) == {"young": "warn", "shared": "warn", "active": "good"}
    texts = {f["key"]: f["text"] for f in fl}
    assert texts["young"] == "Repo created only 6 days before the token launched."
    assert texts["shared"] == "1 other token we track links the same repo."
    assert texts["active"] == "106 commits in the last 7 days (106 in 30), 1 contributor."
    assert [f["level"] for f in fl] == ["warn", "warn", "good"]                       # worst first


def test_fork_archive_abandonment_and_borrowed_repos():
    old = datetime(2024, 7, 1, tzinfo=UTC)
    fork = row(fork=True, parent="elizaOS/eliza", parent_stars=17213, shared=0, created_at=LAUNCH - 2 * H)
    assert {f["key"]: f["text"] for f in gh.flags(fork, None, LAUNCH, NOW)}["fork"] == \
        "A fork of elizaOS/eliza (17.2k stars): most of its code is theirs."
    assert keys(gh.flags(row(fork=True, parent="dev/thing", parent_stars=3, shared=0), None, LAUNCH, NOW))["fork"] \
        == "info"
    dead = row(archived=True, pushed_at=NOW - 200 * D, commits_7d=0, commits_30d=0, shared=0, created_at=old)
    assert keys(gh.flags(dead, None, LAUNCH, NOW)) == {"archived": "bad"}              # archived says it all
    quiet = row(pushed_at=NOW - 120 * D, commits_7d=0, commits_30d=0, shared=0, created_at=old, stars=40)
    assert {f["key"]: f["text"] for f in gh.flags(quiet, None, LAUNCH, NOW)} == {
        "abandoned": "No push in 120 days: abandoned."}
    # docspider (Robinhood) links a settings.py line in a repo last pushed 2023-10-10, as the staging run found
    docspider = row(created_at=datetime(2023, 10, 10, 19, 4, 29, tzinfo=UTC), stars=0, forks=0, shared=0,
                    pushed_at=datetime(2023, 10, 10, 19, 6, 1, tzinfo=UTC), commits_7d=0, commits_30d=0)
    assert gh.flags(docspider, None, LAUNCH, NOW) == [
        {"key": "abandoned", "level": "bad", "text": "No push in 3.0 years: abandoned."}]
    famous = row(created_at=old, stars=17213, shared=0, commits_7d=0, commits_30d=12)
    assert "check the project itself names this coin" in {f["key"]: f["text"] for f in
                                                          gh.flags(famous, None, LAUNCH, NOW)}["established"]
    late = row(created_at=LAUNCH + 3 * H, shared=0)
    assert {f["key"]: f["text"] for f in gh.flags(late, None, LAUNCH, NOW)}["after_launch"] == \
        "Repo created 3 hours after the token launched."


def test_dead_links_gone_repos_and_star_jumps():
    dead = row(status="missing", full_name=None, ts=None, ref="terrafying/docspider")
    assert gh.flags(dead, None, LAUNCH, NOW) == [{"key": "dead_link", "level": "bad", "text":
        "The GitHub link is dead: GitHub answers 404 for terrafying/docspider (deleted, private or never public)."}]
    gone = row(status="missing", gone_at=NOW - 5 * H)
    (f,) = gh.flags(gone, None, LAUNCH, NOW)
    assert f["key"] == "gone" and "since 2026-10-01" in f["text"] and "207 stars" in f["text"]
    assert keys(gh.flags(row(status="empty", ts=None, full_name=None, ref="dev", kind="account"), None, LAUNCH,
                         NOW)) == {"empty": "warn"}
    jump = gh.flags(row(shared=0), {"stars": 120, "forks": 9, "hours": 24.2}, LAUNCH, NOW)
    assert {f["key"]: f["text"] for f in jump}["stars_jump"] == "Stars up 120 (+138%) in 24.2 h."
    assert "stars_jump" not in keys(gh.flags(row(shared=0, stars=2000), {"stars": 30, "forks": 0, "hours": 24.0},
                                             LAUNCH, NOW))                   # 30 on 1,970 is no jump


# ─── the section ─────────────────────────────────────────────────────────────
def test_the_section_shape():
    history = snaps((25 * H, 160, 50), (10 * timedelta(minutes=1), 207, 64))
    s = gh.shape([row()], history, LAUNCH, NOW)
    assert set(s) == {"status", "as_of", "link", "via", "own", "not_own", "repo", "metrics", "change", "signals",
                      "flags", "links"}
    assert (s["own"], s["not_own"]) == (True, None)                         # created 5.8 days before the coin
    assert (s["status"], s["via"], s["repo"]["name"], s["repo"]["url"]) == (
        "fresh", "repo", "terrafying/ai-torture-chamber", "https://github.com/terrafying/ai-torture-chamber")
    assert s["metrics"]["stars"] == 207 and s["metrics"]["release"] is None
    assert s["change"]["h24"] == {"stars": 47, "forks": 14, "hours": 24.8} and s["change"]["d7"] is None
    assert s["signals"] == {"age_at_launch_days": 5.8, "days_since_push": 0.0, "stars_per_day": 28.8,
                            "velocity": "new", "shared_with": 1}                 # 207 stars in 7.2 days
    pending = gh.shape([row(status="pending", full_name=None, ts=None)], [], LAUNCH, NOW)
    assert (pending["status"], pending["repo"], pending["metrics"], pending["flags"]) == ("pending", None, None, [])
    assert gh.shape([row(ts=NOW - 4 * H)], [], LAUNCH, NOW)["status"] == "stale"


def test_the_section_is_about_the_best_link():
    acct = row(kind="account", ref="terrafying", link="https://github.com/terrafying", stars=207)
    dead = row(status="missing", ref="terrafying/old", full_name=None, ts=None, stars=None)
    other = row(ref="terrafying/docspider", full_name="terrafying/docspider", stars=3)
    assert gh.primary([dead, acct, other])["ref"] == "terrafying/docspider"    # a repo link over an account link
    assert gh.primary([dead, acct])["ref"] == "terrafying"
    s = gh.shape([dead, acct, other], [], LAUNCH, NOW)
    assert [x["status"] for x in s["links"]] == ["missing", "ok", "ok"] and s["via"] == "repo"


# ─── registry ────────────────────────────────────────────────────────────────
def test_the_job_runs_every_thirty_minutes():
    from albatross.deepend import registry
    assert "github" in registry.MODULES
    assert set(gh.JOBS) == {n for n, _ in gh.SCHEDULE} == set(gh.CADENCE) == {"deep_github"}
    assert registry.JOBS["deep_github"] is gh.run and registry.CADENCE["deep_github"] == 1800
    assert dict(gh.SCHEDULE)["deep_github"]["minute"] == "7,37"


def test_only_the_coins_own_repo_is_judged():
    """2026-10-01: Ajax linked PewDiePie's account (we picked his most-starred repo: "abandoned", a red flag), and
    3/acc linked a mathematician's repo 251 days older than the coin ("good: 19 commits"). Neither is the coin's."""
    launch = NOW
    acct = {"kind": "account", "full_name": "pewdiepie/x", "created_at": NOW - 900 * D}
    assert gh.own(acct, launch, ("AJAX", "Ajax")) == (False, "account")
    old = {"kind": "repo", "full_name": "teorth/optimizationproblems", "description": "Optimization problems",
           "topics": ["math"], "created_at": NOW - 251 * D}
    assert gh.own(old, launch, ("3ACC", "3/acc", "Addr111")) == (False, "older")
    assert gh.own(old | {"description": "Home of $3ACC"}, launch, ("3ACC", "3/acc")) == (True, None)   # names it
    ca = "3acCPump1111111111111111111111111111111pump"
    assert gh.own(old | {"homepage": f"https://x.io/{ca}"}, launch, ("AI", None, ca)) == (True, None)
    archify = {"kind": "repo", "full_name": "tt-a1i/archify", "description": "Archify: diagrams as code",
               "created_at": NOW - 135 * D}                               # a famous repo the coin borrowed by name
    assert gh.own(archify, launch, ("ARCHIFY", "Archify", ca)) == (False, "older")
    assert gh.own(archify | {"created_at": NOW - 5 * D}, launch, ("ARCHIFY", "Archify", ca)) == (True, None)
    lld = {"kind": "repo", "full_name": "x/lucky", "description": "Official repository of the Lucky Legend (LLD) project",
           "created_at": NOW - 90 * D}
    assert gh.own(lld, launch, ("LLD", "Lucky Legend", ca)) == (True, None)          # "(LLD)" names it
    ai = old | {"description": "Optimization problems (ai) for students"}
    assert gh.own(ai, launch, ("AI", None)) == (False, "older")              # a 2-letter "(ai)" is any text
    assert gh.own(ai | {"description": "Home of $AI"}, launch, ("AI", None)) == (True, None)
    assert gh.own(old | {"created_at": NOW - 10 * D}, launch, ()) == (True, None)          # made for the coin
    assert gh.own(old, None, ()) == (True, None)                                             # no launch: not judged
    from albatross.deepend import dossier
    line = dossier._github(dossier.Evidence(chain="solana", github={
        "own": False, "not_own": "older", "metrics": {"stars": 19, "commits_7d": 19},
        "flags": [{"key": "active", "level": "good", "text": "19 commits"}]}))
    assert line[0] == "unknown" and line[3] is None and "doesn't carry the coin's contract or ticker" in line[2]
    abandoned = dossier._github(dossier.Evidence(chain="solana", github={
        "own": False, "not_own": "account", "metrics": {}, "flags": [{"key": "abandoned", "level": "bad", "text": "x"}]}))
    assert abandoned[0] == "unknown" and abandoned[3] is None                               # no red flag

