# Query Rewrite Contract

## Purpose

`.data-agent/finance-query-rewrite.json` is the versioned, platform-owned semantic input shared by planning, prefetch, and Agent skills. Query Rewrite is LLM-first; the Resolver remains a separate deterministic identity service. Downstream modules must not infer targets, periods, or capabilities from the original wording.

## Required fields

| Field | Contract |
| --- | --- |
| `schemaVersion` | New contracts use `5`; stored `4` remains readable. Historical ranges without structured dates must be regenerated. |
| `originalQuery` | Exact user-facing input before normalization. |
| `normalizedQuery` | Unicode-normalized, whitespace-bounded input. |
| `rewrittenQuery` | Execution summary; never a user quote. |
| `status` | `ready`, `partial`, `needs_clarification`, `failed`, or `refused`. |
| `confidence` | Number between `0` and `1`. |
| `capabilityHint` | LLM-derived task-family hint; only explicit manual selection may override it. |
| `targetCandidates` | Literal names or explicit codes accepted from the LLM result. |
| `resolvedSymbols` | Ordered instruments confirmed by the Resolver. |
| `unresolvedTargets` | Candidates with no accepted result or unavailable resolution. |
| `ambiguousTargets` | Candidates with multiple equal-priority results. |
| `timeRange` | LLM-structured explicit range after literal-evidence validation, or `null`. |
| `analysisFocus` | Stable focus ID and user-facing label. |
| `outputIntent` | `dashboard` or `answer`; answer-only requires literal negative-dashboard evidence. |
| `broadUniverse` | Explicit market or screening universe after literal-evidence validation. |
| `safety` | Deterministic `allow` or `refuse` policy decision. |
| `issues` | Typed resolution, model-availability, and clarification issues. |
| `execution` | Strategy plus bounded LLM attempt, provider, model, latency, confidence, guarded fields, error code, and token usage. |

## Execution strategies

- `llm_primary`: a schema-valid LLM result passed literal-evidence checks; every accepted target then passed through the Resolver.
- `llm_unavailable`: the model was unconfigured, timed out, failed, or returned invalid/ungrounded output. No semantic fallback is produced and downstream execution stops.
- `safety_refusal`: a pre-model safety rule rejected a guaranteed-return request.

`execution.llm.status` is `not_applicable`, `applied`, `skipped_unconfigured`, `invalid_output`, `timed_out`, or `failed`. Only `applied` may set `execution.llm.applied=true`.

The LLM does not own identity. Target candidates must occur literally in `normalizedQuery`, and the platform rejects invented targets before calling `/api/v1/symbols/resolve`. Time range, broad-universe, and answer-only claims carry literal evidence spans checked against the same query.

## Status invariants

- `ready`: all named candidates are resolved; a validated broad-universe request may be ready without symbols.
- `partial`: at least one target is resolved and at least one is unresolved.
- `needs_clarification`: a required target is missing or ambiguous.
- `failed`: Query Rewrite or Resolver infrastructure failed; never ask the user to clarify a system failure.
- `refused`: `safety.decision=refuse`; no model, Resolver, data API, Agent, or dashboard work may follow.

## Issue codes

| Code | Meaning | Retry |
| --- | --- | --- |
| `QUERY_REWRITE_LLM_UNAVAILABLE` | Semantic model is unconfigured, unavailable, timed out, or invalid. | Follow `retryable`; never substitute keyword parsing. |
| `TARGET_NOT_FOUND` | Resolver returned no acceptable security. | No; clarify the target. |
| `TARGET_AMBIGUOUS` | Multiple equal-priority securities remain. | No; ask the user to choose. |
| `SYMBOL_RESOLVER_UNAVAILABLE` | Resolver infrastructure failed. | Yes when marked retryable. |
| `GUARANTEED_RETURN_REQUEST` | Request asks for a guaranteed return or price move. | No; return the safety explanation. |

## Handoff rules

1. Query Rewrite owns natural-language semantics.
2. The symbol Resolver owns instrument identity.
3. Run Planner consumes the rewrite without reinterpreting the question and owns execution defaults/order.
4. Prefetch reads only `run_plan.symbols`; it never resolves names from `run_plan.question`.
5. Data skills own read-only API retrieval with resolved codes.
6. Data Quality owns source, freshness, missing-field, and limitation evidence.
7. Dashboard Visualization consumes only the run plan and validated final/evidence artifacts.

## Historical research time

`timeRange` may carry `startDate` and `endDate` as inclusive ISO calendar dates plus `asOf` as an ISO timestamp with timezone. `date_range` requires `endDate`; an omitted `asOf` defaults to Shanghai end-of-day. Future cutoffs, invalid dates, reversed ranges and ranges over 1,000 calendar days are rejected. Intraday cutoffs exclude the same day's daily bar conservatively.

The run plan freezes this as `researchTime` (schema 1). Prefetch and `quant_api_get` share the cutoff: history/technical/backtests use `start/end`, `period=daily`, `adjustment=none`; financials use the version archive with `as_of`. Unsupported endpoints stop with `QUANT_RESEARCH_TIME_UNSUPPORTED`. A successful HTTP response still fails if its dates, vintage times or snapshot identity violate the contract. Missing financial history is a visible gap, never a latest-data fallback. Calendar-filtered prices do not certify historical price revisions or industry membership.
