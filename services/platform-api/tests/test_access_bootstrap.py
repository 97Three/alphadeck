import asyncio

from test_projects import project  # noqa: F401

from alphadeck_platform.access_bootstrap import ensure_access_control


def test_native_access_bootstrap_is_idempotent_and_reconciles_project_ownership(project, actor):  # noqa: F811
    async def run():
        database = project["database"]
        first = await ensure_access_control(database)
        second = await ensure_access_control(database)
        assert first["permissionProfiles"] == second["permissionProfiles"] == 2
        assert second["membersAssignedPermissionProfile"] == 0
        assert second["membersAssignedQuotaProfile"] == 0
        user = await database.fetch_one(
            "SELECT permission_profile_id,quota_profile_id FROM auth_users WHERE id=%s",
            (actor["user_id"],),
        )
        assert user["permission_profile_id"] and user["quota_profile_id"]
        allocation = await database.fetch_one(
            "SELECT used FROM usage_buckets WHERE actor_user_id=%s AND metric='projects.owned'",
            (actor["user_id"],),
        )
        assert allocation["used"] == 1

    asyncio.run(run())
