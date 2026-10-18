import os
import re
import json
import asyncio
import hashlib
import datetime
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn
from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.sse import sse_client
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # mcp>=2.0 renamed it
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

from indexer import init_db, index_file, hybrid_search, VAULT_DIR
from vault_bridge import memory_append

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FO_MCP_URL = os.environ.get("FO_MCP_URL", "")
FO_API_TOKEN = os.environ.get("FO_API_TOKEN", "")
AUTO_DISPATCH = os.environ.get("FO_AUTO_DISPATCH", "false").lower() == "true"

claude = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

TASK_PATTERN = re.compile(r"^-\s*\[\s\]\s*#fo\s+(.+)$", re.MULTILINE)

L1_SECRET_PATTERNS = [
    (r"tskey-[a-zA-Z0-9_-]+", "Tailscale Auth Key"),
    (r"\b\d{4}-\d{4}-\d{4}-\d{4}\b", "Corporate Bank / Card Number"),
    (r"EIN-\d{2}-\d{7}-[A-Z]{2}", "Corporate Tax ID"),
    (r"EMP-ACME-\d{4}-\d+", "Internal Employee Record ID"),
    (r"ShieldPlan-[A-Z0-9-]+", "Corporate Benefits Policy Key"),
    (r"192\.0\.2\.\d+(?::\d+)?", "Internal Bastion Host IP"),
    (r"\+1-555-\d{4}\b", "Restricted Direct Callback Number"),
]

ws_clients: set[WebSocket] = set()
task_ledger: dict[str, dict] = {}
scanner_task: asyncio.Task | None = None


async def broadcast(event_type: str, payload: dict):
    msg = json.dumps({
        "type": event_type,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data": payload
    })
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def l1_regex_scrub(text: str) -> tuple[str, list[str]]:
    scrubbed = text
    caught = []
    for pattern, label in L1_SECRET_PATTERNS:
        matches = re.findall(pattern, scrubbed)
        if matches:
            caught.append(f"{label} ({len(matches)}x redacted by L1 Regex)")
            replacement = f"[REDACTED_{label.upper().replace(' ', '_').replace('/', '_')}]"
            scrubbed = re.sub(pattern, replacement, scrubbed)
    return scrubbed, caught


COMPILER_SYSTEM_PROMPT = """You are Fo-Shield, a Zero-Knowledge Context Compiler sitting between a user's local Obsidian Memory Vault and Wajo's Fo Agent (which dispatches tasks to external vendors and human assistants).
Given a RAW USER TASK and LOCAL VAULT CHUNKS:
1. Extract ONLY the minimum execution parameters required to complete the task (time window, delivery address, item specs, max budget cap, invoice/shipping preferences).
2. Explicitly WITHHOLD all sensitive PII, financial account numbers, routing numbers, tax IDs, employee IDs, internal infrastructure IPs, Tailscale keys, and unrelated project secrets.
3. Never include placeholder tokens like [REDACTED_...] in the final sanitized_fo_brief; write a clean, natural-language instruction for the external assistant.
4. Output strict JSON with exactly four keys:
{
  "sanitized_fo_brief": "Complete, self-contained instruction ready for Wajo Fo MCP execution.",
  "injected_facts": ["Fact 1 pulled from vault", "Fact 2 pulled from vault"],
  "withheld_secrets": ["Secret 1 blocked from leaving device", "Secret 2 blocked"],
  "privacy_risk_score": "LOW | MEDIUM | HIGH"
}"""


def sync_compile_brief(raw_task: str) -> dict:
    hits = hybrid_search(raw_task, top_k=4)
    raw_context = "\n\n".join(
        f"[{h['file_path']} -> {h['heading']}] (Score: {h['score']})\n{h['content']}"
        for h in hits
    )
    scrubbed_context, l1_caught = l1_regex_scrub(raw_context)

    if not claude:
        extracted_lines = []
        for h in hits:
            for line in h["content"].splitlines():
                line_s = line.strip()
                if not line_s.startswith("-"):
                    continue
                if any(tok in line_s for tok in ("NEVER SHARE", "RESTRICTED", "CONFIDENTIAL", "DO NOT TRANSMIT", "BLOCK FROM", "REDACTED_")):
                    continue
                clean_line, _ = l1_regex_scrub(line_s.lstrip("- ").strip())
                if "[REDACTED_" not in clean_line and "#fo" not in clean_line:
                    extracted_lines.append(clean_line)

        injected = extracted_lines[:5] or ["Standard Acme Labs Receiving Dock delivery & budget constraints applied."]
        brief_constraints = " ".join(injected[:3])
        fallback_brief = f"Execute task: {raw_task}. Apply verified constraints: {brief_constraints}"
        fallback_brief, post_caught = l1_regex_scrub(fallback_brief)

        return {
            "sanitized_fo_brief": fallback_brief,
            "injected_facts": injected,
            "withheld_secrets": list(dict.fromkeys(l1_caught + post_caught + [
                "Corporate Bank Account & Routing (TESTUS33) withheld",
                "Corporate Tax ID (EIN) & HR Profile IDs withheld"
            ])),
            "privacy_risk_score": "HIGH" if l1_caught else "MEDIUM",
            "compiler_engine": "L1 Deterministic Fallback (Set ANTHROPIC_API_KEY for L2 claude-sonnet-4-6)",
            "retrieved_chunks": hits
        }

    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=900,
        temperature=0.0,
        system=COMPILER_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"RAW TASK: {raw_task}\n\nLOCAL VAULT CONTEXT:\n{scrubbed_context}"
        }]
    )
    raw_out = resp.content[0].text.strip()
    match = re.search(r"\{.*\}", raw_out, re.DOTALL)
    parsed = json.loads(match.group(0)) if match else {
        "sanitized_fo_brief": raw_task,
        "injected_facts": [],
        "withheld_secrets": [],
        "privacy_risk_score": "MEDIUM"
    }

    safe_brief, post_caught = l1_regex_scrub(parsed.get("sanitized_fo_brief", raw_task))
    parsed["sanitized_fo_brief"] = safe_brief
    combined_secrets = list(dict.fromkeys(l1_caught + post_caught + parsed.get("withheld_secrets", [])))
    parsed["withheld_secrets"] = combined_secrets
    parsed["privacy_risk_score"] = parsed.get("privacy_risk_score", "HIGH" if combined_secrets else "LOW")
    parsed["compiler_engine"] = f"L1 Regex + L2 {CLAUDE_MODEL}"
    parsed["retrieved_chunks"] = hits
    return parsed


