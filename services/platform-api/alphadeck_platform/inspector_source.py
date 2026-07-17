import re

from .finance_values import js_string
from .messages import js_length, js_slice


def bounded(value, maximum=160):
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return (
        value
        if value and js_length(value) <= maximum
        else js_slice(value, 0, max(0, maximum - 1)) + "…"
        if value
        else None
    )


def strings(value, count=12, maximum=120):
    return (
        [text for item in value if (text := bounded(item, maximum)) is not None][:count]
        if isinstance(value, list)
        else []
    )


def lines_of(file):
    lines = re.split(r"\r?\n", file["content"]) if "content" in file else []
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def matching(lines, pattern, maximum=12, flags=0):
    return [index + 1 for index, line in enumerate(lines) if re.search(pattern, line, flags)][
        :maximum
    ]


def class_names(lines, start=1, end=None, maximum=10):
    names = []

    def add(value):
        for token in re.split(r"\s+", value):
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", token) and token not in names:
                names.append(token)
            if len(names) >= maximum:
                break

    for line in lines[max(0, start - 1) : min(len(lines), len(lines) if end is None else end)]:
        if len(names) >= maximum:
            break
        if "className" not in line and "class=" not in line:
            continue
        for match in re.finditer(r"""className\s*=\s*["'`]([^"'`]+)["'`]""", line):
            add(match[1])
            if len(names) >= maximum:
                break
        for match in re.finditer(r"""\bclass\s*=\s*["']([^"']+)["']""", line):
            add(match[1])
            if len(names) >= maximum:
                break
        # Match the original second pass, including its already-saturated guard.
        for expression in re.finditer(r"className\s*=\s*\{([^}]*)\}", line):
            for quoted in re.finditer(
                r"""["'`]([A-Za-z_][A-Za-z0-9_ -]{0,180})["'`]""", expression[1]
            ):
                add(quoted[1])
                if len(names) >= maximum:
                    break
            if len(names) >= maximum:
                break
    return names


def outline(file):
    lines, declarations = lines_of(file), []
    declaration = re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.ASCII
    )
    arrow = re.compile(
        r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
        re.ASCII,
    )
    for index, line in enumerate(lines):
        match = declaration.search(line) or arrow.search(line)
        if match:
            name = js_slice(match[1], 0, 96)
            declarations.append(
                {
                    "kind": "component" if re.match("[A-Z]", name) else "function",
                    "name": name,
                    "line": index + 1,
                    "defaultExport": bool(re.match(r"export\s+default\b", line, re.ASCII)),
                }
            )
    essentials = {
        "readDashboardData",
        "readSourcesEvidence",
        "getBars",
        "getAssets",
        "getComparisonRows",
        "getHoldings",
        "getPortfolio",
    }
    selected = [
        entry
        for entry in declarations
        if entry["kind"] == "component" or entry["defaultExport"] or entry["name"] in essentials
    ][:24]
    entries = []
    for entry in selected:
        index = next(
            index
            for index, value in enumerate(declarations)
            if value["line"] == entry["line"] and value["name"] == entry["name"]
        )
        next_line = (
            declarations[index + 1]["line"] if index + 1 < len(declarations) else len(lines) + 1
        )
        end = max(entry["line"], next_line - 1)
        render = [
            line
            for line in matching(lines, "<" + re.escape(entry["name"]) + r"\b", 4, re.ASCII)
            if line < entry["line"] or line > end
        ]
        entries.append(
            {
                "kind": entry["kind"],
                "name": entry["name"],
                "line": entry["line"],
                "endLine": end,
                "renderLines": render,
                "classNames": class_names(lines, entry["line"], end),
            }
        )
    return {"entries": entries, "omitted": max(0, len(declarations) - len(entries))}


def page_signals(file):
    lines = lines_of(file)
    final = matching(lines, r"data_file/final/dashboard-data\.json")
    market = matching(lines, r"""["'`]/api/market(?:/|["'`])""")
    chart = matching(
        lines,
        r"<(?:svg|canvas)\b|ResponsiveContainer|(?:Line|Bar|Area|Composed|Scatter)Chart\b|KLine|Candlestick",
        flags=re.I,
    )
    data = matching(
        lines, r"getBars\b|bars\.(?:map|slice)|chartPoints\b|candles?\b|ohlc", flags=re.I
    )
    return {
        "clientComponent": any(re.search(r"""['"]use client['"]|<script\s+setup""", line) for line in lines[:8]),
        "dataBinding": {
            "standardFinalDataPath": bool(final),
            "standardFinalDataPathLines": final,
            "serverFileReadLines": matching(lines, r"fs\.readFile|readDashboardData"),
            "sameOriginMarketApi": bool(market),
            "sameOriginMarketApiLines": market,
        },
        "chart": {
            "hasChartSignal": bool(chart and data),
            "markupOrLibraryLines": chart,
            "dataDerivationLines": data,
        },
        "evidenceDisplayLines": matching(
            lines,
            r"evidence/sources\.json|SOURCES_FILE|readSourcesEvidence|data_quality",
            flags=re.I,
        ),
    }


