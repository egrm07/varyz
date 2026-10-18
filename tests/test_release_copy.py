"""The release copy is a promise, so it is checked like code.

Three claims in the README are commitments rather than description: that local
capabilities are free, that paying never buys a better answer, and that the Alpha
carries a way to reach the author. Each is easy to break silently — by shipping a
paywall stub, by publishing a price, or by tagging the repository while the
contact line is still a placeholder. These tests fail in exactly those cases.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from varyz.privacy import secrets_scan

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

CONTACT_PLACEHOLDER = "CONTACT-ADDRESS-TBD"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"https?://\S+")


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_says_every_capability_is_free_and_open(readme: str) -> None:
    assert "free and open" in readme
    for capability in ("connectors", "Mirror", "privacy gate", "MCP integration"):
        assert capability in readme, f"the free-capability list no longer names {capability}"


def test_the_readme_says_paying_never_improves_an_answer(readme: str) -> None:
    assert "No answer is ever improved by paying" in readme


def test_the_readme_says_unbuilt_capabilities_are_unbuilt_rather_than_locked(
    readme: str,
) -> None:
    assert "unbuilt, not because" in readme
    assert "locked" in readme


def test_the_readme_carries_a_real_contact_address(readme: str) -> None:
    """Fails while the address is still a placeholder. That is the point: the
    enquiry line is the Alpha's only monetisation surface, and shipping it
    unresolved would waste the launch."""
    line = next(
        (ln for ln in readme.splitlines() if "Commercial and company enquiries" in ln),
        None,
    )
    assert line is not None, "the README no longer invites commercial enquiries"
    assert CONTACT_PLACEHOLDER not in line, (
        "the contact address is still the placeholder "
        f"{CONTACT_PLACEHOLDER!r}; choose the address the public repository should "
        "carry and put it in the README before tagging the Alpha"
    )
    assert EMAIL.search(line) or URL.search(line), (
        "the enquiry line names no email address and no link, so there is no way "
        "for a company to reach the author"
    )


def test_the_alpha_publishes_no_price(readme: str) -> None:
    """The Alpha states the commercial model without quoting a number for it."""
    offenders = [
        line
        for line in readme.splitlines()
        if re.search(r"[$€£]\s?\d", line)
        or re.search(r"\bper (?:month|year|seat|user)\b", line, re.I)
        or re.search(r"\b\d+\s?(?:USD|EUR|GBP)\b", line)
    ]
    assert not offenders, f"the Alpha README quotes a price: {offenders}"


def test_the_alpha_shows_no_upgrade_prompt(readme: str) -> None:
    offenders = [
        line
        for line in readme.splitlines()
        if re.search(r"\bupgrade to (?:pro|premium|paid)\b", line, re.I)
        or re.search(r"^#+\s*pricing\b", line, re.I)
    ]
    assert not offenders, f"the Alpha README carries an upgrade prompt: {offenders}"


# Deliberately narrow. "subscription" is not here because Varyz uses the word for
# iCal feeds and for signing in with an existing Claude or ChatGPT account, and a
# term that fires on those would be turned off rather than obeyed.
PRO_SURFACE = re.compile(
    r"""
    # No trailing \b on the nouns: a stub is far likelier to be spelt
    # ``PaywallError`` or ``BillingClient`` than to stand alone as a word.
    licen[cs]e_key | entitlement | \bpaywall | \bbilling
    | \bstripe | \bpaddle\b | lemonsqueezy
    | \bis_pro\b | \bpro_tier\b | \bmeter(?:ed|ing)
    | quota_remaining | trial_expir
    """,
    re.I | re.X,
)

STRING_OR_COMMENT = re.compile(
    r"""[rbfu]{0,2}"[^"\n]*"? | [rbfu]{0,2}'[^'\n]*'? | \#.*$""",
    re.I | re.X,
)


def _executable_part(line: str) -> str:
    """Drop string literals and comments.

    A Varyz Pro surface would be code — an attribute read, a class name, a call.
    Prose is not a surface, and Varyz legitimately writes these words in prose:
    ``secrets_scan.py`` carries a *detector* for leaked Stripe keys, which is
    privacy machinery and the opposite of billing, and the MCP tool descriptions
    use "migrate the billing database" as an example of a well-scoped sentence.
    Matching those would teach the next person to delete this test.
    """
    return STRING_OR_COMMENT.sub(" ", line)