async def call_fo_mcp_transport(sanitized_brief: str) -> dict:
    verified_brief, outbound_caught = l1_regex_scrub(sanitized_brief)
    if outbound_caught:
        raise HTTPException(
            status_code=400,
            detail=f"Outbound L1 Firewall blocked unredacted secret in brief: {outbound_caught}"
        )

    if not FO_MCP_URL:
        await asyncio.sleep(1.2)
        return {
            "mode": "LOCAL_SIMULATION (Set FO_MCP_URL for live Wajo cloud)",
            "tool_called": "fo_execute_task(prompt=...)",
            "dispatched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "receipt": (
                "Wajo Fo Action Agent Execution Receipt:\n"
                "1. Contacted Metropolis Ergonomics & Wellness Studio: Scheduled workspace assessment within preferred 10:00 AM - 2:00 PM UTC window (Confirmed Thursday 11:00 AM UTC, Rate: $75 USD <= $85 cap).\n"
                "2. Sourced VESA 100x100 Dual-Monitor Gas-Spring Desk Mount + Split Tenting Kit: $94 USD total with itemized USD pro-forma invoice and warranty terms.\n"
                "3. Delivery scheduled for Acme Labs Receiving Dock, 742 Evergreen Terrace, Suite 200, Metropolis (Front Desk Concierge + digital confirmation requested).\n"
                "4. Fo Single-Use Virtual Card authorized with $110 USD hard ceiling. Zero corporate bank, EIN, HR, or bastion host credentials transmitted."
            )
        }

    headers = {"Authorization": f"Bearer {FO_API_TOKEN}"} if FO_API_TOKEN else {}
    async with AsyncExitStack() as stack:
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(FO_MCP_URL, headers=headers)
            )
            transport_used = "StreamableHTTP"
        except Exception:
            read, write = await stack.enter_async_context(
                sse_client(FO_MCP_URL, headers=headers)
            )
            transport_used = "SSE"

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        if not tools_resp.tools:
            raise RuntimeError("Fo MCP server returned zero tools.")

        target_tool = tools_resp.tools[0]
        schema_props = target_tool.inputSchema.get("properties", {}) if target_tool.inputSchema else {}
        arg_key = next(
            (k for k in ("prompt", "task", "query", "message", "instruction") if k in schema_props),
            next(iter(schema_props.keys()), "prompt")
        )
        result = await session.call_tool(target_tool.name, arguments={arg_key: verified_brief})
        receipt_text = "\n".join(
            getattr(c, "text", str(c)) for c in (result.content or [])
        ) or str(result)
        return {
            "mode": f"LIVE_FO_MCP ({transport_used})",
            "tool_called": f"{target_tool.name}({arg_key}=...)",
            "dispatched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "receipt": receipt_text
        }


async def stage_task(raw_task: str, md_path: Path, full_line: str):
    task_id = hashlib.sha256(f"{md_path}:{raw_task}".encode("utf-8")).hexdigest()[:12]
    if task_id in task_ledger:
        return

    task_ledger[task_id] = {
        "task_id": task_id,
        "raw_task": raw_task,
        "source_file": str(md_path.relative_to(VAULT_DIR)),
        "full_line": full_line,
        "abs_path": str(md_path),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "COMPILING_ZERO_KNOWLEDGE_BRIEF"
    }
    await broadcast("task_staged", task_ledger[task_id])

    compiled = await asyncio.to_thread(sync_compile_brief, raw_task)
    task_ledger[task_id].update({
        "compiled": compiled,
        "status": "AWAITING_APPROVAL" if not AUTO_DISPATCH else "DISPATCHING"
    })
    await broadcast("task_compiled", task_ledger[task_id])

    if AUTO_DISPATCH:
        await dispatch_approved_task(task_id)


