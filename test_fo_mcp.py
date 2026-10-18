"""Live Fo MCP handshake & schema verifier.

Probes FO_MCP_URL, identifies whether the endpoint speaks StreamableHTTP or
SSE, and prints the exact tool names and JSON input schemas exposed by Wajo's
Fo MCP server. Mirrors the transport negotiation and argument-key mapping in
fo_shield_server.py, so what you see here is what the server will do.

Read-only mode (default) calls zero tools. --ping performs one live tool
call; Fo executes real-world actions, so it asks for confirmation first.

Requires: FO_MCP_URL (and optional FO_API_TOKEN) in the environment.
The repo pins mcp<2 (see requirements.txt); the import below follows the
same fallback fo_shield_server.py uses.
"""
import os
import sys
import json
import asyncio
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.sse import sse_client

try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # mcp 2.x renamed it
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

FO_MCP_URL = os.environ.get("FO_MCP_URL", "").strip()
FO_API_TOKEN = os.environ.get("FO_API_TOKEN", "").strip()

# Same priority order as fo_shield_server.py dispatch_approved_task().
KEY_PRIORITY = ("prompt", "task", "query", "message", "instruction")


def detect_key(schema):
    props = (schema or {}).get("properties", {})
    return next(
        (k for k in KEY_PRIORITY if k in props),
        next(iter(props.keys()), "prompt"),
    )


async def verify_fo_mcp():
    if not FO_MCP_URL:
        sys.exit("ERROR: FO_MCP_URL is not set. Run: export FO_MCP_URL='https://...'")

    headers = {"Authorization": f"Bearer {FO_API_TOKEN}"} if FO_API_TOKEN else {}
    print(f"[1/3] Probing Fo MCP endpoint: {FO_MCP_URL[:35]}...")

    async with AsyncExitStack() as stack:
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(FO_MCP_URL, headers=headers)
            )
            transport_used = "StreamableHTTP"
        except Exception as e_http:
            print(f"      -> StreamableHTTP declined ({type(e_http).__name__}), falling back to SSE...")
            try:
                read, write = await stack.enter_async_context(
                    sse_client(FO_MCP_URL, headers=headers)
                )
                transport_used = "SSE"
            except Exception as e_sse:
                sys.exit(f"FATAL: Both StreamableHTTP and SSE failed.\n  HTTP Err: {e_http}\n  SSE Err : {e_sse}")

        print(f"[2/3] Transport connected via: {transport_used}")
        session = await stack.enter_async_context(ClientSession(read, write))
        init_result = await session.initialize()
        print(f"      -> Server Info: {getattr(init_result, 'serverInfo', 'Verified')}")

        tools_resp = await session.list_tools()
        tools = tools_resp.tools
        if not tools:
            sys.exit("FATAL: Fo MCP server returned zero tools.")
        print(f"\n[3/3] Discovered {len(tools)} exposed Fo MCP tool(s):\n" + "=" * 60)

        for idx, t in enumerate(tools, 1):
            schema = t.inputSchema or {}
            required = schema.get("required", [])
            detected_key = detect_key(schema)
            print(f"Tool #{idx}: {t.name}")
            print(f"  Description   : {t.description or 'No description'}")
            print(f"  Required Keys : {required}")
            print(f"  Auto-Mapped Key: '{detected_key}'  <- fo_shield_server.py will bind the brief here")
            print(f"  Full JSON Schema:\n{json.dumps(schema, indent=4)}")
            print("-" * 60)

        if "--ping" in sys.argv:
            primary = tools[0]
            key = detect_key(primary.inputSchema)
            print(f"\n[PING] About to call live tool '{primary.name}' with ({key}=...).")
            print("       Fo executes REAL-WORLD actions (calls, emails, purchases).")
            confirm = input("       Type YES to proceed: ").strip()
            if confirm != "YES":
                sys.exit("Ping aborted.")
            print("       Sending dry-run ping (asks the agent to confirm connectivity only)...")
            res = await session.call_tool(
                primary.name,
                arguments={key: "DRY RUN PING: Confirm Fo MCP connectivity only. Do not place external calls."},
            )
            print(f"[PING RESPONSE]:\n{res}")


if __name__ == "__main__":
    asyncio.run(verify_fo_mcp())