def test_the_local_source_contains_no_pro_service_surface() -> None:
    """Local capabilities have no entitlement checks or billing stubs.

    The local engine's honesty claim is that unbuilt capabilities are absent rather
    than locked. A single entitlement check would make that sentence false, so the
    absence is asserted instead of promised.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        # The optional hosted service bills for hosting. It must not introduce
        # payment checks into the local engine's free capabilities.
        if path.is_relative_to(ROOT / "src" / "varyz" / "cloud"):
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if PRO_SURFACE.search(_executable_part(line)):
                relative = path.relative_to(ROOT).as_posix()
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        "the local source carries a paid capability gate, which the README "
        "says it does not:\n" + "\n".join(offenders)
    )


def test_the_pro_surface_scanner_catches_a_paywall_stub() -> None:
    """Guards the guard: a scanner that cannot fire proves nothing."""
    stubs = [
        "    if not user.entitlement.active:",
        "        raise PaywallError()",
        "    remaining = quota_remaining(account)",
        "    if is_pro(config):",
        "    self.licence_key = key",
    ]
    for stub in stubs:
        assert PRO_SURFACE.search(_executable_part(stub)), f"scanner missed {stub!r}"

    innocent = [
        '    SecretKind("Stripe key", re.compile(PATTERN)),',
        '    "for example, migrate the billing database",  # prose',
        "    subscription_secret_name(label)",
    ]
    for line in innocent:
        assert not PRO_SURFACE.search(_executable_part(line)), f"scanner fired on {line!r}"


def test_local_engine_does_not_import_hosted_payment_checks() -> None:
    """Hosted billing must remain separate from the local capability engine."""
    package = ROOT / "src" / "varyz"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.is_relative_to(package / "cloud"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                modules = [prefix, *(f"{prefix}.{alias.name}" for alias in node.names)]
            if any("cloud.billing" in module for module in modules):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"local engine imports hosted payment checks: {offenders}"


def test_every_local_alpha_capability_ships_in_the_public_source() -> None:
    """The Community source is the whole product, so its modules are named here.

    If a capability is ever moved out of this repository to become a paid one,
    this test fails and the move has to be argued rather than merged.
    """
    package = ROOT / "src" / "varyz"
    # One entry per capability the release plan names, so the list can be read
    # against it rather than trusted.
    required = {
        "connectors": package / "connectors",
        "archive": package / "db",
        "vault": package / "vault.py",
        "graph": package / "graph",
            "owner resolution": package / "resolution",
            "cited evidence": package / "retrieval" / "answer.py",
        "privacy gate": package / "privacy",
        "receipts": package / "privacy" / "receipts.py",
        "personal model": package / "personal_model",
        "retrieval": package / "retrieval",
        "evidence": package / "evidence",
        "deletion and export": package / "deletion.py",
        "MCP integration": package / "mcp_server.py",
        "installers": package / "install.py",
    }
    missing = sorted(name for name, path in required.items() if not path.exists())
    assert not missing, f"capabilities missing from the public source: {missing}"

    # Corrections are a capability, not a module: the promise is that the owner
    # can overrule the compiler and that the overruling survives a recompile.
    repository = (package / "personal_model" / "repository.py").read_text(encoding="utf-8")
    for method in (
        "def append_override",
        "def overrides_for_generation",
        "def record_override_replay",
    ):
        assert method in repository, f"corrections lost their store: {method}"

    # And the tests themselves ship, because a source drop nobody can verify is
    # not a reproducible one.
    suites = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    assert len(suites) >= 90, f"only {len(suites)} test modules ship"
    for suite in ("test_privacy_egress_gate.py", "test_delete.py", "test_release_copy.py"):
        assert suite in suites, f"required test module missing from the release: {suite}"


def _tracked_files() -> list[Path]:
    """Every path git would publish, asked of git rather than guessed.

    Walking the tree instead would scan `data/`, `.venv/` and the run logs —
    none of which is published, all of which is full of exactly the material
    this test looks for, so the test would be permanently red and quickly
    deleted. What ships is what git tracks.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [ROOT / name for name in listing.stdout.split(chr(0)) if name]