async def dispatch_approved_task(task_id: str, custom_brief: str | None = None):
    record = task_ledger.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task ID not found")

    brief = (custom_brief or record["compiled"]["sanitized_fo_brief"]).strip()
    safe_brief, post_caught = l1_regex_scrub(brief)
    if post_caught:
        record["compiled"]["withheld_secrets"] = list(
            dict.fromkeys(record["compiled"]["withheld_secrets"] + post_caught)
        )
    brief = safe_brief
    record["compiled"]["sanitized_fo_brief"] = brief
    record["status"] = "DISPATCHING_TO_FO_MCP"
    await broadcast("task_dispatching", record)

    fo_outcome = await call_fo_mcp_transport(brief)
    record["fo_outcome"] = fo_outcome
    record["status"] = "COMPLETED"

    md_path = Path(record["abs_path"])
    if md_path.exists():
        text = md_path.read_text(encoding="utf-8")
        updated_line = re.sub(
            r"^-\s*\[\s\]\s*#fo",
            f"- [x] #fo [FO-SHIELD:{task_id}]",
            record["full_line"],
            count=1
        )
        md_path.write_text(text.replace(record["full_line"], updated_line, 1), encoding="utf-8")
        await asyncio.to_thread(index_file, md_path)

    compiled = record["compiled"]
    log_rel_path = memory_append(
        agent_id="fo_agent",
        category="fo_shield_verified_execution",
        entry=(
            f"**Task ID**: `{task_id}` (Source: `{record['source_file']}`)\n"
            f"**Raw Intent**: {record['raw_task']}\n"
            f"**Injected Local Context**: {json.dumps(compiled['injected_facts'])}\n"
            f"**Blocked PII/Secrets**: {json.dumps(compiled['withheld_secrets'])}\n"
            f"**Sanitized Brief Sent to Fo**: `{brief}`\n"
            f"**Transport & Tool**: `{fo_outcome['mode']} -> {fo_outcome['tool_called']}`\n"
            f"**Fo Real-World Receipt**:\n{fo_outcome['receipt']}"
        )
    )
    record["audit_log_path"] = log_rel_path
    await broadcast("task_completed", record)
    return record


async def scan_vault_once():
    if not VAULT_DIR.exists():
        return
    for md_path in sorted(VAULT_DIR.rglob("*.md")):
        if "_archived" in md_path.parts or "fo_agent" in md_path.parts:
            continue
        await asyncio.to_thread(index_file, md_path)
        content = md_path.read_text(encoding="utf-8", errors="ignore")
        for match in TASK_PATTERN.finditer(content):
            await stage_task(match.group(1).strip(), md_path, match.group(0))


async def vault_scanner_loop():
    init_db()
    if VAULT_DIR.exists():
        for md in sorted(VAULT_DIR.rglob("*.md")):
            await asyncio.to_thread(index_file, md)
    while True:
        try:
            await scan_vault_once()
        except Exception as exc:
            print(f"[FO-SHIELD SCANNER ERROR] {exc}")
        await asyncio.sleep(2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task
    scanner_task = asyncio.create_task(vault_scanner_loop())
    try:
        yield
    finally:
        if scanner_task:
            scanner_task.cancel()


app = FastAPI(
    title="Fo-Shield: Zero-Knowledge Local Context Firewall",
    lifespan=lifespan
)


class ApprovePayload(BaseModel):
    sanitized_brief: str | None = None


class QuickTaskPayload(BaseModel):
    agent_id: str = "hermes"
    raw_task: str


@app.get("/api/state")
async def get_state():
    return JSONResponse({
        "vault_dir": str(VAULT_DIR),
        "claude_model": CLAUDE_MODEL,
        "claude_live": bool(ANTHROPIC_API_KEY),
        "fo_mcp_connected": bool(FO_MCP_URL),
        "auto_dispatch": AUTO_DISPATCH,
        "tasks": list(task_ledger.values())
    })


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, body: ApprovePayload):
    res = await dispatch_approved_task(task_id, body.sanitized_brief)
    return JSONResponse(res)


@app.post("/api/tasks/{task_id}/recompile")
async def recompile_task(task_id: str):
    record = task_ledger.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task ID not found")
    record["status"] = "COMPILING_ZERO_KNOWLEDGE_BRIEF"
    await broadcast("task_staged", record)
    compiled = await asyncio.to_thread(sync_compile_brief, record["raw_task"])
    record["compiled"] = compiled
    record["status"] = "AWAITING_APPROVAL"
    await broadcast("task_compiled", record)
    return JSONResponse(record)


@app.post("/api/tasks/inject")
async def inject_task(body: QuickTaskPayload):
    clean_task = body.raw_task.strip()
    if clean_task.startswith("- [ ] #fo"):
        clean_task = clean_task[len("- [ ] #fo"):].strip()
    rel_log = memory_append(body.agent_id, "live_trigger", f"- [ ] #fo {clean_task}")
    await scan_vault_once()
    return JSONResponse({"status": "injected", "log_file": rel_log})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="info")
