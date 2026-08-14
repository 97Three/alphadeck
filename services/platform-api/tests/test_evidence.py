import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.evidence import EvidenceError, manifest, probe, verify

CASES = json.loads((Path(__file__).parent / "fixtures/evidence.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario"]["name"])
def test_evidence_verification_matches_original_typescript(case, tmp_path):
    for relative, content in case["files"].items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / ".data-agent").mkdir(exist_ok=True)
    scenario = case["scenario"]
    reads = []

    def respond(request):
        reads.append(request)
        if scenario.get("mutateSubject"):
            with (tmp_path / "src/App.vue").open("a") as stream:
                stream.write("// changed\n")
        if scenario.get("mutateReport"):
            report = json.loads(case["files"][".data-agent/validation.json"])
            report["runId"] = "replacement-request"
            (tmp_path / ".data-agent/validation.json").write_text(
                json.dumps(report, ensure_ascii=False, separators=(",", ":"))
            )
        return httpx.Response(scenario.get("httpStatus", 200), content=b"<html>verified</html>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await verify(
                {**case["input"], "workspaceRoot": tmp_path},
                client=client,
                now=lambda: datetime.fromisoformat(case["now"].replace("Z", "+00:00")),
            )
        for key in ("verdict", "reasonCodes", "failedCheckIds", "candidateVersion"):
            assert result[key] == case["expected"][key]
        assert result["payload"]["missionSpecSha256"] == case["input"]["missionSpecSha256"]
        assert result["subjectHash"].startswith("sha256:")
        assert result["receiptHash"].startswith("sha256:")
        assert result["payload"]["artifacts"]["subjectManifestSha256"] == result["subjectHash"]
        assert len(reads) == case["reads"]

    asyncio.run(run())


def test_recursive_acceptance_surface_rejects_escapes_cycles_and_hidden_targets(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    app = root / "app"
    app.mkdir()
    requirement = {"path": "app/**", "role": "subject", "mutability": "frozen", "required": True}
    (app / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceError) as escaped:
        manifest(root, [requirement])
    assert escaped.value.code == "ARTIFACT_SYMLINK_ESCAPE"
    (app / "escape").unlink()
    (app / "cycle").symlink_to(app, target_is_directory=True)
    with pytest.raises(EvidenceError) as cycle:
        manifest(root, [requirement])
    assert cycle.value.code == "ARTIFACT_SYMLINK_CYCLE"
    (app / "cycle").unlink()
    (root / ".data-agent").mkdir()
    (root / ".data-agent/control.json").write_text("{}")
    (app / "alias.json").symlink_to(root / ".data-agent/control.json")
    with pytest.raises(EvidenceError) as alias:
        manifest(root, [requirement])
    assert alias.value.code == "ARTIFACT_FORBIDDEN_PATH"


@pytest.mark.parametrize(
    "url,port,code",
    [
        ("https://localhost:4100", 4100, "PREVIEW_URL_NOT_LOCAL"),
        ("http://example.test:4100", 4100, "PREVIEW_URL_NOT_LOCAL"),
        ("http://127.0.0.1:4100", 4200, "PREVIEW_PORT_MISMATCH"),
    ],
)
def test_preview_probe_only_accepts_managed_local_port(url, port, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Unsafe URL reached the network"))
        ) as client:
            with pytest.raises(EvidenceError) as error:
                await probe(client, url, port)
        assert error.value.code == code

    asyncio.run(run())
