"""Update-check relay (GitHub Releases API).

The SPA originally queried ``api.github.com`` directly from the embedded
webview. The desktop shell now launches WebView2 with ``--no-proxy-server``
(loopback traffic to the worker must never be routed into a corporate proxy,
which cannot connect back), so on proxy-only networks the webview has no
internet access of its own. The check therefore runs here in the worker:
``requests`` resolves the system proxy — environment variables *and* the
Windows registry (per-user Internet Settings) — via urllib's ``getproxies()``.

Best-effort by design: on a network where GitHub needs interactive proxy
authentication this returns 502 and the SPA shows its error state instead of
hanging.
"""
from __future__ import annotations

import requests
from fastapi import APIRouter, Depends, HTTPException

from ..security import require_token

router = APIRouter(prefix="/api/update", tags=["update"],
                   dependencies=[Depends(require_token)])

GITHUB_REPO = "alexmasso96/Embeded-Software-Architecture-Validation"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


@router.get("/latest")
def latest_release() -> dict[str, str]:
    """Return ``{tag_name, body}`` of the newest published GitHub release."""
    try:
        res = requests.get(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub unreachable ({exc.__class__.__name__}).",
        )
    if res.status_code == 404:
        raise HTTPException(status_code=404, detail="No published releases found.")
    if not res.ok:
        raise HTTPException(
            status_code=502, detail=f"GitHub API responded {res.status_code}."
        )
    data = res.json()
    return {
        "tag_name": str(data.get("tag_name") or ""),
        "body": str(data.get("body") or ""),
    }
