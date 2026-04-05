# cc-jsonl-fix

[中文文档](README_CN.md)

Repair tool for corrupted Claude Code session JSONL files that lose conversation history on resume.

## What problem does this solve?

Claude Code stores conversation sessions as JSONL files where messages form a linked list via `uuid` and `parentUuid` fields. The `resume` feature walks this chain backward from the last message. When the chain breaks, resume can only load a few messages instead of the full history.

This is a [known bug](https://github.com/anthropics/claude-code/issues/22526) reported across multiple issues:

- [#22526](https://github.com/anthropics/claude-code/issues/22526) — Session loses all but last few messages on resume
- [#24304](https://github.com/anthropics/claude-code/issues/24304) — parentUuid references non-existent UUID
- [#21751](https://github.com/anthropics/claude-code/issues/21751) — Assistant text block missing, only thinking block written

## What does this tool fix?

The tool applies four repair phases, each targeting a specific corruption pattern:

### Phase 1: Sanitize

**NUL byte removal** — Strips `\x00` bytes from corrupted lines (common when writes are interrupted mid-flush). The JSON content underneath is usually intact.

### Phase 1b: Snapshot collision fix

**messageId deduplication** — `file-history-snapshot` entries often share their `messageId` with a real message's `uuid`. If Claude Code indexes both into the same map, the snapshot shadows the real message and breaks chain traversal. This phase nullifies colliding `messageId` values.

### Phase 2: Orphan parentUuid repair

**Phantom UUID resolution** — Finds messages whose `parentUuid` points to a UUID that doesn't exist in the file (the "phantom" was never written, likely due to a race condition). Re-parents to the nearest valid `user`/`assistant` message, with safeguards to never cross `compact_boundary` segment boundaries.

### Phase 3: Chain maximization

**Branch absorption** — After fixing orphans, the main chain may still skip large chunks of conversation that ended up on disconnected branches. This phase iteratively finds sibling branches at each fork point and absorbs them into the main chain by re-parenting, largest branch first.

```
Before:
  Main chain:  ... → M → P → ...     (only 5 messages reachable)
  Lost branch: P → S → ... → T       (988 messages, disconnected)

After:
  ... → M → T → ... → S → P → ...   (all messages in one chain)
  (M.parentUuid changed from P to T)
```

## Usage

**Requirements**: Python >= 3.10, no external dependencies.

```bash
# Basic repair (creates backup automatically)
python repair_jsonl.py ~/.claude/projects/<project-hash>/<session-id>.jsonl

# With uv (recommended)
uv run repair_jsonl.py <session-file>.jsonl

# Preview without modifying
python repair_jsonl.py <session-file>.jsonl --dry-run --verbose

# Output to a different file
python repair_jsonl.py <session-file>.jsonl -o repaired.jsonl

# Recommended: repair + update timestamp (see below)
python repair_jsonl.py <session-file>.jsonl --touch
```

### Options

| Flag | Description |
|---|---|
| `-o, --output PATH` | Write to a different file instead of overwriting (after backup) |
| `--no-backup` | Skip automatic backup creation |
| `--dry-run` | Analyze and report without modifying any files |
| `--verbose` | Show per-line fix details |
| `--touch` | Update last message timestamp to current time |
| `--force` | Write output even if integrity checks fail |

### About `--touch`

Corruption is usually discovered only when you try to resume a session and find the history missing. By that point, the last message in the JSONL file may be hours or days old. After repair, when you resume again, Claude Code compares the last message's timestamp against the current time — if the gap is large, it shows a prompt like:

```
This session is 9h 7m old and 256.9k tokens.
Resuming the full session will consume a substantial portion of your usage limits.

❯ 1. Resume from summary (recommended)
  2. Resume full session as-is
  3. Don't ask me again
```

This is not an error — you can safely choose **"Resume full session as-is"** and the repaired session will load correctly. However, if you want to skip this prompt entirely, use `--touch` when repairing. It updates the last message's timestamp to the current time, so Claude Code sees a "fresh" session and resumes directly.

### Finding your session file

```bash
# List sessions for a project
ls ~/.claude/projects/<project-hash>/

# The project hash is derived from your project path, e.g.:
# /home/user/myproject → F--home-user-myproject
# On Windows: C:\Users\me\project → C--Users-me-project
```

### Example output

```
==================================================
  JSONL Repair Report
==================================================
Input:  18c635d0-e120-46bf-adbf-3b4709b4e43e.jsonl
Lines:  5667  |  UUID entries: 4908

--- Phase 1: Sanitize ---
  NUL-corrupted lines fixed: 1
  Snapshot messageId collisions fixed: 626

--- Phase 2: Fix Orphan parentUuids ---
  Orphans fixed: 1

--- Phase 3: Maximize Main Chain ---
  Branches absorbed: 31
  Messages absorbed: 1620

--- Verification ---
  Orphan parentUuids: 0  [PASS]
  Duplicate UUIDs:    0  [PASS]
  Cycles detected:    No  [PASS]
  Main chain length:  1701  (before: 5)
  Chain growth:       +1696 messages (+33920%)
==================================================
```

## Important notes

**Backup** — The tool creates a timestamped backup before modifying any file. Use `--dry-run` first if you want to preview changes.

**Integrity gate** — After repair, the tool verifies the result (orphans, cycles, duplicates). If any check fails, it refuses to write the output unless `--force` is specified.

**This bug may be fixed upstream** — Anthropic is aware of these issues. Future versions of Claude Code may fix the root cause (race conditions in JSONL writing). Once fixed, this tool will no longer be needed for new sessions, but can still repair already-corrupted files.

**No guarantees** — This tool modifies `parentUuid` and `messageId` fields to repair chain integrity. It does not modify message content, thinking blocks, or signatures. However, repaired sessions may behave differently than the original uncorrupted state. Always keep the backup.

**For tool builders** — If you're building Claude Code session management tools (viewers, exporters, analytics), the chain-walking and corruption-detection logic in this codebase may be useful as a reference.

## Related issues

- [anthropics/claude-code#22526](https://github.com/anthropics/claude-code/issues/22526) — parentUuid chain corruption on resume
- [anthropics/claude-code#24304](https://github.com/anthropics/claude-code/issues/24304) — Broken parentUuid references and snapshot collisions
- [anthropics/claude-code#21751](https://github.com/anthropics/claude-code/issues/21751) — Missing assistant text blocks with extended thinking

## License

MIT
