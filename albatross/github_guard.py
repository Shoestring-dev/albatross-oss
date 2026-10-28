"""Since 2026-10-01: nothing hosted on GitHub is fetched, opened, shown as an image or handed to a model. The one exception is
albatross/deepend/github.py, which reads public metadata (stars, commit counts, dates) from api.github.com through
its own allowlist and never a file. This module is the shared guard:

- hosted(url): is this URL on a GitHub host? Drops GitHub links from model prompts, citations and search evidence
  (llm/openrouter.py, agents/oracle.py, deepend/fit.py).
- SEARCH_EXCLUDE: the domains every web search our agents run leaves out (OpenRouter's search on the Perplexity
  engine, which honours excluded_domains; proven live 2026-10-01: a GitHub-heavy search returned no GitHub page).
  The Reddit scout is the exception: only Google's native search sees Reddit, and it ignores excluded_domains, so
  deepend/reddit.py keeps its queries to site:reddit.com and drops a read's prose when its search saw GitHub.

Any new model with web tools must get the same: GitHub left out of its search and its fetches, and GitHub URLs never
counted as evidence. The Claude plan lane runs with every tool off, so it can't search or fetch at all.
"""
from __future__ import annotations

from urllib.parse import urlsplit

DOMAINS = ("github.com", "githubusercontent.com", "github.io", "githubassets.com", "github.dev", "githubapp.com",
           "ghcr.io")
# Search engines don't all say whether a domain covers its subdomains, so the hosts that serve files are named too.
SEARCH_EXCLUDE = ["github.com", "gist.github.com", "githubusercontent.com", "raw.githubusercontent.com",
                  "gist.githubusercontent.com", "objects.githubusercontent.com", "github.io", "githubassets.com"]


def host(url) -> str:
    """The lower-case hostname of a URL or bare domain ('' when there is none)."""
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip()
    if "://" not in u:
        u = "//" + u.lstrip("/")
    try:
        return (urlsplit(u).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def hosted(url) -> bool:
    """True for anything on github.com, any *.githubusercontent.com host, GitHub Pages and the rest of DOMAINS."""
    h = host(url)
    return any(h == d or h.endswith("." + d) for d in DOMAINS)
