from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse
import hashlib
import hmac
import subprocess
import time
import zipfile

import httpx
import jwt

from .config import settings


class GitHubError(RuntimeError):
    pass


def parse_github_url(url: str) -> tuple[str, int | None]:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError("Use a https://github.com/owner/repository or PR URL")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2 or not all(part.replace("-", "").replace("_", "").replace(".", "").isalnum() for part in parts[:2]):
        raise ValueError("Invalid GitHub repository URL")
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    if len(parts) == 2:
        return repo, None
    if len(parts) == 4 and parts[2] == "pull" and parts[3].isdigit():
        return repo, int(parts[3])
    raise ValueError("Use a repository URL or a /pull/NUMBER URL")


def verify_webhook(body: bytes, signature: str | None) -> bool:
    if not settings.github_webhook_secret or not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, f"sha256={digest}")


class GitHubApp:
    def __init__(self):
        if not settings.github_app_id or not settings.github_private_key:
            raise GitHubError("GitHub App credentials are not configured")

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
            settings.github_private_key, algorithm="RS256",
        )

    async def _request(self, method: str, path: str, *, token: str | None = None,
                       params: dict | None = None, json_body: dict | None = None) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token or self._app_jwt()}",
        }
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.request(
                method, f"{settings.github_api_url}{path}", headers=headers,
                params=params, json=json_body,
            )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub {response.status_code}: {response.text[:500]}")
        return response

    async def installation_for_repo(self, repository: str) -> int:
        return int((await self.installation_details_for_repo(repository))["id"])

    async def installation_details_for_repo(self, repository: str) -> dict:
        response = await self._request("GET", f"/repos/{repository}/installation")
        return response.json()

    async def app_details(self) -> dict:
        response = await self._request("GET", "/app")
        return response.json()

    async def installation_token(self, installation_id: int) -> str:
        response = await self._request("POST", f"/app/installations/{installation_id}/access_tokens")
        return response.json()["token"]

    async def pull_request(self, repository: str, number: int, token: str) -> dict:
        return (await self._request("GET", f"/repos/{repository}/pulls/{number}", token=token)).json()

    async def pull_request_commits(self, repository: str, number: int,
                                   token: str) -> tuple[list[dict], bool]:
        commits = []
        for page in range(1, 4):
            batch = (await self._request(
                "GET", f"/repos/{repository}/pulls/{number}/commits", token=token,
                params={"per_page": 100, "page": page},
            )).json()
            commits.extend({"sha": item["sha"],
                            "message": item.get("commit", {}).get("message", "")[:1000]}
                           for item in batch)
            if len(batch) < 100:
                return commits, False
        return commits, True

    async def git_commit(self, repository: str, sha: str, token: str) -> dict:
        return (await self._request("GET", f"/repos/{repository}/git/commits/{sha}",
                                    token=token)).json()

    async def commit_test_files(self, repository: str, number: int, expected_head: str,
                                files: dict[str, str], token: str, run_id: str) -> str:
        if not files:
            raise ValueError("No generated test files were provided")
        pr = await self.pull_request(repository, number, token)
        head = pr["head"]
        if head["sha"] != expected_head or head["repo"]["full_name"] != repository:
            raise GitHubError("PR head changed or belongs to a different repository; generated tests were not committed")
        ref = quote(head["ref"], safe="/")
        ref_path = f"/repos/{repository}/git/refs/heads/{ref}"
        current = (await self._request("GET", ref_path, token=token)).json()
        if current["object"]["sha"] != expected_head:
            raise GitHubError("PR branch moved before generated tests could be committed")
        original = await self.git_commit(repository, expected_head, token)
        entries = []
        for path, content in sorted(files.items()):
            parts = Path(path).parts
            if (not parts or Path(path).is_absolute() or ".." in parts or
                    "\\" in path or ".github" in {part.lower() for part in parts} or
                    len(content.encode("utf-8")) > 1_000_000):
                raise ValueError(f"Invalid generated test file for publication: {path}")
            entries.append({"path": path, "mode": "100644", "type": "blob", "content": content})
        tree = (await self._request("POST", f"/repos/{repository}/git/trees", token=token,
                                    json_body={"base_tree": original["tree"]["sha"],
                                               "tree": entries})).json()
        commit = (await self._request("POST", f"/repos/{repository}/git/commits", token=token,
                                      json_body={"message": "test: add Ardberg generated cases\n\nArdberg-Run: " + run_id,
                                                 "tree": tree["sha"],
                                                 "parents": [expected_head]})).json()
        updated = (await self._request("PATCH", ref_path, token=token,
                                       json_body={"sha": commit["sha"], "force": False})).json()
        if updated["object"]["sha"] != commit["sha"]:
            raise GitHubError("GitHub did not confirm the generated test commit")
        return commit["sha"]

    async def pull_requests(self, repository: str, token: str) -> list[dict]:
        return (await self._request(
            "GET", f"/repos/{repository}/pulls", token=token,
            params={"state": "open", "per_page": 100},
        )).json()

    async def changed_files(self, repository: str, number: int, token: str) -> list[dict]:
        files = []
        page = 1
        while True:
            batch = (await self._request(
                "GET", f"/repos/{repository}/pulls/{number}/files", token=token,
                params={"per_page": 100, "page": page},
            )).json()
            files.extend(batch)
            if len(batch) < 100 or page >= 20:
                break
            page += 1
        return files

    async def compare_files(self, repository: str, base_sha: str, head_sha: str,
                            token: str) -> list[dict]:
        response = await self._request(
            "GET", f"/repos/{repository}/compare/{base_sha}...{head_sha}",
            token=token, params={"per_page": 100, "page": 1},
        )
        comparison = response.json()
        files = comparison.get("files", [])
        if not isinstance(files, list) or len(files) >= 300:
            raise GitHubError("Pinned commit comparison is missing or exceeds GitHub's 300-file limit")
        return files

    async def source_archive(self, repository: str, sha: str, token: str, destination: Path) -> Path:
        response = await self._request("GET", f"/repos/{repository}/zipball/{sha}", token=token)
        if len(response.content) > settings.max_archive_bytes:
            raise GitHubError("Repository archive exceeds configured size limit")
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            total_size = 0
            for member in archive.infolist():
                parts = Path(member.filename).parts[1:]
                if not parts or member.is_dir():
                    continue
                target = (root.joinpath(*parts)).resolve()
                if (root not in target.parents or member.file_size > settings.max_archive_member_bytes
                        or total_size + member.file_size > settings.max_extracted_bytes):
                    raise GitHubError("Unsafe or oversized repository archive member")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if (copied > member.file_size or
                                total_size + copied > settings.max_extracted_bytes):
                            raise GitHubError("Repository archive exceeds extracted size limit")
                        output.write(chunk)
                    total_size += copied
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        return root

    async def create_check(self, repository: str, sha: str, token: str, name: str) -> int:
        response = await self._request("POST", f"/repos/{repository}/check-runs", token=token,
            json_body={"name": name, "head_sha": sha, "status": "in_progress"})
        return int(response.json()["id"])

    async def complete_check(self, repository: str, check_id: int, token: str,
                             conclusion: str, summary: str, details_url: str | None = None):
        body = {"status": "completed", "conclusion": conclusion,
                "output": {"title": "Ardberg PR test review", "summary": summary[:65000]}}
        if details_url:
            body["details_url"] = details_url
        await self._request("PATCH", f"/repos/{repository}/check-runs/{check_id}",
                            token=token, json_body=body)

    async def upsert_pr_comment(self, repository: str, number: int, token: str,
                                body: str, comment_id: int | None = None) -> int:
        if comment_id:
            response = await self._request("PATCH", f"/repos/{repository}/issues/comments/{comment_id}",
                                           token=token, json_body={"body": body})
        else:
            response = await self._request("POST", f"/repos/{repository}/issues/{number}/comments",
                                           token=token, json_body={"body": body})
        return int(response.json()["id"])
