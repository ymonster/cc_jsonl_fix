"""
Test repair_jsonl.py against known broken/reference file pair.

Usage:
  python test_repair.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BROKEN = Path("test_broken.jsonl")
REFERENCE = Path("18c635d0-e120-46bf-adbf-3b4709b4e43e.jsonl")


def walk_chain(path: Path) -> list[str]:
    """Walk main chain from last uuid backward, return uuid list."""
    uuid_to_parent: dict[str, str | None] = {}
    last_uuid: str | None = None

    with open(path, "rb") as f:
        for line in f:
            line = line.replace(b"\x00", b"").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = obj.get("uuid")
            if uid:
                uuid_to_parent[uid] = obj.get("parentUuid")
                last_uuid = uid  # keep updating; last one wins

    chain: list[str] = []
    visited: set[str] = set()
    current = last_uuid
    while current and current not in visited:
        visited.add(current)
        chain.append(current)
        current = uuid_to_parent.get(current)
    return chain


def get_all_uuids(path: Path) -> set[str]:
    uuids: set[str] = set()
    with open(path, "rb") as f:
        for line in f:
            line = line.replace(b"\x00", b"").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = obj.get("uuid")
            if uid:
                uuids.add(uid)
    return uuids


def test_repair() -> None:
    assert BROKEN.exists(), f"Missing test input: {BROKEN}"
    assert REFERENCE.exists(), f"Missing reference file: {REFERENCE}"

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}  {detail}")

    # Get reference data
    ref_chain = walk_chain(REFERENCE)
    ref_chain_set = set(ref_chain)
    broken_uuids = get_all_uuids(BROKEN)

    # UUIDs in ref chain that also exist in the broken file
    # (some may have been added after repair, e.g. /resume sessions)
    ref_in_broken = ref_chain_set & broken_uuids
    print(f"Reference chain: {len(ref_chain)} msgs, {len(ref_in_broken)} present in broken file\n")

    with tempfile.TemporaryDirectory() as tmp:
        test_file = Path(tmp) / "test.jsonl"
        shutil.copy2(BROKEN, test_file)

        # Run repair
        print("Running repair_jsonl.py ...")
        result = subprocess.run(
            [sys.executable, "repair_jsonl.py", str(test_file), "--no-backup", "--verbose"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent),
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr)

        check("Repair exits successfully", result.returncode == 0,
              f"exit code {result.returncode}")
        if result.returncode != 0:
            print(f"\nAborted: repair failed. stderr:\n{result.stderr}")
            sys.exit(1)

        # Read repaired file
        raw = test_file.read_bytes()

        # Test 1: No NUL bytes
        nul_count = raw.count(b"\x00")
        check("No NUL bytes", nul_count == 0, f"found {nul_count}")

        # Test 2: All JSON lines parseable
        parse_errors = 0
        uuid_to_parent: dict[str, str | None] = {}
        for lineno, line in enumerate(raw.split(b"\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                uid = obj.get("uuid")
                if uid:
                    uuid_to_parent[uid] = obj.get("parentUuid")
            except json.JSONDecodeError:
                parse_errors += 1
        check("All JSON parseable", parse_errors == 0,
              f"{parse_errors} parse errors")

        # Test 3: No orphan parentUuids
        uuid_set = set(uuid_to_parent.keys())
        orphans = [uid for uid, pid in uuid_to_parent.items()
                   if pid and pid not in uuid_set]
        check("No orphan parentUuids", len(orphans) == 0,
              f"{len(orphans)} orphans: {orphans[:3]}")

        # Test 4: Walk repaired chain
        repaired_chain = walk_chain(test_file)
        repaired_set = set(repaired_chain)
        check("Chain length > 0", len(repaired_chain) > 0)

        # Test 5: Chain length >= reference (for UUIDs present in broken file)
        check(f"Chain length >= {len(ref_in_broken)}",
              len(repaired_chain) >= len(ref_in_broken),
              f"got {len(repaired_chain)}")

        # Test 6: All ref chain UUIDs (that exist in broken file) are in repaired chain
        missing = ref_in_broken - repaired_set
        check(f"All {len(ref_in_broken)} ref chain UUIDs present",
              len(missing) == 0,
              f"{len(missing)} missing: {list(missing)[:5]}")

        # Test 7: No cycles
        check("No cycles", len(repaired_chain) <= len(uuid_to_parent),
              f"chain {len(repaired_chain)} > total uuids {len(uuid_to_parent)}")

    # Summary
    print(f"\n{'=' * 40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"  Repaired chain: {len(repaired_chain)} messages")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    test_repair()
