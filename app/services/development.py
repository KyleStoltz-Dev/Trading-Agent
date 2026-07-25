import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.policy import PolicyEngine, PolicyViolation

SOFTWARE_ACTION = re.compile(
    r"\b(add|build|change|create|develop|fix|implement|improve|modify|remove|rename|update)\b",
    re.IGNORECASE,
)
SOFTWARE_TARGET = re.compile(
    r"\b(agent|app|api|cli|code|command|database|feature|interface|program|repo(?:sitory)?|"
    r"schema|software|test|ui)\b",
    re.IGNORECASE,
)
BROKER_WRITE_ACTION = re.compile(
    r"\b(?:plac(?:e[sd]?|ing)|modif(?:y|ies|ied|ying)|cancel(?:s|led|ing)?|"
    r"execut(?:e[sd]?|ing)|clos(?:e[sd]?|ing)|hedg(?:e[sd]?|ing)|"
    r"submit(?:s|ted|ting)?)\b",
    re.IGNORECASE,
)
BROKER_WRITE_TARGET = re.compile(
    r"\b(?:broker )?(?:order|position|trade)s?\b",
    re.IGNORECASE,
)


def detect_development_intent(message: str) -> bool:
    stripped = message.strip()
    if stripped.lower() == "/develop" or stripped.lower().startswith("/develop "):
        return True
    return bool(SOFTWARE_ACTION.search(stripped) and SOFTWARE_TARGET.search(stripped))


def development_request(message: str) -> str:
    stripped = message.strip()
    if stripped.lower() == "/develop" or stripped.lower().startswith("/develop "):
        stripped = stripped[len("/develop") :].strip()
    if not stripped:
        raise ValueError("describe the software change after /develop")
    return stripped


@dataclass
class DevelopmentSession:
    id: str
    request: str
    status: str
    backend: str
    repository: str
    worktree: str
    branch: str
    base_ref: str
    created_at: str
    updated_at: str
    summary: str = ""
    validation: list[dict[str, object]] | None = None


