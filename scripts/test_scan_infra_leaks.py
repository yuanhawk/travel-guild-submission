"""test_scan_infra_leaks.py — unit + e2e tests for scripts/scan_infra_leaks.py.

Run with: python3 -m pytest scripts/test_scan_infra_leaks.py -v
(or via the mini_pytest-style runner used elsewhere in this repo if pytest
isn't installed — the tests below use only bare asserts + pytest.raises-free
patterns so they don't depend on advanced pytest features.)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scan_infra_leaks as s

REPO_ROOT = s.REPO_ROOT
SCRIPT = Path(__file__).resolve().parent / "scan_infra_leaks.py"


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — pure functions, no filesystem/git access
# ─────────────────────────────────────────────────────────────────────────────

def test_ipv4_public_address_is_flagged():
    findings = s.find_text_leaks(Path("x.md"), "server lives at 47.98.123.45\n")
    assert len(findings) == 1
    assert "47.98.123.45" in findings[0]


def test_ipv4_private_and_loopback_and_bind_addresses_are_not_flagged():
    text = "\n".join([
        "bind to 0.0.0.0",
        "loopback 127.0.0.1",
        "private 10.1.2.3",
        "private 172.16.0.5",
        "private 192.168.1.1",
        "link-local 169.254.169.254",
    ])
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_documentation_only_ip_ranges_are_not_flagged():
    text = "192.0.2.1 198.51.100.1 203.0.113.1"
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_cgnat_shared_range_is_not_flagged():
    text = "100.64.0.1 100.100.100.200 100.127.255.255"
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_known_benign_boundary_ips_are_not_flagged():
    # These are documented, individually-justified exceptions (see
    # _KNOWN_BENIGN_IPS) for real SSRF-guard test fixtures in ucp-merchant/.
    text = "8.8.8.8 100.63.255.255 100.128.0.0"
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_known_benign_ips_are_load_bearing_entries_not_dead_config():
    """Pin that every _KNOWN_BENIGN_IPS entry actually changes the outcome —
    i.e. without it, _is_benign_ip would say False (flag it). A prior audit
    found that a since-removed 4th entry (100.100.100.200) was NOT
    load-bearing: it was already covered by the 100.64.0.0/10 CGNAT network
    rule, so the dict entry was silently dead config no test could catch."""
    saved = dict(s._KNOWN_BENIGN_IPS)
    try:
        s._KNOWN_BENIGN_IPS.clear()
        for ip in saved:
            assert not s._is_benign_ip(ip), (
                f"{ip!r} is benign even with _KNOWN_BENIGN_IPS emptied — this "
                f"entry is dead config, already covered by another rule"
            )
    finally:
        s._KNOWN_BENIGN_IPS.clear()
        s._KNOWN_BENIGN_IPS.update(saved)


def test_zero_padded_public_ip_is_still_flagged():
    """Regression: ipaddress.ip_address() rejects zero-padded octets outright
    (ValueError), which used to be silently treated as 'not a real IP,
    therefore benign' — exactly backwards for a value someone zero-padded by
    accident while transcribing a real address."""
    findings = s.find_text_leaks(Path("x.md"), "047.098.123.045")
    assert len(findings) == 1


def test_malformed_non_ip_digit_patterns_are_not_flagged():
    # A version string / date that merely looks like an IPv4 shape.
    text = "release 999.1.2.3 built on 300.400.500.600"
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_ipv6_public_address_is_flagged():
    findings = s.find_text_leaks(Path("x.md"), "resolver at 2606:4700:4700::1111")
    assert len(findings) == 1
    assert "IPv6" in findings[0]


def test_ipv6_loopback_and_documentation_are_not_flagged():
    text = "::1 and 2001:db8::dead:beef"
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_ipv6_looking_timestamp_does_not_crash_or_false_positive():
    # "10:00:00" matches the permissive IPv6 regex shape but is not a valid
    # IPv6 literal — must be silently treated as benign, not crash.
    assert s.find_text_leaks(Path("x.md"), "logged at 10:00:00 UTC") == []


def test_alicloud_instance_id_is_flagged():
    findings = s.find_text_leaks(Path("x.md"), "Instance ID `i-t4n44gbij7622v7ecow4`")
    assert len(findings) == 1
    assert "i-t4n44gbij7622v7ecow4" in findings[0]


def test_alicloud_vpc_and_vswitch_ids_are_flagged():
    text = "vpc-t4nazp2emvnqs9vvmrcyh and vsw-t4njgv4o4fjryun85csyp"
    findings = s.find_text_leaks(Path("x.md"), text)
    assert len(findings) == 2


def test_alicloud_security_group_and_eip_ids_are_flagged():
    """sg-/eip- are in the same alternation as i-/vpc-/vsw- but had no
    positive test — a regex edit dropping either alternative would still
    pass the rest of the suite."""
    text = "sg-t4nabcdef1234567890x and eip-t4nabcdef1234567890x"
    findings = s.find_text_leaks(Path("x.md"), text)
    assert len(findings) == 2
    assert any("sg-t4nabcdef1234567890x" in f for f in findings)
    assert any("eip-t4nabcdef1234567890x" in f for f in findings)


def test_alicloud_resource_id_is_flagged_case_insensitively():
    """Regression: the original regex was lowercase-only, so an uppercased
    or mixed-case transcription of a real instance id silently bypassed it."""
    for variant in ("I-T4N44GBIJ7622V7ECOW4", "i-T4n44Gbij7622v7ecow4"):
        findings = s.find_text_leaks(Path("x.md"), variant)
        assert len(findings) == 1, f"{variant!r} should have been flagged"


def test_leak_on_a_later_line_reports_the_correct_line_number():
    """Every other test puts the leak on line 1 — nothing guarded against an
    off-by-one in the line-number bookkeeping (e.g. enumerate(..., start=1)
    silently becoming start=0)."""
    text = "\n".join([f"filler line {i}" for i in range(1, 5)] + [
        "Instance ID `i-t4n44gbij7622v7ecow4` is here",
    ] + [f"filler line {i}" for i in range(6, 9)])
    findings = s.find_text_leaks(Path("multi.md"), text)
    assert len(findings) == 1
    assert findings[0].startswith("multi.md:5:"), findings[0]


def test_short_dictionary_word_hyphenated_ids_are_not_flagged():
    """Regression: an early version of the resource-id pattern matched
    ordinary code identifiers like 'lb-overlay' (a CSS/UI concept) and
    catalog ids like 'sg-marina-sample' (where 'sg' means Singapore)."""
    text = "\n".join([
        "class lb-overlay is used for the lightbox",
        "hotel id sg-marina-sample in the demo catalog",
    ])
    assert s.find_text_leaks(Path("x.md"), text) == []


def test_check_docs_image_passes_with_matching_allowlist_entry(tmp_path):
    img = tmp_path / "clean.png"
    img.write_bytes(b"fake-png-bytes")
    import hashlib
    digest = hashlib.sha256(img.read_bytes()).hexdigest()
    allowlist = {"docs/clean.png": digest}
    assert s.check_docs_image("docs/clean.png", img, allowlist) is None


def test_check_docs_image_fails_when_not_in_allowlist(tmp_path):
    img = tmp_path / "new.png"
    img.write_bytes(b"unreviewed-bytes")
    reason = s.check_docs_image("docs/new.png", img, {})
    assert reason is not None
    assert "docs/new.png" in reason
    assert "human must visually" in reason


def test_check_docs_image_fails_when_content_changed_since_review(tmp_path):
    img = tmp_path / "changed.png"
    img.write_bytes(b"new-bytes-not-what-was-reviewed")
    allowlist = {"docs/changed.png": "0" * 64}  # stale/wrong hash
    reason = s.check_docs_image("docs/changed.png", img, allowlist)
    assert reason is not None
    assert "changed since it was last reviewed" in reason


def test_is_docs_image_path_is_case_insensitive_on_the_docs_prefix():
    """Regression: default macOS/Windows filesystems are case-insensitive,
    so 'DOCS/x.png' and 'docs/x.png' are the same real directory and must
    get the same allowlist gate — a case-sensitive prefix check let a
    capitalized path silently skip the gate entirely."""
    assert s._is_docs_image_path("docs/x.png", ".png") is True
    assert s._is_docs_image_path("DOCS/x.png", ".png") is True
    assert s._is_docs_image_path("Docs/X.PNG", ".png") is True
    assert s._is_docs_image_path("web/assets/x.png", ".png") is False


def test_scan_does_not_crash_on_a_path_outside_the_repo(tmp_path):
    """Regression: explicit-path mode used to call Path.relative_to(REPO_ROOT)
    unconditionally, which raises ValueError for any path outside the repo."""
    outside = tmp_path / "outside.md"
    outside.write_text("no leaks here", encoding="utf-8")
    findings = s.scan([outside])
    assert findings == []


def test_scan_still_flags_a_leak_in_a_path_outside_the_repo(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("public ip 47.98.123.45 here", encoding="utf-8")
    findings = s.scan([outside])
    assert len(findings) == 1


# ─────────────────────────────────────────────────────────────────────────────
# E2E tests — real filesystem fixtures + the actual CLI subprocess
# ─────────────────────────────────────────────────────────────────────────────

def _run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def test_e2e_cli_reports_clean_and_exit_0_on_a_clean_fixture_tree(tmp_path):
    (tmp_path / "README.md").write_text("nothing sensitive here\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    result = _run_cli([], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_e2e_cli_exits_nonzero_and_reports_leak_on_injected_instance_id(tmp_path):
    leaked = tmp_path / "NOTES.md"
    leaked.write_text("Instance ID `i-t4n44gbij7622v7ecow4` is live\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    result = _run_cli([], cwd=tmp_path)
    assert result.returncode == 1
    assert "i-t4n44gbij7622v7ecow4" in result.stderr


def test_e2e_cli_flags_a_new_unreviewed_docs_image(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "screenshot.png").write_bytes(b"totally-unreviewed-pixels")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    result = _run_cli([], cwd=tmp_path)
    assert result.returncode == 1
    assert "docs/screenshot.png" in result.stderr
    assert "human must visually" in result.stderr


def test_e2e_cli_explicit_path_mode_does_not_crash_on_a_path_outside_any_repo():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "scratch.md"
        p.write_text("nothing here", encoding="utf-8")
        result = _run_cli([str(p)])
        assert result.returncode == 0, result.stderr


def test_e2e_cli_staged_mode_is_a_noop_when_nothing_is_staged(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = _run_cli(["--staged"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_e2e_cli_staged_mode_catches_a_leak_that_is_actually_staged(tmp_path):
    """The whole reason --staged exists (a pre-commit hook gating what's
    about to be committed) — a prior version of this test only covered the
    nothing-staged no-op, so a regression in the underlying `git diff
    --cached --name-only --diff-filter=ACM` command would have gone
    completely undetected."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    leaked = tmp_path / "NOTES.md"
    leaked.write_text("Instance ID `i-t4n44gbij7622v7ecow4` is here\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)  # NOT staged: leaked
    result = _run_cli(["--staged"], cwd=tmp_path)
    assert result.returncode == 0, "unstaged leak must not be scanned in --staged mode"

    subprocess.run(["git", "add", "NOTES.md"], cwd=tmp_path, check=True)
    result = _run_cli(["--staged"], cwd=tmp_path)
    assert result.returncode == 1
    assert "i-t4n44gbij7622v7ecow4" in result.stderr


def test_e2e_cli_falls_back_cleanly_outside_any_git_repo():
    """_current_git_root()'s except branch (cwd not inside a git repo at
    all) — exercised here via a plain tempdir with no `git init`."""
    with tempfile.TemporaryDirectory() as d:
        result = _run_cli([], cwd=Path(d))
        assert result.returncode == 0, result.stderr


def test_e2e_regression_the_real_repo_is_currently_clean():
    """The whole point of this script: running it against the ACTUAL repo,
    right now, must report zero findings. This is the exact check that would
    have caught the AliCloud instance-id/screenshot leak before it merged —
    if this test ever fails, something new landed that this tool considers a
    real leak, and it must be triaged (fixed or added to
    scripts/docs_image_allowlist.json after a real human review), not
    silenced by loosening the detector."""
    result = _run_cli([], cwd=REPO_ROOT)
    assert result.returncode == 0, (
        f"scan_infra_leaks found something on the real repo:\n{result.stderr}"
    )


def test_e2e_allowlisted_docs_architecture_png_is_clean():
    """docs/architecture.png was human-reviewed (see SECURITY-ADVISORY.md /
    scripts/docs_image_allowlist.json's _comment) and confirmed to be a
    generated diagram with no sensitive content — it must pass the gate."""
    allowlist_path = Path(s.ALLOWLIST_PATH)
    allowlist = json.loads(allowlist_path.read_text(encoding="utf-8"))
    architecture_png = REPO_ROOT / "docs" / "architecture.png"
    assert architecture_png.exists(), "docs/architecture.png is expected to exist and be allowlisted"
    reason = s.check_docs_image("docs/architecture.png", architecture_png, allowlist)
    assert reason is None, reason


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