def proxy_signals(file):
    lines = lines_of(file)
    return {
        "exists": file["exists"],
        "path": file["path"],
        "localBackendTargetLines": matching(
            lines, r"127\.0\.0\.1:8000|localhost:8000|MARKET_API_BASE|QUANT.*API.*BASE", flags=re.I
        ),
        "forwardingFetchLines": matching(lines, r"\bfetch\s*\(", flags=re.ASCII),
        "exportedMethodLines": matching(
            lines, r"export\s+(?:async\s+)?function\s+(?:GET|POST)|export\s*\{[^}]*GET"
        ),
    }


def responsive_signals(file):
    lines = lines_of(file)
    return {
        "mediaQueryLines": matching(lines, r"@media\b", flags=re.I),
        "overflowGuardLines": matching(lines, r"overflow-x\s*:\s*(?:auto|hidden|clip)", flags=re.I),
        "responsiveGridLines": matching(
            lines, r"minmax\s*\(|auto-(?:fit|fill)|grid-template-columns", flags=re.I
        ),
        "fluidSizeLines": matching(lines, r"clamp\s*\(|\b(?:vw|dvw|svw)\b", flags=re.I),
    }


def block_end(lines, start):
    depth, started = 0, False
    for index in range(start, len(lines)):
        for char in lines[index]:
            if char == "{":
                depth, started = depth + 1, True
            elif char == "}":
                depth -= 1
        if started and depth <= 0:
            return index + 1
    return len(lines)


def grid_hint(block):
    match = re.search(r"grid-template-columns\s*:\s*([^;\n}]+)", block, re.I)
    if not match:
        return None
    value = match[1]
    repeated = re.search(r"repeat\(\s*([0-9]+)", value, re.I)
    if repeated:
        return repeated[1] + "-track"
    if re.search(r"auto-(?:fit|fill)", value, re.I):
        return "auto-responsive"
    if re.search(r"\b1fr\b", value, re.ASCII) and not re.search(
        r"minmax|\s+\S+\s+", value.strip(), re.I
    ):
        return "single-track"
    count = len(re.findall(r"minmax\s*\(", value, re.I))
    return (
        f"{count}-region-split"
        if count >= 2
        else "split-with-fluid-region"
        if count == 1
        else "custom-tracks"
    )


