package main

// Incoming RFC 9421 HTTP Message Signature verification (§8.1) with replay/
// freshness protection, alg allowlist, identity binding, and an SSRF-hardened
// profile fetch.

import (
	"context"
	"crypto/ecdsa"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const allowedAlg = "ecdsa-p256-sha256"

// agentIdentity is the verified caller identity (bound to its profile + key).
type agentIdentity struct {
	ProfileURL string
	Keyid      string
	Caps       []string // capabilities declared in the agent's profile
}

// KeyResolver resolves an agent profile to its keys + declared capabilities.
// keyid is the kid the caller actually needs (may be "" when the caller has no
// specific kid in mind); a caching resolver may use it to decide whether a
// cached result is still useful (see L2 fix in httpKeyResolver).
type KeyResolver func(profileURL, keyid string) (keys map[string]*ecdsa.PublicKey, caps []string, err error)

type sigParams struct {
	components []string
	keyid      string
	alg        string
	created    int64
	rawValue   string // verbatim value after "label=" (for @signature-params)
}

func parseSignatureInput(h string) (*sigParams, error) {
	eq := strings.IndexByte(h, '=')
	if eq < 0 {
		return nil, errors.New("malformed signature-input")
	}
	val := strings.TrimSpace(h[eq+1:])
	op := strings.IndexByte(val, '(')
	cl := strings.IndexByte(val, ')')
	if op < 0 || cl < op {
		return nil, errors.New("no component list")
	}
	sp := &sigParams{rawValue: val}
	for _, p := range strings.Fields(val[op+1 : cl]) {
		sp.components = append(sp.components, strings.Trim(p, "\""))
	}
	for _, kv := range strings.Split(val[cl+1:], ";") {
		k, v, ok := strings.Cut(strings.TrimSpace(kv), "=")
		if !ok {
			continue
		}
		v = strings.Trim(strings.TrimSpace(v), "\"")
		switch strings.TrimSpace(k) {
		case "keyid":
			sp.keyid = v
		case "alg":
			sp.alg = v
		case "created":
			sp.created, _ = strconv.ParseInt(v, 10, 64)
		}
	}
	if sp.keyid == "" {
		return nil, errors.New("missing keyid")
	}
	return sp, nil
}

func parseSignatureValue(h string) ([]byte, error) {
	eq := strings.IndexByte(h, '=')
	if eq < 0 {
		return nil, errors.New("malformed signature")
	}
	return base64.StdEncoding.DecodeString(strings.Trim(strings.TrimSpace(h[eq+1:]), ":"))
}

func parseProfileURL(ucpAgent string) string {
	i := strings.Index(ucpAgent, "profile=")
	if i < 0 {
		return ""
	}
	return strings.Trim(strings.TrimSpace(ucpAgent[i+len("profile="):]), "\"")
}

// replayGuard rejects re-presented signatures within the freshness window.
type replayGuard struct {
	mu   sync.Mutex
	seen map[string]int64 // signature key -> unix expiry
}

func newReplayGuard() *replayGuard { return &replayGuard{seen: map[string]int64{}} }

func (g *replayGuard) fresh(key string, now, ttl int64) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	for k, exp := range g.seen {
		if exp < now {
			delete(g.seen, k)
		}
	}
	if _, ok := g.seen[key]; ok {
		return false
	}
	g.seen[key] = now + ttl
	return true
}

type sigVerifier struct {
	resolve KeyResolver
	replay  *replayGuard
	now     func() time.Time
	skewSec int64
}

func newSigVerifier(resolve KeyResolver) *sigVerifier {
	return &sigVerifier{resolve: resolve, replay: newReplayGuard(), now: time.Now, skewSec: 300}
}

