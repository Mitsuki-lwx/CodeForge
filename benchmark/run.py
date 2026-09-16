"""CodeForge 基准 CLI。

用法：
  python -m benchmark.run --dataset smoke --variant dev --max-items 1
  codeforge-bench --dataset smoke --variant dev --no-record

flag：
  --no-record   只跑+本地打分，不写 Langfuse（省得污染项目）。
  --keep-artifacts  跑完不清理每个 item 的临时目录（便于调试）。
  --model N      用 providers 里第 N 个（0 起始）作为评测模型，默认 0。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from benchmark.datasets import get_dataset, list_dataset_names
from benchmark.runner import print_table, run_and_record


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codeforge-bench",
        description="用 Langfuse dataset + experiment 评测 CodeForge agent 能力",
    )
    p.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p.add_argument("--dataset", default="smoke", choices=list_dataset_names(),
                   help=f"数据集（默认 smoke；可选 {list_dataset_names()}）")
    p.add_argument("--variant", default="dev", help="experiment run 名（用于 UI 对比）")
    p.add_argument("--model", type=int, default=0,
                   help="用 providers 里第几个（0 起始）作为评测模型，默认 0")
    p.add_argument("--max-items", type=int, default=None,
                   help="只跑前 N 条 item（默认全量）")
    p.add_argument("--max-iterations", type=int, default=12,
                   help="单条 item 的最大 agent 迭代轮数（默认 12）")
    p.add_argument("--keep-artifacts", action="store_true",
                   help="跑完不清理临时目录")
    p.add_argument("--no-record", action="store_true",
                   help="只跑+本地打分，不写 Langfuse")
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows：强制 UTF-8 以支持中文输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    args = _build_parser().parse_args(argv)

    # 启动可观测性（config.yaml 已配 OTLP → Langfuse）；失败静默
    try:
        from core.observability import ensure_initialized

        ensure_initialized()
    except Exception:  # noqa: BLE001
        pass

    providers = load_config(args.config)
    if not providers:
        print("no provider configured")
        return 1
    provider = providers[args.model]
    print(f"[bench] provider = {provider.name} ({provider.model})")

    items = get_dataset(args.dataset)
    try:
        results = asyncio.run(
            run_and_record(
                provider,
                args.dataset,
                items,
                variant=args.variant,
                max_items=args.max_items,
                max_iterations=args.max_iterations,
                keep_artifacts=args.keep_artifacts,
                record=not args.no_record,
            )
        )
    except KeyboardInterrupt:
        print("\n[bench] interrupted")
        return 130

    print_table(results, args.variant)
    return 0


def load_config(config_path: str):
    from config.loader import load_config as _load

    return _load(config_path)


if __name__ == "__main__":
    sys.exit(main())
