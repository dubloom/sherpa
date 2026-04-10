from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Union

from sherpa.commands.address.git import build_github_headers, get_comments_and_reviews, infer_pr_info
from sherpa.commands.base import Command

@dataclass
class CodeLocation:
    path: str
    line: int
    start_line: int
    side: int
    start_side: int

@dataclass
class ReviewEntry:
    id: int
    reply_to_id: int | str | None
    pseudo: str
    comment: str
    created_at: str | None

@dataclass
class CommentEntry(ReviewEntry):
    id: int
    code_location: CodeLocation

@dataclass
class CommentThread:
    thread_id: int | str
    comments: list[CommentEntry]

def extract_code_location(
    entry: dict[str, Union[str | int | bool | None]]
) -> CodeLocation | None:
    path = entry.get("path")
    line = entry.get("line")
    start_line = entry.get("start_line")
    side = entry.get("side")
    start_side = entry.get("start_side")

    if not any(value is not None for value in [path, line, start_line, side, start_side]):
        return None

    return CodeLocation(
        path=path,
        line=line,
        start_line=start_line,
        side=side,
        start_side=start_side
    )

def build_entries(resp: list[dict[str, Union[str | int | bool | None]]]
) -> tuple[list[CommentEntry], list[ReviewEntry]]:
    comments: list[CommentEntry] = []
    reviews: list[ReviewEntry] = []

    for item in resp:
        user = item.get("user")

        # Only take comments from real users
        login = user.get("login", "")
        if user.get("type", "").lower() != "user" or login.endswith("bot"):
            continue

        entry_data = {
            "id": item.get("id", -1),
            "reply_to_id": item.get("in_reply_to_id", None),
            "pseudo": login,
            "comment": item.get("body", ""),
            "created_at": item.get("created_at", item.get("submitted_at", None))
        }

        code_location = extract_code_location(item)
        if not code_location:
            reviews.append(ReviewEntry(**entry_data))
        else:
            comments.append(CommentEntry(**entry_data, code_location=code_location))

    return comments, reviews

def build_threads(comments):
    threads = defaultdict(list)

    for comment in comments:
        thread_id = comment.reply_to_id or comment.id
        threads[thread_id].append(comment)

    return [
        CommentThread(
            thread_id=thread_id,
            comments=sorted(thread_comments, key=lambda c: c.created_at)
        )
        for thread_id, thread_comments in threads.items()
    ]

class AddressCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, model: str):
        build_github_headers()

        owner: str | None = None
        repo: str | None = None
        pr_number: int | None = None

        # The user provided a github PR link
        if len(args) > 0:
            pr_link_reg = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", args[0])
            if pr_link_reg:
                owner = pr_link_reg.group(1)
                repo = pr_link_reg.group(2)
                pr_number = int(pr_link_reg.group(3))

        else:
            # No link provided, infer PR link based on the
            # repo and the branch
            owner, repo, pr_number = infer_pr_info(repo_root)

        resp = get_comments_and_reviews(f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}")

        comments: list[CommentEntry]
        reviews: list[ReviewEntry]
        comments, _reviews = build_entries(resp)
        threads = build_threads(comments)
        threads.sort(key=lambda t: (t.comments[0].created_at or "") if t.comments else "")

        from .interface import run_viewer

        run_viewer(threads, repo_root, owner, repo, pr_number, model=model)