# Which of the scanner's kinds mean "somebody issued this credential". Each is a
# format only one system mints — a prefix, a PEM header, a JWT's three segments
# — so a hit in source is a leak, full stop.
#
# The kinds left out are the scanner's contextual ones: `password` (fires on
# `api_key: str` and "Create an app password"), `payment card number` (a Luhn
# check, and the run id 20260830-015232 is fifteen digits that pass it),
# `credentials in a URL`, and the normalization flag that inherits both. They
# earn their keep on arbitrary prose leaving the machine, where a false positive
# costs one redaction. Here a false positive costs the whole gate, because a
# release test that is red on every branch gets deleted rather than read.
ISSUED_CREDENTIAL_KINDS = frozenset(
    {
        "private key",
        "Anthropic API key",
        "OpenAI API key",
        "GitHub token",
        "GitHub fine-grained token",
        "Slack token",
        "Google API key",
        "AWS access key id",
        "Stripe key",
        "obfuscated API key",
        "JSON web token",
    }
)


def _is_a_placeholder(value: str) -> bool:
    """Is this a test fixture rather than a credential somebody was issued?

    Fixtures in this repository are written the way everyone writes them —
    `"sk-ant-api03-" + "Q" * 30` — and the tell is entropy, not naming. A real
    key drawn from a random alphabet does not repeat four characters for its
    whole length; the odds against it are the same odds that make the key a key.

    Deciding this by filename would have been the easy version and the wrong
    one: `tests/` is exactly where a real key gets pasted to reproduce a bug.
    """
    issuer = r"^(?:sk-ant-|sk-proj-|sk-|gh[pousr]_|github_pat_|xox[abprs]-|AIza|AKIA|eyJ)"
    body = re.sub(issuer, "", value)
    body = re.sub(r"^(?:api03-|live_|test_)", "", body)
    # The synthetic eval archives spell theirs in words (`sk-staging-deploy-...`):
    # an issued key is drawn from a mixed-case alphabet no lowercase phrase fits.
    if re.fullmatch(r"[a-z]+(?:-[a-z0-9]+){2,}", body):
        return True
    return len(set(re.sub(r"[^A-Za-z0-9]", "", body))) <= 4


def test_no_tracked_file_carries_a_credential() -> None:
    """The last gate before the source becomes public and stays public.

    Varyz already knows what a credential looks like: the egress broker scans
    every payload before it leaves the machine. The repository itself was never
    scanned, which is the one leak that cannot be undone afterwards — a pushed
    key is compromised whether or not the commit is reverted.

    So this reuses `secrets_scan` rather than restating it, and one definition of
    "issued credential" covers both directions.
    """
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # Binary or unreadable: nothing to scan, nothing to leak.
        for finding in secrets_scan.find(text):
            if finding.kind not in ISSUED_CREDENTIAL_KINDS:
                continue
            if _is_a_placeholder(text[finding.start : finding.end]):
                continue
            line = text.count("\n", 0, finding.start) + 1
            relative = path.relative_to(ROOT).as_posix()
            # The kind and the place, never the value.
            offenders.append(f"{relative}:{line}: {finding.kind}")
    assert not offenders, (
        "tracked files carry credentials, and pushing them makes those "
        "credentials public permanently:\n" + "\n".join(sorted(set(offenders)))
    )


def test_the_credential_gate_would_catch_a_pushed_key() -> None:
    """Guards the guard: a scan that never fires proves nothing about the tree.

    Both halves matter. The planted keys must be caught, and the ordinary source
    lines must not be — the second half is what stopped this gate from shipping
    with `password` in it, which fires on `api_key: str` in `cli.py`.
    """
    planted = {
        "Anthropic API key": "ANTHROPIC_API_KEY=sk-ant-" + "a" * 40,
        "GitHub token": "ghp_" + "b" * 36,
        "AWS access key id": "AKIA" + "C" * 16,
        "Stripe key": "sk_live_" + "d" * 24,
        "JSON web token": "eyJ" + "a" * 20 + "." + "a" * 20 + "." + "a" * 20,
    }
    for expected, text in planted.items():
        kinds = {f.kind for f in secrets_scan.find(text)}
        assert expected in kinds & ISSUED_CREDENTIAL_KINDS, f"{expected} slipped past"

    innocent = [
        "api_key = os.environ['ANTHROPIC_API_KEY']",
        "    2. Create an app password: https://example.invalid/apppasswords",
        "run directory: data/runs/compile-20260830-015232",
        "def connect_claude(api_key: str) -> None:",
    ]
    for line in innocent:
        fired = {f.kind for f in secrets_scan.find(line)} & ISSUED_CREDENTIAL_KINDS
        assert not fired, f"the gate fired on ordinary source: {line!r} -> {fired}"

    # And a fixture is not a leak, or every test module that needs a fake key
    # would have to hide it from the gate.
    assert _is_a_placeholder("sk-ant-api03-" + "Q" * 30)
    assert _is_a_placeholder("sk-" + "-".join(("staging", "deploy", "example")))
    assert not _is_a_placeholder("sk-" + "-".join(("x7Kd2fLp", "9Qw3ZmR8", "vT1nB6hJ")))
    # Written without its issuer prefix on purpose: a key-shaped literal with real
    # entropy in a tracked file is the exact thing this gate exists to stop, and
    # the entropy is the whole of what is being asserted.
    assert not _is_a_placeholder("x7Kd2fLp9Qw3ZmR8vT1nB6hJ4sY0gC5e")


