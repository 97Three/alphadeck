import asyncio
import os
import re
import sys
from pathlib import Path

from .capability_center import capability_center
from .config import ROOT, market_base_url
from .generation_state import now
from .operations_logs import collect_logs, probe_url
from .operations_profiles import project_profile, runtime_profile, strategy_profile, summary
from .product_health import product_dashboard
from .service_catalog import (
    degradation_config,
    dependency_edges,
    resolved_catalog,
    validate_catalog,
)
from .strategies import Strategies
from .strategy_market import StrategyMarket
from .worker_observability import worker_dashboard
from .workspace_health import workspace_dashboard


async def command_output(command, *arguments):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *arguments,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 1.8)
        return (
            output.decode("utf-8", "replace").splitlines()[0].strip()
            if not process.returncode and output
            else None
        )
    except (OSError, TimeoutError):
        return None
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


def version_at_least(value, minimum):
    def parts(text):
        values = [
            int(match[0]) if (match := re.match(r"\d+", part)) else 0
            for part in text.lstrip("v").split(".")
        ]
        return tuple((values + [0, 0, 0])[:3])

    return bool(value) and parts(value) >= parts(minimum)


def unavailable(component):
    return "failed" if component["required"] else "warning" if component["enabled"] else "unknown"


async def operations_dashboard(
    projects, *, client=None, skills_root=ROOT, scans_root=None, include_logs=False
):
    from .operations_api import infrastructure_health

    config = degradation_config()
    components = config["components"]
    database, market, observation = (
        components[key] for key in ("database", "marketApi", "observability")
    )
    entries, validation = resolved_catalog(), validate_catalog()
    disabled = {"ok": False, "status": None, "ms": 0, "error": "market API 已按降级配置停用"}
    (
        infrastructure_result,
        center,
        workspaces,
        strategy,
        npm,
        node,
        health,
        registry,
        workers,
        product,
        logs,
    ) = await asyncio.gather(
        infrastructure_health(projects.database),
        capability_center(skills_root=skills_root, client=client),
        workspace_dashboard(projects),
        Strategies(projects, StrategyMarket(client), scans_root=scans_root).dashboard(),
        command_output("npm", "--version"),
        command_output("node", "--version"),
        probe_url(market_base_url() + "/health", client=client)
        if market["enabled"]
        else asyncio.sleep(0, result=disabled),
        probe_url(market_base_url() + "/api/v1/registry", client=client)
        if market["enabled"]
        else asyncio.sleep(0, result=disabled),
        worker_dashboard(projects.database),
        product_dashboard(projects.database, enabled=database["enabled"]),
        collect_logs(entries=include_logs, client=client),
    )
    infrastructure = infrastructure_result[0]
    data = infrastructure["data"]
    catalog = center["summary"]
    configured = bool(
        os.getenv("MODELPORT_API_KEY", "").strip() or os.getenv("DEEPSEEK_API_KEY", "").strip()
    )
    agent_installed = Path(__file__).with_name("agent_executor.py").is_file()
    count = (
        len(
            [
                path
                for path in projects.root.glob("project-*")
                if path.is_dir() and not path.is_symlink()
            ]
        )
        if projects.root.is_dir()
        else 0
    )
    missing = [name for name in ("DATABASE_URL",) if not os.getenv(name, "").strip()]
    if not configured:
        missing.append("MODELPORT_API_KEY 或 DEEPSEEK_API_KEY")
    if market["enabled"] and not os.getenv("ALPHADECK_MARKET_API_URL", "").strip():
        missing.append("ALPHADECK_MARKET_API_URL")
    loki = next((source for source in logs if source["id"] == "loki"), {})
    checks, capabilities = [], []

    def add(identity, label, state, message, *, detail=None, actions=(), capability=False):
        value = {"id": identity, "label": label, "status": state, "summary": message}
        if detail is not None:
            value["detail"] = detail
        if actions:
            value["actions"] = list(actions)
        (capabilities if capability else checks).append(value)

    node_ready = version_at_least(node, "22.19.0")
    add(
        "node-runtime",
        "前端 Node.js 运行时",
        "ok" if node_ready else "failed",
        node or "Node.js 不可用",
        detail="前端与生成页面要求 Node.js >= 22.19.0。",
        actions=() if node_ready else ("升级本地 Node.js 版本。",),
    )
    add(
        "python-runtime",
        "Python 后端运行时",
        "ok" if sys.version_info >= (3, 12) else "failed",
        sys.version.split()[0],
        detail="平台 API、Agent 和 Worker 均由 Python 执行。",
    )
    add(
        "npm-cli",
        "npm 工具链",
        "ok" if npm else "failed",
        "npm " + npm if npm else "npm 不可用",
        actions=() if npm else ("确认 Node.js 安装包含 npm。",),
    )
    add(
        "agent-cli",
        "PI Agent",
        "ok" if configured and agent_installed else "failed",
        "模型 Provider 凭据已配置"
        if configured
        else "执行引擎已安装，Provider 凭据未配置"
        if agent_installed
        else "本地执行引擎缺失",
        detail="默认通过 ModelPort 使用本地 Qwen 与托管 DeepSeek，保留可选官方直连。",
        actions=()
        if configured
        else ("在 .env.local 中配置 MODELPORT_API_KEY 或 DEEPSEEK_API_KEY。",),
    )
    ws = workers["summary"]
    add(
        "generation-worker",
        "Data Agent Worker",
        "unknown"
        if workers["dispatchMode"] != "worker"
        else unavailable(database)
        if workers["status"] == "unavailable"
        else workers["status"],
        "inline 调度模式"
        if workers["dispatchMode"] != "worker"
        else f"{ws['activeWorkers']} 个存活进程 · {ws['heldSlots']}/{ws['globalCapacity']} 个全局槽位占用 · {ws['pendingJobs'] + ws['retryWaitJobs']} 个排队任务",
        detail=workers["error"]
        or "；".join(alert["summary"] for alert in workers["alerts"])
        or f"进程容量 {ws['processCapacity']}，运行中 {ws['runningJobs']}，排队用户 {ws['queuedActors']}。",
        actions=("启动 npm run worker:generation，或使用 npm run dev 自动托管本地 Worker。",)
        if workers["dispatchMode"] == "worker" and not ws["activeWorkers"]
        else (),
    )
    add(
        "degradation-mode",
        "降级配置",
        "ok",
        config["mode"],
        detail="auto 适合本地开发；strict 适合 CI/生产；offline 跳过外部组件探测。",
    )
    add(
        "service-catalog",
        "服务目录",
        "failed" if validation["errors"] else "warning" if validation["warnings"] else "ok",
        f"{validation['enabledCount']}/{validation['serviceCount']} 个服务启用 · {validation['requiredCount']} 个硬依赖 · {validation['dependencyCount']} 条依赖",
        detail="；".join([*validation["errors"], *validation["warnings"]][:4])
        or "服务边界、默认端口、依赖关系和启动命令已登记。",
    )
    add(
        "database",
        "PostgreSQL / TimescaleDB",
        "ok" if data["connected"] and data["timescale"]["enabled"] else unavailable(database),
        f"已连接 {data['provider']}，TimescaleDB {data['timescale']['version'] or '未启用'}"
        if data["connected"]
        else "数据库暂不可用",
        detail=infrastructure.get("error")
        or f"{len(data['quantSchema']['tables'])} 张 quant 表可用。",
        actions=("运行 npm run db:up 和 npm run db:migrate:python。",)
        if database["enabled"] and not data["connected"]
        else (),
    )
    docker = data["docker"]
    add(
        "docker-timescaledb",
        "Docker timescaledb 服务",
        "ok"
        if docker["running"]
        else "warning"
        if database["enabled"] and docker["available"]
        else unavailable(database)
        if database["enabled"]
        else "unknown",
        (docker.get("service") or {}).get("status", "compose 服务未运行"),
        detail=docker.get("error"),
        actions=("运行 npm run db:up。",) if database["enabled"] and not docker["running"] else (),
    )
    add(
        "loki",
        "Loki 集中日志",
        "ok" if loki.get("exists") else unavailable(observation),
        f"{loki['lineCount']} 行集中日志"
        if loki.get("exists") and loki.get("entries")
        else "集中日志服务可达"
        if loki.get("exists")
        else "Loki 暂不可用",
        detail=loki.get("error") or "查询 " + loki.get("query", ""),
        actions=("运行 npm run obs:up 启动 Loki、Grafana 和 Alloy。",)
        if observation["enabled"] and not loki.get("exists")
        else (),
    )
    add(
        "market-api",
        "量化数据后端",
        "ok"
        if health["ok"] and registry["ok"]
        else "warning"
        if health["ok"]
        else unavailable(market),
        f"{market_base_url()} · health={health['status'] or '-'} · registry={registry['status'] or '-'}",
        detail=f"响应 {max(health['ms'], registry['ms'])}ms"
        if health["ok"] and registry["ok"]
        else health["error"] or registry["error"],
        actions=("进入 services/market-data 后运行 uv run alphadeck-market-api。",)
        if market["enabled"] and not health["ok"]
        else (),
    )
    add(
        "workspace-storage",
        "工作空间目录",
        "ok" if count else "warning",
        f"{os.path.relpath(projects.root, ROOT)} · {count} 个项目",
        actions=() if count else ("从首页创建一次任务，确认生成链路写入 data/projects。",),
    )
    add(
        "repo-baseline",
        "项目基础文件",
        "failed" if missing else "ok",
        "缺少 " + "、".join(missing) if missing else "核心运行配置齐全",
        actions=("补齐缺失的核心运行配置。",) if missing else (),
    )
    add(
        "capability-center",
        "能力中心",
        "failed"
        if catalog["blockedCapabilities"]
        else "warning"
        if catalog["plannedCapabilities"]
        else "ok",
        f"{catalog['readyCapabilities']}/{catalog['capabilities']} 个能力 ready",
        detail=f"{catalog['plannedCapabilities']} 个规划中，{catalog['blockedCapabilities']} 个阻断。",
        capability=True,
    )
    add(
        "skills",
        "Skills 健康",
        "failed" if catalog["skillErrors"] else "ok",
        f"{catalog['skills']} 个 skill，{catalog['skillErrors']} 个异常",
        actions=("进入 /skills 查看异常 skill 并重新同步。",) if catalog["skillErrors"] else (),
        capability=True,
    )
    add(
        "data-providers",
        "数据源可用性",
        "warning"
        if catalog["availableProviders"] and catalog["degradedProviders"]
        else "ok"
        if catalog["availableProviders"]
        else "failed",
        f"{catalog['availableProviders']}/{catalog['dataProviders']} 个数据源可用",
        detail=f"{catalog['degradedProviders']} 个数据源降级；market API {center['marketApi']['status']}。",
        capability=True,
    )
    tables = data["quantSchema"]["tables"]
    add(
        "quant-schema",
        "量化数据表",
        "ok" if len(tables) >= 4 else unavailable(database),
        f"{len(tables)} 张 quant 表",
        detail="、".join("quant." + table for table in tables) or "尚未初始化 quant schema。",
        actions=("运行 npm run db:init。",) if database["enabled"] and len(tables) < 4 else (),
        capability=True,
    )
    readable = sum(source["exists"] for source in logs)
    add(
        "logs",
        "系统日志入口",
        "ok" if readable else "warning",
        f"{readable}/{len(logs)} 个日志源可读",
        detail="、".join(source["label"] for source in logs if not source["exists"]) or None,
        capability=True,
    )
    return {
        "generatedAt": now(),
        "summary": summary([*checks, *capabilities], logs),
        "infrastructure": data,
        "infrastructureError": infrastructure.get("error"),
        "healthProfiles": [
            project_profile(workspaces),
            runtime_profile(checks, capabilities, logs),
            strategy_profile(strategy),
        ],
        "systemChecks": checks,
        "capabilityChecks": capabilities,
        "serviceCatalog": entries,
        "serviceDependencyEdges": dependency_edges(entries),
        "serviceCatalogValidation": validation,
        "agentWorkers": workers,
        "productHealth": product,
        "logSources": logs,
    }
