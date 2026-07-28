import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
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
FORBIDDEN_DIFF_PATTERNS = (
    re.compile(
        r"\b(?:place_order|create_order|submit_order|cancel_order|modify_order|"
        r"close_position|order_send|placeOrder)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"/v3/accounts/[^\s\"']+/(?:orders|trades/[^\s\"']+/close|"
        r"positions/[^\s\"']+/close)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:OrderCreate|MarketOrderRequest|NewOrderSingle|"
        r"oandapyV20\.endpoints\.orders)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"@(?:app|router)\.(?:post|put|patch|delete)\(\s*[\"']"
        r"(?:/api)?/(?:broker/)?orders?",
        re.IGNORECASE,
    ),
)
KNOWN_SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[opusr]_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret|token)\b\s*[=:]\s*"
    r"[\"']?([^\"'\s,;#]{16,})"
)
SECRET_PLACEHOLDERS = frozenset(
    {
        "change-me",
        "changeme",
        "example",
        "none",
        "null",
        "redacted",
        "replace-me",
        "test",
        "your-key-here",
    }
)
HOST_CODEX_CONFIDENTIALITY_NOTICE = (
    "host Codex development requires explicit acknowledgment that workspace-write "
    "limits writes but is not a filesystem-read or container boundary; Codex and "
    "child tools may read host-accessible files, including staged Codex authentication"
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
        if (
            self.settings.app_env.casefold() != "development"
            or not self.settings.development_acknowledge_host_filesystem_read_risk
        ):
            raise RuntimeError(HOST_CODEX_CONFIDENTIALITY_NOTICE)
        self.policy.assert_unchanged()
        if (
            not self.settings.development_base_ref
            or self.settings.development_base_ref.startswith("-")
            or any(
                character in self.settings.development_base_ref
                for character in "\r\n\0"
            )
        ):
            raise ValueError("development base ref is invalid")
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
        runtime_directory = self._prepare_runtime(session_id, include_codex_auth=True)
        filtered_environment = self._safe_environment(runtime_directory)

        prompt = (
            "Implement the requested change in this repository. Follow AGENTS.md and all "
            "runtime policy boundaries. Make the smallest coherent change, add or update "
            "tests, and run relevant validation. Do not commit, push, open a pull request, "
            "read .env files, access databases, use broker credentials, or add broker order "
            "execution. This is a host process, not a confidential container: do not inspect "
            "paths outside the worktree or expose the staged Codex authentication to child "
            "tools. Leave all changes in the worktree for human review.\n\n"
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
                env=filtered_environment,
                capture_output=True,
                text=True,
                timeout=self.settings.development_timeout_seconds,
                check=False,
            )
            session.summary = self._redact_sensitive_text(
                (result.stdout or result.stderr)[-8000:]
            )
            if result.returncode != 0:
                session.status = "failed"
            else:
                has_changes = bool(self._git("-C", str(worktree), "status", "--porcelain"))
                if has_changes:
                    self._git(
                        "-C",
                        str(worktree),
                        "add",
                        "--intent-to-add",
                        "--all",
                    )
                session.validation = self._validate(worktree)
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
            shutil.rmtree(runtime_directory, ignore_errors=True)
        return session

    def get(self, session_id: str) -> DevelopmentSession:
        path = self._session_path(session_id)
        if path.is_symlink() or not path.is_file():
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
            raise RuntimeError("only a review-ready development session can be approved")
        session.validation = self._validate(Path(session.worktree))
        if not all(item["passed"] for item in session.validation):
            session.status = "failed"
            session.updated_at = datetime.now(UTC).isoformat()
            self._save(session)
            raise RuntimeError("static security validation changed or failed")
        self._git("-C", session.worktree, "add", "--all")
        staged_results = [
            self._scan_forbidden_diff(Path(session.worktree), cached=True),
            self._scan_secret_diff(Path(session.worktree), cached=True),
        ]
        session.validation.extend(staged_results)
        if not all(item["passed"] for item in staged_results):
            session.status = "failed"
            session.updated_at = datetime.now(UTC).isoformat()
            self._save(session)
            raise RuntimeError("staged security scan failed; approval was stopped")
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
        tracked = [
            path.strip()
            for path in self._git(
                "-C",
                str(self.repository),
                "ls-files",
                "-z",
                "--",
            ).split("\0")
            if path.strip()
        ]
        sensitive = sorted(path for path in tracked if self._is_sensitive_path(path))
        if sensitive:
            raise RuntimeError(
                "development repository tracks a private environment or credential file; "
                "remove it first: " + ", ".join(sensitive)
            )
        dangerous_config = self._git(
            "-C",
            str(self.repository),
            "config",
            "--local",
            "--name-only",
            "--get-regexp",
            r"^(filter|diff|merge)\.",
            allow_statuses=(0, 1),
        ).strip()
        if dangerous_config:
            raise RuntimeError(
                "development repository config contains executable filter/diff/merge "
                "drivers; remove them before using development mode"
            )

    def _validate_session_paths(self, session: DevelopmentSession) -> None:
        if Path(session.repository).resolve() != self.repository:
            raise RuntimeError("development session repository does not match configuration")
        worktree_root = (self.state_directory / "worktrees").resolve()
        worktree = Path(session.worktree).resolve()
        if worktree.parent != worktree_root:
            raise RuntimeError("development session worktree is outside the managed directory")

    def _validate(
        self,
        worktree: Path,
    ) -> list[dict[str, object]]:
        # Never execute generated project code from the host process. Codex may run
        # tests with workspace-write limits, but this service performs
        # only non-executing diff inspection before a human reviews the result.
        return [
            self._scan_forbidden_diff(worktree),
            self._scan_secret_diff(worktree),
        ]

    def _scan_forbidden_diff(
        self,
        worktree: Path,
        *,
        cached: bool = False,
    ) -> dict[str, object]:
        diff_arguments = [
            "-C",
            str(worktree),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
        ]
        if cached:
            diff_arguments.append("--cached")
        diff_arguments.append("--")
        diff = self._git(*diff_arguments)
        additions = "\n".join(
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        findings = sorted(
            {
                match.group(0)
                for pattern in FORBIDDEN_DIFF_PATTERNS
                for match in pattern.finditer(additions)
            }
        )
        if "GIT binary patch" in diff or "Binary files " in diff:
            findings.append("binary change")
        if "new file mode 120000" in diff or "new mode 120000" in diff:
            findings.append("changed symlink")
        path_arguments = [
            "-C",
            str(worktree),
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
        ]
        if cached:
            path_arguments.append("--cached")
        path_arguments.append("--")
        changed_paths = self._git(*path_arguments).split("\0")
        for relative in changed_paths:
            if not relative:
                continue
            candidate = worktree / relative
            if candidate.is_symlink():
                findings.append(f"changed symlink: {relative}")
        return {
            "command": "security: forbidden broker-write diff scan",
            "passed": not findings,
            "output": (
                "No broker-order SDK methods or write endpoints detected."
                if not findings
                else "Rejected broker-write additions: " + ", ".join(findings)
            ),
        }

    def _scan_secret_diff(
        self,
        worktree: Path,
        *,
        cached: bool = False,
    ) -> dict[str, object]:
        diff_arguments = [
            "-C",
            str(worktree),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=0",
        ]
        if cached:
            diff_arguments.append("--cached")
        diff_arguments.append("--")
        diff = self._git(*diff_arguments)
        added_lines = [
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        findings: set[str] = set()
        for line in added_lines:
            if KNOWN_SECRET_PATTERN.search(line):
                findings.add("credential-shaped value")
            for match in SECRET_ASSIGNMENT_PATTERN.finditer(line):
                value = match.group(1).strip().casefold()
                if (
                    value in SECRET_PLACEHOLDERS
                    or value.startswith(("${", "<", "os.environ", "settings."))
                ):
                    continue
                findings.add("non-placeholder secret assignment")

        path_arguments = [
            "-C",
            str(worktree),
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
        ]
        if cached:
            path_arguments.append("--cached")
        path_arguments.append("--")
        changed_paths = self._git(*path_arguments).split("\0")
        for relative in changed_paths:
            if self._is_sensitive_path(relative):
                findings.add(f"sensitive file path: {relative}")
        return {
            "command": "security: credential and sensitive-file diff scan",
            "passed": not findings,
            "output": (
                "No credential-shaped additions or sensitive file paths detected."
                if not findings
                else "Rejected possible secret material: " + ", ".join(sorted(findings))
            ),
        }

    @staticmethod
    def _is_sensitive_path(relative: str) -> bool:
        if not relative:
            return False
        path = Path(relative)
        name = path.name.casefold()
        if name == ".env.example":
            return False
        return (
            name == "auth.json"
            or name == ".env"
            or name.startswith(".env.")
            or path.suffix.casefold() in {".key", ".p12", ".pfx", ".dump"}
        )

    @staticmethod
    def _redact_sensitive_text(value: str) -> str:
        redacted = KNOWN_SECRET_PATTERN.sub("[REDACTED]", value)
        return SECRET_ASSIGNMENT_PATTERN.sub(
            lambda match: match.group(0).replace(match.group(1), "[REDACTED]"),
            redacted,
        )

    def _prepare_runtime(
        self,
        name: str,
        *,
        include_codex_auth: bool,
    ) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
        if not safe_name:
            raise ValueError("invalid ephemeral runtime name")
        repository_scope = hashlib.sha256(
            str(self.repository).encode("utf-8")
        ).hexdigest()[:12]
        runtime_root = (
            Path(tempfile.gettempdir())
            / "trading-agent-development"
            / repository_scope
        ).resolve()
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime = (runtime_root / safe_name).resolve()
        if runtime.parent != runtime_root:
            raise RuntimeError("ephemeral runtime escaped its managed directory")
        runtime.mkdir(parents=False, exist_ok=False, mode=0o700)
        for child in (
            "home",
            "xdg-config",
            "xdg-cache",
            "xdg-data",
            "codex",
            "tmp",
        ):
            (runtime / child).mkdir(mode=0o700)
        if include_codex_auth:
            configured_codex_home = os.environ.get("CODEX_HOME")
            source = (
                Path(configured_codex_home).expanduser()
                if configured_codex_home
                else Path.home() / ".codex"
            ) / "auth.json"
            if source.is_file() and not source.is_symlink():
                destination = runtime / "codex" / "auth.json"
                try:
                    descriptor = os.open(
                        source,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        source_stat = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(source_stat.st_mode)
                            or source_stat.st_size > 1_000_000
                        ):
                            raise RuntimeError(
                                "Codex authentication file is not a bounded regular file"
                            )
                        with os.fdopen(descriptor, "rb", closefd=False) as handle:
                            auth_bytes = handle.read(1_000_001)
                    finally:
                        os.close(descriptor)
                    if len(auth_bytes) > 1_000_000:
                        raise RuntimeError(
                            "Codex authentication file is unexpectedly large"
                        )
                    destination.write_bytes(auth_bytes)
                    destination.chmod(0o600)
                except (OSError, RuntimeError) as exc:
                    shutil.rmtree(runtime, ignore_errors=True)
                    raise RuntimeError(
                        "could not stage ephemeral Codex authentication"
                    ) from exc
        return runtime

    def _safe_environment(self, runtime: Path) -> dict[str, str]:
        allowed = (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SHELL",
            "TERM",
            "SystemRoot",
            "WINDIR",
        )
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        safe_path_entries: list[str] = []
        for raw_entry in environment.get("PATH", "").split(os.pathsep):
            if not raw_entry:
                continue
            entry = Path(raw_entry).expanduser()
            if not entry.is_absolute():
                continue
            resolved = entry.resolve()
            if resolved == self.repository or resolved.is_relative_to(self.repository):
                continue
            if str(resolved) not in safe_path_entries:
                safe_path_entries.append(str(resolved))
        environment["PATH"] = os.pathsep.join(safe_path_entries)
        environment.update(
            {
                "HOME": str(runtime / "home"),
                "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
                "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
                "XDG_DATA_HOME": str(runtime / "xdg-data"),
                "CODEX_HOME": str(runtime / "codex"),
                "TMPDIR": str(runtime / "tmp"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Trading Agent Development",
                "GIT_AUTHOR_EMAIL": "trading-agent@localhost",
                "GIT_COMMITTER_NAME": "Trading Agent Development",
                "GIT_COMMITTER_EMAIL": "trading-agent@localhost",
            }
        )
        environment["TRADING_AGENT_DEVELOPMENT"] = "1"
        return environment

    def _git_environment(self) -> dict[str, str]:
        repository_scope = hashlib.sha256(
            str(self.repository).encode("utf-8")
        ).hexdigest()[:12]
        runtime = (
            Path(tempfile.gettempdir())
            / "trading-agent-development"
            / repository_scope
            / "git"
        )
        if not runtime.exists():
            try:
                runtime = self._prepare_runtime("git", include_codex_auth=False)
            except FileExistsError:
                # Another local development process created the same secret-free
                # Git runtime between the existence check and mkdir.
                pass
        return self._safe_environment(runtime.resolve())

    def _git(
        self,
        *arguments: str,
        allow_statuses: tuple[int, ...] = (0,),
    ) -> str:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is not installed or is not on PATH")
        environment = self._git_environment()
        hooks_directory = Path(environment["HOME"]).parent / "hooks"
        hooks_directory.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run(  # noqa: S603
            [git, "-c", f"core.hooksPath={hooks_directory}", *arguments],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in allow_statuses:
            raise RuntimeError((result.stderr or result.stdout).strip()[:1000])
        return result.stdout

    def _session_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", session_id):
            raise ValueError("invalid development session id")
        return self.state_directory / "sessions" / f"{session_id}.json"

    def _save(self, session: DevelopmentSession) -> None:
        path = self._session_path(session.id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise RuntimeError("development session record cannot be a symlink")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(asdict(session), indent=2))
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
