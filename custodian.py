import os
import re
import sys
import fcntl
import shutil
import datetime
import subprocess
from pathlib import Path
from anthropic import Anthropic

from indexer import init_db, index_file, VAULT_DIR

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
claude = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

LOCK_FILE = VAULT_DIR / ".vault.lock"
CANONICAL_DIR = VAULT_DIR / "canonical"
LOGS_DIR = VAULT_DIR / "logs"
ARCHIVE_DIR = LOGS_DIR / "_archived"
PROMPT_FILE = VAULT_DIR / "custodian_prompt.txt"


def git_snapshot(message: str):
    git_dir = VAULT_DIR / ".git"
    try:
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=VAULT_DIR, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "custodian@acmelabs.example"],
                cwd=VAULT_DIR,
                check=True,
                capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Fo-Shield Custodian Daemon"],
                cwd=VAULT_DIR,
                check=True,
                capture_output=True
            )
        subprocess.run(["git", "add", "-A"], cwd=VAULT_DIR, check=True, capture_output=True)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=VAULT_DIR,
            check=True,
            capture_output=True,
            text=True
        )
        if status.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=VAULT_DIR,
                check=True,
                capture_output=True
            )
            print(f"[CUSTODIAN GIT] Committed snapshot: {message}")
    except Exception as exc:
        print(f"[CUSTODIAN GIT WARNING] Snapshot skipped: {exc}")


def collect_unprocessed_logs() -> list[tuple[str, Path, str]]:
    collected = []
    if not LOGS_DIR.exists():
        return collected

    for agent_dir in sorted(LOGS_DIR.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        agent_id = agent_dir.name
        for log_file in sorted(agent_dir.glob("*.md")):
            text = log_file.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            if re.search(r"^-\s*\[\s\]\s*#fo\s+", text, re.MULTILINE):
                print(f"[CUSTODIAN] Skipping {log_file.relative_to(VAULT_DIR)} (contains pending unchecked #fo task)")
                continue
            collected.append((agent_id, log_file, text))
    return collected


def select_target_canonical(log_text: str) -> Path:
    lower = log_text.lower()
    if any(k in lower for k in ("ergonomic", "wellness", "vesa", "monitor", "keyboard", "studio")):
        target = CANONICAL_DIR / "workstation_and_wellness.md"
    elif any(k in lower for k in ("nvme", "ssd", "10gbe", "switch", "cluster", "bastion", "tailscale")):
        target = CANONICAL_DIR / "active_projects.md"
    else:
        target = CANONICAL_DIR / "profile_and_prefs.md"
    return target


def deterministic_reconcile(
    canonical_files: dict[str, str],
    logs: list[tuple[str, Path, str]]
) -> dict[str, str]:
    updated = dict(canonical_files)
    today = datetime.date.today().isoformat()

    for agent_id, log_path, log_text in logs:
        rel_log = str(log_path.relative_to(VAULT_DIR))
        target_path = select_target_canonical(log_text)
        rel_target = str(target_path.relative_to(VAULT_DIR))
        current_md = updated.get(rel_target, "")

        summary_lines = []
        for line in log_text.splitlines():
            line_s = line.strip()
            if line_s.startswith("## ["):
                continue
            if line_s:
                summary_lines.append(line_s.lstrip("- ").strip())

        digest = " | ".join(summary_lines[:3])
        if len(digest) > 220:
            digest = digest[:217] + "..."

        provenance_entry = (
            f"- {digest} "
            f"`[Updated: {today} via {agent_id}:{rel_log}]`"
        )

        if "## Changelog" not in current_md:
            current_md = current_md.rstrip() + f"\n\n## Changelog\n{provenance_entry}\n"
        else:
            current_md = current_md.rstrip() + f"\n{provenance_entry}\n"

        updated[rel_target] = current_md

    return updated


def llm_reconcile_canonical_file(
    system_prompt: str,
    rel_canonical: str,
    canonical_content: str,
    formatted_logs: str
) -> str:
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        temperature=0.0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": (
                f"TARGET CANONICAL FILE: {rel_canonical}\n"
                f"CURRENT CANONICAL CONTENT:\n```markdown\n{canonical_content}\n```\n\n"
                f"UNPROCESSED DAILY AGENT LOGS:\n{formatted_logs}\n\n"
                f"Return ONLY the complete updated Markdown for {rel_canonical}. "
                f"Preserve all confidential/restricted sections intact, append provenance tags "
                f"`[Updated: YYYY-MM-DD via <agent_id>:logs/<agent_id>/YYYY-MM-DD.md]` for new or updated facts, "
                f"and maintain a `## Changelog` section at the bottom."
            )
        }]
    )
    out = resp.content[0].text.strip()
    fence_match = re.search(r"```(?:markdown)?\s*(.*?)```", out, re.DOTALL)
    return (fence_match.group(1).strip() if fence_match else out) + "\n"


