import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from starlette.requests import Request

from alphadeck_platform import privileged_request
from alphadeck_platform.api_errors import ApiError

CASES = json.loads((Path(__file__).parent / "fixtures/privileged-requests.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_privileged_management_boundary_matches_original(case):
    url = urlsplit(case["url"])
    request = Request(
        {
            "type": "http",
            "scheme": url.scheme,
            "server": (url.hostname, url.port or (443 if url.scheme == "https" else 80)),
            "path": url.path,
            "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in case["headers"].items()],
        }
    )
    args = [request, case["env"]] if case["method"] == "privileged_mutation" else [request]
    expected = case["expected"]
    if expected is None:
        getattr(privileged_request, case["method"])(*args)
    else:
        with pytest.raises(ApiError) as failure:
            getattr(privileged_request, case["method"])(*args)
        assert {"status": failure.value.status, "message": failure.value.message} == expected
