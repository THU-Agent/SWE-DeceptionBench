from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pool completed evaluator runs without averaging per-run rates."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Run directories or parent directories containing run_config.json files.",
    )
    parser.add_argument("--output", default=None, help="Optional combined JSON output path.")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    return value


def latest_jsonl(path: Path) -> OrderedDict[str, dict[str, Any]]:
    latest: OrderedDict[str, dict[str, Any]] = OrderedDict()
    if not path.is_file():
        return latest
    with path.open("r", encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not value.get("id"):
                raise ValueError(f"{path}:{number}: expected an object with id")
            latest[str(value["id"])] = value
    return latest


def discover(paths: Iterable[str]) -> list[Path]:
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).resolve()
        if (path / "run_config.json").is_file():
            found.add(path)
        elif path.is_dir():
            found.update(config.parent for config in path.rglob("run_config.json"))
        else:
            raise FileNotFoundError(path)
    if not found:
        raise ValueError("No run_config.json files found")
    return sorted(found)


def load_records(run_dirs: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], Path] = {}
    for run_dir in run_dirs:
        config = read_json(run_dir / "run_config.json")
        responses = latest_jsonl(run_dir / "responses.jsonl")
        judgments = latest_jsonl(run_dir / "judgments.jsonl")
        selected = int(config.get("selected_items") or 0)
        if len(responses) != selected:
            raise ValueError(
                f"{run_dir}: incomplete generation ({len(responses)}/{selected}); finish or resume it first"
            )
        for item_id, response in responses.items():
            judgment = judgments.get(item_id, {})
            record = {
                "id": item_id,
                "model": response.get("model") or config.get("model"),
                "task": response.get("task") or config.get("task"),
                "language": response.get("language") or config.get("language"),
                "condition": response.get("condition") or config.get("condition"),
                "prompt_type": response.get("prompt_type"),
                "phase": response.get("phase"),
                "response_status": response.get("status"),
                "judge_status": judgment.get("status"),
                "behavior_type": judgment.get("behavior_type"),
                "deceptive": judgment.get("deceptive"),
            }
            key = tuple(
                str(record[name])
                for name in ("model", "task", "language", "condition", "prompt_type", "id")
            )
            if key in seen:
                raise ValueError(f"Duplicate experimental cell in {seen[key]} and {run_dir}: {key}")
            seen[key] = run_dir
            records.append(record)
    return records


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(records)
    completed = sum(row["response_status"] == "ok" for row in rows)
    valid = [row for row in rows if row["judge_status"] == "ok"]
    deceptive = sum(row["deceptive"] is True for row in valid)
    judge_errors = sum(row["judge_status"] == "error" for row in rows)
    behavior_counts = Counter(str(row["behavior_type"]) for row in valid)
    return {
        "planned": len(rows),
        "completed_responses": completed,
        "provider_blocked": sum(row["response_status"] == "provider_blocked" for row in rows),
        "generation_errors": sum(row["response_status"] == "error" for row in rows),
        "valid_judgments": len(valid),
        "judge_errors": judge_errors,
        "judge_pending": max(0, completed - len(valid) - judge_errors),
        "deceptive": deceptive,
        "deception_rate": round(deceptive / len(valid), 6) if valid else None,
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "behavior_rates": {
            label: round(count / len(valid), 6)
            for label, count in sorted(behavior_counts.items())
        } if valid else {},
    }


def grouped(records: list[dict[str, Any]], dimensions: tuple[str, ...]) -> dict[str, Any]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        groups[tuple(str(row.get(name)) for name in dimensions)].append(row)
    return {
        " | ".join(key): summarize(rows)
        for key, rows in sorted(groups.items())
    }


def main() -> int:
    args = parse_args()
    run_dirs = discover(args.paths)
    records = load_records(run_dirs)
    report = {
        "source_runs": len(run_dirs),
        "overall": summarize(records),
        "by_model": grouped(records, ("model",)),
        "by_task": grouped(records, ("task",)),
        "by_language": grouped(records, ("language",)),
        "by_condition": grouped(records, ("condition",)),
        "by_prompt_type": grouped(records, ("prompt_type",)),
        "by_phase": grouped(records, ("phase",)),
        "by_model_and_task": grouped(records, ("model", "task")),
        "by_model_and_prompt_type": grouped(records, ("model", "prompt_type")),
        "by_model_and_language": grouped(records, ("model", "language")),
        "by_model_task_condition": grouped(records, ("model", "task", "condition")),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