// verify checks an RFC 9421 signature. Returns present=false when no signature
// headers are supplied (caller decides whether to require). On present=true,
// err==nil means valid and id is the bound caller identity.
func (v *sigVerifier) verify(method, authority, path, query string, headers map[string]string, body []byte) (present bool, id *agentIdentity, err error) {
	si, sg := headers["signature-input"], headers["signature"]
	if si == "" && sg == "" {
		return false, nil, nil
	}
	if si == "" || sg == "" {
		return true, nil, errors.New("signature_missing")
	}
	sp, err := parseSignatureInput(si)
	if err != nil {
		return true, nil, err
	}
	// RFC9421-004 + RFC9421-003: require adequate signature coverage.
	// An empty component list `()` or a list missing @method/@path/@authority
	// signs nothing meaningful — it cannot bind the request identity. A bodied
	// request missing content-digest leaves the payload unbound (MITM-splicing).
	covered := map[string]bool{}
	for _, comp := range sp.components {
		covered[strings.ToLower(comp)] = true
	}
	if !covered["@method"] || !covered["@path"] || !covered["@authority"] {
		return true, nil, errors.New("inadequate_signature_coverage")
	}
	if len(body) > 0 && !covered["content-digest"] {
		return true, nil, errors.New("body_not_digest_covered")
	}
	if sp.alg != "" && sp.alg != allowedAlg {
		return true, nil, errors.New("alg_not_allowed")
	}
	now := v.now().Unix()
	if sp.created == 0 || sp.created < now-v.skewSec || sp.created > now+v.skewSec {
		return true, nil, errors.New("signature_stale")
	}
	sig, err := parseSignatureValue(sg)
	if err != nil {
		return true, nil, err
	}
	if !v.replay.fresh(sp.keyid+":"+base64.StdEncoding.EncodeToString(sig), now, 2*v.skewSec) {
		return true, nil, errors.New("signature_replayed")
	}
	profileURL := parseProfileURL(headers["ucp-agent"])
	keys, caps, err := v.resolve(profileURL, sp.keyid)
	if err != nil {
		return true, nil, errors.New("profile_unreachable")
	}
	pub, ok := keys[sp.keyid]
	if !ok || pub == nil {
		return true, nil, errors.New("key_not_found")
	}
	for _, c := range sp.components {
		if c == "content-digest" {
			if subtle.ConstantTimeCompare([]byte(headers["content-digest"]), []byte(contentDigest(body))) != 1 {
				return true, nil, errors.New("digest_mismatch")
			}
		}
	}
	// FIX 2 (RFC9421-003): body-mutating methods must include content-digest in the
	// signed component list — otherwise an attacker can replace the body transparently.
	if m := strings.ToUpper(method); m == "POST" || m == "PUT" || m == "PATCH" {
		hasCD := false
		for _, c := range sp.components {
			if c == "content-digest" {
				hasCD = true
				break
			}
		}
		if !hasCD {
			return true, nil, errors.New("missing_content_digest: POST/PUT/PATCH must include content-digest in signature")
		}
	}
	resolveComp := func(c string) string {
		switch c {
		case "@method":
			return strings.ToUpper(method)
		case "@authority":
			return strings.ToLower(authority)
		case "@path":
			return path
		case "@query":
			return query
		default:
			return headers[strings.ToLower(c)]
		}
	}
	if !verifyRaw(pub, []byte(signatureBase(sp.components, resolveComp, sp.rawValue)), sig) {
		return true, nil, errors.New("signature_invalid")
	}
	return true, &agentIdentity{ProfileURL: profileURL, Keyid: sp.keyid, Caps: caps}, nil
}

// httpKeyResolver fetches + caches an agent profile's signing_keys + capabilities.
func httpKeyResolver(allowDevSSRF bool) KeyResolver {
	type entry struct {
		keys map[string]*ecdsa.PublicKey
		caps []string
		at   time.Time
	}
	// FIX 5 (RFC9421-011): singleflight — only one concurrent fetch per profileURL.
	// Additional callers for the same URL block on the in-progress fetch and share its
	// result, preventing a thundering-herd of parallel HTTP requests for an uncached profile.
	type call struct {
		wg  sync.WaitGroup
		val entry
		err error
	}
	var mu sync.Mutex
	cache := map[string]entry{}
	inflight := map[string]*call{}
	const ttl = 6 * time.Hour
	return func(profileURL, keyid string) (map[string]*ecdsa.PublicKey, []string, error) {
		if profileURL == "" {
			return nil, nil, errors.New("no agent profile URL")
		}
		mu.Lock()
		e, ok := cache[profileURL]
		// L2 fix (RFC9421 key-cache revocation gap): a cache hit is only served
		// as-is when it already contains the kid the caller needs. If the caller
		// asked for a specific kid and it's absent from the cached set, the key
		// may have just been rotated in (or the cached entry never had it) — fall
		// through to a real refetch (deduped below via the existing singleflight)
		// instead of trusting the cache for up to `ttl` more. A cache hit that
		// already satisfies the caller, or a lookup with no specific kid in mind,
		// still takes the fast (no-network) path.
		if ok && time.Since(e.at) <= ttl {
			if keyid == "" || e.keys[keyid] != nil {
				mu.Unlock()
				return e.keys, e.caps, nil
			}
		}
		// Check if a fetch is already in flight for this URL.
		if c, flying := inflight[profileURL]; flying {
			mu.Unlock()
			c.wg.Wait()
			if c.err != nil {
				return nil, nil, c.err
			}
			return c.val.keys, c.val.caps, nil
		}
		c := &call{}
		c.wg.Add(1)
		inflight[profileURL] = c
		mu.Unlock()

		keys, caps, err := fetchProfile(profileURL, allowDevSSRF)

		mu.Lock()
		delete(inflight, profileURL)
		if err == nil {
			e = entry{keys: keys, caps: caps, at: time.Now()}
			cache[profileURL] = e
		}
		mu.Unlock()

		c.val = entry{keys: keys, caps: caps}
		c.err = err
		c.wg.Done()

		if err != nil {
			return nil, nil, err
		}
		return e.keys, e.caps, nil
	}
}

