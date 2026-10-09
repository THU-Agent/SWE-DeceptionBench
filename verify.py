from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PHASES = {
    "requirements_analysis",
    "system_design",
    "development",
    "testing",
    "deployment",
    "operations_maintenance",
}
BENCHMARKS = {
    "active_zh.jsonl": ("active", "zh"),
    "active_en.jsonl": ("active", "en"),
    "passive_zh.jsonl": ("passive", "zh"),
    "passive_en.jsonl": ("passive", "en"),
}
EXPECTED_SHA256 = {
    "benchmark/active_en.jsonl": "090214518c4b9e3530fd82fffb594911c5e7c68a122d0fb0295af6ac527e9bca",
    "benchmark/active_zh.jsonl": "8cc416c2a4264eb6f106ae935ea869394b28198bbf8dd21fb5ed3c64f0de85b0",
    "benchmark/passive_en.jsonl": "be4caa4d3d7fbd72e8618e75f6b59ebb1811367cd2cdb879fb39a809ed87b4b0",
    "benchmark/passive_zh.jsonl": "5ad5c06c36a8f2c1e5eff96a8bcecab36047bb995245442f8173d460dbdf4f3e",
    "prompts/active_judge_en.txt": "98edcaa5b643be7f49f9a0abaaabe55802c757ce305832fe4badd1227a6bf77b",
    "prompts/active_judge_zh.txt": "20de5aa0aa7b8139991d640ba3e0a32726d3de8681ed478225f0ea6f83df827b",
    "prompts/passive_judge_en.txt": "48357c724a86bef436cd6c6875dae735b259c08c35c5b18c58522a93ba71acc8",
    "prompts/passive_judge_zh.txt": "c9f17e09d9dac0c470f024ac339429df72142909ed520abde02b0a68afdca493",
    "prompts/self_reminder_en.txt": "b17dd053448b47fc7daa396323ca26d13c04368858489fe37f7a3047a1fb7580",
    "prompts/self_reminder_zh.txt": "1d3363362ae0c93b50e5fd3bd65ed60f4e27bcd3303d58b5a1f14d4de0370666",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            rows.append(value)
    return rows


def validate_benchmark(path: Path, task: str, language: str) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if len(rows) != 180:
        raise ValueError(f"{path.name}: expected 180 rows, found {len(rows)}")
    if len({row.get("id") for row in rows}) != 180:
        raise ValueError(f"{path.name}: IDs must be unique")
    if {row.get("task") for row in rows} != {task}:
        raise ValueError(f"{path.name}: unexpected task")
    if {row.get("language") for row in rows} != {language}:
        raise ValueError(f"{path.name}: unexpected language")
    if Counter(row.get("prompt_type") for row in rows) != Counter(P0=60, P1=60, P2=60):
        raise ValueError(f"{path.name}: expected 60 rows for each prompt type")
    if Counter(row.get("phase") for row in rows) != Counter({phase: 30 for phase in PHASES}):
        raise ValueError(f"{path.name}: lifecycle phases are incomplete or unbalanced")

    expected_ids = {f"{number:03d}-P{variant}" for number in range(1, 61) for variant in range(3)}
    actual_ids = {str(row.get("id") or "") for row in rows}
    if actual_ids != expected_ids:
        raise ValueError(f"{path.name}: IDs must follow 001-P0 through 060-P2")

    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item_id = str(row["id"])
        match = re.fullmatch(r"(\d{3})-(P[012])", item_id)
        messages = row.get("messages")
        if not match or match.group(2) != row.get("prompt_type"):
            raise ValueError(f"{path.name}: ID and prompt_type disagree for {item_id}")
        if not isinstance(messages, list) or len(messages) != 1:
            raise ValueError(f"{path.name}: every row must contain one user message")
        if messages[0].get("role") != "user" or not str(messages[0].get("content") or "").strip():
            raise ValueError(f"{path.name}: invalid user message")
        by_case[match.group(1)].append(row)
    if len(by_case) != 60:
        raise ValueError(f"{path.name}: expected 60 paired source cases")
    for case_number, variants in by_case.items():
        if {row["prompt_type"] for row in variants} != {"P0", "P1", "P2"}:
            raise ValueError(f"{path.name}: incomplete variants for case {case_number}")
        if len({row["phase"] for row in variants}) != 1:
            raise ValueError(f"{path.name}: phase mismatch for case {case_number}")
    return rows


def signature(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (str(row["id"]), str(row["phase"]), str(row["prompt_type"]))
        for row in rows
    ]


def main() -> int:
    hashes = {name: sha256(ROOT / name) for name in EXPECTED_SHA256}
    mismatches = {
        name: {"expected": EXPECTED_SHA256[name], "actual": value}
        for name, value in hashes.items()
        if value != EXPECTED_SHA256[name]
    }
    if mismatches:
        raise ValueError("Frozen file hash mismatch: " + json.dumps(mismatches, indent=2))

    loaded = {
        name: validate_benchmark(ROOT / "benchmark" / name, task, language)
        for name, (task, language) in BENCHMARKS.items()
    }
    reference = signature(loaded["active_zh.jsonl"])
    for name, rows in loaded.items():
        if signature(rows) != reference:
            raise ValueError(f"{name}: benchmark ID order or structure differs")

    for name in (
        "active_judge_zh.txt",
        "active_judge_en.txt",
        "passive_judge_zh.txt",
        "passive_judge_en.txt",
    ):
        text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
        if text.count("{{PROMPT}}") != 1 or text.count("{{RESPONSE}}") != 1:
            raise ValueError(f"{name}: expected one PROMPT and one RESPONSE placeholder")
    for name in ("self_reminder_zh.txt", "self_reminder_en.txt"):
        if not (ROOT / "prompts" / name).read_text(encoding="utf-8").strip():
            raise ValueError(f"{name}: empty Self-Reminder")

    print(
        json.dumps(
            {
                "status": "ok",
                "benchmark_files": 4,
                "total_instances": sum(len(rows) for rows in loaded.values()),
                "instances_by_task": {task: 360 for task in ("active", "passive")},
                "instances_by_language": {language: 360 for language in ("zh", "en")},
                "frozen_file_sha256": hashes,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
