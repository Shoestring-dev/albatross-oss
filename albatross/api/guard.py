"""Who may use the Setup page's API (api/setup.py and the routes that connect accounts).

The Setup page reads which keys are set and writes new ones, so it answers only:
- to a signed-in visitor when a site password is set (the same cookie or Basic credentials as api/gate.py), whether
  or not Caddy stands in front;
- with no site password, only on the host names in settings.allowed_hosts (localhost by default). A browser sends the
  name it typed in the Host header, so a page on another site that points its own name at this machine (DNS
  rebinding) is refused.
Writes also need a JSON body, the X-Albatross-Action header (like the Output page's controls) and, when the browser
says where the request came from, the same origin: another site can't make a browser send either without a CORS
preflight, which this app never grants.
"""

import asyncio
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from albatross.api import gate
from albatross.config import settings


def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):                     # [::1]:8787
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def allowed_hosts() -> set[str]:
    return {h.strip().lower().strip("[]") for h in settings.allowed_hosts.split(",") if h.strip()}


async def read_guard(request: Request) -> None:
    if settings.site_password_hash:
        if gate.session_user(request.cookies.get(gate.COOKIE), settings.site_session_secret):
            return
        if await asyncio.to_thread(gate.basic_ok, request.headers.get("authorization")):
            return
        raise HTTPException(401, "Sign in first.")
    if _hostname(request.headers.get("host", "")) not in allowed_hosts():
        raise HTTPException(403, "Setup answers only at http://localhost:8787 on the machine running Albatross. "
                                 "To use it from another device, set a site password or add this host name to "
                                 "ALLOWED_HOSTS.")


async def write_guard(request: Request) -> None:
    await read_guard(request)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Send JSON.")
    if request.headers.get("x-albatross-action") != "1":
        raise HTTPException(403, "missing the X-Albatross-Action header")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Refused a request from another site.")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, "Refused a request from another site.")
