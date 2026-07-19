import re
from uuid import uuid4

from fastapi import APIRouter, Request

from .admin import operation_failed
from .credentials import audit
from .quota import one
from .sessions import iso


def membership(row):
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "userId": row["user_id"],
        "role": row["role"],
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row["updated_at"]),
    }


def member_routes(services):
    router = APIRouter()

    @router.get("/api/projects/{project_id}/members")
    async def get_members(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.members.manage", project_id)
        project = await database.fetch_one(
            "SELECT owner_id FROM projects WHERE id=%s", (project_id,)
        )
        if not project:
            from fastapi.responses import JSONResponse

            return JSONResponse({"success": False, "error": "PROJECT_NOT_FOUND"}, status_code=404)
        rows = await database.fetch_all(
            "SELECT m.*,u.name,u.email,u.banned FROM project_memberships m JOIN auth_users u ON u.id=m.user_id WHERE m.project_id=%s ORDER BY m.role,m.created_at",
            (project_id,),
        )
        return {
            "success": True,
            "data": {
                "ownerId": project["owner_id"],
                "memberships": [
                    {
                        "id": row["id"],
                        "role": row["role"],
                        "createdAt": iso(row["created_at"]),
                        "user": {
                            "id": row["user_id"],
                            "name": row["name"],
                            "email": row["email"],
                            "banned": row["banned"],
                        },
                    }
                    for row in rows
                ],
            },
        }

    @router.put("/api/projects/{project_id}/members")
    async def put_member(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.members.manage", project_id)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or body.keys() != {"email", "role"}
                or not isinstance(body["email"], str)
                or len(body["email"].strip()) > 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body["email"].strip())
                or body["role"] not in {"viewer", "editor"}
            ):
                raise ValueError("Invalid membership request")
            async with database.transaction() as connection:
                user = await one(
                    connection,
                    "SELECT id,banned FROM auth_users WHERE email=%s",
                    (body["email"].strip().lower(),),
                )
                if not user or user["banned"]:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        {"success": False, "error": "USER_NOT_AVAILABLE"}, status_code=404
                    )
                row = await one(
                    connection,
                    """INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,%s,now())
                    ON CONFLICT (project_id,user_id) DO UPDATE SET role=excluded.role,updated_at=now() RETURNING *""",
                    (str(uuid4()), project_id, user["id"], body["role"]),
                )
            await audit(
                database,
                request,
                "project.member_updated",
                actor=actor.user["id"] if actor.user else None,
                target_type="project_membership",
                target_id=row["id"],
                metadata={"projectId": project_id, "role": body["role"]},
            )
            return {"success": True, "data": membership(row)}
        except ValueError as error:
            return operation_failed(error)

    @router.delete("/api/projects/{project_id}/members")
    async def remove_member(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.members.manage", project_id)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or body.keys() != {"userId"}
                or not isinstance(body["userId"], str)
                or not body["userId"]
            ):
                raise ValueError("Invalid remove member request")
            async with database.transaction() as connection:
                project = await one(
                    connection,
                    "SELECT owner_id FROM projects WHERE id=%s FOR UPDATE",
                    (project_id,),
                )
                if project and project["owner_id"] == body["userId"]:
                    raise ValueError("不能移除项目所有者。")
                result = await connection.execute(
                    "DELETE FROM project_memberships WHERE project_id=%s AND user_id=%s AND role<>'owner'",
                    (project_id, body["userId"]),
                )
            await audit(
                database,
                request,
                "project.member_removed",
                actor=actor.user["id"] if actor.user else None,
                target_type="project",
                target_id=project_id,
                metadata={"removedUserId": body["userId"], "removedCount": result.rowcount},
            )
            return {"success": True, "data": {"removedCount": result.rowcount}}
        except ValueError as error:
            return operation_failed(error)

    return router
