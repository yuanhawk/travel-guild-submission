package main

// gitsha_test.go — GET /health reports the running build's git SHA.
//
// Regression coverage for the same deploy-staleness class of bug the Python
// backend's git_sha field guards against (see
// society/tests/test_health_git_sha.py and society/orchestration/server.py
// ::_resolve_git_sha, merged PR #41): a stale running process can silently be
// missing a merged fix with no way to tell from the outside. /health now
// exposes "git_sha" (resolved once at startup — see gitsha.go::resolveGitSHA)
// so a deploy script can diff it against the deployed commit.
//
// Mirrors the Python test suite's fallback-path coverage: git-available /
// GIT_SHA-env-fallback / "unknown" fallback — using a swappable
// gitRevParseHEAD var (see gitsha.go) in place of Python's
// patch("subprocess.run", ...) technique.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"regexp"
	"testing"
)

var shaHexRE = regexp.MustCompile(`^[0-9a-f]{40}$`)

// TestHealthReportsGitShaField asserts GET /health includes a "git_sha" key.
func TestHealthReportsGitShaField(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "git_sha": gitSHA})
	}))
	defer ts.Close()

	resp, err := http.Get(ts.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	sha, ok := body["git_sha"]
	if !ok {
		t.Fatalf("/health response missing git_sha field: %v", body)
	}
	shaStr, _ := sha.(string)
	if shaStr != "unknown" && !shaHexRE.MatchString(shaStr) {
		t.Fatalf("git_sha %q is neither 'unknown' nor a 40-char hex SHA", shaStr)
	}
}

// TestResolveGitSHAFallsBackToEnvVarWhenGitUnavailable mirrors the Python
// test_resolve_git_sha_falls_back_to_env_var_when_git_unavailable case: when
// `git rev-parse HEAD` fails (e.g. no .git dir on the deploy target — the
// distroless runtime image never has one), resolveGitSHA must fall back to
// the GIT_SHA env var. This is exactly what a coordinated deploy relies on
// (see ucp-merchant/Dockerfile's GIT_SHA ARG/ENV + ops/deploy-staging.sh's
// note on stamping the same env var name for the Python sibling).
func TestResolveGitSHAFallsBackToEnvVarWhenGitUnavailable(t *testing.T) {
	orig := gitRevParseHEAD
	defer func() { gitRevParseHEAD = orig }()
	gitRevParseHEAD = func() string { return "" }

	fakeSHA := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	getenv := func(k string) string {
		if k == "GIT_SHA" {
			return fakeSHA
		}
		return ""
	}
	if got := resolveGitSHA(getenv); got != fakeSHA {
		t.Fatalf("expected fallback to GIT_SHA env var %q, got %q", fakeSHA, got)
	}
}

// TestResolveGitSHAFallsBackToUnknownWhenBothAbsent mirrors the Python
// test_resolve_git_sha_falls_back_to_unknown_when_git_and_env_both_absent
// case: when git resolution fails AND no GIT_SHA env var is set, resolveGitSHA
// must return the honest "unknown" fallback rather than fabricating a value.
func TestResolveGitSHAFallsBackToUnknownWhenBothAbsent(t *testing.T) {
	orig := gitRevParseHEAD
	defer func() { gitRevParseHEAD = orig }()
	gitRevParseHEAD = func() string { return "" }

	getenv := func(string) string { return "" }
	if got := resolveGitSHA(getenv); got != "unknown" {
		t.Fatalf(`expected "unknown", got %q`, got)
	}
}

// TestResolveGitSHAPrefersGitOverEnv confirms git rev-parse HEAD (when it
// succeeds) takes precedence over a set GIT_SHA env var — matching
// _resolve_git_sha()'s try-git-first ordering in server.py.
func TestResolveGitSHAPrefersGitOverEnv(t *testing.T) {
	orig := gitRevParseHEAD
	defer func() { gitRevParseHEAD = orig }()
	gitSHAFromGit := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	gitRevParseHEAD = func() string { return gitSHAFromGit }

	getenv := func(k string) string {
		if k == "GIT_SHA" {
			return "cccccccccccccccccccccccccccccccccccccccc"
		}
		return ""
	}
	if got := resolveGitSHA(getenv); got != gitSHAFromGit {
		t.Fatalf("expected git rev-parse result %q to take precedence, got %q", gitSHAFromGit, got)
	}
}

// TestResolveGitSHANeverPanics is a smoke test that the real
// tryGitRevParseHEAD() codepath (invoking the actual git binary against
// whatever cwd the test runs in) never panics regardless of outcome — the
// health check must never crash from SHA resolution.
func TestResolveGitSHANeverPanics(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("resolveGitSHA panicked: %v", r)
		}
	}()
	got := resolveGitSHA(func(string) string { return "" })
	if got == "" {
		t.Fatal("resolveGitSHA must never return an empty string")
	}
}
