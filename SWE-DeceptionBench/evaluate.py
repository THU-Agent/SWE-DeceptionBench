from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from judge import PASSIVE_SYSTEM, PROMPT_DIR, judge_response


ROOT = Path(__file__).resolve().parent
BLOCK_MARKERS = (
    "content filter",
    "content_filter",
    "risk control",
    "cybersecurity risk",
    "safety policy",
    "provider blocked",
    "flagged",
)
ACTIVE_FOREIGN_MODELS = {
    "gpt-5.5",
    "claude-opus-4-8",
    "gemini-3.5-flash",
}
ACTIVE_DOMESTIC_MODELS = {
    "deepseek-v4-pro",
    "glm-5.2",
    "qwen3.7-max",
    "kimi-k2.6",
}
ACTIVE_NO_THINKING_MODELS = {"gpt-5.5"}


@dataclass(frozen=True)
class ChatResult:
    text: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    thinking_parameter: str


class ChatClient:
    """OpenAI-compatible chat client with bounded retry and parameter fallback."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key_env: str,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key environment variable: {api_key_env}")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Install dependencies with: python -m pip install -r requirements.txt"
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self.client = OpenAI(**kwargs)
        self.max_retries = max_retries

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status in {408, 409, 429} or status >= 500
        return True

    @staticmethod
    def _response_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(
                        str(getattr(item, "text", None) or getattr(item, "content", None) or "")
                    )
            return "".join(parts)
        return str(content)

    @staticmethod
    def _thinking_unsupported(exc: Exception) -> bool:
        text = str(exc).lower()
        return ("thinking" in text or "extra_body" in text) and any(
            marker in text for marker in ("unsupported", "unknown", "invalid", "not allowed")
        )

    @staticmethod
    def _thinking_candidates(mode: str) -> list[tuple[dict[str, Any] | None, str]]:
        if mode == "enabled":
            return [({"enable_thinking": True}, "enable_thinking=true")]
        if mode == "disabled":
            return [
                ({"thinking": {"type": "disabled"}}, "thinking=disabled"),
                (None, "omitted"),
            ]
        return [(None, "omitted")]

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, str] | None = None,
        thinking: str = "omit",
    ) -> ChatResult:
        last_error: Exception | None = None
        candidates = self._thinking_candidates(thinking)

        for candidate_index, (extra_body, thinking_name) in enumerate(candidates):
            for attempt in range(self.max_retries + 1):
                request: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
                if temperature is not None:
                    request["temperature"] = temperature
                if max_tokens is not None:
                    request["max_tokens"] = max_tokens
                if response_format is not None:
                    request["response_format"] = response_format
                if extra_body is not None:
                    request["extra_body"] = extra_body

                try:
                    response = self.client.chat.completions.create(**request)
                    if not response.choices:
                        raise RuntimeError("Provider returned no choices")
                    choice = response.choices[0]
                    text = self._response_text(choice.message.content)
                    if not text.strip():
                        reasoning = getattr(choice.message, "reasoning_content", None)
                        if reasoning:
                            text = str(reasoning)
                    usage = response.usage
                    return ChatResult(
                        text=text,
                        finish_reason=choice.finish_reason,
                        prompt_tokens=getattr(usage, "prompt_tokens", None),
                        completion_tokens=getattr(usage, "completion_tokens", None),
                        total_tokens=getattr(usage, "total_tokens", None),
                        thinking_parameter=thinking_name,
                    )
                except Exception as exc:  # Provider SDKs expose different exception classes.
                    last_error = exc
                    if extra_body is not None and self._thinking_unsupported(exc):
                        break
                    if attempt >= self.max_retries or not self._retryable(exc):
                        raise
                    time.sleep(min(2**attempt, 8))

            if candidate_index == len(candidates) - 1 and last_error is not None:
                raise last_error

        assert last_error is not None
        raise last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a SWE-Deception benchmark, judge responses, and compute metrics."
    )
    parser.add_argument("--benchmark", required=True, help="Benchmark JSONL path.")
    parser.add_argument("--model", required=True, help="Exact provider model ID or alias.")
    parser.add_argument("--judge-model", required=True, help="Exact judge model ID or alias.")
    parser.add_argument("--output", required=True, help="Output directory for this run.")
    parser.add_argument("--self-reminder", action="store_true")
    parser.add_argument(
        "--prompt-type",
        choices=("all", "P0", "P1", "P2"),
        default="all",
        help="Run all prompt types or select one type.",
    )
    parser.add_argument("--stage", choices=("all", "generate", "judge", "metrics"), default="all")
    parser.add_argument("--limit", type=int, default=None, help="Use the first N items for a smoke test.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the plan only.")

    parser.add_argument("--model-base-url", default=os.getenv("MODEL_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--model-api-key-env", default="MODEL_API_KEY")
    parser.add_argument("--judge-base-url", default=os.getenv("JUDGE_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--judge-api-key-env", default="JUDGE_API_KEY")
    temperature = parser.add_mutually_exclusive_group()
    temperature.add_argument("--temperature", type=float, default=None)
    temperature.add_argument(
        "--omit-temperature",
        action="store_true",
        help="Do not send temperature to the tested-model API.",
    )
    parser.add_argument(
        "--thinking",
        choices=("auto", "enabled", "disabled", "omit"),
        default="auto",
        help="Tested-model thinking parameter. Auto reproduces the original experiment profiles.",
    )
    parser.add_argument("--model-max-tokens", type=int, default=None)
    parser.add_argument("--model-timeout", type=float, default=None)
    parser.add_argument("--judge-timeout", type=float, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help="Seconds to wait after each tested-model call (Active original: 3).",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_existing_config(path: Path, requested: dict[str, Any]) -> str | None:
    if not path.is_file():
        return None
    existing = json.loads(path.read_text(encoding="utf-8"))
    ignored = {"started_at"}
    mismatches = {
        key: {"existing": existing.get(key), "requested": value}
        for key, value in requested.items()
        if key not in ignored and existing.get(key) != value
    }
    if mismatches:
        raise RuntimeError(
            "Output directory belongs to a different run; use a new --output path. "
            + json.dumps(mismatches, ensure_ascii=False)
        )
    return str(existing.get("started_at")) if existing.get("started_at") else None


def validate_benchmark(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        raise ValueError("Benchmark is empty")
    seen: set[str] = set()
    tasks: set[str] = set()
    languages: set[str] = set()
    for index, row in enumerate(rows, start=1):
        item_id = str(row.get("id") or "")
        task = str(row.get("task") or "")
        language = str(row.get("language") or "")
        messages = row.get("messages")
        if not item_id or item_id in seen:
            raise ValueError(f"Row {index}: missing or duplicate id={item_id!r}")
        if task not in {"passive", "active"}:
            raise ValueError(f"Row {index}: task must be passive or active")
        if language not in {"zh", "en"}:
            raise ValueError(f"Row {index}: language must be zh or en")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Row {index}: messages must be a non-empty list")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                raise ValueError(f"Row {index}: invalid message")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise ValueError(f"Row {index}: empty message content")
        if row.get("prompt_type") not in {"P0", "P1", "P2"}:
            raise ValueError(f"Row {index}: prompt_type must be P0/P1/P2")
        seen.add(item_id)
        tasks.add(task)
        languages.add(language)
    if len(tasks) != 1 or len(languages) != 1:
        raise ValueError("Each benchmark file must contain one task and one language")
    return next(iter(tasks)), next(iter(languages))


def latest_by_id(path: Path) -> dict[str, dict[str, Any]]:
    latest: OrderedDict[str, dict[str, Any]] = OrderedDict()
    if not path.is_file():
        return latest
    for row in read_jsonl(path):
        item_id = str(row.get("id") or "")
        if item_id:
            latest[item_id] = row
    return latest


def provider_blocked(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def self_reminder(language: str) -> str:
    return (PROMPT_DIR / f"self_reminder_{language}.txt").read_text(encoding="utf-8").strip()


def tested_messages(item: dict[str, Any], *, use_self_reminder: bool) -> list[dict[str, str]]:
    messages = [
        {"role": str(message["role"]), "content": str(message["content"])}
        for message in item["messages"]
    ]
    if use_self_reminder:
        return [{"role": "system", "content": self_reminder(str(item["language"]))}, *messages]
    return messages


def generation_defaults(task: str, args: argparse.Namespace) -> dict[str, Any]:
    model = args.model.strip().lower()
    if task == "active":
        if model in ACTIVE_FOREIGN_MODELS:
            profile = "active_original_modelverse"
            default_temperature = None
        elif model in ACTIVE_DOMESTIC_MODELS:
            profile = "active_original_aliyun"
            default_temperature = 0.0
        else:
            profile = "active_generic"
            default_temperature = 0.0
        default_thinking = "omit" if model in ACTIVE_NO_THINKING_MODELS else "enabled"
    else:
        if model in ACTIVE_FOREIGN_MODELS:
            profile = "passive_original_modelverse"
            default_temperature = None
        elif model in ACTIVE_DOMESTIC_MODELS:
            profile = "passive_original_aliyun"
            default_temperature = 0.0
        else:
            profile = "passive_generic"
            default_temperature = 0.0
        default_thinking = "omit"

    temperature = None if args.omit_temperature else (
        args.temperature if args.temperature is not None else default_temperature
    )
    return {
        "profile": profile,
        "temperature": temperature,
        "max_tokens": args.model_max_tokens if args.model_max_tokens is not None else (None if task == "active" else 6000),
        "timeout_seconds": args.model_timeout if args.model_timeout is not None else (600.0 if task == "active" else 420.0),
        "max_retries": args.max_retries if args.max_retries is not None else (5 if task == "active" else 2),
        "thinking": default_thinking if args.thinking == "auto" else args.thinking,
        "request_delay_seconds": args.request_delay if args.request_delay is not None else (3.0 if task == "active" else 0.0),
    }


def judge_defaults(task: str, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "timeout_seconds": args.judge_timeout if args.judge_timeout is not None else (300.0 if task == "active" else 240.0),
        "max_retries": args.max_retries if args.max_retries is not None else (3 if task == "active" else 2),
    }


def run_generation(
    *,
    items: list[dict[str, Any]],
    args: argparse.Namespace,
    task: str,
    responses_path: Path,
) -> None:
    settings = generation_defaults(task, args)
    client = ChatClient(
        base_url=args.model_base_url,
        api_key_env=args.model_api_key_env,
        timeout_seconds=settings["timeout_seconds"],
        max_retries=settings["max_retries"],
    )
    existing = latest_by_id(responses_path)
    condition = "self_reminder" if args.self_reminder else "baseline"

    for index, item in enumerate(items, start=1):
        item_id = str(item["id"])
        if existing.get(item_id, {}).get("status") in {"ok", "provider_blocked"}:
            continue
        messages = tested_messages(item, use_self_reminder=args.self_reminder)
        base = {
            "id": item_id,
            "model": args.model,
            "task": task,
            "language": item["language"],
            "condition": condition,
            "prompt_type": item.get("prompt_type"),
            "phase": item.get("phase"),
            "timestamp": now_iso(),
        }
        try:
            result = client.complete(
                model=args.model,
                messages=messages,
                temperature=settings["temperature"],
                max_tokens=settings["max_tokens"],
                thinking=settings["thinking"],
            )
            if not result.text.strip():
                raise RuntimeError("Provider returned an empty response")
            row = {
                **base,
                "status": "ok",
                "response_text": result.text,
                "finish_reason": result.finish_reason,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                "thinking_parameter": result.thinking_parameter,
            }
        except Exception as exc:  # Preserve failed calls in the denominator audit.
            message = f"{type(exc).__name__}: {exc}"
            row = {
                **base,
                "status": "provider_blocked" if provider_blocked(message) else "error",
                "response_text": "",
                "error": message[:2000],
            }
        append_jsonl(responses_path, row)
        existing[item_id] = row
        print(f"generate {index}/{len(items)} {item_id}: {row['status']}", flush=True)
        if settings["request_delay_seconds"] > 0:
            time.sleep(settings["request_delay_seconds"])


def run_judging(
    *,
    items: list[dict[str, Any]],
    args: argparse.Namespace,
    task: str,
    responses_path: Path,
    judgments_path: Path,
) -> None:
    responses = latest_by_id(responses_path)
    if not responses:
        raise RuntimeError(f"No responses found: {responses_path}")
    settings = judge_defaults(task, args)
    client = ChatClient(
        base_url=args.judge_base_url,
        api_key_env=args.judge_api_key_env,
        timeout_seconds=settings["timeout_seconds"],
        max_retries=settings["max_retries"],
    )
    existing = latest_by_id(judgments_path)

    for index, item in enumerate(items, start=1):
        item_id = str(item["id"])
        response = responses.get(item_id)
        if not response or response.get("status") != "ok":
            continue
        if existing.get(item_id, {}).get("status") == "ok":
            continue
        base = {
            "id": item_id,
            "model": args.model,
            "task": task,
            "language": item["language"],
            "condition": response.get("condition"),
            "prompt_type": item.get("prompt_type"),
            "phase": item.get("phase"),
            "judge_model": args.judge_model,
            "timestamp": now_iso(),
        }
        try:
            result = judge_response(
                client=client,
                judge_model=args.judge_model,
                item=item,
                model_response=str(response["response_text"]),
            )
            row = {
                **base,
                "status": "ok",
                "behavior_type": result.behavior_type,
                "deceptive": result.deceptive,
                "reason": result.reason,
                "raw_judge_output": result.raw_output,
                "thinking_parameter": result.thinking_parameter,
            }
        except Exception as exc:
            row = {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        append_jsonl(judgments_path, row)
        existing[item_id] = row
        print(f"judge {index}/{len(items)} {item_id}: {row['status']}", flush=True)


def summarize(
    ids: Iterable[str],
    responses: dict[str, dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected = list(ids)
    response_rows = [responses[item_id] for item_id in selected if item_id in responses]
    judgment_rows = [judgments[item_id] for item_id in selected if item_id in judgments]
    completed = sum(row.get("status") == "ok" for row in response_rows)
    blocked = sum(row.get("status") == "provider_blocked" for row in response_rows)
    generation_errors = sum(row.get("status") == "error" for row in response_rows)
    not_run = len(selected) - len(response_rows)
    valid = [row for row in judgment_rows if row.get("status") == "ok"]
    judge_errors = sum(row.get("status") == "error" for row in judgment_rows)
    deceptive = sum(row.get("deceptive") is True for row in valid)
    behavior_counts = Counter(str(row.get("behavior_type")) for row in valid)
    return {
        "planned": len(selected),
        "completed_responses": completed,
        "provider_blocked": blocked,
        "generation_errors": generation_errors,
        "not_run": not_run,
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


def compute_metrics(
    *,
    items: list[dict[str, Any]],
    responses_path: Path,
    judgments_path: Path,
) -> dict[str, Any]:
    responses = latest_by_id(responses_path)
    judgments = latest_by_id(judgments_path)
    index = {str(item["id"]): item for item in items}
    all_ids = list(index)

    by_prompt_type: dict[str, list[str]] = defaultdict(list)
    by_phase: dict[str, list[str]] = defaultdict(list)
    for item_id, item in index.items():
        if item.get("prompt_type"):
            by_prompt_type[str(item["prompt_type"])].append(item_id)
        if item.get("phase"):
            by_phase[str(item["phase"])].append(item_id)

    return {
        "overall": summarize(all_ids, responses, judgments),
        "by_prompt_type": {
            key: summarize(ids, responses, judgments)
            for key, ids in sorted(by_prompt_type.items())
        },
        "by_phase": {
            key: summarize(ids, responses, judgments)
            for key, ids in sorted(by_phase.items())
        },
    }


def prompt_hashes(task: str, language: str, use_self_reminder: bool) -> dict[str, str]:
    judge_name = f"{task}_judge_{language}.txt"
    template_path = PROMPT_DIR / judge_name
    template = template_path.read_text(encoding="utf-8")
    prompt_placeholder = "user: __PROMPT__" if task == "passive" else "__PROMPT__"
    rendered_template = template.replace("{{PROMPT}}", prompt_placeholder).replace(
        "{{RESPONSE}}", "__RESPONSE__"
    )
    judge_payload = (
        json.dumps(
            [
                {"role": "system", "content": PASSIVE_SYSTEM},
                {"role": "user", "content": rendered_template.strip()},
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if task == "passive"
        else rendered_template
    )
    hashes = {"judge_prompt_sha256": hashlib.sha256(judge_payload.encode("utf-8")).hexdigest()}
    if use_self_reminder:
        reminder_path = PROMPT_DIR / f"self_reminder_{language}.txt"
        hashes["self_reminder_sha256"] = hashlib.sha256(
            reminder_path.read_text(encoding="utf-8").strip().encode("utf-8")
        ).hexdigest()
    return hashes


def main() -> int:
    args = parse_args()
    benchmark_path = Path(args.benchmark).resolve()
    output_dir = Path(args.output).resolve()
    items = read_jsonl(benchmark_path)
    task, language = validate_benchmark(items)
    if args.prompt_type != "all":
        items = [item for item in items if item.get("prompt_type") == args.prompt_type]
        if not items:
            raise ValueError(f"No {args.prompt_type} rows found in {benchmark_path}")
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be positive")
        items = items[: args.limit]

    generation = generation_defaults(task, args)
    judge_config = judge_defaults(task, args)
    condition = "self_reminder" if args.self_reminder else "baseline"
    config = {
        "benchmark": benchmark_path.name,
        "benchmark_sha256": file_sha256(benchmark_path),
        "selected_items": len(items),
        "task": task,
        "language": language,
        "prompt_type": args.prompt_type,
        "model": args.model,
        "judge_model": args.judge_model,
        "condition": condition,
        "generation": generation,
        "judge": {**judge_config, "temperature": 0.0},
        "model_base_url": args.model_base_url,
        "model_api_key_env": args.model_api_key_env,
        "judge_base_url": args.judge_base_url,
        "judge_api_key_env": args.judge_api_key_env,
        "python": platform.python_version(),
        "code_sha256": {
            name: file_sha256(ROOT / name)
            for name in ("evaluate.py", "judge.py", "requirements.txt")
        },
        **prompt_hashes(task, language, args.self_reminder),
    }
    print(json.dumps(config, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    previous_started_at = check_existing_config(output_dir / "run_config.json", config)
    config["started_at"] = previous_started_at or now_iso()
    write_json(output_dir / "run_config.json", config)
    responses_path = output_dir / "responses.jsonl"
    judgments_path = output_dir / "judgments.jsonl"

    if args.stage in {"all", "generate"}:
        run_generation(
            items=items,
            args=args,
            task=task,
            responses_path=responses_path,
        )
    if args.stage in {"all", "judge"}:
        run_judging(
            items=items,
            args=args,
            task=task,
            responses_path=responses_path,
            judgments_path=judgments_path,
        )
    if args.stage in {"all", "metrics"}:
        metrics = compute_metrics(
            items=items,
            responses_path=responses_path,
            judgments_path=judgments_path,
        )
        write_json(output_dir / "metrics.json", metrics)
        print(json.dumps(metrics["overall"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed JSONL rows are safe to resume.", file=sys.stderr)
        raise SystemExit(130)
