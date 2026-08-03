import asyncio
import os
from urllib.parse import quote, unquote, urlsplit

import httpx

from .config import market_base_url
from .finance_values import js_string
from .service_catalog import degradation_config
from .skill_source import strict_json
from .strategy_mappers import CATALOG, mapped, record, string


def bounded(value, fallback, minimum, maximum):
    return max(minimum, min(maximum, value if type(value) in (int, float) else fallback))


def number(value, fallback):
    return value if type(value) in (int, float) else fallback


class StrategyMarket:
    def __init__(self, client=None, *, base_url=None):
        self.client, self.base = client, (base_url or market_base_url()).rstrip("/")

    async def request(self, path, *, query=None, body=None, method="GET", admin=False, timeout=30):
        if not degradation_config()["components"]["marketApi"]["enabled"]:
            raise ValueError("market API 已按降级配置停用")
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not path.startswith("/api/v1/")
            or any(part in (".", "..") for part in unquote(path).split("/"))
        ):
            raise ValueError("Invalid relative market API path")
        headers = {"Accept": "application/json"}
        if admin and (token := os.getenv("ALPHADECK_MARKET_ADMIN_TOKEN", "").strip()):
            headers["X-AlphaDeck-Admin-Token"] = token
        owned = self.client is None
        client = self.client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            async with (
                asyncio.timeout(timeout),
                client.stream(
                    method,
                    self.base + path,
                    params={key: js_string(value) for key, value in query.items()}
                    if query
                    else None,
                    headers=headers,
                    **({"json": body} if body is not None else {}),
                    timeout=timeout,
                ) as response,
            ):
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 33554432:
                        raise ValueError("market API response exceeds size limit")
                    chunks.append(chunk)
                if not response.is_success:
                    raise ValueError("market API " + str(response.status_code))
                return strict_json(b"".join(chunks))
        except TimeoutError as cause:
            raise ValueError(f"market API timeout after {int(timeout * 1000)}ms: {path}") from cause
        finally:
            if owned:
                await client.aclose()

    async def members(self, universe=None, *, page=1, page_size=10, keyword=None, timeout=30):
        universe = universe or CATALOG["sampleUniverseId"]
        page, page_size = max(1, page), bounded(page_size, 10, 1, 100)
        query = {"page": page, "page_size": page_size}
        if keyword and keyword.strip():
            query["keyword"] = keyword.strip()
        value = await self.request(
            "/api/v1/research/universes/" + quote(universe, safe="") + "/members",
            query=query,
            timeout=timeout,
        )
        return mapped("mapResearchUniverseMembersPage", value, universe, page, page_size)

    async def backtest(self, symbol, parameters, strategy_id, limit=1260):
        query = {
            "fee_bps": parameters.get("fee_bps", 5),
            "period": "daily",
            "adjustment": "qfq",
            "limit": limit,
            **parameters,
        }
        return await self.request(
            "/api/v1/backtests/strategies/"
            + quote(strategy_id, safe="")
            + "/"
            + quote(symbol, safe=""),
            query=query,
            timeout=180,
        )

    async def action(self, action, body):
        universe = string(body.get("universeId")) or CATALOG["sampleUniverseId"]
        symbol = js_string(body.get("symbol") if body.get("symbol") is not None else "")
        default = CATALOG["researchFallback"]["ingestionPlan"]
        if action == "universe-members":
            return await self.members(
                universe,
                page=number(body.get("page"), 1),
                page_size=number(body.get("pageSize"), 10),
                keyword=string(body.get("keyword")),
            )
        if action == "a-share-screener":
            mode = (
                body.get("mode")
                if body.get("mode") in ("limit_up_relay", "trend_liquidity", "short_term")
                else "short_term"
            )
            query = {
                "universe_id": universe,
                "mode": mode,
                "limit": bounded(body.get("limit"), 20, 1, 100),
            }
            if string(body.get("tradeDate")).strip():
                query["trade_date"] = body["tradeDate"].strip()
            return mapped(
                "mapScreenerResponse",
                await self.request(
                    "/api/v1/research/screeners/a-share/short-term-candidates", query=query
                ),
            )
        if action == "ingestion-jobs":
            query = {"limit": bounded(body.get("limit"), 20, 1, 100)}
            if string(body.get("universeId")).strip():
                query["universe_id"] = body["universeId"].strip()
            return mapped(
                "mapIngestionJobsResponse",
                await self.request("/api/v1/ingestion/jobs", query=query),
            )
        if action == "control-ingestion-job":
            identity = js_string(body.get("jobId") or "").strip()
            control = body.get("control", body.get("controlAction"))
            if not identity or control not in ("pause", "resume", "stop"):
                raise ValueError("缺少补数任务 ID 或控制 action 无效")
            payload = {"action": control}
            if isinstance(body.get("reason"), str):
                payload["reason"] = body["reason"]
            return mapped(
                "mapIngestionJobControlResult",
                await self.request(
                    "/api/v1/ingestion/jobs/" + quote(identity, safe="") + "/control",
                    method="POST",
                    body=payload,
                    admin=True,
                ),
            )
        if action == "data-quality-scan":
            payload = {
                "universe_id": universe,
                "timeframe": string(body.get("timeframe")) or "daily",
                "adjustment": string(body.get("adjustment")) or "qfq",
                "lookback_years": number(body.get("lookbackYears"), default["lookbackYears"]),
                "persist": body.get("persist") is not False,
            }
            if isinstance(body.get("symbols"), list) and body["symbols"]:
                payload["symbols"] = [
                    js_string(item) for item in body["symbols"] if js_string(item)
                ]
            return mapped(
                "mapDataQualityScan",
                await self.request(
                    "/api/v1/foundation/data-quality/scan",
                    method="POST",
                    body=payload,
                    admin=True,
                    timeout=180,
                ),
            )
        if action == "sector-capital-flow":
            query = {"universe_id": universe, "limit": bounded(body.get("limit"), 40, 1, 120)}
            if isinstance(body.get("sector"), str) and body["sector"]:
                query["sector"] = body["sector"]
            if type(body.get("detailDays")) in (int, float) and body["detailDays"]:
                query["detail_days"] = bounded(body["detailDays"], 20, 5, 60)
            return mapped(
                "mapSectorCapitalFlowResponse",
                await self.request("/api/v1/research/sector-capital-flow", query=query),
            )
        if action == "run-ingestion-batch" and body.get("provider") == "eastmoney":
            offset = max(0, int(number(body.get("offset"), 0)))
            cursor = offset
            batch_size = bounded(body.get("batchSize"), 5, 1, 10)
            symbols = []
            total = 0
            while len(symbols) < batch_size:
                page = cursor // 100 + 1
                members_page = await self.members(universe, page=page, page_size=100)
                total = int(number(members_page.get("total"), 0))
                members = members_page.get("members", [])
                chunk = members[cursor % 100 : cursor % 100 + batch_size - len(symbols)]
                symbols.extend(
                    string(member.get("symbol"))
                    for member in chunk
                    if isinstance(member, dict) and string(member.get("symbol"))
                )
                cursor += len(chunk)
                if not chunk or cursor >= total:
                    break
            if not symbols:
                raise ValueError("当前批次没有标的，请检查起始位置和股票池。")
            payload = {
                "universe_id": universe,
                "symbols": symbols,
                "period": string(body.get("period")) or "daily",
                "adjustment": string(body.get("adjustment")) or "qfq",
                "limit": number(body.get("limit"), default["suggestedLimit"]),
                "lookback_years": number(body.get("lookbackYears"), default["lookbackYears"]),
                "request_delay_seconds": 0.2,
                "max_retries": 1,
                "allow_fallback": True,
            }
            for key in ("start", "end"):
                if isinstance(body.get(key), str) and body[key]:
                    payload[key] = body[key]
            result = await self.request(
                "/api/v1/ingestion/eastmoney/history",
                method="POST",
                body=payload,
                admin=True,
                timeout=300,
            )
            result["batch_offset"] = max(0, int(number(body.get("offset"), 0)))
            result["batch_size"] = len(symbols)
            result["next_offset"] = min(total, result["batch_offset"] + len(symbols))
            result["universe_total_symbols"] = total
            return result
        if action in ("run-ingestion-batch", "start-ingestion-autofill"):
            payload = {
                "universe_id": universe,
                "offset": max(0, number(body.get("offset"), 0)),
                "batch_size": bounded(body.get("batchSize"), 25, 1, 200),
                "period": string(body.get("period")) or "daily",
                "adjustment": string(body.get("adjustment")) or "qfq",
                "limit": number(body.get("limit"), default["suggestedLimit"]),
                "lookback_years": number(body.get("lookbackYears"), default["lookbackYears"]),
                "include_valuation_factors": body.get("includeValuationFactors") is True,
                "request_delay_seconds": 0.2,
            }
            for key in ("start", "end"):
                if isinstance(body.get(key), str) and body[key]:
                    payload[key] = body[key]
            autofill = action == "start-ingestion-autofill"
            if autofill:
                payload["batch_delay_seconds"] = 0.2
                if type(body.get("maxBatches")) in (int, float):
                    payload["max_batches"] = body["maxBatches"]
            return await self.request(
                "/api/v1/ingestion/baostock/history/" + ("autofill" if autofill else "batch"),
                method="POST",
                body=payload,
                admin=True,
                timeout=1800,
            )
        if action == "add-universe-member":
            payload = record(
                await self.request(
                    "/api/v1/research/universes/" + quote(universe, safe="") + "/members",
                    method="POST",
                    body={"query": js_string(body.get("query") or "")},
                    admin=True,
                )
            )
            member = mapped("mapResearchMember", payload.get("member"))
            ingestion = None
            if body.get("syncHistory") is True:
                ingestion = await self.request(
                    "/api/v1/ingestion/eastmoney/history",
                    method="POST",
                    body={
                        "universe_id": universe,
                        "symbols": [member["symbol"]],
                        "period": "daily",
                        "adjustment": "qfq",
                        "limit": default["suggestedLimit"],
                        "lookback_years": default["lookbackYears"],
                    },
                    admin=True,
                    timeout=1800,
                )
            return {
                "universe_id": string(payload.get("universe_id"), universe),
                "member": member,
                "candidates": [
                    {
                        "symbol": string(record(item).get("symbol")),
                        "name": string(record(item).get("name"), None),
                        **{
                            key: string(record(item).get(key), fallback)
                            for key, fallback in (
                                ("market", "UNKNOWN"),
                                ("asset_type", "stock"),
                                ("secid", ""),
                                ("source", "eastmoney"),
                            )
                        },
                    }
                    for item in payload.get("candidates", [])
                ]
                if isinstance(payload.get("candidates"), list)
                else [],
                "ingestion": ingestion,
            }
        if action == "resolve-symbol":
            return await self.request(
                "/api/v1/symbols/resolve",
                query={"query": string(body.get("query")) or symbol, "count": 5},
            )
        if action == "trading-calendar":
            query = {"market": "CN-A", "limit": bounded(body.get("limit"), 260, 1, 5000)}
            for key in ("start", "end"):
                if string(body.get(key)).strip():
                    query[key] = body[key]
            return await self.request("/api/v1/foundation/trading-calendar", query=query)
        if action in (
            "technical-indicators",
            "financial-reports",
            "fundamental-indicators",
            "announcements",
        ):
            if not symbol.strip():
                raise ValueError("缺少查询标的")
            route = {
                "technical-indicators": "/api/v1/indicators/technical/",
                "financial-reports": "/api/v1/fundamentals/financials/",
                "fundamental-indicators": "/api/v1/indicators/fundamental/",
                "announcements": "/api/v1/events/announcements/",
            }[action]
            limit = bounded(body.get("limit"), 120 if action == "technical-indicators" else 8, 1, 1260)
            return await self.request(route + quote(symbol.strip(), safe=""), query={"limit": limit})
        if action == "symbol-bars":
            query = {
                "timeframe": string(body.get("timeframe")) or "daily",
                "adjustment": string(body.get("adjustment")) or "qfq",
                "limit": number(body.get("limit"), 240),
                "include_metadata": "true" if body.get("includeMetadata") is True else "false",
            }
            if isinstance(body.get("provider"), str) and body["provider"]:
                query["provider"] = body["provider"]
            return mapped(
                "mapLocalKlineResponse",
                await self.request("/api/v1/research/bars/" + quote(symbol, safe=""), query=query),
            )
        if action == "symbol-dividends":
            return mapped(
                "mapDividendEventsResponse",
                await self.request(
                    "/api/v1/events/dividends/" + quote(symbol, safe=""),
                    query={"limit": number(body.get("limit"), 20)},
                ),
            )
        if action in ("realtime-quote", "intraday-bars"):
            symbol = symbol.strip()
            if not symbol:
                raise ValueError("缺少行情标的")
            if action == "realtime-quote":
                return mapped(
                    "mapRealtimeQuote",
                    await self.request("/api/v1/quotes/realtime/" + quote(symbol, safe="")),
                )
            period = string(body.get("period")) or "minute1"
            if period not in ("minute1", "minute5", "minute15", "minute30", "minute60"):
                raise ValueError("不支持的分时周期：" + period)
            query = {
                "period": period,
                "adjustment": "none",
                "limit": bounded(body.get("limit"), 241, 1, 1000),
            }
            if body.get("refresh") is True or body.get("forceRefresh") is True:
                query["refresh"] = "true"
            return mapped(
                "mapLocalKlineResponse",
                await self.request("/api/v1/quotes/history/" + quote(symbol, safe=""), query=query),
            )
        raise ValueError("Unsupported strategy market action")
