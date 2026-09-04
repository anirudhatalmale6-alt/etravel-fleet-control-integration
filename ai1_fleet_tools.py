"""Guarded fleet adapter for ai.etravel.gr and ai1.etravel.gr.

This is a corrected replacement for server/ai1_fleet_tools.py as shipped in
eTravel AI Fleet Control Pack 1.0.0. The shipped version cannot work against
this server; what follows says exactly why, so the next person does not undo it.

1. AUTHENTICATION.  The shipped fleet_status() did a plain unauthenticated GET
   to http://127.0.0.1:8787/fleet/status. That route is behind the controller's
   browser login session (app/local_auth.py) and answers 401. The only doors on
   8787 that accept a machine caller are /node/v1/*, which are exempt from the
   session middleware and verified by HMAC in app/fleet.py
   verify_incoming_request(). This module uses that protocol.

2. /fleet/status IS NOT A SITE LIST.  app/fleet.py all_status() returns
   {"nodes": {...}} - the status of the four nodes. The shipped
   fleet_list_sites() looked for a key named "sites" or "targets", found
   neither, and returned an EMPTY LIST without raising. Asked to list the 27
   eShops sites it would have answered "0", confidently and wrongly. Here a
   missing site list is an error, never an empty answer.

3. NO ROUTE RETURNED THE 126 SITES AT ALL.  /health gives counts only. So this
   module reads GET /node/v1/sites, which is added by the companion patch
   01-controller-node-v1-sites.diff. /etc/etravel-ai-ops/sites.yml stays the
   single source of truth; no second inventory is created.

4. TOOL NAME COLLISION.  The pack asked for a tool called
   request_production_action. A tool of that exact name already exists in the
   live agent.py with a different schema - it is the one that creates the
   Navigator approvals. This module exports fleet_request_production_action
   instead, so the working approval flow is left alone.

5. CLOUDFLARE.  The shipped NAVIGATOR default was https://www.etravel.gr/...,
   which from this server hits Cloudflare's bot challenge and returns 403 HTML.
   A 403 there is not an authentication result. The default here is the origin,
   with the Host header set, so the answer comes from WordPress.

Secrets are read from the environment only. Both files this needs are
root-owned and unreadable by the etravelai service user, so they must arrive
through a root-owned systemd EnvironmentFile - see 03-etravel-ai-next-fleet.env.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any

CONTROLLER = os.environ.get(
    "ETR_FLEET_CONTROLLER_URL", "http://127.0.0.1:8787"
).rstrip("/")

# The public hostname is kept for SNI and the Host header, but the connection
# is made to the origin address. Dropping the hostname and dialling the IP
# directly is not equivalent: the TLS handshake would then carry the IP as its
# server name, Apache would serve the default virtual host, and the answer
# would be a 403 that looks exactly like an authentication failure.
NAVIGATOR_HOST = os.environ.get("ETR_NAVIGATOR_HOST", "www.etravel.gr")
NAVIGATOR_ORIGIN_IP = os.environ.get("ETR_NAVIGATOR_ORIGIN_IP", "65.21.44.213")
NAVIGATOR_BASE = os.environ.get("ETR_NAVIGATOR_BASE", "/wp-json/eto/v1")

CALLER_ID = os.environ.get("ETR_FLEET_CALLER_ID", "primary")
TIMEOUT = int(os.environ.get("ETR_FLEET_TIMEOUT", "30"))
USER_AGENT = os.environ.get(
    "ETR_FLEET_USER_AGENT",
    "eTravel-AI-Fleet/1.0 (+https://ai1.etravel.gr; server 65.21.44.213)",
)


class FleetAdapterError(RuntimeError):
    """Raised instead of returning an empty result that looks like an answer."""


# --------------------------------------------------------------- controller


def _controller_secret() -> bytes:
    secret = os.environ.get("ETR_FLEET_NODE_SECRET", "").strip()
    if len(secret) < 32:
        raise FleetAdapterError(
            "ETR_FLEET_NODE_SECRET is not configured. The controller cannot be "
            "reached without it; see 03-etravel-ai-next-fleet.env."
        )
    return secret.encode()


def _signed_get(path: str) -> Any:
    """GET a /node/v1/ route using the controller's own HMAC protocol.

    Wire format, from app/fleet.py signature_payload():
        timestamp LF nonce LF METHOD LF path LF sha256(body)
    carried in X-AIOPS-Node-ID, X-AIOPS-Timestamp, X-AIOPS-Nonce,
    X-AIOPS-Signature. The nonce must be exactly 32 lowercase hex characters or
    the controller rejects it as fleet_nonce_invalid.
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body = b""
    payload = "\n".join(
        (timestamp, nonce, "GET", path, hashlib.sha256(body).hexdigest())
    ).encode()
    signature = hmac.new(_controller_secret(), payload, hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        CONTROLLER + path,
        method="GET",
        headers={
            "Accept": "application/json",
            "X-AIOPS-Node-ID": CALLER_ID,
            "X-AIOPS-Timestamp": timestamp,
            "X-AIOPS-Nonce": nonce,
            "X-AIOPS-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise FleetAdapterError(
            f"controller {path} returned HTTP {exc.code}: {detail}"
        ) from exc


def fleet_status() -> dict[str, Any]:
    """Live status of the node this controller runs on, from the controller."""
    return _signed_get("/node/v1/status")


def fleet_list_sites(
    family: str | None = None,
    node: str | None = None,
    environment: str | None = None,
) -> list[dict[str, Any]]:
    """The authoritative site inventory, filtered.

    Raises rather than returning an empty list when the controller does not
    supply a site list. An empty answer to "list the eShops sites" is
    indistinguishable from "there are none", and only one of those is ever true.
    """
    payload = _signed_get("/node/v1/sites")
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise FleetAdapterError(
            "The controller did not return a site list. Do not report this as "
            "zero sites; the inventory is unavailable. Check that "
            "/node/v1/sites exists on the controller "
            "(01-controller-node-v1-sites.diff)."
        )
    return [
        site
        for site in sites
        if (not family or site.get("family") == family)
        and (not node or site.get("node") == node)
        and (not environment or site.get("environment") == environment)
    ]


# ---------------------------------------------------------------- navigator


class _OriginHTTPSConnection(http.client.HTTPSConnection):
    """Speak to the origin address while still naming the public host.

    Same effect as curl --resolve www.etravel.gr:443:65.21.44.213. The TCP
    connection goes to the origin, but SNI and the Host header both say
    www.etravel.gr, so Apache picks the right virtual host and WordPress
    answers instead of Cloudflare's bot challenge.

    Certificate verification is off because the certificate is issued for the
    hostname and we are dialling the address; the request body is HMAC-signed
    either way, and the hop never leaves this machine's own network interface.
    """

    def __init__(self, host: str, timeout: int = 30) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, 443, timeout=timeout, context=context)

    def connect(self) -> None:
        self.sock = self._context.wrap_socket(
            socket.create_connection((NAVIGATOR_ORIGIN_IP, 443), self.timeout),
            server_hostname=self.host,
        )


