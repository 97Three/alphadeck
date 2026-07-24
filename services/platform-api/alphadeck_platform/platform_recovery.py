import re

from .finance_planning import write_initial_plan
from .prefetch_market import timestamp
from .scaffold import ensure_scaffold, read, write
from .validation_repair import failed_checks

PRESENTATION_CHECKS = {
    "vue_build",
    "preview_http_200",
    "visual_presentation",
    "dashboard_data_binding",
    "chart_presence",
}
PROTECTED = (
    ".data-agent/finance-run-plan.json",
    "data_file/final/dashboard-data.json",
    "evidence/sources.json",
    "evidence/data_quality.json",
)


def recovery_eligible(report):
    failures = {check["id"] for check in failed_checks(report)}
    return (
        not report["passed"]
        and report["status"] == "failed"
        and bool(failures)
        and failures <= PRESENTATION_CHECKS
    )


def stalled_repair(attempt, maximum, execution_failed, previous_ids, current_ids):
    previous, current = (
        sorted(set(filter(None, previous_ids))),
        sorted(set(filter(None, current_ids))),
    )
    return bool(execution_failed and attempt < maximum and previous and previous == current)


async def repair_platform(project, artifacts, request, instruction, report, *, rewriter=None):
    text = "\n".join(
        check["id"] + "\n" + check["summary"] + "\n" + (check.get("details") or "")
        for check in failed_checks(report)
    )
    if not re.search(
        r"(?:run_plan|运行计划).*(?:缺失|无效|契约|不一致|误写|template)|(?:template|模板).*(?:run_plan|运行计划)",
        text,
        re.I,
    ):
        return {"runPlanRebuilt": False}
    await write_initial_plan(
        {
            "projectId": project,
            "requestId": request,
            "instruction": instruction,
            "capabilitySource": "inferred",
        },
        artifacts,
        rewriter=rewriter,
    )
    await artifacts.event(
        {
            "event_type": "platform_artifact_repaired",
            "stage": "validation_repair",
            "status": "success",
            "run_id": request,
            "artifact_path": ".data-agent/finance-run-plan.json",
            "summary": "平台已根据原始请求重建只读 run_plan，Agent 无需且不得修改 .data-agent。",
            "created_at": timestamp(),
        }
    )
    return {"runPlanRebuilt": True}


async def restore_template(project, artifacts, report, *, restore=None):
    failed = list(dict.fromkeys(check["id"] for check in failed_checks(report)))

    def result(reason, restored=False):
        return {"restored": restored, "reason": reason, "failedCheckIds": failed}

    if report["passed"] or report["status"] != "failed":
        return result("验证报告未处于失败状态，无需恢复平台看板模板。")
    if not failed:
        return result("验证报告没有阻断性失败项，未恢复平台看板模板。")
    other = [identity for identity in failed if identity not in PRESENTATION_CHECKS]
    if other:
        return result(
            f"存在非页面类失败项（{'、'.join(other)}），为避免覆盖有效页面，未恢复平台看板模板。"
        )

    async def operation(_tx):
        protected = {relative: read(artifacts.root, relative) for relative in PROTECTED}
        missing = [relative for relative, value in protected.items() if value is None]
        if missing:
            return result(
                f"缺少恢复所需的只读数据产物（{'、'.join(missing)}），未恢复平台看板模板。"
            )
        failure = None
        try:
            if restore:
                restore(artifacts.root)
            else:
                ensure_scaffold(artifacts.root, project, restore=True)
        except Exception as cause:
            failure = str(cause)
        changed = {
            relative: value
            for relative, value in protected.items()
            if read(artifacts.root, relative) != value
        }
        if changed:
            failed_rollbacks = []
            for relative, content in changed.items():
                try:
                    write(artifacts.root, relative, content)
                except Exception:
                    failed_rollbacks.append(relative)
            changed_text = "、".join(changed)
            return result(
                f"模板恢复意外改动了受保护产物（{changed_text}），且回滚失败：{'、'.join(failed_rollbacks)}。"
                if failed_rollbacks
                else f"模板恢复意外改动了受保护产物（{changed_text}），已回滚数据产物并拒绝标记为恢复成功。"
            )
        if failure:
            return result(f"平台看板模板恢复失败：{failure}")
        return result(
            f"仅检测到页面类失败项（{'、'.join(failed)}），已恢复平台看板模板；final 与 evidence 保持不变，需重新运行验证。",
            True,
        )

    return await artifacts.mutate(operation)
