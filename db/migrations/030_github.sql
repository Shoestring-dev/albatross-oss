-- The Deep End, GitHub (albatross/deepend/github.py): the public metadata of the repos behind the tokens we watch,
-- over time. Metadata only, through the GitHub REST API. The owner's hard rule: NEVER download a repo (no clone, archive,
-- file, readme or release asset); github.py can only request allowlisted metadata endpoints.

-- Which repo each token links: one row per token and GitHub link (a token may link several).
CREATE TABLE IF NOT EXISTS de_token_repos (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    ref             TEXT NOT NULL,                 -- what the link names, lowercased: 'owner/repo', or 'owner' alone
    kind            TEXT NOT NULL,                 -- repo (the link names a repo, or a file in one) | account (a user
                                                   -- or org: it stands for the account's most-starred public repo)
    link            TEXT NOT NULL,                 -- the URL as the token's links gave it, first seen
    source          TEXT NOT NULL,                 -- where we found it: jupiter | dexscreener | discovery
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending (not asked yet) | ok | missing (GitHub answers 404:
                                                   -- deleted, private or never public) | empty (an account without
                                                   -- public repos)
    repo_id         BIGINT,                        -- the repo it resolved to (de_github_repos); kept when it's gone
    first_seen      TIMESTAMPTZ NOT NULL,
    seen_at         TIMESTAMPTZ NOT NULL,          -- the last time the token's links still showed it
    resolved_at     TIMESTAMPTZ,                   -- the last time GitHub was asked (account and dead links: daily)
    PRIMARY KEY (chain, address, ref)
);
CREATE INDEX IF NOT EXISTS de_token_repos_ref ON de_token_repos (ref);
CREATE INDEX IF NOT EXISTS de_token_repos_repo ON de_token_repos (repo_id);

-- One row per repo: who it is and what rarely changes, as of our last snapshot.
CREATE TABLE IF NOT EXISTS de_github_repos (
    repo_id         BIGINT PRIMARY KEY,            -- GitHub's id: survives renames and transfers
    full_name       TEXT NOT NULL,                 -- owner/name as GitHub spells it now
    owner_type      TEXT,                          -- User | Organization
    description     TEXT,
    homepage        TEXT,
    language        TEXT,                          -- GitHub's main language
    license         TEXT,                          -- SPDX id, or GitHub's name for one it can't identify ('Other');
                                                   -- NULL: none
    topics          TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ,                   -- when the repo was created on GitHub (vs the token's launch)
    fork            BOOLEAN,
    parent          TEXT,                          -- a fork: the full name of the repo it was forked from
    parent_stars    INTEGER,                       -- that repo's stars when we looked ("a fork of a known project")
    first_seen      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,          -- our last snapshot
    gone_at         TIMESTAMPTZ                    -- since when GitHub answers 404 for it (deleted or made private)
);

-- The repo over time (job deep_github: a hot token's repo every 30 minutes, a warm one's every 2 hours). Star and
-- fork changes over 24 h and 7 d are computed from these rows.
CREATE TABLE IF NOT EXISTS de_github_snapshots (
    repo_id         BIGINT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    stars           INTEGER,
    forks           INTEGER,
    watchers        INTEGER,                       -- subscribers_count: people watching (watchers_count is the stars)
    open_issues     INTEGER,                       -- open issues plus open pull requests (GitHub counts both)
    pushed_at       TIMESTAMPTZ,                   -- the last push to any branch
    archived        BOOLEAN,                       -- read-only by its owner's choice
    disabled        BOOLEAN,                       -- disabled by GitHub
    commits_7d      INTEGER,                       -- commits on the default branch in the 7 / 30 days to activity_at
    commits_30d     INTEGER,
    contributors    INTEGER,                       -- commit authors: GitHub accounts plus emails linked to none
    release_tag     TEXT,                          -- the latest release, prereleases included; NULL: none
    release_at      TIMESTAMPTZ,
    activity_at     TIMESTAMPTZ,                   -- when commits, contributors and the release were read: after a push
                                                   -- or every 6 hours, carried forward in between; NULL: never
    PRIMARY KEY (repo_id, ts)
);
