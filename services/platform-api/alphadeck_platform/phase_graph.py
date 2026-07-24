"""Platform-owned lane selection and independent resource budgets."""

import re
import unicodedata

ACTION = "(?:修改|调整|重构|优化|美化|定制|自定义|替换|改为|改成|不要|去掉|取消|新增|增加|添加|删除|移动|移到|重排|折叠|展开|隐藏|避免)"
TARGET = "(?:页面|看板|布局|样式|视觉|界面|交互|动效|响应式|配色|颜色|字体|字号|指标卡|卡片|首屏|导航|侧栏|模块|图表|区域|主题|数据源渠道)"
CUSTOMIZATION = re.compile(
    rf"(?:{ACTION}.{{0,20}}{TARGET}|{TARGET}.{{0,20}}{ACTION}|customi[sz]e|restyle|redesign|refactor|layout|theme|color|font|sidebar|navigation|responsive|remove|replace|without\s+cards?)",
    re.I,
)


def classify_prepared_intent(instruction):
    return (
        "custom" if CUSTOMIZATION.search(unicodedata.normalize("NFKC", instruction)) else "standard"
    )


def phase_graph(input):
    if input["profile"] == "repair":
        lane, phase, mode, effort, limits = (
            "model_repair",
            "failure-scoped-repair",
            "model",
            "medium",
            (3, 8, 6000, 20000, 20000, 60000, 1),
        )
    elif (
        input["platformPrepared"]
        and not input["hasAttachments"]
        and input["preparedIntent"] == "standard"
        and input["dashboardSpecReady"]
    ):
        lane, phase, mode, effort, limits = (
            "deterministic_standard",
            "deterministic-prepare",
            "deterministic",
            "low",
            (2, 2, 1, 1, 1, 1, 1),
        )
    elif (
        input["platformPrepared"]
        and not input["hasAttachments"]
        and input["preparedIntent"] == "custom"
    ):
        lane, phase, mode, effort, limits = (
            "model_custom",
            "inspect-edit-submit",
            "model",
            "medium",
            (8, 12, 8000, 24000, 24000, 144000, 1),
        )
    else:
        lane, phase, mode, effort, limits = (
            "model_data_preparation",
            "data-prepare-edit-submit",
            "model",
            "high",
            (8, 20, 16000, 60000, 60000, 480000, 2),
        )
    return {
        "schemaVersion": 1,
        "lane": lane,
        "phase": phase,
        "providerMode": mode,
        "reasoningEffort": effort,
        "budgets": dict(
            zip(
                (
                    "maxTurns",
                    "maxToolCalls",
                    "maxOutputTokens",
                    "maxCacheMissInputTokens",
                    "maxPreparedInputTokens",
                    "maxCumulativePreparedInputTokens",
                    "progressStallTurns",
                ),
                limits,
                strict=True,
            )
        ),
        "invariants": {
            key: True
            for key in (
                "stableToolSchema",
                "singleWriter",
                "terminalSubmissionRequired",
                "platformVerificationRequired",
            )
        },
    }
