import os
import sys
import fcntl
import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "~/MemoryVault")).expanduser().resolve()
ALLOWED_AGENTS = {"grokbot", "hermes", "claude_code", "chatgpt_dots", "meta_muse", "fo_agent"}

mcp = FastMCP("VaultBridge", host="127.0.0.1", port=8443)

@mcp.tool()
def memory_append(agent_id: str, category: str, entry: str) -> str:
    if agent_id not in ALLOWED_AGENTS:
        raise ValueError(f"Unauthorized agent_id: {agent_id}")
    today = datetime.date.today().isoformat()
    log_dir = VAULT_DIR / "logs" / agent_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{today}.md"
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    block = f"\n## [{timestamp}] ({category})\n{entry.strip()}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(block)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return f"Appended to {log_file.relative_to(VAULT_DIR)}"

@mcp.tool()
def memory_query(query: str, top_k: int = 5) -> str:
    from indexer import hybrid_search
    hits = hybrid_search(query, top_k=top_k)
    if not hits:
        return "No matching memory found."
    return "\n\n".join(
        f"--- {h['file_path']} ({h['heading']}) [Score: {h['score']}] ---\n{h['content']}"
        for h in hits
    )

@mcp.tool()
def bridge_status() -> dict:
    return {
        "status": "ok",
        "vault": str(VAULT_DIR),
        "markdown_files": len(list(VAULT_DIR.rglob("*.md")))
    }

if __name__ == "__main__":
    if not VAULT_DIR.exists():
        sys.exit("FATAL: VAULT_DIR does not exist. Failing closed.")
    mcp.run()
