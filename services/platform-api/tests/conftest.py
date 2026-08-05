import base64
import hmac
import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.migrations import migrate


@pytest.fixture
def database_url():
    url = os.getenv("PLATFORM_TEST_DATABASE_URL") or os.getenv("PI_AGENT_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Set PLATFORM_TEST_DATABASE_URL")
    if not urlsplit(url).path.endswith("_test"):
        pytest.fail("API tests require an isolated _test database")
    migrate(url)
    return url


@pytest.fixture
def actor(database_url):
    user_id, session_id, token = str(uuid4()), str(uuid4()), str(uuid4())
    with psycopg.connect(database_url) as connection:
        profile = connection.execute(
            "SELECT id FROM permission_profiles WHERE key='member-default'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO auth_users (id,name,email,role,permission_profile_id,updated_at)
               VALUES (%s,'Python migration test',%s,'member',%s,now())""",
            (user_id, f"{user_id}@example.test", profile),
        )
        connection.execute(
            """INSERT INTO auth_sessions (id,token,user_id,expires_at,updated_at)
               VALUES (%s,%s,%s,now()+interval '1 hour',now())""",
            (session_id, token, user_id),
        )
    signature = base64.b64encode(hmac.digest(b"test-secret", token.encode(), "sha256")).decode()
    yield {
        "user_id": user_id,
        "session_id": session_id,
        "cookie": quote(token + "." + signature, safe=""),
    }
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM auth_users WHERE id=%s", (user_id,))
