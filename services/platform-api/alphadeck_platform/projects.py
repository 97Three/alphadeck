import json
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import ROOT
from .llm_config import project_llm_config
from .provision import Provisioner
from .sessions import iso
from .settings import normalize_model
from .workspace import project_path

logger = logging.getLogger(__name__)


class ProjectNotFound(Exception):
    pass


class ProjectProvisioningUncertain(Exception):
    pass


def settings_record(value):
    try:
        result = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        result = None
    return result if isinstance(result, dict) else {}


def serialize_project(row):
    settings = settings_record(row["settings"])
    agent = settings.get("dataAgent")
    capability = agent.get("capabilityId") if isinstance(agent, dict) else None
    fields = {
        "id": "id",
        "name": "name",
        "description": "description",
        "status": "status",
        "previewUrl": "preview_url",
        "previewPort": "preview_port",
        "initialPrompt": "initial_prompt",
        "agentProfileId": "agent_profile_id",
        "agentProfileVersion": "agent_profile_version",
        "dataAgentCompositionSha256": "data_agent_composition_sha256",
    }
    result = {public: row[stored] for public, stored in fields.items()}
    result.update(
        {
            "preferredCli": "pi",
            "selectedModel": normalize_model(row["selected_model"]),
            "capabilityId": capability if isinstance(capability, str) else None,
        }
    )
    for public, stored in (
        ("createdAt", "created_at"),
        ("updatedAt", "updated_at"),
        ("lastActiveAt", "last_active_at"),
    ):
        result[public] = iso(row[stored]) if row[stored] else None
    return result