def navigator_search(query: str, limit: int = 20) -> Any:
    """Search Operations Navigator using its canonical signed protocol.

    The wire format is fixed by includes/class-eto-signing.php:
        message = timestamp LF nonce LF METHOD LF route LF q
    The route is signed WITHOUT the /wp-json prefix; q is signed as the plain
    value, not percent-encoded.
    """
    secret = os.environ.get("ETR_NAVIGATOR_HMAC_SECRET", "").strip()
    if len(secret) < 32:
        raise FleetAdapterError("ETR_NAVIGATOR_HMAC_SECRET is not configured")

    route = "/eto/v1/search"
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    message = "\n".join((timestamp, nonce, "GET", route, query))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    path = NAVIGATOR_BASE + "/search?" + urllib.parse.urlencode({"q": query})
    connection = _OriginHTTPSConnection(NAVIGATOR_HOST, timeout=TIMEOUT)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "Host": NAVIGATOR_HOST,
                # Required. A request with no User-Agent, or one that looks like
                # a script, is answered with a bare "Access denied." 403 by the
                # site's own protection - before WordPress or the Navigator
                # signature check are ever reached. Measured on 5 Sep 2026:
                #   default curl UA        -> 401 rest_forbidden  (WordPress)
                #   empty UA               -> 403 Access denied.  (blocked)
                #   Python-urllib/3.11     -> 403 Access denied.  (blocked)
                # A 403 here is not an authentication result. Do not read it as
                # one, and do not "fix" it by weakening the signature.
                "User-Agent": USER_AGENT,
                "X-ETO-Timestamp": timestamp,
                "X-ETO-Nonce": nonce,
                "X-ETO-Signature": signature,
            },
        )
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise FleetAdapterError(
                f"navigator search returned HTTP {response.status} "
                f"from the origin: {raw[:200]!r}"
            )
        payload = json.loads(raw.decode())
    finally:
        connection.close()
    results = payload.get("results")
    if isinstance(results, list) and limit:
        payload["results"] = results[:limit]
    return payload


# ------------------------------------------------------------- proposal only


ALLOWED_PROPOSAL_ACTIONS = {
    "flush_rewrite_rules",
    "delete_expired_transients",
    "spawn_due_cron",
    "activate_plugin",
    "deactivate_plugin",
    "update_allowlisted_option",
    "cloudflare_purge_urls",
    "cloudflare_purge_zone",
}


def fleet_request_production_action(
    target_id: str,
    action: str,
    parameters: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Queue a proposal. This function never executes anything.

    Named fleet_request_production_action, not request_production_action: a
    tool with the latter name already exists in the live agent and creates the
    Navigator approvals. Two tools cannot share a name.
    """
    if action not in ALLOWED_PROPOSAL_ACTIONS:
        raise FleetAdapterError(f"action {action!r} is not allowlisted")
    if not target_id:
        raise FleetAdapterError("target_id is required")

    known = {site.get("target_id") for site in fleet_list_sites()}
    if target_id not in known:
        raise FleetAdapterError(
            f"target_id {target_id!r} is not in the controller inventory"
        )

    return {
        "proposal": {
            "target_id": target_id,
            "action": action,
            "parameters": parameters,
            "reason": reason,
            "source": "ai1",
            "execute": False,
        },
        "execution_performed": False,
        "status": "proposal_recorded_locally",
        "note": (
            "The controller's approval-create endpoint has not been mapped yet. "
            "This proposal is returned to the caller and is NOT queued anywhere. "
            "Do not tell the owner an approval exists."
        ),
    }
