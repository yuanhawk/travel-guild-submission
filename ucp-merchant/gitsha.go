// gitsha.go — resolve the running build's git SHA for /health, mirroring
// society/orchestration/server.py::_resolve_git_sha so both
// services report build provenance the same way.
package main

import (
	"context"
	"os/exec"
	"strings"
	"time"
)

// gitSHA is resolved once at process startup (not per-request) — see main(),
// which assigns it before the HTTP server starts serving /health.
var gitSHA = "unknown"

// resolveGitSHA tries `git rev-parse HEAD` in the process's working directory
// first (works when a .git dir is present at runtime, e.g. the CI e2e job,
// which builds and runs this binary from inside the checked-out repo).
//
// The production Docker image explicitly disables Go's own VCS build-info
// stamping (`go build -buildvcs=false`, see Dockerfile — the build context
// there is the ucp-merchant/ subdirectory only, which has no .git dir to
// stamp from anyway) and the distroless runtime has no git binary and no
// .git dir at all, so on a real deploy this predictably falls through to the
// GIT_SHA env var — the SAME env var name a coordinated deploy would stamp for
// the Python backend's process, so whatever starts this container
// should set GIT_SHA too (see Dockerfile's GIT_SHA build ARG/ENV plumbing).
//
// Falls back to "unknown" rather than ever panicking — a stale/missing SHA
// must never crash or degrade the health check.
func resolveGitSHA(getenv func(string) string) string {
	if sha := gitRevParseHEAD(); sha != "" {
		return sha
	}
	if v := getenv("GIT_SHA"); v != "" {
		return v
	}
	return "unknown"
}

// gitRevParseHEAD is a package-level var (not a plain func) so tests can
// swap in a stub — mirrors the Python test suite's `patch("subprocess.run",
// ...)` technique for exercising the git-unavailable fallback path without
// depending on whether the test sandbox happens to have a real .git dir.
var gitRevParseHEAD = tryGitRevParseHEAD

// tryGitRevParseHEAD runs `git rev-parse HEAD` with a bounded timeout so a
// hung or missing git binary can never delay startup. Returns "" on any
// failure (git not found, not a repo, timeout, empty output).
func tryGitRevParseHEAD() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	out, err := exec.CommandContext(ctx, "git", "rev-parse", "HEAD").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
