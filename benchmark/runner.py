"""CodeForge 基准：Langfuse dataset + 手动 experiment 记录（显式 trace_id 关联）。

流程：
  1. load_auth：优先 env，否则 decode config.yaml 的 observability 凭据。
  2. 用 langfuse SDK 建 dataset + upsert items（幂等，按 id 覆盖）。
  3. 逐个 item 驱动 CodeForge（benchmark.engine.run_one），拿到每条的输出 + trace_id。
  4. 打 ground-truth 分（确定性 evaluator）+ 用量/耗时分数。
  5. 手动记录 experiment：对每个 run item 调 api.dataset_run_items.create，
     把 dataset_item_id 关联到 run_one 返回的 CodeForge 真实 trace_id
     （Langfuse 数据模型：DatasetRunItem.traceId 必填，UI 里点 run item 能钻进执行 trace）。
  6. 输出 rich 表格。

凭据获取顺序（与 core/observability 一致：env 优先，yaml 兜底）：
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST  否则 decode config.yaml:36。
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from benchmark.engine import run_one
from benchmark.evaluators import evaluate_multi, score_item
from benchmark.evaluators.efficiency import EFFICIENCY_GATE
from config.model import ProviderConfig

logger = logging.getLogger(__name__)
console = Console()

# metric -> Langfuse NUMERIC score config id（在 Langfuse 项目里建的，range 0–1）。
# 让 item 级确定性分挂到 config 上，UI 可按 config 过滤/聚合正确率。
SCORE_CONFIG_IDS: dict[str, str] = {
    "contains": "0daaa483-f7dd-4b3b-b1ae-6026a0608831",  # codeforge-contains
    "regex": "96bbc16a-4785-4873-9fd0-8dad42685102",  # codeforge-regex
    "pytest_pass": "5f1e686f-d6b4-4892-b799-8eae63ee20f2",  # codeforge-pytest-pass
}

# 多维评测各维度 -> 对应 NUMERIC score config id。
EVAL_CONFIG_IDS: dict[str, str] = {
    "semantic": "70f7cd53-ecdb-4a46-9d7e-fe60e9af9109",  # codeforge-eval-semantic
    "conformance": "6d35d3a8-5f9a-4299-9b05-632af7a69e77",  # codeforge-eval-conformance
    "quality": "16457231-378e-4ff9-a2d6-9246f7aa546d",  # codeforge-eval-quality
    "efficiency": "d804c957-aed2-4a57-bbe0-568a0ce899bf",  # codeforge-eval-efficiency
    "steps": "d0b4d688-fbc3-486c-9951-8961f01576b1",  # codeforge-eval-steps
}


# ── 认证与客户端 ─────────────────────────────────────────────────────


def load_auth(config_path: str = "config.yaml") -> tuple[str, str, str]:
    """返回 (public_key, secret_key, host)。env 优先，否则 decode config.yaml。"""
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST")
    if pk and sk and host:
        return pk, sk, host

    cfg = _load_yaml(config_path)
    obs = (cfg or {}).get("observability") or {}
    endpoint = obs.get("endpoint", "")
    headers = obs.get("headers") or {}
    auth_header = headers.get("Authorization", "")

    if host is None:
        # 去掉 OTLP 路径留基址；如无路径则原样
        host = endpoint.split("/api/public/otel/v1/traces")[0] if endpoint else ""
    if pk is None or sk is None:
        if auth_header.startswith("Basic "):
            decoded = base64.b64decode(auth_header[len("Basic ") :]).decode()
            pk, _, sk = decoded.partition(":") if ":" in decoded else (decoded, "", "")
    if not (pk and sk and host):
        raise RuntimeError(
            "缺少 Langfuse 凭据：请在 env 设 LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST，"
            "或在 config.yaml 的 observability 里配好 endpoint + Authorization。"
        )
    return pk, sk, host


def _load_yaml(config_path: str) -> dict[str, Any]:
    import yaml

    p = Path(config_path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def get_langfuse_client(config_path: str = "config.yaml"):
    """先加载凭据再 import Langfuse（SDK 在 import 时读 creds，违反会拿错配置）。"""
    pk, sk, host = load_auth(config_path)
    from langfuse import Langfuse

    return Langfuse(public_key=pk, secret_key=sk, host=host)


# ── Dataset / items 幂等建 ──────────────────────────────────────────


def ensure_dataset(client, dataset_name: str, items: list[dict]) -> None:
    """建 dataset（若不存在）并 upsert 全部 item（按 id 幂等覆盖）。"""
    try:
        client.get_dataset(dataset_name)
    except Exception:  # NotFound
        client.create_dataset(name=dataset_name, description="CodeForge 能力基准")

    for it in items:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=f"{dataset_name}:{it.get('name')}",
            input={"name": it.get("name")},
            expected_output={
                "metric": it.get("metric", "contains"),
                "expected_output": it.get("expected_output", ""),
            },
            metadata={"task": it.get("task", "")},
        )
    client.flush()


def _dataset_item_id(client, dataset_name: str, item_name: str) -> str | None:
    """从 Langfuse 数据集里查一条 item 的 id；无则 None。"""
    ds = client.get_dataset(dataset_name)
    for it in ds.items:
        if it.input and isinstance(it.input, dict) and it.input.get("name") == item_name:
            return it.id
    return None


# ── 跑 + 记录 ───────────────────────────────────────────────────────


async def run_and_record(
    provider: ProviderConfig,
    dataset_name: str,
    items: list[dict],
    *,
    variant: str = "dev",
    max_items: int | None = None,
    max_iterations: int = 12,
    keep_artifacts: bool = False,
    record: bool = True,
    deny_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """跑一组 item 并（可选）记录到 Langfuse，返回每条结果。"""
    selected = items[:max_items] if max_items else items
    client = get_langfuse_client() if record else None

    if record:
        ensure_dataset(client, dataset_name, selected)

    results: list[dict[str, Any]] = []
    for it in selected:
        console.print(f"[cyan]▶ item[/cyan] {it.get('name')}")
        res = await run_one(
            provider,
            it,
            max_iterations=max_iterations,
            keep_artifacts=True,  # pytest_pass 需要 cwd；统一评分后再清理
            dataset_name=dataset_name,
            session_id=f"bench:{variant}",  # 整条 run 归到一个 Langfuse Session
            deny_tools=deny_tools or (it.get("edge") or {}).get("deny_tools"),
        )
        # 评分（确定性 evaluator）：pytest_pass 需要真实 cwd，故先于清理
        score = score_item(it, res["output"], cwd=Path(res["tmpdir"]))

        # 多维评测（可执行时黑盒语义/规约/质量/效率），cwd 未清理前跑
        dims = evaluate_multi(it, res["output"], res, cwd=str(res["tmpdir"]))

        result_row = {
            **res,
            "score_value": score["value"],
            "score_comment": score["comment"],
            "expected_output": it.get("expected_output", ""),
            "dims": dims,
        }
        results.append(result_row)

        if record and result_row["trace_id"]:
            _record_item(client, dataset_name, variant, it, result_row, score)
            _record_multi(client, variant, it, result_row, dims)

        # 清理临时 cwd
        if not keep_artifacts:
            import shutil

            shutil.rmtree(Path(res["tmpdir"]), ignore_errors=True)

    if record:
        client.flush()
    return results


def _record_item(
    client,
    dataset_name: str,
    variant: str,
    item: dict,
    result: dict[str, Any],
    score: dict[str, Any],
) -> None:
    """把一条结果的 trace_id 显式关联到 experiment run item，并打分。"""
    item_name = item.get("name")
    item_id = _dataset_item_id(client, dataset_name, item_name)
    if not item_id:
        logger.warning("item %s 不在 Langfuse 数据集里，跳过记录", item_name)
        return
    trace_id = result["trace_id"]
    try:
        # 同一 variant 的所有 item 复用同一个 run_name，会归入同一个 dataset run。
        # `create` 的 metadata 是 run 级且 last-write-wins（对同一 run 覆盖），
        # 所以这里只放稳定、与 item 无关的字段，避免最后一个 item 覆盖整条 run 的
        # metadata（item / elapsed_s 属于 item 级信息，已分别打在 trace score 上）。
        dri = client.api.dataset_run_items.create(
            run_name=variant,
            run_description=variant,
            metadata={
                "variant": variant,
                "dataset": dataset_name,
                "shape": "same-variant-items-in-one-run",
            },
            dataset_item_id=item_id,
            trace_id=trace_id,
        )
        ds_run_id = dri.dataset_run_id
    except Exception as e:  # noqa: BLE001
        logger.error("dataset_run_items.create 失败（item=%s）: %s", item_name, e)
        ds_run_id = None

    # 打分：ground-truth deterministric score + usage + 耗时
    client.create_score(
        name=score["name"], value=score["value"], trace_id=trace_id,
        comment=score["comment"], metadata={"variant": variant, "item": item_name},
        config_id=SCORE_CONFIG_IDS.get(score["name"]),
        data_type="NUMERIC",
    )
    usage = result.get("usage") or {}
    for key, sname in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
        if usage.get(key) is not None:
            client.create_score(
                name=sname, value=int(usage[key]), trace_id=trace_id,
                metadata={"variant": variant, "item": item_name},
            )
    client.create_score(
        name="elapsed_s", value=float(result["elapsed_s"]), trace_id=trace_id,
        metadata={"variant": variant, "item": item_name},
    )
    if ds_run_id:
        client.create_score(
            name="avg_" + score["name"], value=score["value"],
            dataset_run_id=ds_run_id, comment=f"{item_name} {score['comment']}",
            config_id=SCORE_CONFIG_IDS.get(score["name"]), data_type="NUMERIC",
        )


def _record_multi(client, variant: str, item: dict, result: dict, dims: dict) -> None:
    """把多维评测各维度分打到 trace 上，挂对应 config。

    效率超限（dims['efficiency']['over']）时，语义正确性分乘 `EFFICIENCY_GATE`
    降档，作为“超预算不是错误、但计分打折扣”的可观测门禁。
    """
    trace_id = result.get("trace_id")
    if not trace_id:
        return
    item_name = item.get("name")
    eff = dims.get("efficiency") or {}
    gate = EFFICIENCY_GATE if eff.get("over") else 1.0

    for key in ("semantic", "conformance", "quality", "efficiency", "steps"):
        d = dims.get(key)
        if not d or d.get("value") is None:
            continue  # 缺测（如无 cwd / 无 limits）不写 0，避免误导
        value = d["value"]
        name = d["name"]  # semantic / conformance / quality / efficiency / steps
        if key == "semantic" and gate < 1.0:
            value = round(value * gate, 3)
        client.create_score(
            name=name, value=value, trace_id=trace_id,
            comment=d.get("comment", ""),
            metadata={"variant": variant, "item": item_name,
                      "over": bool(eff.get("over"))},
            config_id=EVAL_CONFIG_IDS.get(name),
            data_type="NUMERIC",
        )


# ── 展示 ─────────────────────────────────────────────────────────────


def _dim_cell(dims: dict, key: str) -> str:
    """格式化维度分：有值显示 `0.85`，缺测显示 `—`。"""
    d = dims.get(key)
    if not d or d.get("value") is None:
        return "—"
    return f"{d['value']:.2f}"


def print_table(results: list[dict[str, Any]], variant: str) -> None:
    table = Table(title=f"CodeForge 基准 — {variant}", title_justify="left")
    table.add_column("item")
    table.add_column("metric")
    table.add_column("score")
    table.add_column("语义")
    table.add_column("规约")
    table.add_column("质量")
    table.add_column("效率")
    table.add_column("steps")
    table.add_column("elapsed_s")
    table.add_column("in_tok")
    table.add_column("out_tok")
    table.add_column("trace_id (前12)")
    table.add_column("output（截断）")
    for r in results:
        dims = r.get("dims") or {}
        usage = r.get("usage") or {}
        out = (r.get("output") or "").replace("\n", " ")
        table.add_row(
            r["name"],
            r.get("score_comment", ""),
            f"{r['score_value']:.2f}" if isinstance(r["score_value"], float) else str(r["score_value"]),
            _dim_cell(dims, "semantic"),
            _dim_cell(dims, "conformance"),
            _dim_cell(dims, "quality"),
            _dim_cell(dims, "efficiency"),
            _dim_cell(dims, "steps"),
            str(r["elapsed_s"]),
            str(usage.get("input_tokens", "")),
            str(usage.get("output_tokens", "")),
            (r.get("trace_id") or "")[:12],
            out[:60],
        )
    console.print(table)
