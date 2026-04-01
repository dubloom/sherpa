"""Persist and load Sherpa review artifacts under `.sherpa/reviews/`."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sherpa.commands.review import Issue, ReviewResult

STORE_VERSION = 1
LATEST_FILENAME = "latest.json"
ARTIFACT_PREFIX = "review-"


def _sherpa_dir(repo_root: Path) -> Path:
    return repo_root / ".sherpa"


def reviews_dir(repo_root: Path) -> Path:
    return _sherpa_dir(repo_root) / "reviews"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _review_result_to_dict(result: ReviewResult) -> dict[str, Any]:
    return {
        "decision": result.decision,
        "summary": result.summary,
        "high_issues": [asdict(i) for i in result.high_issues],
        "medium_issues": [asdict(i) for i in result.medium_issues],
        "low_issues": [asdict(i) for i in result.low_issues],
        "nits": [asdict(i) for i in result.nits],
    }


def _issue_from_dict(data: dict[str, Any]) -> Issue:
    return Issue(
        name=str(data["name"]),
        title=str(data["title"]),
        severity=data.get("severity"),
        file=str(data["file"]),
        details=str(data["details"]),
        suggested_fix=str(data["suggested_fix"]),
    )


def review_result_from_dict(data: dict[str, Any]) -> ReviewResult:
    def issues_list(key: str) -> list[Issue]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        out: list[Issue] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(_issue_from_dict(item))
        return out

    decision = str(data.get("decision", "")).strip().upper()
    if decision not in ("APPROVE", "BLOCKED"):
        decision = "BLOCKED"
    return ReviewResult(
        decision=decision,  # type: ignore[arg-type]
        summary=str(data.get("summary", "")),
        high_issues=issues_list("high_issues"),
        medium_issues=issues_list("medium_issues"),
        low_issues=issues_list("low_issues"),
        nits=issues_list("nits"),
    )


def save_review(
    repo_root: Path,
    commit_message: Optional[str],
    modified_files: Optional[str],
    git_diff: Optional[str],
    review_result: Optional[ReviewResult],
    raw_review: Optional[str],
) -> Optional[Path]:
    """Write a new review artifact and update the latest pointer. Returns artifact path or None on skip."""
    if review_result is None and not (raw_review and raw_review.strip()):
        return None

    rdir = reviews_dir(repo_root)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_name = f"{ARTIFACT_PREFIX}{ts}.json"
    artifact_path = rdir / artifact_name

    payload: dict[str, Any] = {
        "version": STORE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(repo_root.resolve()),
        "commit_message": commit_message,
        "modified_files": modified_files,
        "git_diff": git_diff,
        "review_result": _review_result_to_dict(review_result) if review_result else None,
        "raw_review": raw_review if review_result is None else None,
    }

    _atomic_write_json(artifact_path, payload)

    latest_payload = {"artifact": artifact_name, "version": STORE_VERSION}
    _atomic_write_json(rdir / LATEST_FILENAME, latest_payload)

    return artifact_path


def load_latest_artifact_path(repo_root: Path) -> Optional[Path]:
    latest = reviews_dir(repo_root) / LATEST_FILENAME
    if not latest.is_file():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    artifact = data.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        return None
    path = reviews_dir(repo_root) / artifact
    return path if path.is_file() else None


class StoredReview:
    """Loaded review artifact with optional parsed result."""

    def __init__(
        self,
        artifact_path: Path,
        repo_root: Path,
        created_at: str,
        commit_message: Optional[str],
        modified_files: Optional[str],
        git_diff: Optional[str],
        review_result: Optional[ReviewResult],
        raw_review: Optional[str],
    ):
        self.artifact_path = artifact_path
        self.repo_root = repo_root
        self.created_at = created_at
        self.commit_message = commit_message
        self.modified_files = modified_files
        self.git_diff = git_diff
        self.review_result = review_result
        self.raw_review = raw_review

    @property
    def has_parsed_issues(self) -> bool:
        return self.review_result is not None


def load_stored_review(repo_root: Path) -> Optional[StoredReview]:
    path = load_latest_artifact_path(repo_root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    rr_data = data.get("review_result")
    review_result: Optional[ReviewResult] = None
    if isinstance(rr_data, dict):
        try:
            review_result = review_result_from_dict(rr_data)
        except Exception:
            review_result = None

    raw = data.get("raw_review")
    raw_review = str(raw) if isinstance(raw, str) else None

    created = str(data.get("created_at", ""))
    root = data.get("repo_root")
    stored_root = Path(root) if isinstance(root, str) else repo_root

    return StoredReview(
        artifact_path=path,
        repo_root=stored_root,
        created_at=created,
        commit_message=data.get("commit_message") if data.get("commit_message") is not None else None,
        modified_files=data.get("modified_files") if isinstance(data.get("modified_files"), str) else None,
        git_diff=data.get("git_diff") if isinstance(data.get("git_diff"), str) else None,
        review_result=review_result,
        raw_review=raw_review,
    )


def build_issue_index(review_result: ReviewResult) -> dict[str, Issue]:
    """Map issue name (uppercase) to Issue."""
    index: dict[str, Issue] = {}
    for group in (
        review_result.high_issues,
        review_result.medium_issues,
        review_result.low_issues,
        review_result.nits,
    ):
        for issue in group:
            key = str(issue.name).strip().upper()
            if key:
                index[key] = issue
    return index


def parse_issue_id_args(args: list[str]) -> list[str]:
    """Parse `H0`, `H0,H1`, or multiple argv tokens into deduplicated uppercase IDs (order preserved)."""
    if not args:
        return []
    parts: list[str] = []
    for arg in args:
        for piece in arg.split(","):
            s = piece.strip().upper()
            if s:
                parts.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def flatten_issues_ordered(review_result: ReviewResult) -> list[Issue]:
    """All issues in display order: high, medium, low, nits."""
    return (
        list(review_result.high_issues)
        + list(review_result.medium_issues)
        + list(review_result.low_issues)
        + list(review_result.nits)
    )


def parse_interactive_selection(line: str, max_index: int) -> Optional[list[int]]:
    """
    Parse a line like '1', '1,2', '1 2 3' into 1-based indices.
    Returns None if invalid or empty.
    """
    line = line.strip()
    if not line:
        return None
    raw_tokens: list[str] = []
    for chunk in line.replace(",", " ").split():
        raw_tokens.append(chunk.strip())
    indices: list[int] = []
    seen: set[int] = set()
    for tok in raw_tokens:
        if not tok.isdigit():
            return None
        n = int(tok)
        if n < 1 or n > max_index:
            return None
        if n not in seen:
            seen.add(n)
            indices.append(n)
    return indices if indices else None
