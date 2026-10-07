from __future__ import annotations

import json
import os
import subprocess
import tomllib
import urllib.request
from pathlib import Path

PACKAGE = "fastmcp"
UPSTREAM_REPO = "PrefectHQ/fastmcp"


def _gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def _latest_release(token: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "avanza-mcp-upstream-watch",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return str(payload["tag_name"]).removeprefix("v")


def _locked_version() -> str:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    versions = {item["name"]: item["version"] for item in lock["package"]}
    try:
        return str(versions[PACKAGE])
    except KeyError as exc:
        raise RuntimeError(f"{PACKAGE} is missing from uv.lock") from exc


def main() -> None:
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GH_REPO"]
    current = _locked_version()
    latest = _latest_release(token)

    _gh(
        "label",
        "create",
        "upstream-watch",
        "--repo",
        repo,
        "--color",
        "5319e7",
        "--description",
        "Upstream GitHub release differs from the locked dependency",
        "--force",
    )

    prefix = f"upstream-watch: {PACKAGE} "
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            "upstream-watch",
            "--limit",
            "100",
            "--json",
            "number,title",
        )
    )
    related = [issue for issue in issues if issue["title"].startswith(prefix)]

    pull_requests = json.loads(
        _gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "title,author",
        )
    )
    has_dependabot_pr = any(
        pr.get("author", {}).get("login") == "dependabot[bot]"
        and PACKAGE in pr["title"].lower()
        for pr in pull_requests
    )

    if latest == current or has_dependabot_pr:
        reason = "lock matches upstream" if latest == current else "Dependabot PR already open"
        for issue in related:
            _gh(
                "issue",
                "close",
                str(issue["number"]),
                "--repo",
                repo,
                "--comment",
                f"Closing automatically: {reason}.",
            )
        status = reason
    else:
        title = f"{prefix}{latest}"
        if not any(issue["title"] == title for issue in related):
            body = (
                f"Latest upstream GitHub release for `{PACKAGE}` is `{latest}`, "
                f"while `uv.lock` contains `{current}`.\n\n"
                "Dependabot remains the normal package-update path. "
                "This issue exists to catch an upstream GitHub release that has not "
                "yet resulted in a Dependabot update."
            )
            _gh(
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
                "--label",
                "dependencies",
                "--label",
                "upstream-watch",
            )

        for issue in related:
            if issue["title"] != title:
                _gh(
                    "issue",
                    "close",
                    str(issue["number"]),
                    "--repo",
                    repo,
                    "--comment",
                    f"Superseded by upstream release {latest}.",
                )
        status = "issue tracked"

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write("## Upstream release watch\n\n")
            handle.write(
                f"- {PACKAGE}: locked {current}, upstream {latest} — {status}\n"
            )


if __name__ == "__main__":
    main()
