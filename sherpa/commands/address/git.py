import os
from pathlib import Path
import re
from typing import Any, Union

import requests

from sherpa.git import execute_git_command


github_headers = {}

def build_github_headers() -> None:
    global github_headers

    git_token = os.getenv("GITHUB_TOKEN")

    if not git_token:
        raise RuntimeError(f"[sherpa] Missing GITHUB_TOKEN, address command cannot run")

    github_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {git_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def infer_pr_info(repo_root: Path) -> tuple[str, str, int]:
    branch = execute_git_command(["branch", "--show-current"], repo_root)
    remote = execute_git_command(["config", "--get", "remote.origin.url"], repo_root)

    git_author_repo = re.match(
        r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+)\.git",
        remote,
    )
    if not git_author_repo:
        raise RuntimeError(
            f"[sherpa] Unsupported remote.origin.url format for GitHub: '{remote}'. "
            "Expected git@github.com:owner/repo.git or https://github.com/owner/repo.git."
        )
    owner, repo = git_author_repo.groups()

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    params = {"head": f"{owner}:{branch}", "state": "open"}

    resp = requests.get(
        url,
        headers=github_headers,
        params=params)
    resp.raise_for_status()

    prs = resp.json()
    if not prs:
        raise RuntimeError(
            f"[sherpa] No open pull request found for branch '{branch}' in {owner}/{repo}."
        )

    pr_number = prs[0].get("number")

    return owner, repo, pr_number

def get_all(pr_url) -> list[dict[str, Union[str | int | bool | None]]]:
    all_pages_results = []
    next_page_url: str | None = pr_url

    while next_page_url:
        resp = requests.get(next_page_url, headers=github_headers)
        resp.raise_for_status()

        all_pages_results.extend(resp.json())
        next_page_url = resp.links.get("next", {}).get("url")

    return all_pages_results

def get_comments_and_reviews(pr_url):
    return get_all(f"{pr_url}/comments?per_page=100") + get_all(f"{pr_url}/reviews?per_page=100")


def post_pull_review_comment_reply(
    owner: str,
    repo: str,
    pr_number: int,
    in_reply_to: int,
    body: str,
) -> dict[str, Any]:
    """
    Post ``body`` as a reply to an existing PR review comment (same thread).

    ``POST /repos/{owner}/{repo}/pulls/{pull}/comments/{comment_id}/replies``.

    Returns the JSON body of the created review comment on success.
    """
    if not body.strip():
        raise ValueError("Reply body is empty.")

    url = (
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        f"/comments/{in_reply_to}/replies"
    )
    response = requests.post(
        url,
        headers=github_headers,
        json={"body": body},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned a non-object response for the new comment.")
    return data