"""
lanegate/orchestrate/guards.py — safety gates run against a ticket or a worktree diff.

Extracted from orchestrate.py (TICK-255/TICK-271): prompt-injection scanning
of ticket text, the hard-blocked-file allowlist, the unified-diff line
parser, and the static-analysis gate (gitleaks/semgrep/bandit/pip-audit/
npm-audit/composer-audit/bundler-audit).
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Injection signal scan
# ---------------------------------------------------------------------------

_INJECTION_SIGNALS: list[tuple[str, str]] = [
    # Instruction override attempts
    (r"ignore\s+(previous|above|prior)\s+instructions?", "instruction override"),
    (r"disregard\s+(the\s+)?(above|previous|prior)", "instruction override"),
    (r"forget\s+(previous|above|prior)\s+instructions?", "instruction override"),
    (r"new\s+(system\s+)?(prompt|instructions?)\s*:", "instruction override"),
    (r"you\s+are\s+now\s+", "role reassignment"),
    (r"act\s+as\s+(an?\s+)?AI\s+without", "role reassignment"),
    (r"pretend\s+(you\s+are|to\s+be)\s+", "role reassignment"),
    # ``build_prompt`` renders its own delimiter and escapes either delimiter
    # in every untrusted value.  Do not reject those literal strings here:
    # review findings and code examples must be able to describe the fence
    # without making a ticket permanently unrunnable.  Textual attempts to
    # override the instruction layer are still scanned below.
    (r"</?system>", "system tag injection"),
    (r"</?assistant>", "assistant tag injection"),
    # Explicit jailbreak vocabulary
    (r"\bjailbreak\b", "jailbreak keyword"),
    (r"DAN\s+mode", "jailbreak keyword"),
    (r"developer\s+mode\s+enabled", "jailbreak keyword"),
    # Instruction injection disguised as content
    (r"IMPORTANT\s*:\s*(after|before|also|additionally)\s+", "hidden instruction"),
    (r"NOTE\s*:\s*per\s+(policy|protocol|security\s+policy)\s+", "hidden instruction"),
    (r"mandatory\s+(requirement|step|action)\s*:", "hidden instruction"),
]

_SYSTEM_SECTION_HEADERS = [
    "## Needs Review Reason",
    "## Hibernation Reason",
    "## Prior Agent Notes",
    "## Failure Reason",
    "## Conflict-aware resume",
]


def _scan_injection_signals(ticket: dict) -> list[str]:
    """Scan ticket text fields for known prompt injection patterns.

    Checks title, body (_body), close_criteria, and safeguards for patterns in
    _INJECTION_SIGNALS.  Returns a list of human-readable finding strings;
    an empty list means the ticket is clean.

    Scans the entire body without exemptions to prevent bypass attacks that
    prefix the body with system-section headers to truncate the scan window.
    Literal ``<untrusted-data>`` delimiters are intentionally handled by
    ``prompts.build_prompt`` at the rendering boundary, where they are escaped
    in every untrusted field.  Scanning them here would reject legitimate
    historical review evidence without adding protection.
    """
    def _field_text(value: object) -> str:
        """Render legacy list-valued criteria without skipping any content.

        Older hand-authored tickets may express close criteria as YAML lists.
        Treat each list element as ticket text so the injection gate still
        inspects it instead of crashing before dispatch or silently omitting
        a criterion from the scan.
        """
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return str(value or "")

    fields = {
        "title": _field_text(ticket.get("title", "")),
        "body": _field_text(ticket.get("_body", "")),
        "close_criteria": _field_text(ticket.get("close_criteria", "")),
    }
    findings: list[str] = []
    for field_name, content in fields.items():
        if not content:
            continue
        for pattern, label in _INJECTION_SIGNALS:
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                snippet = content[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ")
                findings.append(f"{label} in {field_name!r}: ...{snippet!r}...")

    safeguards = ticket.get("safeguards")
    if isinstance(safeguards, dict):
        for stage, guards in safeguards.items():
            guard_list = [guards] if isinstance(guards, str) else (guards or [])
            for guard in guard_list:
                if not isinstance(guard, str):
                    continue
                for pattern, label in _INJECTION_SIGNALS:
                    m = re.search(pattern, guard, re.IGNORECASE)
                    if m:
                        snippet = guard[max(0, m.start() - 20) : m.end() + 20]
                        findings.append(f"{label} in safeguards[{stage!r}]: ...{snippet!r}...")

    return findings


# ---------------------------------------------------------------------------
# Risk-lane classification (TICK-467)
# ---------------------------------------------------------------------------

_RED_LANE_SIGNALS: list[tuple[str, str, str]] = [
    # External credentials / secrets
    (r"(?i)aws_secret_access_key\s*[:=]", "AWS secret key", "credentials"),
    (r"-----BEGIN\s+(RSA|OPENSSH|EC|DSA|PGP)?\s*PRIVATE KEY-----", "private key material", "credentials"),
    (r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"][A-Za-z0-9/_\-+=]{8,}['\"]", "hardcoded credential", "credentials"),
    (r"\bghp_[A-Za-z0-9]{20,}\b", "GitHub personal access token", "credentials"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "API secret key", "credentials"),
    # Irreversible / security-sensitive operations
    (r"\brm\s+-rf\s+/", "irreversible filesystem deletion", "security_actions"),
    (r"(?i)\bdrop\s+(table|database|schema)\b", "irreversible database schema change", "security_actions"),
    (r"\bgit\s+push\s+.*--force\b", "force-push (irreversible history rewrite)", "security_actions"),
    # scan_risk_lane() strips the unified-diff '+' marker before matching.
    (r"(?im)^\s*sudo\s+", "privilege escalation", "security_actions"),
    (r"\bchmod\s+(-R\s+)?777\b", "insecure permission change", "security_actions"),
]

_YELLOW_LANE_SIGNALS: list[tuple[str, str]] = [
    (r"(?im)^##\s*Review Findings", "review-findings amendment"),
    (r"(?im)^##\s*Requirement", "requirement amendment"),
    (r"(?i)\bclose_criteria\b", "close-criteria amendment"),
]


class RiskLane(str):
    """A risk-lane result with the red-signal categories that caused it.

    It remains a ``str`` subclass so existing callers comparing the result to
    ``"green"``, ``"yellow"``, or ``"red"`` retain their API unchanged.
    """

    signals: frozenset[str]

    def __new__(cls, lane: str, signals: frozenset[str] = frozenset()) -> "RiskLane":
        result = super().__new__(cls, lane)
        result.signals = signals
        return result


def scan_risk_lane(diff_text: str, ticket: dict | None = None) -> RiskLane:
    """Classify a change's risk lane from its diff (and optional ticket context).

    Returns:
      - "red": external credentials, security-sensitive, or irreversible
        operations found in added lines — always escalates to a human,
        regardless of the ticket's configured autonomy.
      - "yellow": requirement amendments or review-finding-driven changes —
        stays on the automatic fix/re-review path (like "full" autonomy).
      - "green": ordinary scoped changes — the default.

    Only added (``+``) lines are scanned so removing a risky pattern does
    not itself trigger escalation.
    """
    added_lines = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    red_signals = frozenset(
        category
        for pattern, _label, category in _RED_LANE_SIGNALS
        if re.search(pattern, added_lines)
    )
    if red_signals:
        return RiskLane("red", red_signals)

    haystack = added_lines
    if ticket:
        haystack += "\n" + str(ticket.get("_body", "")) + "\n" + str(ticket.get("close_criteria", ""))
    for pattern, _label in _YELLOW_LANE_SIGNALS:
        if re.search(pattern, haystack):
            return RiskLane("yellow")

    return RiskLane("green")


def risk_lane_requires_human_review(risk_lane: str, escalation_triggers: dict) -> bool:
    """Return whether a classified lane matches an enabled escalation trigger.

    A red classification alone is intentionally insufficient: credentials and
    security actions are configured independently.  A plain ``"red"`` from a
    legacy caller is treated conservatively as both categories because it has
    no signal metadata to filter on.
    """
    if risk_lane != "red":
        return False
    detected_triggers = getattr(
        risk_lane,
        "signals",
        frozenset({"credentials", "security_actions"}),
    )
    return any(escalation_triggers.get(trigger, False) for trigger in detected_triggers)


def _run_acceptance_contract_audit(ticket: dict, repo_root: Path, cfg: dict) -> list[str]:
    """Run and persist the deterministic acceptance-contract audit for a ticket."""
    from lanegate.analyze import audit_acceptance_contract
    from lanegate.lifecycle import _commit_generated_ticket_write
    from lanegate.ticket import write_ticket

    audit = audit_acceptance_contract(ticket, repo_root)
    ticket["acceptance_contract_audit"] = audit.as_metadata()
    if ticket.get("_path"):
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root,
            Path(ticket["_path"]),
            ticket["id"],
            "acceptance-contract-audit",
            cfg,
        )
    return audit.findings


def check_control_plane_compliance(
    ticket: dict,
    repo_root: Path | None = None,
    cfg: dict | None = None,
    worktree_path: Path | None = None,
    *,
    check_review_independence: bool = True,
) -> tuple[bool, str | None]:
    """Verify ticket-branch isolation and review compliance for control-plane files."""
    from lanegate.safeguards import collect_control_plane_touches

    cp_touches, curr_branch = collect_control_plane_touches(ticket, worktree_path, cfg)

    if not cp_touches:
        return True, None

    trunk_branch = (cfg or {}).get("trunk_branch", "main")
    cp_list = ", ".join(sorted(set(cp_touches)))

    # 1. Branch isolation check
    if ticket.get("status") not in ("merged", "post_merge"):
        if (
            ticket.get("is_main")
            or not ticket.get("id")
            or curr_branch in (trunk_branch, "main", "master")
        ):
            return False, f"Control-plane files ({cp_list}) require ticket-branch isolation and cannot be modified directly on {trunk_branch}."

    # 2. Independent review compliance check
    if check_review_independence:
        independence = ticket.get("review_independence") or (ticket.get("review") or {}).get("review_independence")
        review_driver = ticket.get("review_driver") or (ticket.get("review") or {}).get("driver")
        implementer = ticket.get("implement_driver") or ticket.get("executor") or ticket.get("implementer")
        review_model = ticket.get("review_model") or (ticket.get("review") or {}).get("model")
        implement_model = ticket.get("implement_session_model") or ticket.get("implement_model")

        if independence == "self":
            return False, f"Control-plane files ({cp_list}) require independent model review, but same-model review was recorded."
        if independence == "undetermined":
            return False, f"Control-plane files ({cp_list}) require independent model review, but undetermined review independence was recorded."
        if not independence or independence not in ("independent", "different-model"):
            return False, f"Control-plane files ({cp_list}) require independent model review, but independent review was not recorded."

        if review_driver and implementer and review_driver == implementer:
            if review_model and implement_model and review_model == implement_model:
                return False, f"Control-plane files ({cp_list}) require independent model review, but same driver and model were used for review."
            elif (not review_model or not implement_model or review_model == implement_model) and independence not in ("independent", "different-model"):
                return False, f"Control-plane files ({cp_list}) require independent model review, but same driver and model were used for review."

    return True, None



# ---------------------------------------------------------------------------
# Hard-blocked file categories
# ---------------------------------------------------------------------------

# Each entry: (glob_or_prefix, rule_description)
# Glob patterns use fnmatch against the full relative path (forward-slash separators).
# Prefix patterns are checked with str.startswith().
_BLOCKED_FILE_RULES: list[tuple[str, str]] = [
    # LaneGate control plane
    (".lanegate.yml", "LaneGate control configuration: .lanegate.yml"),
    # CI/CD
    (".github/", "CI/CD: .github/ directory"),
    (".circleci/", "CI/CD: .circleci/ directory"),
    (".gitlab-ci.yml", "CI/CD: GitLab CI config"),
    ("Jenkinsfile", "CI/CD: Jenkinsfile"),
    (".travis.yml", "CI/CD: Travis CI config"),
    # Dependency manifests
    ("requirements.txt", "dependency manifest: requirements.txt"),
    ("requirements*.txt", "dependency manifest: requirements*.txt"),
    ("pyproject.toml", "dependency manifest: pyproject.toml"),
    ("package.json", "dependency manifest: package.json"),
    ("package-lock.json", "dependency manifest: package-lock.json"),
    ("Pipfile", "dependency manifest: Pipfile"),
    ("Pipfile.lock", "dependency manifest: Pipfile.lock"),
    ("Cargo.toml", "dependency manifest: Cargo.toml"),
    ("go.mod", "dependency manifest: go.mod"),
    ("go.sum", "dependency manifest: go.sum"),
    # Java/JVM (Maven, Gradle — e.g. AEM projects)
    ("pom.xml", "dependency manifest: pom.xml"),
    ("build.gradle", "dependency manifest: build.gradle"),
    ("build.gradle.kts", "dependency manifest: build.gradle.kts"),
    ("settings.gradle", "dependency manifest: settings.gradle"),
    ("settings.gradle.kts", "dependency manifest: settings.gradle.kts"),
    # Ruby
    ("Gemfile", "dependency manifest: Gemfile"),
    ("Gemfile.lock", "dependency manifest: Gemfile.lock"),
    # PHP
    ("composer.json", "dependency manifest: composer.json"),
    ("composer.lock", "dependency manifest: composer.lock"),
    # .NET
    ("*.csproj", "dependency manifest: *.csproj"),
    ("*.fsproj", "dependency manifest: *.fsproj"),
    ("packages.config", "dependency manifest: packages.config"),
    # Credential-shaped filenames
    (".env", "credentials: .env"),
    (".env.*", "credentials: .env.* variant"),
    ("*.pem", "credentials: PEM certificate/key"),
    ("*.key", "credentials: private key file"),
    ("*.p12", "credentials: PKCS#12 keystore"),
    ("secrets.*", "credentials: secrets.* file"),
    ("credentials.*", "credentials: credentials.* file"),
]


def _is_blocked_file(path: str, extra_patterns: list[str] | None = None) -> tuple[bool, str]:
    """Return (True, rule_description) if path matches a hard-blocked file pattern.

    Matching is performed against the filename portion and/or the full relative
    path using fnmatch (glob-style) and prefix checks.  Path separators are
    normalised to forward slashes before matching.

    Args:
        path: Relative file path (as returned by git diff --name-only).
        extra_patterns: Additional project-specific glob patterns from
            ``protected_paths`` in .lanegate.yml.  Each pattern is matched as a
            fnmatch glob against the full path.

    Returns:
        (True, rule_description) on a match; (False, "") if clean.
    """
    # Normalise to forward slashes for consistent cross-platform matching.
    norm = path.replace("\\", "/")
    filename = norm.rsplit("/", 1)[-1]  # basename

    for pattern, rule in _BLOCKED_FILE_RULES:
        if pattern.endswith("/"):
            # Prefix/directory check
            if norm.startswith(pattern) or ("/" + pattern) in norm:
                return True, rule
        elif "*" in pattern or "?" in pattern or "[" in pattern:
            # Glob: match against filename OR full path
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(norm, pattern):
                return True, rule
        else:
            # Exact match against filename OR full path
            if filename == pattern or norm == pattern:
                return True, rule

    # Extra project-specific patterns (fnmatch against full path or filename)
    for pat in extra_patterns or []:
        pat_norm = pat.replace("\\", "/")
        if fnmatch.fnmatch(norm, pat_norm) or fnmatch.fnmatch(filename, pat_norm):
            return True, f"protected_paths: {pat}"

    return False, ""


# ---------------------------------------------------------------------------
# Unified diff parser: extract changed line ranges per file
# ---------------------------------------------------------------------------


def _parse_diff_changed_lines(diff_output: str) -> dict[str, set[int]]:
    """Parse git diff -U0 output and return changed line ranges per file.

    Returns a dict mapping file paths to sets of line numbers that were
    added or modified in the diff (not deleted lines).

    Example:
        Input: unified diff output from `git diff -U0 <trunk>...HEAD`
        Output: {"src/main.py": {10, 11, 12}, "src/util.py": {5}}
    """
    changed_lines: dict[str, set[int]] = {}
    current_file = None

    for line in diff_output.splitlines():
        # Parse diff header: --- a/path or +++ b/path
        if line.startswith("--- a/"):
            current_file = line[6:]  # Remove "--- a/"
        elif line.startswith("+++ b/"):
            current_file = line[6:]  # Remove "+++ b/"
        # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
        elif line.startswith("@@"):
            if current_file is None:
                continue
            # Extract the +new_start,new_count part from the hunk header
            # Format: @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_start = int(match.group(1))
                new_count = int(match.group(2)) if match.group(2) else 1
                if current_file not in changed_lines:
                    changed_lines[current_file] = set()
                # Add all line numbers in this hunk to the changed set
                for line_num in range(new_start, new_start + new_count):
                    changed_lines[current_file].add(line_num)

    return changed_lines


# ---------------------------------------------------------------------------
# Static analysis gate
# ---------------------------------------------------------------------------


def _run_static_analysis(
    worktree_path: Path, cfg: dict, audit_bundle_path: Path | None = None
) -> list[str]:
    """Run language-aware static analysis tools on the worktree diff.

    Always runs gitleaks (secret scanning).  Runs semgrep when installed
    (primary cross-language SAST scanner); falls back to bandit for Python-only
    scanning when semgrep is absent.  Runs pip-audit when Python dep manifests
    changed; runs npm audit when JS dep manifests changed; runs composer audit
    when PHP dep manifests changed; runs bundler-audit when Ruby dep manifests
    changed. There is no built-in dependency-vulnerability scan for Java/Gradle
    or .NET manifests yet — wire one in via a project ``safeguards`` script if
    needed.

    Each tool is silently skipped when not installed (``shutil.which`` returns
    None) or when disabled in cfg[``static_analysis``].

    Args:
        worktree_path: Path to the git worktree to scan.
        cfg: Loaded config dict.  Reads ``static_analysis.enabled``,
            ``static_analysis.tools.*`` enable flags.

    Returns a list of human-readable finding strings.  An empty list means
    the scan was clean.
    """
    from lanegate.orchestrate import (
        _finish_gate_capture,
        _record_gate,
        _run_gate_command,
        _start_gate_capture,
        _write_bounded_text,
    )

    sa_cfg: dict = cfg.get("static_analysis") or {}
    gates_dir, gate_records = _start_gate_capture(audit_bundle_path, cfg)
    if not sa_cfg.get("enabled", True):
        _record_gate(
            gate_records,
            "static-analysis",
            "skipped",
            reason="static_analysis.enabled is false",
        )
        _finish_gate_capture(audit_bundle_path, gates_dir, gate_records, findings=[])
        return []
    if not worktree_path.exists():
        _record_gate(
            gate_records,
            "static-analysis",
            "skipped",
            reason=f"worktree path does not exist: {worktree_path}",
        )
        _finish_gate_capture(audit_bundle_path, gates_dir, gate_records, findings=[])
        return []

    from lanegate.config import resolve_trunk_branch

    trunk_branch = resolve_trunk_branch(cfg, worktree_path)

    tools_cfg: dict = sa_cfg.get("tools") or {}

    findings: list[str] = []

    # Collect committed file list for language detection and diff-scoped scans.
    try:
        result = _run_gate_command(
            gates_dir,
            gate_records,
            "changed-files",
            ["git", "diff", "--name-only", f"{trunk_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        changed_files: list[str] = (
            [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if result.returncode == 0
            else []
        )
        if gates_dir is not None and result.returncode == 0:
            _write_bounded_text(gates_dir / "changed-files.txt", result.stdout)
    except Exception as exc:
        _record_gate(gate_records, "changed-files", "error", reason=str(exc))
        changed_files = []

    has_python = any(f.endswith(".py") for f in changed_files)
    changed_set = {Path(f).as_posix() for f in changed_files}
    changed_existing_paths = [
        worktree_path / f for f in changed_files if (worktree_path / f).is_file()
    ]

    _py_manifests = {"requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock"}
    _py_manifest_patterns = ("requirements", "Pipfile")
    has_py_manifests = any(
        f == m or any(f.startswith(p) for p in _py_manifest_patterns)
        for f in changed_files
        for m in _py_manifests
    )

    has_js_manifests = any(f == "package.json" or f == "package-lock.json" for f in changed_files)

    has_php_manifests = any(f == "composer.json" or f == "composer.lock" for f in changed_files)

    has_rb_manifests = any(f == "Gemfile" or f == "Gemfile.lock" for f in changed_files)

    # --- gitleaks ---
    if not tools_cfg.get("gitleaks", True):
        _record_gate(gate_records, "gitleaks", "skipped", reason="disabled by config")
    elif not shutil.which("gitleaks"):
        _record_gate(gate_records, "gitleaks", "skipped", reason="tool unavailable")
    elif changed_files and not changed_existing_paths:
        _record_gate(gate_records, "gitleaks", "skipped", reason="no existing changed files")
    else:
        tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            source_path = worktree_path
            if changed_existing_paths:
                tmp_dir = tempfile.TemporaryDirectory(prefix="lanegate-static-gitleaks-")
                source_path = Path(tmp_dir.name)
                for path in changed_existing_paths:
                    rel = path.relative_to(worktree_path)
                    target = source_path / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            gitleaks_cmd = [
                "gitleaks",
                "detect",
                "--source",
                str(source_path),
                "--no-git",
                "--no-banner",
                "--log-level",
                "fatal",
            ]
            # The scan source above is a scratch copy of only the changed files
            # (not the repo root), so gitleaks cannot auto-discover a project
            # .gitleaks.toml by walking up from --source. Pass it explicitly
            # when the worktree has one, so repo-level allowlist entries (e.g.
            # known-benign env-var-name patterns) are actually honored.
            gitleaks_config = worktree_path / ".gitleaks.toml"
            if gitleaks_config.exists():
                gitleaks_cmd.extend(["--config", str(gitleaks_config)])
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "gitleaks",
                gitleaks_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                # gitleaks exits non-zero when secrets found or on error
                output = (r.stdout + r.stderr).strip()
                if output:
                    for line in output.splitlines():
                        stripped = line.strip()
                        if stripped:
                            findings.append(f"gitleaks: {stripped}")
                else:
                    findings.append("gitleaks: potential secret detected (no details)")
        except Exception as exc:
            _record_gate(gate_records, "gitleaks", "error", reason=str(exc))
            findings.append(f"gitleaks: scan error — {exc}")
        finally:
            if tmp_dir is not None:
                tmp_dir.cleanup()

    # --- semgrep (primary cross-language SAST) ---
    semgrep_ran = False
    if not tools_cfg.get("semgrep", True):
        _record_gate(gate_records, "semgrep", "skipped", reason="disabled by config")
    elif not shutil.which("semgrep"):
        _record_gate(gate_records, "semgrep", "skipped", reason="tool unavailable")
    elif changed_files and not changed_existing_paths:
        _record_gate(gate_records, "semgrep", "skipped", reason="no existing changed files")
    else:
        semgrep_ran = True
        try:
            # Fetch git diff -U0 to extract exact changed line ranges per file.
            # This lets us filter findings to only those on lines the ticket actually touched.
            diff_result = _run_gate_command(
                gates_dir,
                gate_records,
                "diff-for-semgrep",
                ["git", "diff", "-U0", f"{trunk_branch}...HEAD"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            changed_lines_by_file: dict[str, set[int]] = {}
            if diff_result.returncode == 0:
                changed_lines_by_file = _parse_diff_changed_lines(diff_result.stdout)

            semgrep_targets = (
                [str(path) for path in changed_existing_paths]
                if changed_existing_paths
                else [str(worktree_path)]
            )
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "semgrep",
                ["semgrep", "--config=auto", *semgrep_targets, "--json", "--quiet"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            try:
                data = json.loads(r.stdout)
                results = data.get("results") or []
                for item in results:
                    path = item.get("path", "?")
                    try:
                        rel_path = (
                            Path(path)
                            .resolve()
                            .relative_to(worktree_path.resolve())
                            .as_posix()
                        )
                    except (OSError, ValueError):
                        rel_path = Path(path).as_posix()
                    if changed_set and rel_path not in changed_set:
                        continue
                    # Filter findings to only those on lines that were actually changed.
                    if changed_lines_by_file:
                        line_num = item.get("start", {}).get("line")
                        if line_num is not None and rel_path in changed_lines_by_file:
                            if line_num not in changed_lines_by_file[rel_path]:
                                continue  # Finding is outside changed lines; skip it
                    rule = item.get("check_id", "?")
                    msg = item.get("extra", {}).get("message", "")
                    line = item.get("start", {}).get("line", "?")
                    findings.append(f"semgrep: {path}:{line} [{rule}] {msg}")
            except (json.JSONDecodeError, KeyError):
                if r.returncode != 0:
                    err = (r.stdout + r.stderr).strip()
                    if err:
                        findings.append(f"semgrep: error — {err[:200]}")
        except Exception as exc:
            _record_gate(gate_records, "semgrep", "error", reason=str(exc))
            findings.append(f"semgrep: scan error — {exc}")

    # --- bandit (Python fallback when semgrep absent) ---
    if semgrep_ran:
        _record_gate(gate_records, "bandit", "skipped", reason="semgrep ran as primary scanner")
    elif not has_python:
        _record_gate(gate_records, "bandit", "skipped", reason="no changed Python files")
    elif not tools_cfg.get("bandit", True):
        _record_gate(gate_records, "bandit", "skipped", reason="disabled by config")
    elif not shutil.which("bandit"):
        _record_gate(gate_records, "bandit", "skipped", reason="tool unavailable")
    else:
        changed_py = [f for f in changed_files if f.endswith(".py")]
        py_paths = [str(worktree_path / f) for f in changed_py if (worktree_path / f).exists()]
        if py_paths:
            try:
                r = _run_gate_command(
                    gates_dir,
                    gate_records,
                    "bandit",
                    ["bandit", "-f", "json"] + py_paths,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                try:
                    data = json.loads(r.stdout)
                    for issue in data.get("results") or []:
                        fname = issue.get("filename", "?")
                        line = issue.get("line_number", "?")
                        test_id = issue.get("test_id", "?")
                        issue_text = issue.get("issue_text", "")
                        severity = issue.get("issue_severity", "")
                        findings.append(
                            f"bandit: {fname}:{line} [{test_id}/{severity}] {issue_text}"
                        )
                except (json.JSONDecodeError, KeyError):
                    if r.returncode not in (0, 1):
                        err = (r.stdout + r.stderr).strip()
                        if err:
                            findings.append(f"bandit: error — {err[:200]}")
            except Exception as exc:
                _record_gate(gate_records, "bandit", "error", reason=str(exc))
                findings.append(f"bandit: scan error — {exc}")
        else:
            _record_gate(gate_records, "bandit", "skipped", reason="changed Python paths absent")

    # --- pip-audit (Python dep manifests) ---
    if not has_py_manifests:
        _record_gate(gate_records, "pip-audit", "skipped", reason="no changed Python manifests")
    elif not tools_cfg.get("pip_audit", True):
        _record_gate(gate_records, "pip-audit", "skipped", reason="disabled by config")
    elif not shutil.which("pip-audit"):
        _record_gate(gate_records, "pip-audit", "skipped", reason="tool unavailable")
    else:
        try:
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "pip-audit",
                ["pip-audit", "--format=json", str(worktree_path)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            try:
                data = json.loads(r.stdout)
                # pip-audit returns a list or {"dependencies": [...]} depending on version
                deps = data if isinstance(data, list) else data.get("dependencies", [])
                for dep in deps:
                    if not dep.get("vulns"):
                        continue
                    name = dep.get("name", "?")
                    ver = dep.get("version", "?")
                    for vuln in dep.get("vulns", []):
                        vid = vuln.get("id", "?")
                        desc = vuln.get("description", "")
                        findings.append(f"pip-audit: {name}=={ver} [{vid}] {desc[:100]}")
            except (json.JSONDecodeError, KeyError):
                if r.returncode != 0:
                    err = (r.stdout + r.stderr).strip()
                    if err:
                        findings.append(f"pip-audit: error — {err[:200]}")
        except Exception as exc:
            _record_gate(gate_records, "pip-audit", "error", reason=str(exc))
            findings.append(f"pip-audit: scan error — {exc}")

    # --- npm audit (JS dep manifests) ---
    if not has_js_manifests:
        _record_gate(gate_records, "npm-audit", "skipped", reason="no changed JS manifests")
    elif not tools_cfg.get("npm_audit", True):
        _record_gate(gate_records, "npm-audit", "skipped", reason="disabled by config")
    elif not shutil.which("npm"):
        _record_gate(gate_records, "npm-audit", "skipped", reason="tool unavailable")
    else:
        try:
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "npm-audit",
                ["npm", "audit", "--json"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            try:
                data = json.loads(r.stdout)
                vulns = data.get("vulnerabilities") or {}
                for pkg_name, vuln_info in vulns.items():
                    severity = vuln_info.get("severity", "?")
                    via = vuln_info.get("via") or []
                    advisories = [
                        v.get("title", str(v)) if isinstance(v, dict) else str(v) for v in via[:3]
                    ]
                    advisory_str = "; ".join(advisories) if advisories else ""
                    findings.append(f"npm-audit: {pkg_name} [{severity}] {advisory_str}")
            except (json.JSONDecodeError, KeyError):
                if r.returncode not in (0, 1):
                    err = (r.stdout + r.stderr).strip()
                    if err:
                        findings.append(f"npm-audit: error — {err[:200]}")
        except Exception as exc:
            _record_gate(gate_records, "npm-audit", "error", reason=str(exc))
            findings.append(f"npm-audit: scan error — {exc}")

    # --- composer audit (PHP dep manifests) ---
    if not has_php_manifests:
        _record_gate(gate_records, "composer-audit", "skipped", reason="no changed PHP manifests")
    elif not tools_cfg.get("composer_audit", True):
        _record_gate(gate_records, "composer-audit", "skipped", reason="disabled by config")
    elif not shutil.which("composer"):
        _record_gate(gate_records, "composer-audit", "skipped", reason="tool unavailable")
    else:
        try:
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "composer-audit",
                ["composer", "audit", "--format=json"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            try:
                data = json.loads(r.stdout)
                php_advisories = data.get("advisories") or {}
                for pkg_name, advisory_list in php_advisories.items():
                    for advisory in advisory_list:
                        title = advisory.get("title", "")
                        cve = advisory.get("cve") or advisory.get("advisoryId", "?")
                        findings.append(f"composer-audit: {pkg_name} [{cve}] {title}")
            except (json.JSONDecodeError, KeyError):
                if r.returncode not in (0, 1):
                    err = (r.stdout + r.stderr).strip()
                    if err:
                        findings.append(f"composer-audit: error — {err[:200]}")
        except Exception as exc:
            _record_gate(gate_records, "composer-audit", "error", reason=str(exc))
            findings.append(f"composer-audit: scan error — {exc}")

    # --- bundler-audit (Ruby dep manifests) ---
    # bundler-audit has no stable machine-readable output format, unlike the
    # JSON-emitting tools above — findings are captured as raw report lines.
    if not has_rb_manifests:
        _record_gate(gate_records, "bundler-audit", "skipped", reason="no changed Ruby manifests")
    elif not tools_cfg.get("bundler_audit", True):
        _record_gate(gate_records, "bundler-audit", "skipped", reason="disabled by config")
    elif not shutil.which("bundle-audit"):
        _record_gate(gate_records, "bundler-audit", "skipped", reason="tool unavailable")
    else:
        try:
            r = _run_gate_command(
                gates_dir,
                gate_records,
                "bundler-audit",
                ["bundle-audit", "check", "--update"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if r.returncode != 0:
                output = (r.stdout + r.stderr).strip()
                if output:
                    for line in output.splitlines():
                        stripped = line.strip()
                        if stripped:
                            findings.append(f"bundler-audit: {stripped}")
                else:
                    findings.append("bundler-audit: vulnerability detected (no details)")
        except Exception as exc:
            _record_gate(gate_records, "bundler-audit", "error", reason=str(exc))
            findings.append(f"bundler-audit: scan error — {exc}")

    _finish_gate_capture(audit_bundle_path, gates_dir, gate_records, findings=findings)
    return findings
