"""Evidence output: one machine-readable JSON + one human-readable Markdown."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .model import RunReport, Status


def _badge(status: Status) -> str:
    return {"PASS": "通过", "FAIL": "失败", "SKIP": "跳过", "BLOCKED": "阻塞"}[status.value]


def write_reports(report: RunReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / (report.run_id + ".json")
    md_path = output_dir / (report.run_id + ".md")
    body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
    json_path.write_text(body, encoding="utf-8")
    (output_dir / (json_path.name + ".sha256")).write_text(
        hashlib.sha256(body.encode("utf-8")).hexdigest() + "  " + json_path.name + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def _markdown(r: RunReport) -> str:
    lines: list[str] = []
    lines.append("# macOS → A7M4 0336 分阶段测试报告")
    lines.append("")
    lines.append("- 运行 ID：`%s`" % r.run_id)
    lines.append("- 开始：%s" % r.started_at)
    lines.append("- 模拟场景：`%s`" % (r.simulate or "无（真实 macOS 主机）"))
    lines.append("- 向相机发送的命令次数（认证/会话，不含任何写入）：**%d**" %
                 sum(x.camera_commands for x in r.results))
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append("**`%s`** — %s" % (r.verdict.value, r.verdict_reason))
    lines.append("")
    milestones = [e for e in r.timeline if e.get("kind") == "milestone"]
    if milestones:
        lines.append("## 节点里程碑")
        lines.append("")
        for event in milestones:
            lines.append("- ★ `%s` **[%6.1fs]** %s" % (
                event.get("stage") or "--", event.get("elapsed_s", 0),
                event.get("message", "")))
        lines.append("")
    lines.append("| 阶段 | 名称 | 层级 | 结果 | 耗时 | 相机命令 |")
    lines.append("|---|---|---|---|---:|---:|")
    for x in r.results:
        lines.append("| %s | %s | %s | %s | %d ms | %d |" % (
            x.stage, x.title, x.tier.name, _badge(x.status), x.elapsed_ms, x.camera_commands))
    lines.append("")
    lines.append("## 逐阶段证据")
    lines.append("")
    for x in r.results:
        lines.append("### %s %s — %s" % (x.stage, x.title, _badge(x.status)))
        lines.append("")
        lines.append(x.summary or "（无摘要）")
        lines.append("")
        if x.errors:
            lines.append("- 错误：" + "；".join(x.errors[:4]))
        if x.warnings:
            lines.append("- ⚠ 附注（不阻断，但需跟进）：" + "；".join(x.warnings[:4]))
        if x.blocking_codes:
            lines.append("- 阻塞码：`%s`" % "`, `".join(x.blocking_codes))
        if x.evidence:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(x.evidence, ensure_ascii=False, indent=2)[:6000])
            lines.append("```")
        lines.append("")
    if r.timeline:
        lines.append("## 实时进展时间线")
        lines.append("")
        lines.append("角色：`a 用户` / `b 相机` / `c 门卫` / `d 探针`。完整逐事件流见同目录 `.progress.jsonl`。")
        lines.append("")
        lines.append("```text")
        for event in r.timeline[:200]:
            lines.append("[%6.1fs] %-4s %-4s %-9s %s" % (
                event.get("elapsed_s", 0), event.get("stage") or "----",
                {"run_start": "开始", "stage_start": "进入", "step": "步骤",
                 "heartbeat": "心跳", "milestone": "里程碑", "stage_end": "结束",
                 "verdict": "判定", "run_end": "完成"}.get(event.get("kind"), event.get("kind")),
                event.get("actor", ""), event.get("message", "")))
        if len(r.timeline) > 200:
            lines.append("... 其余 %d 条事件见 JSONL" % (len(r.timeline) - 200))
        lines.append("```")
        lines.append("")
    lines.append("## 建议的下一步")
    lines.append("")
    for i, step in enumerate(r.next_steps, 1):
        lines.append("%d. %s" % (i, step))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("本报告只覆盖主机侧的 0336 链路可行性，不构成固件写入许可，")
    lines.append("也不代表任何 macOS 版本或架构已被项目方验收。")
    lines.append("")
    return "\n".join(lines)
