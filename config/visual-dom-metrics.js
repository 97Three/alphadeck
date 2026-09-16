() => {
    const rects = Array.from(document.querySelectorAll('svg, canvas, rect, path, polyline'));
    const charts = Array.from(document.querySelectorAll('svg, canvas')).map((element) => {
        const rect = element.getBoundingClientRect();
        const visible = rect.width > 4 && rect.height > 4 && window.getComputedStyle(element).display !== 'none';
        const text = element.closest('section,article,div')?.textContent || '';
        return {
            visible,
            width: rect.width,
            height: rect.height,
            top: rect.top,
            area: rect.width * rect.height,
            hasMarketContext: /K\s*线|K线|成交量|均线|MA5|MA10|MA20|收益|回撤|波动|对比|矩阵|强弱|排名/i.test(text),
        };
    });
    const largeCharts = charts.filter((chart) => chart.visible && chart.width >= 280 && chart.height >= 140 && chart.hasMarketContext);
    const tinyCharts = charts.filter((chart) => chart.visible && chart.width < 260 && chart.height < 140);
    const visibleGraphicCount = rects.filter((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return rect.width > 4 && rect.height > 4 && style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') > 0;
    }).length;
    const bodyText = document.body.innerText || '';
    const bodyRect = document.body.getBoundingClientRect();
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const firstViewportElements = Array.from(document.querySelectorAll('h1,h2,h3,p,li,td,th,span,strong,article,section,svg,canvas,table'))
        .filter((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return rect.bottom > 0 && rect.top < viewportHeight && rect.width > 4 && rect.height > 4 && style.visibility !== 'hidden' && style.display !== 'none';
    });
    const firstViewportText = firstViewportElements
        .map((element) => element.textContent || '')
        .join(' ')
        .replace(/\s+/g, '');
    const firstViewportGraphicCount = firstViewportElements.filter((element) => ['svg', 'canvas'].includes(element.tagName.toLowerCase()) ||
        element.querySelector?.('svg,canvas,rect,path,polyline')).length;
    const firstViewportTableCount = firstViewportElements.filter((element) => element.tagName.toLowerCase() === 'table' || element.querySelector?.('table')).length;
    const headingElements = Array.from(document.querySelectorAll('h1,.name,.price')).filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.bottom > 0 && rect.top < viewportHeight;
    });
    const squashedMetricCount = Array.from(document.querySelectorAll('dd,td,strong,em,.metric-value,.price-box span,.price-box em')).filter((element) => {
        const rect = element.getBoundingClientRect();
        const text = (element.textContent || '').trim();
        if (text.length < 4 || rect.width <= 0 || rect.height <= 0)
            return false;
        const style = window.getComputedStyle(element);
        const lineHeight = Number.parseFloat(style.lineHeight || style.fontSize || '16');
        return rect.height > Math.max(34, lineHeight * 2.2) && rect.width < 72;
    }).length;
    const orphanedMetricRowCount = Array.from(document.querySelectorAll('main .metric-strip, main .comparison-metrics, main .selection-metrics, main .portfolio-metrics, main .risk-strip')).filter((container) => {
        const containerRect = container.getBoundingClientRect();
        const containerStyle = window.getComputedStyle(container);
        if (containerRect.width < 240 ||
            containerRect.height < 40 ||
            containerStyle.visibility === 'hidden' ||
            containerStyle.display === 'none') {
            return false;
        }
        const rows = new Map();
        for (const child of Array.from(container.children)) {
            const rect = child.getBoundingClientRect();
            const style = window.getComputedStyle(child);
            if (rect.width <= 4 || rect.height <= 4 || style.visibility === 'hidden' || style.display === 'none') {
                continue;
            }
            const rowKey = Math.round(rect.top / 4) * 4;
            rows.set(rowKey, [...(rows.get(rowKey) ?? []), rect]);
        }
        const orderedRows = Array.from(rows.entries()).sort(([left], [right]) => left - right).map(([, rects]) => rects);
        if (orderedRows.length < 2 || orderedRows[0].length < 3) {
            return false;
        }
        const lastRow = orderedRows[orderedRows.length - 1];
        return lastRow.length === 1 && lastRow[0].width < containerRect.width * 0.72;
    }).length;
    const oversizedHeading = headingElements.some((element) => {
        const style = window.getComputedStyle(element);
        const fontSize = Number.parseFloat(style.fontSize || '0');
        const rect = element.getBoundingClientRect();
        return fontSize > 64 || rect.height > viewportHeight * 0.22;
    });
    const contentRegions = Array.from(new Set(document.querySelectorAll('main section, main article, main [class*="card" i], main [class*="panel" i]'))).filter((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return rect.width >= 120 && rect.height >= 52 && style.visibility !== 'hidden' && style.display !== 'none';
    });
    const cardLikeRegions = contentRegions.filter((element) => {
        // Keep this predicate inline. The callback passed to page.evaluate is
        // serialized into the browser; a named local helper is rewritten by
        // the server-side TS transform with an out-of-scope `__name` helper.
        if (element.matches('dialog,[role="dialog"],[role="alert"],[popover],.alert,.warning,.error,.tooltip,.popover,.toast')) {
            return false;
        }
        const style = window.getComputedStyle(element);
        const parentStyle = element.parentElement ? window.getComputedStyle(element.parentElement) : null;
        const radii = [
            style.borderTopLeftRadius,
            style.borderTopRightRadius,
            style.borderBottomRightRadius,
            style.borderBottomLeftRadius,
        ].map((value) => Number.parseFloat(value || '0'));
        const rounded = Math.max(...radii) >= 6;
        const hasBorder = [style.borderTopStyle, style.borderRightStyle, style.borderBottomStyle, style.borderLeftStyle]
            .some((value) => value !== 'none');
        const elevated = style.boxShadow !== 'none' && !/^rgba?\(0, 0, 0, 0\)/.test(style.boxShadow);
        const surfaceContrast = Boolean(parentStyle &&
            style.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
            style.backgroundColor !== 'transparent' &&
            style.backgroundColor !== parentStyle.backgroundColor);
        return rounded && (hasBorder || elevated || surfaceContrast);
    });
    const cardLikeSet = new Set(cardLikeRegions);
    const cardGridClusterCount = Array.from(new Set(cardLikeRegions.map((element) => element.parentElement).filter(Boolean)))
        .filter((parent) => parent instanceof HTMLElement)
        .filter((parent) => {
        const display = window.getComputedStyle(parent).display;
        if (display !== 'grid' && display !== 'flex')
            return false;
        return Array.from(parent.children).filter((child) => cardLikeSet.has(child)).length >= 3;
    }).length;
    const firstViewportRegions = contentRegions.filter((element) => element.getBoundingClientRect().top < viewportHeight);
    const firstViewportCardLikeRegions = cardLikeRegions.filter((element) => element.getBoundingClientRect().top < viewportHeight);
    return {
        textLength: bodyText.replace(/\s+/g, '').length,
        svgCount: document.querySelectorAll('svg').length,
        canvasCount: document.querySelectorAll('canvas').length,
        rectCount: document.querySelectorAll('rect').length,
        graphicCount: rects.length,
        visibleGraphicCount,
        bodyArea: Math.max(0, bodyRect.width * bodyRect.height),
        textBlockCount: Array.from(document.querySelectorAll('h1,h2,h3,p,li,td,th,span')).filter((element) => {
            const text = (element.textContent || '').trim();
            const rect = element.getBoundingClientRect();
            return text.length > 0 && rect.width > 8 && rect.height > 8;
        }).length,
        firstViewportTextLength: firstViewportText.length,
        firstViewportGraphicCount,
        firstViewportTableCount,
        firstViewportHasMarketLanguage: /最新价|实时|价格|price|K\s*线|成交量|均线|财务|回撤|波动|净值|持仓|收益|风险/i.test(firstViewportText),
        firstViewportHasCoreVisual: largeCharts.some((chart) => chart.top < viewportHeight) || firstViewportTableCount > 0,
        largeChartCount: largeCharts.length,
        firstViewportLargeChartCount: largeCharts.filter((chart) => chart.top < viewportHeight).length,
        tinyChartCount: tinyCharts.length,
        squashedMetricCount,
        orphanedMetricRowCount,
        contentRegionCount: contentRegions.length,
        cardLikeSurfaceCount: cardLikeRegions.length,
        firstViewportCardLikeSurfaceCount: firstViewportCardLikeRegions.length,
        cardGridClusterCount,
        cardLikeSurfaceRatio: cardLikeRegions.length / Math.max(1, contentRegions.length),
        firstViewportCardLikeSurfaceRatio: firstViewportCardLikeRegions.length / Math.max(1, firstViewportRegions.length),
        hasFinancialWorkbenchMarker: Boolean(document.querySelector('[data-visual-language="financial-workbench"]')),
        oversizedHeroLike: oversizedHeading && firstViewportGraphicCount === 0 && firstViewportTableCount === 0,
        horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
        blankLike: bodyText.trim().length < 80 && rects.length < 8,
        hasMarketLanguage: /最新价|实时|价格|price|K\s*线|成交量|均线|财务|回撤|波动|净值|持仓|收益|风险/i.test(bodyText),
        hasDataFreshnessLanguage: /更新时间|更新：|数据截至|数据时间|行情时间|报告期|样本区间|样本窗口/i.test(bodyText),
    };
}
