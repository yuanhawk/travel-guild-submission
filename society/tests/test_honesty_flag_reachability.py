"""
test_honesty_flag_reachability.py — CI safety net for the "orphaned honesty flag" bug
class.

Trigger case: ``name_needs_translation`` (stamped by day_planner_agent.py's
``_stamp_attraction``/``_stamp_meal`` to mark a POI's ``display_name`` as non-Latin /
needing translation) was, for 3+ days, the ONLY honesty/assumption flag in this codebase
with zero downstream consumers. It was SET correctly, but nothing ever read it to
actually surface a translated name to a user — so a commit message claiming "so English
users never see local script" was false the whole time. It only stopped mattering
(without ever being fixed) when an unrelated change started populating ``name_en`` with
real values, which made ``_display_name`` rarely fall through to the untranslated branch.

Every OTHER honesty/assumption flag in the codebase (assumed_*, ignored_*, dropped_*) IS
wired to a real consumer — almost always ``attach_assumption_notes`` in
``society/utils/intent_parser.py``, which turns the flag into a user-facing disclosure
note. That function's own docstring is effectively the existing "manifest" of wired
flags; this test independently re-derives (does not just trust) that manifest by
statically scanning the source.

What this test does, mechanically:

  1. AST-scans society/agents, society/utils, society/orchestration (source only — test
     files are excluded) for any dict-key / attribute ASSIGNMENT whose name matches the
     project's honesty/assumption-flag naming convention (``assumed_*``, ``ignored_*``,
     ``dropped_*``, ``*_needs_translation``, ``*_needs_*``, ``*_romanized``).
  2. Fails if any such flag is found that is NOT in the hand-maintained ALLOWLIST below
     — forcing whoever adds a new flag of this shape to explicitly allowlist it (and, via
     code review, justify that it has a real consumer) instead of silently shipping an
     orphan.
  3. For every ALLOWLIST entry marked "consumed", independently re-verifies (via the same
     AST scan, not a hardcoded claim) that the flag is READ somewhere other than its own
     definition site — i.e. a different (function, source-expression) pair than every
     (function, target-expression) pair that SETS it. This is what would have caught the
     name_needs_translation gap: it was set in two places and read in exactly zero.
  4. Entries marked "known_gap" are flags this test discovered DO NOT currently have a
     real consumer. They are not silently dropped from the scan (that would just recreate
     the bug this test exists to prevent) and are not fixed here (fixing them is a
     production-code change, out of scope for this CI-infra-only PR) — they are
     xfail(strict=True)'d, so the day a real consumer is wired up, this test starts
     FAILING (XPASS) until a human moves the entry to "consumed". That flip is the
     intended forcing function, not a bug in this test.

See the PR/report for the full audit trail of how each entry was verified.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Where to look. Deliberately matches the task's scope: source packages only,
# never tests/ (a flag "consumed" only by a unit test is exactly the orphan
# this test exists to catch — test-only usage must not count as a consumer).
# ---------------------------------------------------------------------------
_SOCIETY_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_ROOTS = [
    _SOCIETY_ROOT / "agents",
    _SOCIETY_ROOT / "utils",
    _SOCIETY_ROOT / "orchestration",
]
_EXCLUDE_DIR_NAMES = {"tests", "__pycache__"}

# ---------------------------------------------------------------------------
# Naming convention for a "honesty / assumption / degradation" flag, per the
# existing usages documented in attach_assumption_notes's docstring plus the
# day_planner_agent translation-honesty flags.
#
# Judgment call: keys ending in "_note" (date_assumption_note, children_note,
# dropped_legs_note, dropped_vibes_note, ...) are EXCLUDED even though some
# (dropped_legs_note, dropped_vibes_note) start with "dropped_". A "_note" key
# is the user-facing disclosure TEXT that a flag's consumer produces — it's the
# output of "consumption", not a separate flag that itself needs a consumer.
# Treating it as one would force a nonsensical "who consumes the consumer's own
# output" check. See report for the full reasoning.
# ---------------------------------------------------------------------------
_FLAG_NAME_RE = re.compile(
    r"^(assumed_[a-z0-9_]+"
    r"|ignored_[a-z0-9_]+"
    r"|dropped_[a-z0-9_]+"
    r"|[a-z0-9_]*_needs_translation"
    r"|[a-z0-9_]*_needs_[a-z0-9_]+"
    r"|[a-z0-9_]*_romanized)$"
)
_NOTE_SUFFIX_RE = re.compile(r"_note$")


def _is_flag_name(name: str) -> bool:
    return bool(_FLAG_NAME_RE.match(name)) and not _NOTE_SUFFIX_RE.search(name)


# ---------------------------------------------------------------------------
# The allowlist. Every name below was independently verified (see module
# docstring / PR report) at the time this test was added. Anything NOT listed
# here that the scan finds in source is a new, unreviewed honesty flag and
# fails test_no_undocumented_honesty_flags_in_source.
# ---------------------------------------------------------------------------

# flag -> human-readable pointer to its real (non-test, non-definition-site)
# consumer, for the benefit of anyone reading a failure message. The pointer
# text itself is NOT trusted by the test — _has_real_consumer() re-derives
# reachability from the AST scan every run.
CONSUMED_HONESTY_FLAGS = {
    "assumed_adults": "attach_assumption_notes -> adults_assumption_note",
    "assumed_currency": "attach_assumption_notes -> currency_assumption_note",
    "assumed_date_year": "attach_assumption_notes -> date_year_assumption_note",
    "assumed_start_date": "attach_assumption_notes -> date_assumption_note",
    "assumed_start_date_season_hint": (
        "attach_assumption_notes -> words date_assumption_note "
        "(season/holiday phrasing vs. flat 'no dates given')"
    ),
    "assumed_budget_from_tier": "attach_assumption_notes -> budget_tier_assumption_note",
    "assumed_budget_per_person": "attach_assumption_notes -> budget_per_person_assumption_note",
    # Not in the task's given list — found by the AST scan while building this test.
    # Consumed by negotiate_from_text's budget-estimate reason enrichment (the
    # needs_clarification/BUDGET-slot path), NOT by attach_assumption_notes.
    "assumed_nights": (
        "negotiate_from_text -> '_assume_note' prepended to req['reason'] "
        "(budget-estimate-guidance enrichment path)"
    ),
    "ignored_children": "attach_assumption_notes -> children_note",
    "ignored_children_is_plural_estimate": (
        "attach_assumption_notes -> words children_note (plural 'the kids' vs. a count)"
    ),
    "dropped_legs": "attach_assumption_notes -> dropped_legs_note",
    "dropped_vibes": "attach_assumption_notes -> dropped_vibes_note",
}

# Flags this test's own scan VERIFIED still have zero real (non-test) consumer as of
# this writing. See module docstring for why they're listed (not silently dropped)
# and xfail'd (not fixed) here.
KNOWN_GAP_HONESTY_FLAGS = {
    # FINDING (see PR report): the task that added this test assumed a "Japan
    # romanization fix" had already wired name_needs_translation (and introduced a
    # name_romanized) to a real consumer. Independently checked: false. As of this
    # commit, name_needs_translation is set in day_planner_agent.py's
    # _stamp_attraction/_stamp_meal and read ONLY from society/tests/test_day_planner_agent.py
    # and society/tests/test_plan_realism.py — i.e. it is STILL exactly the orphaned
    # flag this whole test suite exists to catch. `name_romanized` does not exist
    # anywhere in source at all (not even a definition site) — there is nothing to
    # allowlist for it yet.
    "name_needs_translation": (
        "NONE FOUND as of this writing (verified: reads exist only in test files). "
        "xfail(strict=True) below: flips to a hard failure the moment a real "
        "consumer appears, so a human must promote this entry to CONSUMED."
    ),
}

ALL_ALLOWLISTED_FLAGS = set(CONSUMED_HONESTY_FLAGS) | set(KNOWN_GAP_HONESTY_FLAGS)


# ---------------------------------------------------------------------------
# AST scan
# ---------------------------------------------------------------------------


def _iter_source_files():
    for root in _SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIR_NAMES]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield Path(dirpath) / fn


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive only
        return f"<unparseable:{id(node)}>"


def _build_parent_map(tree: ast.AST) -> dict:
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict) -> str:
    n = node
    while n in parents:
        n = parents[n]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n.name
    return "<module>"


class _Occurrence:
    __slots__ = ("path", "lineno", "func", "expr")

    def __init__(self, path, lineno, func, expr):
        self.path = path
        self.lineno = lineno
        self.func = func
        self.expr = expr

    def __repr__(self):
        return f"{self.path}:{self.lineno} in {self.func}() [{self.expr}]"


def _scan_file(path: Path, writes: dict, reads: dict) -> None:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return
    parents = _build_parent_map(tree)

    def _add(bucket, name, node, expr):
        func = _enclosing_function(node, parents)
        bucket.setdefault(name, []).append(_Occurrence(path, node.lineno, func, expr))

    for node in ast.walk(tree):
        # --- writes: d[key] = ...  /  obj.attr = ... ---
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript):
                    key = _const_str(tgt.slice)
                    if key and _is_flag_name(key):
                        _add(writes, key, node, _unparse(tgt.value))
                elif isinstance(tgt, ast.Attribute):
                    if _is_flag_name(tgt.attr):
                        _add(writes, tgt.attr, node, _unparse(tgt.value))

        # --- writes: {"key": ...} dict literals (e.g. built-and-returned request dicts) ---
        if isinstance(node, ast.Dict):
            for k in node.keys:
                key = _const_str(k) if k is not None else None
                if key and _is_flag_name(key):
                    # A dict literal isn't "writing into" a pre-existing named
                    # object -- give it a unique expr so it never spuriously
                    # matches (and thus suppresses) a real read elsewhere.
                    _add(writes, key, node, f"<dict-literal@{node.lineno}>")

        # --- writes: d.setdefault("key", ...) ---
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and node.args
        ):
            key = _const_str(node.args[0])
            if key and _is_flag_name(key):
                _add(writes, key, node, _unparse(node.func.value))

        # --- reads: d.get("key") ---
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            key = _const_str(node.args[0])
            if key and _is_flag_name(key):
                _add(reads, key, node, _unparse(node.func.value))

        # --- reads: d["key"] in a Load context ---
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            key = _const_str(node.slice)
            if key and _is_flag_name(key):
                _add(reads, key, node, _unparse(node.value))

        # --- reads: obj.attr in a Load context ---
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if _is_flag_name(node.attr):
                _add(reads, node.attr, node, _unparse(node.value))


def _scan_codebase():
    writes: dict = {}
    reads: dict = {}
    for path in _iter_source_files():
        _scan_file(path, writes, reads)
    return writes, reads


def _has_real_consumer(flag: str, writes: dict, reads: dict) -> bool:
    """True if `flag` is read somewhere that isn't just its own (function, target-expr)
    write site re-reading itself. This is deliberately conservative: it does not require
    a DIFFERENT function (attach_assumption_notes legitimately reads req[...] and writes
    result[...] in the same function -- that's a real consumer, not self-reference), but
    it does require a different target expression than any write in the same function, so
    a trivial `d[flag] = x(); y = d.get(flag)` on the SAME variable in the SAME function
    does not count as proof of a real downstream consumer.
    """
    write_sites = {(occ.func, occ.expr) for occ in writes.get(flag, [])}
    for occ in reads.get(flag, []):
        if (occ.func, occ.expr) not in write_sites:
            return True
    return False


@pytest.fixture(scope="module")
def scanned_codebase():
    return _scan_codebase()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_undocumented_honesty_flags_in_source(scanned_codebase):
    """Every assumed_*/ignored_*/dropped_*/*_needs_*/*_romanized flag SET anywhere in
    society/agents, society/utils, or society/orchestration must be explicitly
    allowlisted here. An unlisted match means someone added a new honesty/assumption
    flag without this test (and, by review process, a human) confirming it has a real
    consumer -- exactly how name_needs_translation shipped unread for 3+ days.
    """
    writes, _reads = scanned_codebase
    found_flags = set(writes)
    undocumented = found_flags - ALL_ALLOWLISTED_FLAGS
    assert not undocumented, (
        "Found honesty/assumption-style flag(s) set in source that are NOT in "
        "CONSUMED_HONESTY_FLAGS or KNOWN_GAP_HONESTY_FLAGS in "
        "test_honesty_flag_reachability.py:\n"
        + "\n".join(
            f"  {flag}: " + ", ".join(str(o) for o in writes[flag]) for flag in sorted(undocumented)
        )
        + "\n\nAdd the flag to one of the allowlists above, and only mark it "
        "'consumed' once you've confirmed a real (non-test) reader exists."
    )


def test_allowlist_has_no_stale_entries(scanned_codebase):
    """Catches allowlist rot: an entry naming a flag that no longer appears anywhere
    in source (e.g. renamed/removed in a refactor) should be deleted from the
    allowlist, not left behind as dead documentation.
    """
    writes, _reads = scanned_codebase
    found_flags = set(writes)
    stale = ALL_ALLOWLISTED_FLAGS - found_flags
    assert not stale, (
        "Allowlisted flag(s) no longer found anywhere in "
        "society/agents|utils|orchestration source -- remove from the allowlist "
        f"in test_honesty_flag_reachability.py: {sorted(stale)}"
    )


@pytest.mark.parametrize("flag", sorted(CONSUMED_HONESTY_FLAGS))
def test_consumed_flag_has_a_real_consumer(flag, scanned_codebase):
    """For each flag claimed 'consumed', independently re-derive (not just trust the
    allowlist's pointer comment) that it's read somewhere other than its own
    write/definition site.
    """
    writes, reads = scanned_codebase
    ok = _has_real_consumer(flag, writes, reads)
    detail = (
        f"\n  writes: {[str(o) for o in writes.get(flag, [])]}"
        f"\n  reads:  {[str(o) for o in reads.get(flag, [])]}"
    )
    assert ok, (
        f"'{flag}' is allowlisted as CONSUMED in test_honesty_flag_reachability.py "
        f"(pointer: {CONSUMED_HONESTY_FLAGS[flag]!r}) but no real consumer was found "
        f"by the AST scan -- this is exactly the name_needs_translation bug shape. "
        f"Either the consumer was removed (regression) or the allowlist entry/pointer "
        f"is wrong." + detail
    )


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param(
            flag,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "Known, tracked gap (see KNOWN_GAP_HONESTY_FLAGS docstring): this "
                    "flag genuinely has no real consumer yet. strict=True means this "
                    "flips to a hard CI failure (XPASS) the moment a consumer is wired "
                    "up -- at which point promote the entry to CONSUMED_HONESTY_FLAGS "
                    "instead of leaving it here."
                ),
            ),
        )
        for flag in sorted(KNOWN_GAP_HONESTY_FLAGS)
    ],
)
def test_known_gap_flag_is_still_unconsumed(flag, scanned_codebase):
    writes, reads = scanned_codebase
    assert _has_real_consumer(flag, writes, reads), (
        f"'{flag}' unexpectedly HAS a real consumer now -- this assertion is "
        f"inverted on purpose (see xfail reason above); promote it to "
        f"CONSUMED_HONESTY_FLAGS."
    )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
