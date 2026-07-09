#!/usr/bin/env python3
"""scan_infra_leaks.py — CI/pre-commit gate for infra-identifier leaks that
gitleaks' credential-shaped ruleset does not catch.

CONTEXT
-------
A security review found that docs/alicloud-deployment-proof.png (an AliCloud
ECS console screenshot merged into this public repo) exposed a real public
IP address, private IP address, and VPC/vSwitch resource IDs — none of which
are "secrets" in the API-key/token sense gitleaks scans for, so gitleaks
(already wired into CI + pre-commit) never flagged it. This script closes
that specific gap:

  1. TEXT SCAN — every git-tracked text file is scanned for public IPv4
     addresses and AliCloud resource-ID patterns (instance/VPC/vSwitch/
     security-group/EIP/disk IDs). This would have caught the leaked
     Instance ID that was ALSO named in ALICLOUD-PROOF.md's prose (the
     screenshot's pixel content is a separate problem — see #2).

  2. DOCS IMAGE ALLOWLIST — a screenshot's pixel content can leak an IP/
     account-id/VPC-id that no text-pattern scan or `strings` dump will ever
     catch (the actual class of leak that happened here). Automated OCR is a
     partial, easy-to-be-overconfident-in mitigation, so this gate instead
     requires an explicit HUMAN sign-off: every image file under docs/ must
     have its path + sha256 recorded in scripts/docs_image_allowlist.json.
     A new or changed docs/ image with no matching (path, hash) entry fails
     the check — forcing a human to actually look at it and add the hash
     only after confirming no secrets/IPs/account-ids are visible. This is
     the same "honest, no silent automation confidence" posture the rest of
     this codebase already uses (see e.g. fraud_agent.py, health_agent.py).

     HONEST LIMIT: the allowlist file is an ordinary repo file, editable in
     the SAME commit/PR that adds a leaked image — this script alone cannot
     stop someone (or something) from adding both the image and a matching
     allowlist entry together without a real human having looked at it. It
     is an audit-trail convention ("here is a record that a human claims to
     have reviewed this"), not a hard security boundary. Pair it with branch
     protection / required review (e.g. a CODEOWNERS entry) on
     scripts/docs_image_allowlist.json if that boundary matters to you.

SCOPE
-----
IPv4 and IPv6 public-address detection; AliCloud resource-id prefixes
actually involved in the incident this exists to prevent (i-/vpc-/vsw-/sg-/
eip-, case-insensitive). Only git-tracked files are scanned (default: all
tracked files; --staged: only staged files) — an untracked file sitting in
a working tree was never shared with anyone and is out of scope by design.

USAGE
-----
    python3 scripts/scan_infra_leaks.py [--staged] [paths...]

    (no args)   scan every git-tracked file in the repo
    --staged    scan only staged files (for a pre-commit hook)
    paths...    scan only the given paths (may be outside the repo; anything
                outside REPO_ROOT is scanned for IP/resource-id leaks in text
                but is never eligible for the docs/-image allowlist gate,
                which only makes sense for a path actually inside docs/)

Exit code 0 = clean, 1 = findings (printed to stderr with file:line).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = Path(__file__).resolve().parent / "docs_image_allowlist.json"

# Only these extensions are treated as text and scanned for IPs/resource ids.
# Deliberately excludes binary formats (images, fonts, etc.) — those go
# through the docs-image-allowlist gate instead, not a text/regex scan.
_TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".ts", ".tsx", ".js", ".mjs", ".svelte", ".json",
    ".yml", ".yaml", ".toml", ".go", ".html", ".sh", ".cfg", ".ini",
}

# Images under this prefix get the human-reviewed allowlist gate (#2 above).
# Scoped to docs/ deliberately: web/src/lib/assets/destinations/*.jpg are
# pre-sourced decorative stock photography (see DATA-ATTRIBUTIONS.md), not
# operational screenshots — the leak risk this gate exists for (a console/
# terminal capture showing real infra) lives specifically under docs/.
# Compared case-INsensitively (see _is_docs_image_path) — default macOS/
# Windows filesystems are case-insensitive, so "DOCS/x.png" and "docs/x.png"
# are the same real directory and must get the same gate.
_DOCS_IMAGE_PREFIX = "docs/"
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _is_docs_image_path(rel_posix: str, suffix_lower: str) -> bool:
    return rel_posix.lower().startswith(_DOCS_IMAGE_PREFIX) and suffix_lower in _IMAGE_EXTENSIONS


# AliCloud resource-id prefixes (Elastic Compute Service instance, VPC,
# vSwitch, security group, elastic IP) — scoped to the prefixes actually
# involved in the incident this script exists to prevent, plus a couple of
# close cousins. Deliberately narrow: real AliCloud resource ids are long,
# effectively-random alphanumeric strings (e.g. "i-t4n44gbij7622v7ecow4",
# 21 chars after the prefix), so requiring >=14 chars AND at least one digit
# in the suffix rules out short, dictionary-word hyphenated identifiers that
# an early version of this pattern false-positived on (e.g. a CSS-ish
# "lb-overlay", or a catalog id like "sg-marina-sample" where "sg" means
# "Singapore", not "security group"). Other resource types (disk, RDS, NAS,
# generic load balancers) were left out rather than guessed at, to avoid
# reintroducing that same noise — extend deliberately if a real need shows up.
# re.IGNORECASE: AliCloud ids are conventionally lowercase, but a value
# transcribed from a screenshot or pasted into prose could easily pick up
# stray uppercasing — that must not be a silent bypass.
_ALICLOUD_RESOURCE_RE = re.compile(
    r"\b(?:i|vpc|vsw|sg|eip)-(?=[0-9a-zA-Z]*\d)[0-9a-zA-Z]{14,}\b",
    re.IGNORECASE,
)

# RFC 5737 documentation-only ranges + loopback + "any" + RFC 6598 shared/
# CGNAT space — legitimate to appear in code/docs (examples, bind addresses,
# SSRF-guard test fixtures like ucp-merchant/rfc9421.go's cgnatCIDR) and must
# never be flagged as if they identified THIS project's infrastructure.
# Networks (not just addresses) so the same list works for both IPv4 and
# IPv6 lookups below.
_BENIGN_IP_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 CGNAT/shared address
    # space. Notably this range ALSO covers 100.100.100.200 — AliCloud's own
    # publicly-documented metadata-service IP (their equivalent of AWS/GCP's
    # 169.254.169.254), named in ucp-merchant/rfc9421.go's SSRF-guard comment
    # — so that specific address doesn't need (and never had) its own entry
    # in _KNOWN_BENIGN_IPS below; this one CIDR rule already covers it.
    ipaddress.ip_network("2001:db8::/32"),  # RFC 3849 IPv6 documentation range
]
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Heuristic IPv6 literal matcher (full or compressed "::" form). Each group
# is [0-9a-fA-F]{0,4} (not {1,4}) specifically so an empty group can appear
# on either side of a "::" compression — e.g. "2606:4700:4700::1111" needs an
# empty-hex + ":" repetition to match through the "::" at all; requiring at
# least one hex digit per group would stop matching right before it.
# Deliberately permissive — over-matching here just means a candidate string
# that fails ipaddress.ip_address() parsing, which is treated as benign (see
# _is_benign_ip), not a false leak report.
_IPV6_RE = re.compile(
    r"(?<![0-9a-fA-F:.])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![0-9a-fA-F:.])"
)

# Specific, individually-justified well-known public IPs that are safe to
# mention because they are documented, third-party, or provider-published
# addresses — never THIS project's own infra. Mirrors .gitleaks.toml's
# allowlist convention (specific value + a written reason), rather than
# trying to make the regex "smart" about intent.
_KNOWN_BENIGN_IPS = {
    "8.8.8.8": "Google Public DNS — used in ucp-merchant/rfc9421_test.go as a "
               "'this is definitely not a private IP' SSRF-guard test fixture.",
    "100.63.255.255": "ucp-merchant/adversarial_money_test.go boundary-of-CGNAT-"
                       "range test fixture (just outside 100.64.0.0/10, asserting "
                       "isBlockedIP does NOT over-block) — not real infra.",
    "100.128.0.0": "ucp-merchant/adversarial_money_test.go boundary-of-CGNAT-"
                    "range test fixture (just outside 100.64.0.0/10) — not real infra.",
}


def _normalize_ip_candidate(candidate: str) -> str:
    """Strip leading zeros from each IPv4 octet (e.g. "047.098.123.045" ->
    "47.98.123.45") before validation. Python's ipaddress module rejects
    zero-padded octets outright (ValueError), which _is_benign_ip would
    otherwise silently treat as "not a real IP, therefore benign" — exactly
    backwards for a value a human might zero-pad by accident when
    transcribing a real address. A no-op for IPv6 candidates (no '.')."""
    if "." not in candidate or ":" in candidate:
        return candidate
    parts = candidate.split(".")
    normalized = []
    for part in parts:
        stripped = part.lstrip("0")
        normalized.append(stripped if stripped else "0")
    return ".".join(normalized)


def _is_benign_ip(candidate: str) -> bool:
    """True iff `candidate` is not a real, potentially-sensitive public IP:
    private (RFC 1918), loopback, documentation-only, or not a valid IP
    address at all (e.g. a version string or timestamp that happens to match
    the digit-dot-digit shape)."""
    if candidate in _KNOWN_BENIGN_IPS:
        return True
    try:
        ip = ipaddress.ip_address(_normalize_ip_candidate(candidate))
    except ValueError:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    return any(ip in net for net in _BENIGN_IP_NETWORKS)


def find_text_leaks(path: Path, text: str) -> list[str]:
    """Return a list of 'path:line: reason' strings for every suspicious
    IP/resource-id occurrence in `text`. Pure function — no filesystem/git
    access — so it's directly unit-testable."""
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _IPV4_RE.finditer(line):
            candidate = m.group(0)
            if not _is_benign_ip(candidate):
                findings.append(f"{path}:{lineno}: public IP address {candidate!r}")
        for m in _IPV6_RE.finditer(line):
            candidate = m.group(0)
            if not _is_benign_ip(candidate):
                findings.append(f"{path}:{lineno}: public IPv6 address {candidate!r}")
        for m in _ALICLOUD_RESOURCE_RE.finditer(line):
            findings.append(f"{path}:{lineno}: AliCloud resource id {m.group(0)!r}")
    return findings


