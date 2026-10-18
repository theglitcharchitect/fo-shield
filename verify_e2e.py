import os
import asyncio
import secrets
from pathlib import Path

os.environ.setdefault("VAULT_DIR", str(Path("~/MemoryVault").expanduser().resolve()))
os.environ.setdefault("BRIDGE_OAUTH_SECRET", "fo_shield_synthetic_test_secret_32bytes_min_2026")
os.environ.setdefault("CLAUDE_MODEL", "claude-sonnet-4-6")

from fastapi.testclient import TestClient

import seed_vault
from indexer import init_db, index_file, hybrid_search, VAULT_DIR, DB_PATH
from fo_shield_server import (
    TASK_PATTERN,
    l1_regex_scrub,
    sync_compile_brief,
    stage_task,
    dispatch_approved_task,
    task_ledger,
)
from oauth_gateway import app as oauth_app, compute_pkce_s256
from custodian import run_custodian

# Synthetic fixtures ONLY (RFC 2606 / RFC 5737 style). These are not real
# credentials. They exist so the e2e suite can prove the L1 scrubber catches
# secret-shaped strings. GitHub secret scanning may flag the tskey-shaped
# fixture below; it is fake by construction ("k00000TEST-example").
FORBIDDEN_RAW_SECRETS = [
    "0000-1111-2222-3333",
    "EIN-99-0000000-XX",
    "EMP-ACME-2026-00421",
    "ShieldPlan-GRP-00099-TEST-PRIVATE",
    "tskey-auth-k00000TEST-example",
    "192.0.2.10",
    "+1-555-0199",
]


async def main():
    print("=" * 76)
    print("FO-SHIELD: END-TO-END ZERO-KNOWLEDGE ARCHITECTURE VERIFICATION SUITE")
    print("=" * 76)

    print("\n[STEP 1/5] Seeding Zero-PII Synthetic MemoryVault...")
    seed_vault.seed()
    assert VAULT_DIR.exists(), "VAULT_DIR failed to initialize"

    print("\n[STEP 2/5] Building Hybrid Index (sqlite-vec 384-d + FTS5 BM25 + CrossEncoder)...")
    init_db()
    md_files = sorted(VAULT_DIR.rglob("*.md"))
    for md in md_files:
        index_file(md)
    assert DB_PATH.exists(), f"Index database not created at {DB_PATH}"

    query = "Book ergonomic workspace assessment session and VESA dual-monitor desk mount"
    hits = hybrid_search(query, top_k=4)
    assert len(hits) >= 2, "Hybrid search returned insufficient chunks"
    print(f"  -> Retrieved {len(hits)} ranked chunks. Top hit: {hits[0]['file_path']} ({hits[0]['heading']}) [Score: {hits[0]['score']}]")

    print("\n[STEP 3/5] Auditing L1 Regex Scrubber & L2 Brief Compiler...")
    raw_sample = "\n".join(h["content"] for h in hits)
    scrubbed, caught = l1_regex_scrub(raw_sample)
    for secret in FORBIDDEN_RAW_SECRETS:
        assert secret not in scrubbed, f"CRITICAL: Secret '{secret}' bypassed L1 regex scrubber!"
    print(f"  -> L1 Scrubber caught and redacted: {caught}")

    hermes_log = VAULT_DIR / "logs" / "hermes" / "2026-10-18.md"
    log_text = hermes_log.read_text(encoding="utf-8")
    matches = list(TASK_PATTERN.finditer(log_text))
    assert len(matches) == 1, f"Expected 1 unchecked #fo task in {hermes_log}, found {len(matches)}"

    raw_task = matches[0].group(1).strip()
    full_line = matches[0].group(0)
    await stage_task(raw_task, hermes_log, full_line)
    assert len(task_ledger) == 1, "Task failed to stage in task_ledger"

    task_id = next(iter(task_ledger.keys()))
    compiled = task_ledger[task_id]["compiled"]
    brief = compiled["sanitized_fo_brief"]

    for secret in FORBIDDEN_RAW_SECRETS:
        assert secret not in brief, f"CRITICAL: Secret '{secret}' leaked into sanitized_fo_brief!"

    print(f"  -> Sanitized Fo Brief: {brief}")
    print(f"  -> Injected Facts ({len(compiled['injected_facts'])}): {compiled['injected_facts']}")
    print(f"  -> Withheld Secrets ({len(compiled['withheld_secrets'])}): {compiled['withheld_secrets']}")

    completed_record = await dispatch_approved_task(task_id)
    assert completed_record["status"] == "COMPLETED", "Task dispatch did not complete"
    updated_hermes_text = hermes_log.read_text(encoding="utf-8")
    assert f"- [x] #fo [FO-SHIELD:{task_id}]" in updated_hermes_text, "Source Markdown checkbox was not checked off!"
    print(f"  -> Verified source Markdown writeback: - [x] #fo [FO-SHIELD:{task_id}]")

    print("\n[STEP 4/5] Verifying OAuth 2.1 + PKCE (S256) Gateway & Agent Isolation...")
    client = TestClient(oauth_app)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = compute_pkce_s256(code_verifier)

    auth_resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "grokbot",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "test_state_123"
        }
    )
    assert auth_resp.status_code == 200, f"OAuth authorize failed: {auth_resp.text}"
    auth_code = auth_resp.json()["authorization_code"]

    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "code_verifier": code_verifier,
            "client_id": "grokbot"
        }
    )
    assert token_resp.status_code == 200, f"OAuth token exchange failed: {token_resp.text}"
    jwt_token = token_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {jwt_token}"}

    ok_append = client.post(
        "/mcp/memory_append",
        headers=headers,
        json={"agent_id": "grokbot", "category": "vendor_research", "entry": "Confirmed NVMe Gen4 2TB stock availability at $128/unit."}
    )
    assert ok_append.status_code == 200, f"Authorized append failed: {ok_append.text}"

    spoof_append = client.post(
        "/mcp/memory_append",
        headers=headers,
        json={"agent_id": "claude_code", "category": "spoof", "entry": "Unauthorized cross-agent write"}
    )
    assert spoof_append.status_code == 403, "SECURITY FAILURE: Cross-agent spoofing was not blocked!"
    print("  -> OAuth 2.1 + PKCE (S256) verified. Cross-agent log spoofing blocked with HTTP 403.")

    print("\n[STEP 5/5] Running Single-Writer Custodian Daemon (fcntl.flock + Git Snapshot)...")
    custodian_res = run_custodian()
    assert custodian_res["status"] == "reconciled", f"Custodian failed: {custodian_res}"
    print(f"  -> Custodian reconciled {custodian_res['processed_logs']} logs into {custodian_res['updated_canonicals']}")

    print("\n" + "=" * 76)
    print("ALL 5 ARCHITECTURAL SUBSYSTEMS PASSED VERIFICATION WITH ZERO PII LEAKAGE.")
    print("=" * 76)


if __name__ == "__main__":
    asyncio.run(main())
