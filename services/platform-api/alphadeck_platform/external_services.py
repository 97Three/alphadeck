"""Native GitHub, Supabase and Vercel integrations with injectable HTTP/Git adapters."""

import re
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from .environment import Environment
from .git_repository import GitRepository
from .project_services import ProjectServices
from .service_tokens import ServiceTokens
from .sessions import iso

BASES = {
    "github": "https://api.github.com",
    "supabase": "https://api.supabase.com/v1",
    "vercel": "https://api.vercel.com",
}


class ExternalServiceError(Exception):
    def __init__(self, provider, status=502, message=None):
        self.provider, self.status = provider, status
        self.message = message or provider.capitalize() + " service temporarily unavailable"
        super().__init__(self.message)


def now():
    return iso(datetime.now(UTC))


def deployment_url(value):
    return value if not value or value.startswith(("https://", "http://")) else "https://" + value


def deployment_time(value, default=None):
    return iso(datetime.fromtimestamp(value / 1000, UTC)) if value else default


def validate_repository_name(value):
    if not isinstance(value, str) or not value.strip():
        return "Repository name is required"
    if len(value) > 100:
        return "Repository name must be 100 characters or less"
    if value.startswith((".", "-")) or value.endswith((".", "-")):
        return "Repository name cannot start or end with a period or hyphen"
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", value):
        return "Repository name can only contain alphanumeric characters, periods, hyphens, and underscores"
    if ".." in value:
        return "Repository name cannot contain consecutive periods"
    if re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", value, re.I):
        return "Repository name cannot be a reserved name"
    return ""


