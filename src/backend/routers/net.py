"""Network settings — outbound proxy for the worker's internet traffic.

Backs Preferences → Network. The proxy URL may embed Basic-auth credentials,
so it is stored in the encrypted per-user credential store (never in the
plaintext prefs file) and is returned to the UI with the password masked.

NTLM/Kerberos-only proxies (no Basic auth) are not supported yet; the AI
provider calls will surface the proxy's 407 in their normal error paths.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from Application_Logic import Logic_AI_Credentials as credentials

from .. import net
from ..security import require_token

router = APIRouter(prefix="/api/net", tags=["net"],
                   dependencies=[Depends(require_token)])


class ProxyBody(BaseModel):
    url: str


@router.get("/proxy")
def get_proxy() -> dict:
    return net.proxy_status()


@router.put("/proxy")
def set_proxy(body: ProxyBody) -> dict:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=422, detail="Proxy URL is empty.")
    error = net.validate_proxy_url(url)
    if error:
        raise HTTPException(status_code=422, detail=error)
    credentials.set_network_proxy(url)
    net.apply_stored_proxy()  # live — no worker restart needed
    return net.proxy_status()


@router.delete("/proxy")
def clear_proxy() -> dict:
    credentials.clear_network_proxy()
    net.apply_stored_proxy()
    return net.proxy_status()