# --- every command the documentation advertises ------------------------------

_DOC_COMMAND = re.compile(r"^(?:uv run )?varyz ")


def _documented_commands() -> dict[str, set[str]]:
    """Every `varyz ...` line inside a fenced block in the README or docs/.

    Fences are tracked by toggling on each fence line rather than by a regex
    pairing an opening with a closing. The regex version of this shipped for an
    hour and silently found nothing in the last third of any file containing a
    block tagged with a language it did not list: the unmatched opening made it
    pair each real closing fence with the next opening one, so it scanned the
    prose and skipped the commands. A gate reading the wrong half of the file
    passes for the same reason an empty one does, and this one did.
    """

    found: dict[str, set[str]] = {}
    for path in [README, *sorted((ROOT / "docs").glob("*.md"))]:
        inside = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.lstrip().startswith("```"):
                inside = not inside
                continue
            if not inside:
                continue
            # Trailing `# ...` in these blocks is prose about the command.
            line = raw.split("#")[0].strip()
            if _DOC_COMMAND.match(line):
                found.setdefault(line, set()).add(path.name)
    return found


def _resolve(line: str) -> tuple[object | None, str]:
    """Walk the real CLI for the words in `line`; return (command, complaint)."""

    import click
    from typer.main import get_command

    from varyz.cli import app

    words = line.replace("uv run varyz", "varyz", 1).split()[1:]
    current: object = get_command(app)
    for index, word in enumerate(words):
        if word.startswith("-"):
            break
        # A group resolves the next word; anything else takes it as an argument.
        if not hasattr(current, "get_command"):
            break
        following = current.get_command(click.Context(current), word)  # type: ignore[attr-defined]
        if following is None:
            if index == 0:
                return None, f"no such command: {word}"
            # Later words may be arguments to a command that does exist.
            break
        current = following
    return current, ""


def test_every_command_the_documentation_advertises_exists() -> None:
    """A command in the README is an instruction, and it has to run.

    The README shipped `scripts/evaluate_agent_ab.py evals/agent_ab_local.jsonl`
    for four weeks. The harness accepts two task sets, one per authorized
    project, and rejects every other path by name, so the line exited 2 for
    everyone who tried it — including its author, who never did. Nothing caught
    it because nothing was looking. This looks.
    """

    documented = _documented_commands()
    assert documented, "no commands found; the extractor stopped matching"

    broken: list[str] = []
    for line, where in sorted(documented.items()):
        command, complaint = _resolve(line)
        if command is None:
            broken.append(f"{', '.join(sorted(where))}: {line} -> {complaint}")
            continue
        known = {
            option
            for param in command.params  # type: ignore[attr-defined]
            for option in [*param.opts, *param.secondary_opts]
        }
        unknown = [
            token.split("=")[0]
            for token in line.split()
            if token.startswith("-") and token.split("=")[0] not in known
        ]
        if unknown:
            broken.append(f"{', '.join(sorted(where))}: {line} -> unknown {unknown}")

    assert not broken, "the documentation advertises commands that do not run:\n" + "\n".join(
        broken
    )


def test_the_command_gate_would_catch_an_invented_command() -> None:
    """Guards the guard: a resolver that accepts anything proves nothing."""

    missing, complaint = _resolve("uv run varyz teleport")
    assert missing is None and "teleport" in complaint

    real, _ = _resolve("uv run varyz setup")
    assert real is not None
    options = {o for p in real.params for o in [*p.opts, *p.secondary_opts]}  # type: ignore[attr-defined]
    assert "--dry-run" in options and "--no-autosync" in options
    assert "--warp-drive" not in options