def run_custodian() -> dict:
    if not VAULT_DIR.exists():
        sys.exit(f"FATAL: VAULT_DIR ({VAULT_DIR}) does not exist.")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock_fd:
        print(f"[CUSTODIAN] Acquiring exclusive vault lock at {LOCK_FILE}...")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            git_snapshot(f"Pre-custodian snapshot ({timestamp})")

            logs = collect_unprocessed_logs()
            if not logs:
                print("[CUSTODIAN] No unprocessed completed logs found. Vault is clean.")
                return {"status": "clean", "processed_logs": 0}

            canonical_files: dict[str, str] = {}
            for c_path in sorted(CANONICAL_DIR.glob("*.md")):
                rel_c = str(c_path.relative_to(VAULT_DIR))
                canonical_files[rel_c] = c_path.read_text(encoding="utf-8")

            system_prompt = (
                PROMPT_FILE.read_text(encoding="utf-8")
                if PROMPT_FILE.exists()
                else "Reconcile daily logs into canonical Markdown with provenance tags."
            )

            if claude:
                print(f"[CUSTODIAN] Reconciling {len(logs)} log file(s) via {CLAUDE_MODEL}...")
                formatted_logs = "\n\n".join(
                    f"=== LOG: {lp.relative_to(VAULT_DIR)} (agent_id={aid}) ===\n{txt}"
                    for aid, lp, txt in logs
                )
                updated_canonicals = {}
                for rel_c, c_text in canonical_files.items():
                    updated_canonicals[rel_c] = llm_reconcile_canonical_file(
                        system_prompt, rel_c, c_text, formatted_logs
                    )
            else:
                print(f"[CUSTODIAN] Reconciling {len(logs)} log file(s) via deterministic provenance merger...")
                updated_canonicals = deterministic_reconcile(canonical_files, logs)

            init_db()
            for rel_c, new_text in updated_canonicals.items():
                full_c_path = VAULT_DIR / rel_c
                full_c_path.write_text(new_text, encoding="utf-8")
                index_file(full_c_path)
                print(f"[CUSTODIAN] Updated & re-indexed {rel_c}")

            archived_paths = []
            for agent_id, log_path, _ in logs:
                dest_dir = ARCHIVE_DIR / agent_id
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / log_path.name
                if dest_path.exists():
                    existing_arch = dest_path.read_text(encoding="utf-8")
                    new_arch = log_path.read_text(encoding="utf-8")
                    dest_path.write_text(existing_arch.rstrip() + "\n\n" + new_arch, encoding="utf-8")
                    log_path.unlink()
                else:
                    shutil.move(str(log_path), str(dest_path))
                archived_paths.append(str(dest_path.relative_to(VAULT_DIR)))
                print(f"[CUSTODIAN] Archived {log_path.relative_to(VAULT_DIR)} -> {dest_path.relative_to(VAULT_DIR)}")

            git_snapshot(f"Post-custodian reconciliation ({len(logs)} logs merged)")
            return {
                "status": "reconciled",
                "processed_logs": len(logs),
                "archived": archived_paths,
                "updated_canonicals": list(updated_canonicals.keys())
            }
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            print("[CUSTODIAN] Released exclusive vault lock.")


if __name__ == "__main__":
    result = run_custodian()
    print(f"[CUSTODIAN COMPLETE] {result}")
