# Fo-Shield: The Universal Local-First Memory Vault & Zero-Knowledge Brief Compiler for Wajo Fo

Built for **Wajo's "The GSD (Get s*** done!) Virtual Hackathon"** (Supported by Anthropic).

---

## 1. The Problem: Wajo Fo's Privacy-Induced Amnesia vs. Multi-Agent Context Sprawl
1. **Multi-Agent Memory Lock-In & Corruption**: Power users operate across `Claude Code`, `Grok Bot`, `ChatGPT Dots`, `Meta Muse`, and local `Hermes` daemons. Centralizing memory in a local Obsidian Markdown vault eliminates cloud lock-in, but concurrent multi-agent writes corrupt files, and naive keyword search fails at scale.
2. **The Action-Agent Privacy Paradox**: Wajo's **Fo** executes real-world tasks (phone calls, vendor emails, appointment bookings, virtual card purchases). Because Fo protects privacy via strict data minimization—refusing to index your personal messages and escalating complex tasks (~29%) to human assistants—it starts every new task functionally blind.
3. **The Vulnerability**: Pasting raw personal or corporate context into an action agent risks leaking corporate bank accounts, EIN tax IDs, HR records, or internal infrastructure keys to external vendors and human middlemen.

---

## 2. The Solution: Fo-Shield Architecture

```text
+-----------------------------------------------------------------------------------+
|                      ~/MemoryVault/ (Local Obsidian Markdown)                     |
|  - /canonical/*.md (Read-Only Authoritative State + Provenance Tags)              |
|  - /logs/<agent_id>/YYYY-MM-DD.md (Append-Only Worker Logs via fcntl.flock)       |
+-------------------+-------------------------------------------+-------------------+
                    |                                           ^
      watchdog + SHA-256 Chunking                               | Nightly Single-Writer
                    v                                           | Git Snapshot + Merge
+---------------------------------------+       +---------------+-------------------+
| ~/.local/share/memory-vault/index.db  |       |           custodian.py            |
| - Stage 1: FTS5 (BM25) + sqlite-vec   |       | - Locks .vault.lock (fcntl.flock) |
|   (384-d BAAI/bge-small-en-v1.5) RRF  |       | - Reconciles logs -> /canonical/  |
| - Stage 2: Cross-Encoder Reranking    |       | - Archives to /logs/_archived/    |
|   + Exponential Recency Decay         |       +-----------------------------------+
+-------------------+-------------------+
                    | Top-4 Ranked Chunks
                    v
+-----------------------------------------------------------------------------------+
|                Fo-Shield Zero-Knowledge Context Firewall (Port 8090)              |
|  1. L1 Deterministic Regex Scrubber (Pre-LLM & Post-LLM):                         |
|     Strips tskey-*, Bank A/Cs, EIN-*, EMP-ACME-*, ShieldPlan-*, 192.0.2.0/24      |
|  2. L2 Semantic Brief Compiler (claude-sonnet-4-6):                               |
|     Synthesizes minimum-viable sanitized_fo_brief + green/red audit telemetry     |
+-------------------+---------------------------------------------------------------+
                    | Auto-Negotiated Dual-Transport MCP (StreamableHTTP -> SSE)
                    v
+-----------------------------------------------------------------------------------+
|                        Wajo Fo MCP Server (Real-World Action)                     |
|  - Executes phone calls, vendor quotes, bookings, & single-use virtual cards      |
|  - Checks off `- [x] #fo [FO-SHIELD:<id>]` & logs receipt to `/logs/fo_agent/`    |
+-----------------------------------------------------------------------------------+
```

---

## 3. Repository Manifest (10 Polished Project Files)

| File | Subsystem Role |
| :--- | :--- |
| `seed_vault.py` | Initializes `~/MemoryVault` with 100% synthetic RFC 2606 / RFC 5737 fixtures ("Alex Rivera", "Acme Labs Engineering Vault", "742 Evergreen Terrace, Suite 200, Metropolis"). |
| `indexer.py` | Incremental Markdown `##` chunker, `FTS5` + `sqlite-vec` (384-d) RRF retriever, and `ms-marco-MiniLM-L-6-v2` Cross-Encoder reranker with exponential log time-decay. |
| `vault_bridge.py` | Local FastMCP server providing `fcntl.flock` concurrency-safe `memory_append` and `memory_query` tools. |
| `fo_shield_server.py` | FastAPI + WebSocket daemon that watches `- [ ] #fo` tasks, runs L1/L2 Zero-Knowledge compilation via `claude-sonnet-4-6`, and dispatches to Wajo Fo MCP. |
| `dashboard.html` | 3-column real-time dark-mode telemetry UI showing hybrid retrieval scores, green *Injected Facts*, red *Blocked Secrets*, editable brief, and Fo MCP receipts. |
| `custodian.py` | Nightly single-writer compaction daemon with global `.vault.lock`, Git snapshots, provenance tagging, and log archiving. |
| `oauth_gateway.py` | Zero-Trust OAuth 2.1 + PKCE (`S256`) gateway bound to `127.0.0.1:8443` with cryptographic per-agent JWT isolation. |
| `verify_e2e.py` | Automated 5-stage verification harness proving zero secret leakage and end-to-end writeback. |
| `requirements.txt` | Pinned Python dependencies. |
| `.gitignore` | Clean Git ignore rules preventing `.venv` or `.env` commits. |

---

## 4. Quickstart & One-Command Verification

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (Optional: set ANTHROPIC_API_KEY and FO_MCP_URL for live cloud)
export VAULT_DIR="$HOME/MemoryVault"
export CLAUDE_MODEL="claude-sonnet-4-6"
export BRIDGE_OAUTH_SECRET="fo_shield_production_oauth_secret_key_2026_32b"

# 3. Run the automated 5-stage verification suite
python3 verify_e2e.py

# 4. Seed fresh state and launch the live Fo-Shield WebSocket Dashboard
rm -rf "$HOME/MemoryVault" "$HOME/.local/share/memory-vault/index.sqlite"
python3 seed_vault.py
python3 fo_shield_server.py
# Open http://127.0.0.1:8090 in your browser
```