class DevelopmentService:
    def __init__(self, settings: Settings, policy: PolicyEngine | None = None) -> None:
        self.settings = settings
        self.policy = policy or PolicyEngine.load()
        self.repository = settings.development_repository.expanduser().resolve()
        state = settings.development_state_directory
        self.state_directory = (
            state if state.is_absolute() else self.repository / state
        ).resolve()

    def start(self, request: str) -> DevelopmentSession:
        if not self.settings.development_enabled:
            raise RuntimeError("development mode is disabled")
        self.policy.assert_unchanged()
        if BROKER_WRITE_ACTION.search(request) and BROKER_WRITE_TARGET.search(request):
            raise PolicyViolation(
                "development mode cannot create broker order execution"
            )
        self._verify_repository()
        codex = shutil.which("codex")
        if codex is None:
            raise RuntimeError("Codex CLI is not installed or is not on PATH")

        session_id = uuid.uuid4().hex[:12]
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:32] or "change"
        branch = f"agent/dev-{slug}-{session_id[:6]}"
        worktree_root = self.state_directory / "worktrees"
        worktree = worktree_root / session_id
        worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git(
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            self.settings.development_base_ref,
        )

        now = datetime.now(UTC).isoformat()
        session = DevelopmentSession(
            id=session_id,
            request=request,
            status="running",
            backend=self.settings.development_backend,
            repository=str(self.repository),
            worktree=str(worktree),
            branch=branch,
            base_ref=self.settings.development_base_ref,
            created_at=now,
            updated_at=now,
            validation=[],
        )
        self._save(session)

        prompt = (
            "Implement the requested change in this repository. Follow AGENTS.md and all "
            "runtime policy boundaries. Make the smallest coherent change, add or update "
            "tests, and run relevant validation. Do not commit, push, open a pull request, "
            "read .env files, access databases, use broker credentials, or add broker order "
            "execution. Leave all changes in the worktree for human review.\n\n"
            f"Requested change:\n{request}"
        )
        try:
            result = subprocess.run(  # noqa: S603
                [
                    codex,
                    "exec",
                    "--cd",
                    str(worktree),
                    "--sandbox",
                    "workspace-write",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--color",
                    "never",
                    prompt,
                ],
                cwd=worktree,
                env=self._safe_environment(),
                capture_output=True,
                text=True,
                timeout=self.settings.development_timeout_seconds,
                check=False,
            )
            session.summary = (result.stdout or result.stderr)[-8000:]
            if result.returncode != 0:
                session.status = "failed"
            else:
                session.validation = self._validate(worktree)
                has_changes = bool(self._git("-C", str(worktree), "status", "--porcelain"))
                if has_changes:
                    self._git(
                        "-C",
                        str(worktree),
                        "add",
                        "--intent-to-add",
                        "--all",
                    )
                checks_passed = all(item["passed"] for item in session.validation)
                session.status = (
                    "needs_review" if has_changes and checks_passed else "failed"
                )
                if not has_changes:
                    session.summary += "\nCodex completed without changing files."
        except subprocess.TimeoutExpired:
            session.status = "failed"
            session.summary = "Development run exceeded its configured timeout."
        finally:
            session.updated_at = datetime.now(UTC).isoformat()
            self._save(session)
        return session

    def get(self, session_id: str) -> DevelopmentSession:
        path = self._session_path(session_id)
        if not path.is_file():
            raise LookupError(f"development session not found: {session_id}")
        session = DevelopmentSession(**json.loads(path.read_text()))
        self._validate_session_paths(session)
        return session

    def diff(self, session_id: str) -> str:
        session = self.get(session_id)
        return self._git("-C", session.worktree, "diff", "--no-ext-diff", "--")

    def approve(self, session_id: str) -> DevelopmentSession:
        session = self.get(session_id)
        if session.status != "needs_review":
            raise RuntimeError("only a reviewed, validated development session can be approved")
        session.validation = self._validate(Path(session.worktree))
        if not all(item["passed"] for item in session.validation):
            session.status = "failed"
            session.updated_at = datetime.now(UTC).isoformat()
            self._save(session)
            raise RuntimeError("validation changed or failed; approval was stopped")
        self._git("-C", session.worktree, "add", "--all")
        self._git(
            "-C",
            session.worktree,
            "commit",
            "-m",
            f"Develop: {session.request[:68]}",
        )
        session.status = "approved"
        session.updated_at = datetime.now(UTC).isoformat()
        self._save(session)
        return session

    def _verify_repository(self) -> None:
        if not (self.repository / ".git").exists():
            raise RuntimeError(f"development repository is not a Git repository: {self.repository}")
        if not (self.repository / "AGENTS.md").is_file():
            raise RuntimeError("development repository must contain AGENTS.md")

    def _validate_session_paths(self, session: DevelopmentSession) -> None:
        if Path(session.repository).resolve() != self.repository:
            raise RuntimeError("development session repository does not match configuration")
        worktree_root = (self.state_directory / "worktrees").resolve()
        worktree = Path(session.worktree).resolve()
        if worktree.parent != worktree_root:
            raise RuntimeError("development session worktree is outside the managed directory")

    def _validate(self, worktree: Path) -> list[dict[str, object]]:
        commands = (
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "pytest", "-q"],
        )
        results: list[dict[str, object]] = []
        for command in commands:
            completed = subprocess.run(  # noqa: S603
                command,
                cwd=worktree,
                env=self._safe_environment(),
                capture_output=True,
                text=True,
                timeout=min(self.settings.development_timeout_seconds, 600),
                check=False,
            )
            results.append(
                {
                    "command": " ".join(command[1:]),
                    "passed": completed.returncode == 0,
                    "output": (completed.stdout + completed.stderr)[-4000:],
                }
            )
        return results

    def _safe_environment(self) -> dict[str, str]:
        allowed = (
            "CODEX_HOME",
            "COMSPEC",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SHELL",
            "TERM",
            "TMPDIR",
            "USER",
            "SystemRoot",
            "WINDIR",
        )
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment["TRADING_AGENT_DEVELOPMENT"] = "1"
        return environment

    def _git(self, *arguments: str) -> str:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is not installed or is not on PATH")
        result = subprocess.run(  # noqa: S603
            [git, "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=self.repository,
            env=self._safe_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[:1000])
        return result.stdout

    def _session_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", session_id):
            raise ValueError("invalid development session id")
        return self.state_directory / "sessions" / f"{session_id}.json"

    def _save(self, session: DevelopmentSession) -> None:
        path = self._session_path(session.id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(asdict(session), indent=2))
        path.chmod(0o600)
