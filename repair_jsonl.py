"""
Generic repair tool for Claude Code session JSONL files with broken parentUuid chains.

Fixes four categories of corruption:
  Phase 1  - Sanitize:   Strip NUL bytes, validate JSON
  Phase 1b - Snapshots:  Nullify file-history-snapshot messageId collisions
  Phase 2  - Orphans:    Fix parentUuids pointing to non-existent UUIDs
  Phase 3  - Maximize:   Absorb disconnected branches into the main chain

Usage:
  python repair_jsonl.py INPUT_FILE [-o OUTPUT] [--no-backup] [--dry-run] [--verbose] [--touch] [--force]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    line_number: int  # 1-based
    raw: bytes
    obj: dict | None = None
    uuid: str | None = None
    parent_uuid: str | None = None
    entry_type: str | None = None
    modified: bool = False


@dataclass
class Action:
    phase: int
    line: int
    desc: str


@dataclass
class Report:
    input_path: str
    backup_path: str | None = None
    total_lines: int = 0
    total_uuids: int = 0
    nul_lines_fixed: int = 0
    snapshot_collisions_fixed: int = 0
    orphans_fixed: int = 0
    branches_absorbed: int = 0
    branch_msgs_absorbed: int = 0
    chain_before: int = 0
    chain_after: int = 0
    has_cycle: bool = False
    remaining_orphans: int = 0
    remaining_parse_errors: int = 0
    duplicate_uuids: int = 0
    actions: list[Action] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 1: Sanitize
# ---------------------------------------------------------------------------

def phase1_sanitize(entries: list[Entry], report: Report) -> None:
    """Strip NUL bytes and parse JSON for every line."""
    for e in entries:
        # Strip NUL bytes
        if b"\x00" in e.raw:
            cleaned = e.raw.replace(b"\x00", b"")
            nul_count = len(e.raw) - len(cleaned)
            e.raw = cleaned
            e.modified = True
            report.nul_lines_fixed += 1
            act = Action(1, e.line_number, f"Removed {nul_count} NUL bytes")
            report.actions.append(act)

        # Parse JSON
        stripped = e.raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            if not isinstance(obj, dict):
                report.remaining_parse_errors += 1
                continue
            e.obj = obj
            uid = obj.get("uuid")
            pid = obj.get("parentUuid")
            e.uuid = uid if isinstance(uid, str) else None
            e.parent_uuid = pid if isinstance(pid, str) else None
            e.entry_type = obj.get("type")
        except (json.JSONDecodeError, UnicodeDecodeError):
            report.remaining_parse_errors += 1


# ---------------------------------------------------------------------------
# Phase 1b: Fix snapshot messageId collisions
# ---------------------------------------------------------------------------

def phase1b_fix_snapshot_collisions(entries: list[Entry], report: Report) -> None:
    """Nullify file-history-snapshot messageIds that collide with real UUIDs.

    Claude Code writes ``file-history-snapshot`` entries whose ``messageId``
    duplicates the ``uuid`` of the message they follow.  If the runtime
    indexes both fields into a single map, the snapshot can shadow the real
    message and break parentUuid chain traversal.  Nullifying the colliding
    ``messageId`` eliminates the ambiguity.
    """
    uuid_set = {e.uuid for e in entries if e.uuid}

    for e in entries:
        if not e.obj or e.entry_type != "file-history-snapshot":
            continue
        mid = e.obj.get("messageId")
        if mid and mid in uuid_set:
            e.obj["messageId"] = None
            e.raw = json.dumps(e.obj, ensure_ascii=False).encode("utf-8")
            e.modified = True
            report.snapshot_collisions_fixed += 1
            act = Action(1, e.line_number,
                         f"Snapshot messageId collision: nullified {mid[:12]}...")
            report.actions.append(act)


# ---------------------------------------------------------------------------
# Phase 2: Fix orphan parentUuids
# ---------------------------------------------------------------------------

def phase2_fix_orphans(entries: list[Entry], report: Report) -> None:
    """Re-parent messages whose parentUuid points to a non-existent UUID."""
    uuid_set = {e.uuid for e in entries if e.uuid}

    for idx, e in enumerate(entries):
        if not e.parent_uuid:
            continue
        if e.parent_uuid in uuid_set:
            continue

        # Orphan detected — scan backward for nearest valid uuid
        old_parent = e.parent_uuid
        nearest = _find_nearest_ancestor(entries, idx, uuid_set)

        if nearest:
            _set_parent(e, nearest)
        else:
            _set_parent(e, None)

        report.orphans_fixed += 1
        target = nearest[:12] + "..." if nearest else "None"
        act = Action(2, e.line_number,
                     f"Orphan parentUuid {old_parent[:12]}... -> {target}")
        report.actions.append(act)


def _find_nearest_ancestor(entries: list[Entry], idx: int,
                           uuid_set: set[str]) -> str | None:
    """Walk backward from *idx* and return the nearest valid ancestor.

    Prefers user/assistant messages.  Falls back to any UUID-bearing entry
    except compact_boundary (which has parentUuid=null and would truncate
    the chain).  Stops scanning at a compact_boundary to respect compaction
    segment boundaries.
    """
    best: str | None = None
    for i in range(idx - 1, -1, -1):
        e = entries[i]
        # Never cross a compact_boundary
        if e.obj and e.obj.get("subtype") == "compact_boundary":
            break
        if e.uuid and e.uuid in uuid_set:
            if e.entry_type in ("user", "assistant"):
                return e.uuid  # ideal match, return immediately
            if best is None:
                best = e.uuid  # fallback candidate
    return best


# ---------------------------------------------------------------------------
# Phase 3: Maximize main chain by absorbing sibling branches
# ---------------------------------------------------------------------------

def phase3_maximize(entries: list[Entry], report: Report) -> None:
    """Iteratively absorb disconnected branches into the main chain."""
    uuid_to_entry = {e.uuid: e for e in entries if e.uuid}
    uuid_to_parent: dict[str, str | None] = {
        e.uuid: e.parent_uuid for e in entries if e.uuid
    }

    last_uuid = _find_last_uuid(entries)
    if not last_uuid:
        return

    iteration = 0
    while iteration < 5000:  # safety cap
        iteration += 1

        # Rebuild children map
        children: dict[str, list[str]] = defaultdict(list)
        for uid, pid in uuid_to_parent.items():
            if pid:
                children[pid].append(uid)

        # Walk main chain
        chain, chain_set = _walk_chain(last_uuid, uuid_to_parent)

        # Find best branch to absorb (greedy: largest first)
        best = _find_best_branch(chain, chain_set, children, uuid_to_parent)
        if best is None:
            break

        fork_uuid, chain_child_uuid, branch_tail, branch_len = best

        # Re-parent: chain_child → branch_tail (insert branch before chain_child)
        old_parent = uuid_to_parent[chain_child_uuid]
        uuid_to_parent[chain_child_uuid] = branch_tail

        entry = uuid_to_entry[chain_child_uuid]
        _set_parent(entry, branch_tail)

        report.branches_absorbed += 1
        report.branch_msgs_absorbed += branch_len
        act = Action(
            3, entry.line_number,
            f"Absorbed {branch_len}-msg branch: "
            f"{chain_child_uuid[:12]}... parent {old_parent[:12] if old_parent else 'None'}... "
            f"-> {branch_tail[:12]}..."
        )
        report.actions.append(act)


def _find_best_branch(
    chain: list[str],
    chain_set: set[str],
    children: dict[str, list[str]],
    uuid_to_parent: dict[str, str | None],
) -> tuple[str, str, str, int] | None:
    """Return (fork_uuid, chain_child_uuid, branch_tail, branch_len) or None."""
    best: tuple[str, str, str, int] | None = None
    best_len = 0

    for i, uid in enumerate(chain):
        siblings = [c for c in children.get(uid, [])
                    if c not in chain_set]
        if not siblings:
            continue

        if i == 0:
            continue  # can't re-parent the very last message

        for sib in siblings:
            spine = _longest_path(sib, children, chain_set)
            if len(spine) > best_len:
                best_len = len(spine)
                best = (uid, chain[i - 1], spine[-1], len(spine))

    return best


def _longest_path(start: str, children: dict[str, list[str]],
                  exclude: set[str]) -> list[str]:
    """DFS to find longest forward path from *start*, excluding nodes in *exclude*."""
    best: list[str] = []
    stack: list[tuple[str, list[str], set[str]]] = [
        (start, [start], {start})
    ]
    while stack:
        node, path, visited = stack.pop()
        kids = [k for k in children.get(node, []) if k not in exclude and k not in visited]
        if not kids:
            if len(path) > len(best):
                best = path
        else:
            for kid in kids:
                stack.append((kid, path + [kid], visited | {kid}))
    return best


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk_chain(last_uuid: str,
                uuid_to_parent: dict[str, str | None]) -> tuple[list[str], set[str]]:
    chain: list[str] = []
    visited: set[str] = set()
    current: str | None = last_uuid
    while current and current not in visited:
        visited.add(current)
        chain.append(current)
        current = uuid_to_parent.get(current)
    return chain, visited


def _find_last_uuid(entries: list[Entry]) -> str | None:
    for e in reversed(entries):
        if e.uuid:
            return e.uuid
    return None


def _touch_last_timestamp(entries: list[Entry], report: Report) -> None:
    """Update the last timestamped message's timestamp to now."""
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for e in reversed(entries):
        if e.obj and e.obj.get("timestamp"):
            old_ts = e.obj["timestamp"]
            e.obj["timestamp"] = now
            e.raw = json.dumps(e.obj, ensure_ascii=False).encode("utf-8")
            e.modified = True
            act = Action(0, e.line_number,
                         f"Touch: timestamp {old_ts[:19]} -> {now[:19]}")
            report.actions.append(act)
            print(f"[touch] Line {e.line_number}: timestamp -> {now}")
            break


