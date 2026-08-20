import json
from pathlib import Path

import pytest

from alphadeck_platform.integration_config import (
    create_integration_scope,
    knowledge_config,
    memory_config,
    modelport_scope_headers,
)

CASES = json.loads((Path(__file__).parent / "fixtures/integration-config.json").read_text())


@pytest.mark.parametrize("case", CASES["configs"])
def test_integration_configuration_matches_original(case):
    function = memory_config if case["kind"] == "memory" else knowledge_config
    if "error" in case:
        with pytest.raises(ValueError) as error:
            function(case["environment"])
        assert str(error.value) == case["error"]
    else:
        assert function(case["environment"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["scopes"])
def test_project_owned_integration_scope_matches_original(case):
    input = case["input"]
    if "error" in case:
        with pytest.raises(ValueError) as error:
            create_integration_scope(
                input["projectId"], input["memory"], input["knowledge"], input["environment"]
            )
        assert str(error.value) == case["error"]
    else:
        result = create_integration_scope(
            input["projectId"], input["memory"], input["knowledge"], input["environment"]
        )
        assert result == case["expected"]
        assert modelport_scope_headers(result) == case["headers"]