// safeClient blocks non-HTTPS, redirects, and connections to private/loopback/
// link-local IPs (SSRF guard, H4). It pins the dialed IP to the vetted one.
func safeClient(allowDevSSRF bool) *http.Client {
	base := &net.Dialer{Timeout: 5 * time.Second}
	return &http.Client{
		Timeout:       5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirects_blocked") },
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				host, port, err := net.SplitHostPort(addr)
				if err != nil {
					return nil, err
				}
				ips, err := net.LookupIP(host)
				if err != nil {
					return nil, err
				}
				for _, ip := range ips {
					// #56 (audit-narrowed): even under UCP_ALLOW_DEV_SSRF we permit ONLY LOOPBACK
					// (127.0.0.0/8, ::1) — private/link-local/cloud-metadata(169.254.169.254)/
					// external stay BLOCKED in dev too. The dev allowance is for a LOCAL agent
					// profile, not wide-open SSRF. Default OFF => full guard armed.
					if isBlockedIP(ip) && !(allowDevSSRF && ip.IsLoopback()) {
						return nil, errors.New("blocked_ip")
					}
				}
				return base.DialContext(ctx, network, net.JoinHostPort(ips[0].String(), port))
			},
		},
	}
}

// cgnatCIDR is the RFC 6598 Carrier-Grade NAT / shared-address range
// (100.64.0.0/10). It is NOT covered by net.IP.IsPrivate (RFC1918) yet cloud
// metadata endpoints live inside it — notably AliCloud at 100.100.100.200 — so
// an attacker-influenceable outbound URL could otherwise reach instance
// metadata. We block it explicitly, and (like link-local/private) the dev SSRF
// allowance never opens it: only loopback is permitted under AllowDevSSRF.
var _, cgnatCIDR, _ = net.ParseCIDR("100.64.0.0/10")

func isBlockedIP(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsUnspecified() ||
		(cgnatCIDR != nil && cgnatCIDR.Contains(ip))
}

func fetchProfile(profileURL string, allowDevSSRF bool) (map[string]*ecdsa.PublicKey, []string, error) {
	u, err := url.Parse(profileURL)
	// Production requires https. #56: under UCP_ALLOW_DEV_SSRF a loopback http profile is
	// permitted too, so a local agent can complete the signed loop in dev/CI.
	if err != nil || (u.Scheme != "https" && !(allowDevSSRF && u.Scheme == "http")) {
		return nil, nil, errors.New("profile_url_not_https")
	}
	// FIX 6 (RFC9421-007): domain allowlist. UCP_PROFILE_ALLOW_DOMAINS is a
	// comma-separated list of permitted hostnames; an empty/unset var means allow-all
	// (backwards-compatible). If the list is non-empty and the profile hostname is not
	// in it, reject before making any outbound connection.
	if raw := strings.TrimSpace(os.Getenv("UCP_PROFILE_ALLOW_DOMAINS")); raw != "" {
		allowed := false
		for _, d := range strings.Split(raw, ",") {
			if strings.EqualFold(strings.TrimSpace(d), u.Hostname()) {
				allowed = true
				break
			}
		}
		if !allowed {
			return nil, nil, errors.New("profile_url_not_allowed")
		}
	}
	resp, err := safeClient(allowDevSSRF).Get(profileURL)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	// FIX 3 (RFC9421-008): treat non-200 responses as fetch failures rather than
	// silently decoding an empty/error body and returning zero keys.
	if resp.StatusCode != http.StatusOK {
		return nil, nil, fmt.Errorf("profile_fetch_error: status %d", resp.StatusCode)
	}
	var doc struct {
		UCP struct {
			Capabilities map[string]any `json:"capabilities"`
		} `json:"ucp"`
		SigningKeys []struct{ Kid, Kty, Crv, X, Y string } `json:"signing_keys"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&doc); err != nil {
		return nil, nil, err
	}
	keys := map[string]*ecdsa.PublicKey{}
	for _, k := range doc.SigningKeys {
		if k.Kty == "EC" && k.Crv == "P-256" {
			if pub, err := pubFromJWK(k.X, k.Y); err == nil {
				keys[k.Kid] = pub
			}
		}
	}
	caps := make([]string, 0, len(doc.UCP.Capabilities))
	for id := range doc.UCP.Capabilities {
		caps = append(caps, id)
	}
	return keys, caps, nil
}