def _set_parent(entry: Entry, new_parent: str | None) -> None:
    entry.parent_uuid = new_parent
    if entry.obj:
        entry.obj["parentUuid"] = new_parent
        entry.raw = json.dumps(entry.obj, ensure_ascii=False).encode("utf-8")
        entry.modified = True


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(entries: list[Entry], report: Report) -> None:
    uuid_to_parent: dict[str, str | None] = {
        e.uuid: e.parent_uuid for e in entries if e.uuid
    }
    uuid_set = set(uuid_to_parent.keys())
    report.total_uuids = len(uuid_set)

    # Duplicate UUID check
    seen: set[str] = set()
    for e in entries:
        if e.uuid:
            if e.uuid in seen:
                report.duplicate_uuids += 1
            seen.add(e.uuid)

    # Orphan check
    orphans = [uid for uid, pid in uuid_to_parent.items()
               if pid and pid not in uuid_set]
    report.remaining_orphans = len(orphans)

    # Chain walk
    last_uuid = _find_last_uuid(entries)
    if last_uuid:
        chain, visited = _walk_chain(last_uuid, uuid_to_parent)
        report.chain_after = len(chain)
        # cycle detection: if walk stopped because of revisit
        current: str | None = last_uuid
        for _ in range(len(chain)):
            current = uuid_to_parent.get(current)  # type: ignore[arg-type]
        if current and current in visited:
            report.has_cycle = True


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def create_backup(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_suffix(f".backup_{ts}.jsonl")
    # Avoid overwriting existing backup
    counter = 0
    while dst.exists():
        counter += 1
        dst = path.with_suffix(f".backup_{ts}_{counter}.jsonl")
    shutil.copy2(path, dst)
    return dst


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(r: Report, verbose: bool = False) -> None:
    print(f"\n{'=' * 50}")
    print(f"  JSONL Repair Report")
    print(f"{'=' * 50}")
    print(f"Input:  {r.input_path}")
    if r.backup_path:
        print(f"Backup: {r.backup_path}")
    print(f"Lines:  {r.total_lines}  |  UUID entries: {r.total_uuids}")

    # Phase 1
    print(f"\n--- Phase 1: Sanitize ---")
    if verbose:
        for a in r.actions:
            if a.phase == 1:
                print(f"  Line {a.line}: {a.desc}")
    print(f"  NUL-corrupted lines fixed: {r.nul_lines_fixed}")
    print(f"  Snapshot messageId collisions fixed: {r.snapshot_collisions_fixed}")
    if r.remaining_parse_errors:
        print(f"  Unparseable lines remaining: {r.remaining_parse_errors}")

    # Phase 2
    print(f"\n--- Phase 2: Fix Orphan parentUuids ---")
    if verbose:
        for a in r.actions:
            if a.phase == 2:
                print(f"  Line {a.line}: {a.desc}")
    print(f"  Orphans fixed: {r.orphans_fixed}")

    # Phase 3
    print(f"\n--- Phase 3: Maximize Main Chain ---")
    if verbose:
        for a in r.actions:
            if a.phase == 3:
                print(f"  Line {a.line}: {a.desc}")
    print(f"  Branches absorbed: {r.branches_absorbed}")
    print(f"  Messages absorbed: {r.branch_msgs_absorbed}")

    # Verification
    p = lambda ok: "PASS" if ok else "FAIL"
    print(f"\n--- Verification ---")
    print(f"  Orphan parentUuids: {r.remaining_orphans}  [{p(r.remaining_orphans == 0)}]")
    print(f"  Duplicate UUIDs:    {r.duplicate_uuids}  [{p(r.duplicate_uuids == 0)}]")
    print(f"  Cycles detected:    {'Yes' if r.has_cycle else 'No'}  [{p(not r.has_cycle)}]")
    print(f"  Main chain length:  {r.chain_after}  (before: {r.chain_before})")
    if r.chain_before > 0:
        growth = r.chain_after - r.chain_before
        pct = growth / r.chain_before * 100
        print(f"  Chain growth:       +{growth} messages (+{pct:.0f}%)")

    print(f"{'=' * 50}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def repair(input_path: Path, output_path: Path | None = None,
           no_backup: bool = False, dry_run: bool = False,
           verbose: bool = False, touch: bool = False,
           force: bool = False) -> Report:
    report = Report(input_path=str(input_path))

    # Read file
    raw = input_path.read_bytes()
    raw_lines = raw.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()

    entries = [
        Entry(line_number=i + 1, raw=line)
        for i, line in enumerate(raw_lines)
    ]
    report.total_lines = len(entries)

    # Pre-repair: parse what we can (NUL-tolerant) for baseline chain length
    for e in entries:
        stripped = e.raw.replace(b"\x00", b"").strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            if not isinstance(obj, dict):
                continue
            uid = obj.get("uuid")
            pid = obj.get("parentUuid")
            e.uuid = uid if isinstance(uid, str) else None
            e.parent_uuid = pid if isinstance(pid, str) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    last_uuid = _find_last_uuid(entries)
    if last_uuid:
        uuid_to_parent_baseline = {e.uuid: e.parent_uuid for e in entries if e.uuid}
        chain_b, _ = _walk_chain(last_uuid, uuid_to_parent_baseline)
        report.chain_before = len(chain_b)

    # Reset entries for actual repair
    for e in entries:
        e.obj = None
        e.uuid = None
        e.parent_uuid = None
        e.entry_type = None
        e.modified = False

    # Phase 1: sanitize
    phase1_sanitize(entries, report)

    # Phase 1b: snapshot collisions
    phase1b_fix_snapshot_collisions(entries, report)

    # Phase 2: orphan parents
    phase2_fix_orphans(entries, report)

    # Phase 3
    phase3_maximize(entries, report)

    # Touch: update last message timestamp to now
    if touch:
        _touch_last_timestamp(entries, report)

    # Verify
    verify(entries, report)

    # Report
    print_report(report, verbose=verbose)

    if dry_run:
        print("[dry-run] No files modified.")
        return report

    # Abort on integrity failures unless --force
    failures: list[str] = []
    if report.remaining_parse_errors > 0:
        failures.append(f"{report.remaining_parse_errors} unparseable lines")
    if report.remaining_orphans > 0:
        failures.append(f"{report.remaining_orphans} orphan parentUuids")
    if report.has_cycle:
        failures.append("cycle detected in chain")
    if report.duplicate_uuids > 0:
        failures.append(f"{report.duplicate_uuids} duplicate UUIDs")
    if failures and not force:
        print(f"[abort] Integrity check failed: {'; '.join(failures)}. "
              f"Use --force to write anyway.", file=sys.stderr)
        sys.exit(1)

    # Backup
    if not no_backup:
        backup = create_backup(input_path)
        report.backup_path = str(backup)
        print(f"[backup] {backup}")

    # Write output (atomic: temp file + replace)
    out = output_path or input_path
    output_bytes = b"\n".join(e.raw for e in entries) + b"\n"
    tmp = out.with_suffix(".tmp")
    try:
        tmp.write_bytes(output_bytes)
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"[done] Written to {out}  ({len(output_bytes):,} bytes)")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair corrupted Claude Code session JSONL files."
    )
    parser.add_argument("input", type=Path, help="Path to the JSONL file")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Output path (default: overwrite input after backup)")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backup creation")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and report without modifying")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-line fix details")
    parser.add_argument("--touch", action="store_true",
                        help="Update last message timestamp to now (avoids 'session too old' warning)")
    parser.add_argument("--force", action="store_true",
                        help="Write output even if integrity checks fail")

    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    repair(args.input, args.output, args.no_backup, args.dry_run,
           args.verbose, args.touch, args.force)


if __name__ == "__main__":
    main()