def _load_allowlist() -> dict[str, str]:
    if not ALLOWLIST_PATH.exists():
        return {}
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def check_docs_image(rel: str, abspath: Path, allowlist: dict[str, str]) -> str | None:
    """Return a failure reason string, or None if `rel` (a docs/ image's
    repo-relative path) is covered by a matching (path, sha256) entry in the
    allowlist. `abspath` is used only to read the file's bytes."""
    digest = hashlib.sha256(abspath.read_bytes()).hexdigest()
    expected = allowlist.get(rel)
    if expected is None:
        return (
            f"{rel}: new/unreviewed image under docs/ — a human must visually "
            f"inspect it for leaked IPs/account-ids/VPC-ids/secrets, then add "
            f'"{rel}": "{digest}" to {ALLOWLIST_PATH.relative_to(REPO_ROOT)}'
        )
    if expected != digest:
        return (
            f"{rel}: image content changed since it was last reviewed "
            f"(allowlisted sha256 {expected[:12]}... != current {digest[:12]}...) — "
            f"a human must re-review it and update the allowlist entry"
        )
    return None


def _current_git_root() -> Path:
    """The git repo containing the CURRENT working directory — deliberately
    NOT hardcoded to REPO_ROOT (this script's own location). A tool that
    only ever scanned the repo it happens to live in would be untestable
    against fixture repos and would misbehave if ever copied into another
    project or invoked from a git worktree. Falls back to REPO_ROOT if the
    cwd isn't inside a git repo at all (e.g. a throwaway sandbox)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path.cwd(), capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return REPO_ROOT


def _git_tracked_files(only_staged: bool) -> list[Path]:
    root = _current_git_root()
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if only_staged \
        else ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True)
    return [root / p for p in out.stdout.splitlines() if p.strip()]


def scan(paths: list[Path], root: Path = REPO_ROOT) -> list[str]:
    """`root` is the git repo root to resolve paths relative to for the
    docs/-image-prefix check (defaults to REPO_ROOT, i.e. this script's own
    repo, but callers scanning a different repo — e.g. a test fixture, or
    this script invoked with a different cwd — should pass that repo's own
    root; see _current_git_root())."""
    allowlist = _load_allowlist()
    findings: list[str] = []
    for abspath in paths:
        if not abspath.is_file():
            continue
        # A path outside `root` still gets text-scanned (using an absolute-
        # path label instead of crashing on relative_to()), but is never
        # eligible for the docs/-image allowlist gate, which is repo-
        # relative by definition.
        try:
            rel_path = abspath.relative_to(root)
            rel_posix = rel_path.as_posix()
            inside_root = True
        except ValueError:
            rel_path = abspath
            rel_posix = abspath.as_posix()
            inside_root = False
        suffix = abspath.suffix.lower()
        if inside_root and _is_docs_image_path(rel_posix, suffix):
            reason = check_docs_image(rel_posix, abspath, allowlist)
            if reason:
                findings.append(reason)
            continue
        if suffix not in _TEXT_EXTENSIONS:
            continue
        try:
            text = abspath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(find_text_leaks(rel_path, text))
    return findings


def main(argv: list[str]) -> int:
    only_staged = "--staged" in argv
    explicit_paths = [Path(a) for a in argv if not a.startswith("--")]
    root = _current_git_root()
    if explicit_paths:
        # Relative paths are relative to the CWD (standard CLI convention —
        # matches how git/gitleaks/any normal tool resolves a path argument),
        # not to this script's own on-disk location.
        targets = [p if p.is_absolute() else Path.cwd() / p for p in explicit_paths]
    else:
        targets = _git_tracked_files(only_staged)

    findings = scan(targets, root=root)
    if findings:
        print("scan_infra_leaks: found potential infra-identifier leak(s):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"scan_infra_leaks: clean ({len(targets)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