class Projects:
    def __init__(self, database, root: Path, provisioner=None):
        if root.is_symlink():
            raise ValueError("Managed projects root must be a real directory")
        self.database, self.root = database, root.resolve()
        self.provisioner = provisioner

    async def create(self, fields, owner_id=None):
        if self.provisioner is None:
            self.provisioner = Provisioner()
        application = self.provisioner.catalog.resolve(
            fields.get("agentProfileId"), fields.get("capabilityId")
        )
        self.root.mkdir(parents=True, exist_ok=True)
        target = project_path(self.root, fields["projectId"])
        if target.exists() or target.is_symlink():
            raise FileExistsError("Managed workspace already exists")
        model = normalize_model(fields.get("selectedModel"))
        fields = {**fields, "selectedModel": model}
        settings = {
            "llm": project_llm_config(model),
            "dataAgent": {
                "profileId": application["profile"]["id"],
                "profileVersion": application["profile"]["version"],
                "capabilityId": application["capability"]["id"],
                "compositionSha256": application["composition"]["sha256"],
            },
        }
        attempt = str(uuid4())
        settings["_provisioningAttemptId"] = attempt
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    """INSERT INTO projects
                (id,owner_id,name,description,initial_prompt,repo_path,preferred_cli,selected_model,
                 agent_profile_id,agent_profile_version,data_agent_composition_sha256,settings,status,template_type,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,'pi',%s,%s,%s,%s,%s,'initializing','vue',now()) RETURNING *""",
                    (
                        fields["projectId"],
                        owner_id,
                        fields["name"],
                        fields.get("description"),
                        fields.get("initialPrompt", ""),
                        str(target),
                        model,
                        application["profile"]["id"],
                        application["profile"]["version"],
                        application["composition"]["sha256"],
                        json.dumps(settings, ensure_ascii=False),
                    ),
                )
            ).fetchone()
            if owner_id:
                await connection.execute(
                    "INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,'owner',now())",
                    (str(uuid4()), row["id"], owner_id),
                )
        stage = None
        published = None
        try:
            stage = Path(tempfile.mkdtemp(prefix=f".{row['id']}.provision-", dir=self.root))
            prepared = await self.provisioner.provision(stage, target, fields, application, attempt)
            prepared["_provisioningAttemptId"] = attempt
            async with self.database.transaction() as connection:
                owned = await (
                    await connection.execute(
                        "SELECT id FROM projects WHERE id=%s AND settings::jsonb->>'_provisioningAttemptId'=%s FOR UPDATE",
                        (fields["projectId"], attempt),
                    )
                ).fetchone()
                if not owned:
                    raise ProjectNotFound()
                if target.exists() or target.is_symlink():
                    raise FileExistsError("Managed workspace already exists")
                stage.rename(target)
                stage = None
                identity = target.stat()
                published = (identity.st_dev, identity.st_ino)
                row = await (
                    await connection.execute(
                        "UPDATE projects SET status='idle',settings=%s,updated_at=now() WHERE id=%s RETURNING *",
                        (json.dumps(prepared, ensure_ascii=False), fields["projectId"]),
                    )
                ).fetchone()
            return row
        except BaseException as error:
            try:
                present = await self.database.fetch_one(
                    "SELECT * FROM projects WHERE id=%s", (fields["projectId"],)
                )
                if (
                    present
                    and present["status"] == "idle"
                    and settings_record(present["settings"]).get("_provisioningAttemptId")
                    == attempt
                ):
                    return present
                async with self.database.transaction() as connection:
                    await connection.execute(
                        "DELETE FROM projects WHERE id=%s AND settings::jsonb->>'_provisioningAttemptId'=%s",
                        (fields["projectId"], attempt),
                    )
            except Exception as reconciliation_error:
                raise ProjectProvisioningUncertain(
                    "Cannot confirm project initialization state; recovery metadata has been retained"
                ) from reconciliation_error
            # Only this attempt's staging directory and published workspace are eligible for cleanup.
            try:
                if (
                    stage is not None
                    and stage.parent == self.root
                    and stage.is_dir()
                    and not stage.is_symlink()
                ):
                    shutil.rmtree(stage)
                if published:
                    checked = project_path(self.root, fields["projectId"])
                    identity = checked.stat()
                    if checked == target and (identity.st_dev, identity.st_ino) == published:
                        shutil.rmtree(checked)
            except (OSError, ValueError):
                logger.exception("Initialization rollback left an orphan workspace")
            try:
                await self.provisioner.rollback(target, attempt)
            except Exception:
                logger.exception("Initialization rollback left an unreferenced Skill deployment")
            raise error

    async def list(self, user=None):
        if user is None or user["role"] == "admin":
            rows = await self.database.fetch_all(
                "SELECT * FROM projects ORDER BY last_active_at DESC"
            )
        else:
            rows = await self.database.fetch_all(
                """SELECT p.* FROM projects p WHERE p.owner_id=%s OR EXISTS
                   (SELECT 1 FROM project_memberships m WHERE m.project_id=p.id AND m.user_id=%s)
                   ORDER BY p.last_active_at DESC""",
                (user["id"], user["id"]),
            )
        return [serialize_project(row) for row in rows]

    async def get(self, project_id):
        row = await self.database.fetch_one("SELECT * FROM projects WHERE id=%s", (project_id,))
        if not row:
            raise ProjectNotFound()
        return row

    def managed_path(self, row, *, must_exist=False):
        target = project_path(self.root, row["id"])
        stored = row["repo_path"]
        if stored:
            stored_path = Path(stored)
            if not stored_path.is_absolute():
                stored_path = ROOT / stored_path
            if stored_path.resolve() != target:
                raise ValueError("Project repoPath is outside its canonical managed workspace")
        if must_exist and not target.is_dir():
            raise ValueError("Base path does not exist")
        return target

    async def update(self, project_id, fields):
        columns = {
            "name": "name",
            "description": "description",
            "status": "status",
            "previewUrl": "preview_url",
            "previewPort": "preview_port",
            "settings": "settings",
        }
        async with self.database.transaction() as connection:
            current = await (
                await connection.execute(
                    "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project_id,)
                )
            ).fetchone()
            if not current:
                raise ProjectNotFound()
            selected = normalize_model(fields.get("selectedModel", current["selected_model"]))
            changes = {columns[key]: value for key, value in fields.items() if key in columns}
            changes.update(
                preferred_cli="pi", selected_model=selected, updated_at=datetime.now(UTC)
            )
            if "settings" in fields or "selectedModel" in fields:
                settings = settings_record(fields.get("settings", current["settings"]))
                settings["llm"] = project_llm_config(selected)
                changes["settings"] = json.dumps(
                    settings, ensure_ascii=False, separators=(",", ":")
                )
            query = sql.SQL("UPDATE projects SET {} WHERE id=%s RETURNING *").format(
                sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in changes)
            )
            return await (
                await connection.execute(query, (*changes.values(), project_id))
            ).fetchone()

    async def delete(self, project_id, deleted_by=None):
        try:
            preview = await self.get(project_id)
        except ProjectNotFound:
            return False
        path = self.managed_path(preview)
        existed = path.exists()
        if existed:
            self.managed_path(preview, must_exist=True)
        async with self.database.transaction() as connection:
            owner_id = preview["owner_id"]
            if owner_id:
                identity = f"quota-bucket:{owner_id}:projects.owned:1970-01-01T00:00:00.000Z:9999-12-31T23:59:59.999Z"
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (identity,)
                )
            current = await (
                await connection.execute(
                    "SELECT p.*, u.role AS owner_role FROM projects p LEFT JOIN auth_users u ON u.id=p.owner_id WHERE p.id=%s FOR UPDATE OF p",
                    (project_id,),
                )
            ).fetchone()
            if not current:
                return False
            if current["owner_id"] != owner_id or current["repo_path"] != preview["repo_path"]:
                raise ValueError("Project identity changed during deletion")
            await connection.execute("DELETE FROM projects WHERE id=%s", (project_id,))
            if owner_id:
                bucket = await (
                    await connection.execute(
                        """SELECT * FROM usage_buckets WHERE actor_user_id=%s AND metric='projects.owned'
                       AND window_start='1970-01-01T00:00:00.000' AND window_end='9999-12-31T23:59:59.999'
                       FOR UPDATE""",
                        (owner_id,),
                    )
                ).fetchone()
                if bucket and bucket["used"] > 0:
                    await connection.execute(
                        "UPDATE usage_buckets SET used=used-1,version=version+1,updated_at=now() WHERE id=%s",
                        (bucket["id"],),
                    )
                    created_ms = int(current["created_at"].replace(tzinfo=UTC).timestamp() * 1000)
                    await connection.execute(
                        """INSERT INTO usage_events (id,idempotency_key,actor_user_id,bucket_id,metric,quantity,
                           source_type,source_id,enforcement_exempt,occurred_at,metadata)
                           VALUES (%s,%s,%s,%s,'projects.owned',-1,'project_deleted',%s,%s,now(),%s)""",
                        (
                            str(uuid4()),
                            f"project-delete:{project_id}:{created_ms}:owned-adjustment",
                            owner_id,
                            bucket["id"],
                            project_id,
                            current["owner_role"] == "admin",
                            Jsonb({"transactional": True, "deletedByUserId": deleted_by}),
                        ),
                    )
        if existed:
            try:
                # Revalidate after the transaction, before recursive cleanup.
                checked = self.managed_path(preview, must_exist=True)
                if checked != path or checked.parent != self.root:
                    raise ValueError("Workspace changed during deletion")
                shutil.rmtree(checked)
            except (OSError, ValueError):
                logger.warning(
                    "Database deletion committed, workspace cleanup needs retry: %s", project_id
                )
        return True
