import os
import sys
import hmac
import json
import time
import base64
import hashlib
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from vault_bridge import ALLOWED_AGENTS, memory_append, memory_query, bridge_status

VAULT_DIR_RAW = os.environ.get("VAULT_DIR", "")
VAULT_DIR = Path(VAULT_DIR_RAW).expanduser().resolve() if VAULT_DIR_RAW else Path("~/MemoryVault").expanduser().resolve()
BRIDGE_OAUTH_SECRET = os.environ.get("BRIDGE_OAUTH_SECRET", "")


def validate_startup_security():
    if not VAULT_DIR.exists():
        sys.exit(f"FATAL [FAIL-CLOSED]: VAULT_DIR '{VAULT_DIR}' does not exist.")
    if len(BRIDGE_OAUTH_SECRET.encode("utf-8")) < 32:
        sys.exit(
            "FATAL [FAIL-CLOSED]: BRIDGE_OAUTH_SECRET environment variable must be set "
            "and at least 32 bytes long before starting oauth_gateway.py."
        )


app = FastAPI(title="Fo-Shield Zero-Trust OAuth 2.1 + PKCE Remote Agent Bridge")
security = HTTPBearer(auto_error=False)

AUTH_CODES: dict[str, dict] = {}
TOKEN_TTL_SECONDS = 3600


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    padding = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + padding)


def compute_pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return b64url_encode(digest)


def issue_jwt(agent_id: str, scope: str = "vault:read vault:append") -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": "fo-shield-oauth-gateway",
        "sub": agent_id,
        "agent_id": agent_id,
        "scope": scope,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": secrets.token_hex(8)
    }
    signing_input = f"{b64url_encode(json.dumps(header).encode())}.{b64url_encode(json.dumps(payload).encode())}"
    sig = hmac.new(
        BRIDGE_OAUTH_SECRET.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256
    ).digest()
    return f"{signing_input}.{b64url_encode(sig)}"


def verify_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="Malformed JWT token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected_sig = hmac.new(
        BRIDGE_OAUTH_SECRET.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256
    ).digest()
    actual_sig = b64url_decode(parts[2])
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise HTTPException(status_code=401, detail="Invalid JWT signature")

    payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
    if int(time.time()) >= int(payload.get("exp", 0)):
        raise HTTPException(status_code=401, detail="JWT token expired")
    agent_id = payload.get("agent_id")
    if agent_id not in ALLOWED_AGENTS:
        raise HTTPException(status_code=403, detail=f"Unauthorized agent_id in token: {agent_id}")
    return payload


async def require_agent_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict:
    if not creds or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing OAuth 2.1 Bearer token")
    return verify_jwt(creds.credentials)


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": "fo-shield-oauth-gateway",
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"]
    }


@app.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str,
    client_id: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    redirect_uri: str = "https://localhost/callback",
    state: str = ""
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="Unsupported response_type; must be 'code'")
    if client_id not in ALLOWED_AGENTS:
        raise HTTPException(status_code=403, detail=f"Client agent '{client_id}' is not in ALLOWED_AGENTS")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="PKCE S256 is strictly required (plain is forbidden)")
    if len(code_challenge) < 43:
        raise HTTPException(status_code=400, detail="Invalid S256 code_challenge length")

    code = secrets.token_urlsafe(32)
    AUTH_CODES[code] = {
        "client_id": client_id,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + 180.0
    }
    return JSONResponse({
        "authorization_code": code,
        "client_id": client_id,
        "state": state,
        "redirect_uri": f"{redirect_uri}?code={code}&state={state}",
        "pkce_method": "S256"
    })


@app.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(...),
    code_verifier: str = Form(...),
    client_id: str = Form(...)
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant_type")

    record = AUTH_CODES.pop(code, None)
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or already-used authorization code")
    if time.time() > record["expires_at"]:
        raise HTTPException(status_code=400, detail="Authorization code expired")
    if record["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    computed_challenge = compute_pkce_s256(code_verifier)
    if not hmac.compare_digest(computed_challenge, record["code_challenge"]):
        raise HTTPException(status_code=401, detail="PKCE S256 verification failed")

    access_token = issue_jwt(agent_id=client_id)
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "scope": "vault:read vault:append",
        "agent_id": client_id
    })


class BridgeAppendRequest(BaseModel):
    agent_id: str | None = None
    category: str = "remote_agent_sync"
    entry: str


class BridgeQueryRequest(BaseModel):
    query: str
    top_k: int = 5


@app.post("/mcp/memory_append")
async def gateway_memory_append(
    body: BridgeAppendRequest,
    claims: dict = Depends(require_agent_claims)
):
    token_agent = claims["agent_id"]
    if body.agent_id and body.agent_id != token_agent:
        raise HTTPException(
            status_code=403,
            detail=f"Agent isolation violation: token bound to '{token_agent}' cannot write to '{body.agent_id}'"
        )
    rel_path = memory_append(agent_id=token_agent, category=body.category, entry=body.entry)
    return {
        "status": "appended",
        "authenticated_agent": token_agent,
        "log_path": rel_path
    }


@app.post("/mcp/memory_query")
async def gateway_memory_query(
    body: BridgeQueryRequest,
    claims: dict = Depends(require_agent_claims)
):
    res = memory_query(query=body.query, top_k=body.top_k)
    return {
        "authenticated_agent": claims["agent_id"],
        "results": res
    }


@app.get("/mcp/status")
async def gateway_status(claims: dict = Depends(require_agent_claims)):
    status = bridge_status()
    status["authenticated_agent"] = claims["agent_id"]
    status["oauth_pkce_s256"] = "enforced"
    return status


if __name__ == "__main__":
    validate_startup_security()
    print(f"[OAUTH GATEWAY] Starting Zero-Trust Bridge on 127.0.0.1:8443 for vault {VAULT_DIR}")
    uvicorn.run(app, host="127.0.0.1", port=8443, log_level="info")