class ExternalServices:
    def __init__(self, projects, *, transport=None, git=None):
        self.projects = projects
        self.connections = ProjectServices(projects.database)
        self.tokens = ServiceTokens(projects.database)
        self.transport, self.git = transport, git or GitRepository()

    async def token(self, provider):
        value = await self.tokens.get(provider, reveal=True, touch=True)
        if not value:
            raise ExternalServiceError(
                provider, 401, provider.capitalize() + " token not configured"
            )
        return value

    async def fetch(self, provider, endpoint, *, method="GET", body=None, team=None, query=None):
        token = await self.token(provider)
        headers = {"Authorization": "Bearer " + token}
        if provider == "github":
            headers.update(
                Accept="application/vnd.github+json", **{"User-Agent": "AlphaDeck-Python"}
            )
        params = {**({"teamId": team} if team else {}), **(query or {})}
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=30, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    BASES[provider] + endpoint,
                    headers=headers,
                    params=params,
                    **({"json": body} if body is not None else {}),
                )
            if not response.is_success:
                # Upstream messages can contain tokens or environment values; expose only safe status.
                raise ExternalServiceError(provider, response.status_code)
            return None if response.status_code == 204 else response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ExternalServiceError(provider) from error

    async def github_user(self):
        return await self.fetch("github", "/user")

    async def github_available(self, name):
        user = await self.github_user()
        try:
            await self.fetch(
                "github", "/repos/" + quote(user["login"], safe="") + "/" + quote(name, safe="")
            )
        except ExternalServiceError as error:
            if error.status == 404:
                return {"exists": False, "username": user["login"]}
            raise
        return {"exists": True, "username": user["login"]}

    async def github_create(self, name, description="", private=False):
        return await self.fetch(
            "github",
            "/user/repos",
            method="POST",
            body={
                "name": name,
                "description": description,
                "private": private,
                "auto_init": False,
            },
        )

    async def github_connect(self, project, name, description="", private=False):
        value = await self.projects.get(project)
        user = await self.github_user()
        repo = await self.github_create(name, description, private)
        root = self.projects.managed_path(value, must_exist=True)
        url = repo["clone_url"]
        self.valid_clone_url(url)
        await self.git.connect(root, user, url)
        data = {
            "repo_url": repo["html_url"],
            "repo_name": name,
            "clone_url": url,
            "default_branch": repo["default_branch"],
            "owner": user["login"],
        }
        await self.connections.save(project, "github", data)
        return {key: value for key, value in data.items() if key != "repo_name"}

    @staticmethod
    def valid_clone_url(url):
        if not isinstance(url, str) or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url
        ):
            raise ExternalServiceError("github", 400, "Invalid GitHub repository URL")

    async def github_push(self, project):
        value = await self.projects.get(project)
        connection = await self.connections.get(project, "github")
        data = connection["serviceData"] if connection else {}
        if not data.get("clone_url") or not data.get("owner"):
            raise ExternalServiceError("github", 404, "GitHub repository not connected")
        self.valid_clone_url(data["clone_url"])
        root = self.projects.managed_path(value, must_exist=True)
        if await self.git.push(root, await self.github_user(), data, await self.token("github")):
            await self.connections.save(project, "github", {"last_pushed_at": now()}, patch=True)

    async def supabase_keys(self, project):
        value = await self.fetch("supabase", "/projects/" + quote(project, safe="") + "/api-keys")
        if isinstance(value, list):

            def key(name):
                return next(
                    (
                        entry.get("api_key")
                        for entry in value
                        if isinstance(entry, dict)
                        and (entry.get("name") == name or entry.get("id") == name)
                    ),
                    None,
                )

            anon, service = key("anon"), key("service_role")
        elif isinstance(value, dict):
            anon, service = (
                value.get("anon", value.get("anon_key")),
                value.get("service_role", value.get("service_role_key")),
            )
        else:
            anon, service = None, None
        if not all(isinstance(key, str) and key for key in (anon, service)):
            raise ExternalServiceError("supabase", 404, "Supabase API keys not available yet")
        return {"anon": anon, "service_role": service}

    async def supabase_create(self, project, name, organization, password, region):
        await self.projects.get(project)
        value = await self.fetch(
            "supabase",
            "/projects",
            method="POST",
            body={
                "organization_id": organization,
                "name": name,
                "region": region,
                "db_pass": password,
            },
        )
        await self.connections.save(
            project,
            "supabase",
            {
                "project_id": value["id"],
                "project_name": value["name"],
                "project_url": "https://" + value["id"] + ".supabase.co",
                "region": value["region"],
                "organization_id": value["organization_id"],
                "created_at": value.get("inserted_at") or value.get("created_at") or now(),
            },
        )
        return value

    async def supabase_connect(self, project, identity, url, name=None, region=None):
        await self.projects.get(project)
        await self.token("supabase")
        data = {
            "project_id": identity,
            "project_url": url,
            "project_name": name if name is not None else url,
            "region": region,
            "connected_at": now(),
        }
        await self.connections.save(project, "supabase", data)
        return data

    async def vercel_available(self, name, team=None):
        try:
            value = await self.fetch(
                "vercel", "/v9/projects", team=team, query={"search": name, "limit": "1"}
            )
        except ExternalServiceError as error:
            if error.status == 404:
                return {"available": True}
            raise
        return {
            "available": not any(item.get("name") == name for item in value.get("projects", []))
        }

    async def github_connection(self, project):
        connection = await self.connections.get(project, "github")
        data = connection["serviceData"] if connection else {}
        return (
            data
            if isinstance(data.get("owner"), str) and isinstance(data.get("repo_name"), str)
            else None
        )

    async def vercel_connect(self, project, name, team=None, github=None):
        await self.projects.get(project)
        if not github and (connected := await self.github_connection(project)):
            github = connected["owner"] + "/" + connected["repo_name"]
        payload = {"name": name, "framework": "vite"}
        if github:
            payload["gitRepository"] = {"type": "github", "repo": github}
        try:
            value = await self.fetch(
                "vercel", "/v10/projects", method="POST", body=payload, team=team
            )
        except ExternalServiceError as error:
            if error.status != 409:
                raise
            value = await self.fetch("vercel", "/v9/projects/" + quote(name, safe=""), team=team)
        for item in await Environment(self.projects).list(project, reveal=True):
            # Optional env projection matches the source's best-effort contract.
            with suppress(ExternalServiceError):
                await self.fetch(
                    "vercel",
                    "/v10/projects/" + quote(value["id"], safe="") + "/env",
                    method="POST",
                    team=team,
                    body={
                        "key": item["key"],
                        "value": item["value"],
                        "target": ["production", "preview", "development"],
                        "type": "encrypted" if item["isSecret"] else "plain",
                    },
                )
        latest = (value.get("latestDeployments") or [{}])[0]
        data = {
            "project_id": value["id"],
            "project_name": value["name"],
            "project_url": (value.get("link") or {}).get("url")
            or "https://vercel.com/dashboard/projects/" + value["id"],
            "github_repo": github,
            "team_id": team,
            "connected_at": now(),
            "last_deployment_id": latest.get("id"),
            "last_deployment_status": latest.get("readyState"),
            "last_deployment_url": deployment_url(latest.get("url")),
            "last_deployment_at": deployment_time(latest.get("createdAt")),
        }
        await self.connections.save(project, "vercel", data)
        return data

    async def vercel_deploy(self, project):
        await self.token("vercel")
        connection = await self.connections.get(project, "vercel")
        if not connection:
            raise ExternalServiceError("vercel", 404, "Vercel project not connected")
        data = connection["serviceData"]
        if not data.get("project_id"):
            raise ExternalServiceError("vercel", 400, "Vercel project ID missing")
        github = await self.github_connection(project)
        if not github:
            raise ExternalServiceError("vercel", 400, "GitHub repository not connected")
        repo = await self.fetch(
            "github",
            "/repos/" + quote(github["owner"], safe="") + "/" + quote(github["repo_name"], safe=""),
        )
        name = data.get("project_name") or repo["name"]
        deployment = await self.fetch(
            "vercel",
            "/v13/deployments",
            method="POST",
            team=data.get("team_id"),
            body={
                "project": data["project_id"],
                "name": name,
                "target": "production",
                "gitSource": {
                    "type": "github",
                    "repoId": repo["id"],
                    "ref": github.get("default_branch") or "main",
                },
            },
        )
        await self.connections.save(
            project,
            "vercel",
            {
                "project_name": name,
                "github_repo": github["owner"] + "/" + github["repo_name"],
                **self.deployment_patch(deployment),
            },
            patch=True,
        )
        return {
            "success": True,
            "deployment_id": deployment.get("id"),
            "deployment_url": deployment_url(deployment.get("url")),
            "status": deployment.get("readyState") or "QUEUED",
        }

    @staticmethod
    def deployment_patch(value, previous=None):
        previous = previous or {}
        return {
            "last_deployment_id": value.get("id") or previous.get("last_deployment_id"),
            "last_deployment_status": value.get("readyState"),
            "last_deployment_url": deployment_url(value.get("url")),
            "last_deployment_at": deployment_time(
                value.get("createdAt"), previous.get("last_deployment_at") or now()
            ),
        }

    async def vercel_status(self, project):
        empty = {
            "has_deployment": False,
            "status": None,
            "deployment_id": None,
            "deployment_url": None,
            "last_deployment_url": None,
            "inspector_url": None,
            "vercel_configured": True,
        }
        if not await self.tokens.get("vercel", reveal=True):
            return {**empty, "status": "not_configured", "vercel_configured": False}
        connection = await self.connections.get(project, "vercel")
        data = connection["serviceData"] if connection else {}
        if not data.get("project_id"):
            return {**empty, "vercel_configured": False}
        value = None
        if data.get("last_deployment_id"):
            try:
                value = await self.fetch(
                    "vercel",
                    "/v13/deployments/" + quote(data["last_deployment_id"], safe=""),
                    team=data.get("team_id"),
                )
            except ExternalServiceError as error:
                if error.status != 404:
                    raise
        if value is None:
            response = await self.fetch(
                "vercel",
                "/v13/projects/" + quote(data["project_id"], safe="") + "/deployments",
                team=data.get("team_id"),
                query={"limit": "1"},
            )
            value = (response.get("deployments") or [None])[0]
        if value is None:
            return empty
        await self.connections.save(
            project, "vercel", self.deployment_patch(value, data), patch=True
        )
        identity = value.get("id", data.get("last_deployment_id"))
        status = value.get("readyState", data.get("last_deployment_status"))
        url = deployment_url(value.get("url", data.get("last_deployment_url")))
        return {
            "has_deployment": bool(identity and status in ("QUEUED", "BUILDING")),
            "status": status,
            "deployment_id": identity,
            "deployment_url": url,
            "last_deployment_url": url,
            "inspector_url": value.get("inspectorUrl"),
            "vercel_configured": True,
        }
