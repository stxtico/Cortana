"""Benchmark a local Ollama model's time-to-first-token and tokens/sec at
increasing context depths. Model comes from config/cortana.toml [models].primary
unless overridden with --model.
"""

import argparse
import json
import time
import tomllib
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"
LOGS_DIR = ROOT / "logs"

DEFAULT_CONTEXT_SIZES = [1024, 8192, 32768]
DEFAULT_RUNS = 3
DEFAULT_MAX_OUTPUT_TOKENS = 200
DEFAULT_OLLAMA_URL = "http://localhost:11434"

FILLER = (
    "The quick brown fox jumps over the lazy dog near the riverbank while "
    "the sun sets slowly behind the distant mountains. "
)


def load_primary_model() -> str:
    with CONFIG_PATH.open("rb") as f:
        config = tomllib.load(f)
    model = config["models"]["primary"]
    if not model:
        raise SystemExit(f"[models].primary is empty in {CONFIG_PATH}")
    return model


def build_prompt(target_tokens: int) -> str:
    # ~4 chars/token is a reasonable cross-tokenizer approximation for filler text.
    target_chars = target_tokens * 4
    reps = target_chars // len(FILLER) + 1
    text = (FILLER * reps)[:target_chars]
    return text + "\n\nSummarize the above in one sentence."


def run_once(client: httpx.Client, model: str, prompt: str, num_ctx: int, max_tokens: int) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"num_ctx": num_ctx, "num_predict": max_tokens},
    }
    start = time.perf_counter()
    first_token_time = None
    final = None
    with client.stream("POST", "/api/generate", json=payload, timeout=120.0) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if first_token_time is None and chunk.get("response"):
                first_token_time = time.perf_counter()
            if chunk.get("done"):
                final = chunk
    end = time.perf_counter()

    if final is None:
        raise RuntimeError("stream ended without a done=true chunk")

    ttft_s = (first_token_time or end) - start
    eval_count = final.get("eval_count", 0)
    eval_duration_s = final.get("eval_duration", 0) / 1e9
    tokens_per_sec = eval_count / eval_duration_s if eval_duration_s > 0 else 0.0

    return {
        "ttft_ms": round(ttft_s * 1000, 1),
        "tokens": eval_count,
        "duration_s": round(end - start, 3),
        "tokens_per_sec": round(tokens_per_sec, 2),
    }


def print_table(results: list[dict], contexts: list[int]) -> None:
    print("\n| Context | Run | TTFT (ms) | Tokens | Duration (s) | Tok/s |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['context']} | {r['run']} | {r['ttft_ms']} | {r['tokens']} | {r['duration_s']} | {r['tokens_per_sec']} |")

    print("\n### Averages per context depth\n")
    print("| Context | Avg TTFT (ms) | Avg Tok/s |")
    print("|---|---|---|")
    for ctx in contexts:
        rows = [r for r in results if r["context"] == ctx]
        avg_ttft = sum(r["ttft_ms"] for r in rows) / len(rows)
        avg_tps = sum(r["tokens_per_sec"] for r in rows) / len(rows)
        print(f"| {ctx} | {avg_ttft:.1f} | {avg_tps:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local Ollama model TTFT and tok/s.")
    parser.add_argument("--model", default=None, help="Override model (defaults to [models].primary in cortana.toml)")
    parser.add_argument("--contexts", type=int, nargs="+", default=DEFAULT_CONTEXT_SIZES)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--url", default=DEFAULT_OLLAMA_URL)
    args = parser.parse_args()

    model = args.model or load_primary_model()

    print(f"Benchmarking `{model}` at {args.url}")
    print(f"Context depths: {args.contexts} | runs each: {args.runs} | output cap: {args.max_tokens} tokens\n")

    results = []
    with httpx.Client(base_url=args.url) as client:
        for ctx in args.contexts:
            prompt = build_prompt(ctx)
            for run_idx in range(1, args.runs + 1):
                print(f"  context={ctx} run={run_idx}/{args.runs} ...", end=" ", flush=True)
                metrics = run_once(client, model, prompt, ctx, args.max_tokens)
                print(f"TTFT={metrics['ttft_ms']}ms  {metrics['tokens_per_sec']} tok/s")
                results.append({"context": ctx, "run": run_idx, **metrics})

    print_table(results, args.contexts)

    LOGS_DIR.mkdir(exist_ok=True)
    out_path = LOGS_DIR / f"bench-{date.today().isoformat()}.json"
    out_path.write_text(json.dumps({"model": model, "url": args.url, "results": results}, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
