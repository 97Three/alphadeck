import math
from datetime import UTC, datetime

from .evaluation_queue import date
from .finance_values import js_string


def clamp(value):
    return max(0, min(100, math.floor(value + 0.5)))


def score_status(value):
    return "ok" if value >= 85 else "warning" if value >= 70 else "failed"


def percent(numerator, denominator):
    return clamp(numerator / denominator * 100) if denominator > 0 else 0


def profile(identity, label, summary, factors):
    score = clamp(
        sum(factor["score"] * factor["weight"] for factor in factors)
        / (sum(factor["weight"] for factor in factors) or 1)
    )
    return {
        "id": identity,
        "label": label,
        "score": score,
        "status": score_status(score),
        "summary": summary,
        "factors": factors,
    }


def factor(identity, label, score, weight, summary):
    return {
        "id": identity,
        "label": label,
        "score": score,
        "weight": weight,
        "status": score_status(score),
        "summary": summary,
    }


def project_profile(workspace):
    summary = workspace["summary"]
    delivery = workspace.get("delivery") or {
        "showcase": summary["healthy"],
        "atRisk": summary["warning"] + summary["unknown"],
        "needsRepair": summary["failed"],
        "archiveCandidates": 0,
        "activeTotal": summary["total"],
        "activeAverageScore": summary["averageScore"],
    }
    values = [
        factor(
            "active-delivery-score",
            "活跃交付分",
            delivery["activeAverageScore"],
            45,
            f"{delivery['activeTotal']} 个非归档候选工作空间平均 {js_string(delivery['activeAverageScore'])} 分。",
        ),
        factor(
            "showcase-ratio",
            "可演示占比",
            percent(delivery["showcase"], delivery["activeTotal"]),
            30,
            f"{delivery['showcase']}/{delivery['activeTotal']} 个活跃工作空间可直接演示。",
        ),
        factor(
            "repair-pressure",
            "修复压力",
            clamp(100 - delivery["needsRepair"] * 18 - delivery["atRisk"] * 7),
            20,
            f"{delivery['needsRepair']} 个待修复、{delivery['atRisk']} 个风险项。",
        ),
        factor(
            "archive-hygiene",
            "历史归档",
            clamp(100 - delivery["archiveCandidates"] * 4),
            5,
            f"{delivery['archiveCandidates']} 个历史失败工作空间建议归档，不纳入主平台可用性。",
        ),
    ]
    return profile(
        "project", "项目健康", "衡量生成工作空间的产物完整性、验证结果和交付可靠性。", values
    )


def runtime_profile(system, capabilities, logs):
    checks = {item["id"]: item for item in [*system, *capabilities]}

    def group(ids, warning_penalty, failure_penalty):
        states = [checks[identity]["status"] for identity in ids if identity in checks]
        return clamp(
            100
            - sum(state in ("warning", "unknown") for state in states) * warning_penalty
            - states.count("failed") * failure_penalty
        )

    core = group(("database", "docker-timescaledb", "market-api"), 12, 35)
    tools = group(("node-runtime", "npm-cli", "agent-cli"), 10, 25)
    storage = group(("workspace-storage", "repo-baseline", "quant-schema"), 10, 30)
    visible = sum(item["exists"] for item in logs)
    return profile(
        "runtime",
        "运行健康",
        "衡量基础服务、工具链、存储结构和日志可观测性。",
        [
            factor(
                "core-services",
                "核心服务",
                core,
                40,
                "数据库、TimescaleDB、Docker 和量化数据后端可用性。",
            ),
            factor(
                "toolchain", "工具链", tools, 20, "Node.js、npm、Agent CLI 是否满足生成链路要求。"
            ),
            factor(
                "storage-schema",
                "存储与表结构",
                storage,
                25,
                "工作空间目录、基础文件和 quant schema 是否完整。",
            ),
            factor(
                "observability",
                "可观测性",
                percent(visible, len(logs)),
                15,
                f"{visible}/{len(logs)} 个日志源可读。",
            ),
        ],
    )


def strategy_profile(strategy, *, now=None):
    summary, research = strategy["summary"], strategy["research"]
    universes = research["universes"]
    primary = next(
        (universe for universe in universes if universe["id"] == research["primaryUniverseId"]),
        universes[0] if universes else {},
    )
    latest = primary.get("latestTs")
    timestamp = date(latest)
    age = (
        max(0, int(((now or datetime.now(UTC)) - timestamp).total_seconds() // 86400))
        if timestamp
        else None
    )
    freshness = (
        0 if age is None else 100 if age <= 3 else 82 if age <= 10 else 65 if age <= 30 else 40
    )
    runs = strategy["scanRuns"][:20]
    total, succeeded = (
        sum(max(1, run["total"]) for run in runs),
        sum(run["succeeded"] for run in runs),
    )
    active = sum(job["status"] in ("queued", "running") for job in strategy["scanJobs"])
    failed = sum(job["status"] == "failed" for job in strategy["scanJobs"])
    scan = (
        clamp(percent(succeeded, total) - failed * 8 - active * 2)
        if runs
        else 72
        if summary["parameterScans"] > 0
        else 55
    )
    return profile(
        "strategy",
        "策略健康",
        "衡量策略目录、股票池行情覆盖、数据新鲜度和回测扫描准备度。",
        [
            factor(
                "template-readiness",
                "策略目录",
                percent(summary["readyTemplates"], summary["templates"]),
                25,
                f"{summary['readyTemplates']}/{summary['templates']} 个策略模板 ready。",
            ),
            factor(
                "universe-coverage",
                "股票池覆盖",
                percent(summary["syncedSymbols"], summary["trackedSymbols"]),
                30,
                f"{summary['syncedSymbols']}/{summary['trackedSymbols']} 个标的已有行情覆盖，{summary['syncedBars']:,} 根 K 线。",
            ),
            factor(
                "freshness",
                "数据新鲜度",
                freshness,
                20,
                f"主股票池最新数据 {latest}，距今 {age if age is not None else '-'} 天。"
                if latest
                else "主股票池暂无最新数据时间。",
            ),
            factor(
                "scan-backtest",
                "回测准备度",
                scan,
                15,
                f"最近 {len(runs)} 次扫描成功 {succeeded}/{total} 组参数。"
                if runs
                else f"{summary['parameterScans']} 个参数扫描已配置，等待真实扫描沉淀。",
            ),
            factor(
                "research-source",
                "研究数据源",
                100 if research["source"] == "market-api" and not research.get("error") else 65,
                10,
                "market-api 降级：" + research["error"]
                if research.get("error")
                else "研究数据来自 " + research["source"] + "。",
            ),
        ],
    )


def summary(checks, logs):
    counts = {
        state: sum(item["status"] == state for item in checks)
        for state in ("ok", "warning", "failed", "unknown")
    }
    return {
        "status": "failed" if counts["failed"] else "warning" if counts["warning"] else "ok",
        "score": max(
            0, 100 - counts["failed"] * 18 - counts["warning"] * 7 - counts["unknown"] * 3
        ),
        **counts,
        "logSources": sum(item["exists"] for item in logs),
    }