def visual_source_map(page, styles, source_outline, guidance):
    page_lines, style_lines = lines_of(page), lines_of(styles)
    used = set(class_names(page_lines, maximum=500))
    default_line = next(
        (
            index + 1
            for index, line in enumerate(page_lines)
            if re.match(r"export\s+default\s+(?:async\s+)?function\b", line)
        ),
        0,
    )
    entries = source_outline["entries"]
    root = next((entry for entry in entries if entry["line"] == default_line), None)
    if root is None:
        root = next(
            (
                entry
                for entry in entries
                if entry["kind"] == "component" and not entry["renderLines"]
            ),
            None,
        )
    start, end = (root["line"], root["endLine"]) if root else (1, len(page_lines))
    main = matching(page_lines[max(0, start - 1) : end], r"<main\b", 1)
    main_line = main[0] + start - 1 if main else None
    main_classes = class_names(page_lines, main_line, main_line, 12) if main_line else []
    regions = []
    for index in range(max(0, start - 1), min(len(page_lines), end)):
        if len(regions) >= 18:
            break
        match = re.search(r"<(main|header|section|article|aside|footer)\b", page_lines[index], re.I)
        if match:
            regions.append(
                {
                    "tag": match[1].lower(),
                    "line": index + 1,
                    "classNames": class_names(page_lines, index + 1, index + 1, 8),
                }
            )
    structural = set(
        [
            *main_classes,
            *(name for region in regions for name in strings(region["classNames"], 8, 48)),
        ]
    )
    rules = []
    for index, line in enumerate(style_lines):
        brace = line.find("{")
        if brace < 0:
            continue
        selector_start = index
        while selector_start > 0 and style_lines[selector_start - 1].strip().endswith(","):
            selector_start -= 1
        selector = bounded(" ".join(style_lines[selector_start:index]) + " " + line[:brace], 220)
        if not selector or selector.startswith("@") or ";" in selector:
            continue
        selectors = re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", selector)
        if not selectors:
            continue
        end_line = block_end(style_lines, index)
        block = "\n".join(style_lines[index:end_line])
        radii = []
        for raw in re.findall(r"border-radius\s*:\s*([0-9.]+)px", block, re.I):
            match = re.match(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+", raw)
            if match:
                radii.append(float(match[0]))
        radius = max(radii) if radii else 0
        shadow = bool(re.search(r"box-shadow\s*:\s*(?!none\b)[^;\n}]+", block, re.I))
        border = bool(
            re.search(
                r"(?:^|[;{\n]\s*)border(?:-(?:top|right|bottom|left|inline|block))?\s*:",
                block,
                re.I,
            )
        )
        surface = bool(re.search(r"(?:^|[;{\n]\s*)background(?:-color)?\s*:", block, re.I))
        referenced = any(name in used for name in selectors)
        display = re.search(r"display\s*:\s*(grid|flex|block|inline-flex|inline-grid)", block, re.I)
        excluded = re.search(
            r"(?:badge|pill|button|tooltip|popover|toast|tag|dot|legend|track|candle|change|source)(?:\b|-)",
            selector,
            re.I,
        )
        container = re.search(
            r"(?:hero|panel|card|item|channel|summary|metric|tile|box)(?:\b|-)", selector, re.I
        )
        rules.append(
            {
                "selector": selector,
                "startLine": selector_start + 1,
                "endLine": end_line,
                "referenced": referenced,
                "display": display[1].lower() if display else None,
                "gridTracks": grid_hint(block),
                "maxRadiusPx": radius,
                "shadow": shadow,
                "border": border,
                "surface": surface,
                "cardCandidate": bool(
                    referenced
                    and not excluded
                    and container
                    and (shadow or radius >= 6 and (border or surface))
                ),
            }
        )

    def score(rule):
        selectors = re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", rule["selector"])
        return (
            (8 if any(name in structural for name in selectors) else 0)
            + (
                4
                if re.search(
                    r"command-center|content-grid|chart-zone|main-grid|holding-grid|comparison|selection|metric-(?:bar|strip)",
                    rule["selector"],
                    re.I,
                )
                else 0
            )
            + (2 if rule["gridTracks"] else 0)
            + (1 if rule["display"] == "grid" else 0)
        )

    layout = [
        rule
        for rule in rules
        if rule["referenced"] and (rule["display"] in {"grid", "flex"} or rule["gridTracks"])
    ]
    layout.sort(key=lambda rule: (-score(rule), rule["startLine"]))
    layout_rules = [
        {
            "selector": rule["selector"],
            "range": [rule["startLine"], rule["endLine"]],
            "display": rule["display"],
            "gridTracks": rule["gridTracks"],
        }
        for rule in layout[:12]
    ]
    cards = [
        {
            "selector": rule["selector"],
            "range": [rule["startLine"], rule["endLine"]],
            "reasons": (
                ["rounded-" + js_string(rule["maxRadiusPx"]) + "px"]
                if rule["maxRadiusPx"] >= 6
                else []
            )
            + (["shadow"] if rule["shadow"] else [])
            + (["boxed-border"] if rule["border"] else [])
            + (["detached-surface"] if rule["surface"] else []),
        }
        for rule in rules
        if rule["cardCandidate"]
    ][:12]
    marker = matching(page_lines, r"""data-visual-language=["']financial-workbench["']""", 1)
    marker = marker[0] if marker else None
    page_anchor = main_classes[0] if main_classes else "<main" if main_line else None
    css_anchors = list(
        dict.fromkeys(
            "." + name
            for card in cards
            for name in re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", card["selector"])
        )
    )[:12]
    components = [
        entry for entry in entries if entry["kind"] == "component" and entry["renderLines"]
    ]
    components.sort(key=lambda entry: entry["renderLines"][0])
    return {
        "root": {
            "component": root["name"] if root else None,
            "range": [root["line"], root["endLine"]] if root else None,
            "mainLine": main_line,
            "classNames": main_classes,
            "financialWorkbenchMarkerLine": marker,
        },
        "regions": regions,
        "components": [
            {
                "name": entry["name"],
                "range": [entry["line"], entry["endLine"]],
                "renderLines": entry["renderLines"],
                "classNames": entry["classNames"],
            }
            for entry in components
        ],
        "styles": {
            "usedClassCount": len(used),
            "inspectedRuleCount": len(rules),
            "layoutRules": layout_rules,
            "cardSurfaceCandidates": cards,
        },
        "preparedVisualEdit": {
            "status": "card_surfaces_detected" if cards else "continuous_canvas_ready",
            "objective": guidance["objective"],
            "pageMarker": {"present": True, "line": marker}
            if marker
            else {
                "present": False,
                "targetRange": [main_line, main_line] if main_line else None,
                "action": guidance["pageMarker"]["action"],
            },
            "cssTargetSelectors": [card["selector"] for card in cards],
            "cssRecipe": guidance["cssRecipe"],
            "minimalReadsBeforeEdit": (
                [{"path": page["path"], "anchors": [page_anchor]}] if page_anchor else []
            )
            + (
                [{"path": styles["path"], "anchors": css_anchors}]
                if styles["exists"] and css_anchors
                else []
            ),
        },
    }
