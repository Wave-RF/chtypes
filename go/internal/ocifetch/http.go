package ocifetch

// http.go — the v1 HTTP stack (docs/guides/fetch-v1.md §2, §7): one retry
// table, Retry-After in both forms, redirects with cross-origin Authorization
// stripping, the anonymous Bearer-token flow for mirrors, the static
// CHTYPES_DOWNLOAD_TOKEN bearer, and HTTPS_PROXY/NO_PROXY plus the system CA
// store via net/http's own defaults. Nothing here is a bespoke TLS or proxy
// implementation; it is net/http configured the way the guide asks.
//
// Every attempt goes through a single retrying core so the retry table (§7,
// constants_gen.go) has exactly one implementation. A Clock lets the
// conformance suite inject sleep(seconds) and now() so retry cases run
// instantly and assert their sleep sequence exactly (plan §3.2).

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Clock is the fetch layer's only source of time and delay, so tests can
// replace both with deterministic, instant equivalents.
type Clock struct {
	Now   func() time.Time
	Sleep func(ctx context.Context, d time.Duration)
}

// DefaultClock is wall-clock time and a real sleep, cancellable by ctx.
func DefaultClock() Clock {
	return Clock{
		Now: time.Now,
		Sleep: func(ctx context.Context, d time.Duration) {
			if d <= 0 {
				return
			}
			t := time.NewTimer(d)
			defer t.Stop()
			select {
			case <-t.C:
			case <-ctx.Done():
			}
		},
	}
}

// retryPolicy is the one retry table every request in this package shares
// (constants_gen.go: RetryAttempts, RetryFirstWaitSeconds, RetryMultiplier).
type retryPolicy struct {
	attempts   int
	firstWait  time.Duration
	multiplier float64
}

func defaultRetryPolicy() retryPolicy {
	return retryPolicy{
		attempts:   RetryAttempts,
		firstWait:  time.Duration(RetryFirstWaitSeconds * float64(time.Second)),
		multiplier: RetryMultiplier,
	}
}

// wait returns the backoff before the (i+2)th attempt, i is 0-based over the
// waits between attempts (attempts-1 of them).
func (p retryPolicy) wait(i int) time.Duration {
	return time.Duration(float64(p.firstWait) * math.Pow(p.multiplier, float64(i)))
}

// totalBudget is the sum of every wait the full schedule would spend. A
// Retry-After larger than this is refused rather than shortened
// (docs/guides/fetch-v1.md §7; spec layout-v2 A6): the SDK's own reading of
// "past the remaining budget" is the schedule's own total, computed from the
// generated constants rather than a second hand-typed number.
func (p retryPolicy) totalBudget() time.Duration {
	var total time.Duration
	for i := 0; i < p.attempts-1; i++ {
		total += p.wait(i)
	}
	return total
}

func retryableStatus(status int, extra ...int) bool {
	for _, s := range RetryStatuses {
		if s == status {
			return true
		}
	}
	for _, s := range extra {
		if s == status {
			return true
		}
	}
	return false
}

func honorsRetryAfter(status int) bool {
	for _, s := range RetryAfterStatuses {
		if s == status {
			return true
		}
	}
	return false
}

// parseRetryAfter reads Retry-After in either form: delta-seconds, or an
// HTTP-date, resolved against baseline (the response's own Date header when
// present, else the clock's now()).
func parseRetryAfter(h http.Header, baseline time.Time) (time.Duration, bool) {
	v := strings.TrimSpace(h.Get("Retry-After"))
	if v == "" {
		return 0, false
	}
	if secs, err := strconv.Atoi(v); err == nil {
		if secs < 0 {
			secs = 0
		}
		return time.Duration(secs) * time.Second, true
	}
	if t, err := http.ParseTime(v); err == nil {
		d := t.Sub(baseline)
		if d < 0 {
			d = 0
		}
		return d, true
	}
	return 0, false
}

func retryAfterBaseline(h http.Header, now time.Time) time.Time {
	if d := strings.TrimSpace(h.Get("Date")); d != "" {
		if t, err := http.ParseTime(d); err == nil {
			return t
		}
	}
	return now
}

// httpResult is one successful (non-transport-error) HTTP round trip: the
// response has been fully read and its body closed.
type httpResult struct {
	status  int
	headers http.Header
	body    []byte
	url     string // the URL this response actually came from, after redirects
}

// client wraps *http.Client with this package's redirect, retry, auth and
// size-cap rules. One client is shared across every request a session makes.
type client struct {
	http            *http.Client
	clock           Clock
	connectTimeout  time.Duration
	idleReadTimeout time.Duration
	token           string   // CHTYPES_DOWNLOAD_TOKEN, sent only to tokenHosts
	tokenHosts      []string // exact host:port matches (the configured bases)
	// onRequest, when set, is called with every outgoing request just
	// before it is sent (after auth/Accept headers are attached, before
	// redirects are followed) — test-only, for a conformance runner to
	// count requests and inspect headers without a server-side log.
	onRequest func(*http.Request)
}

func newClient(clock Clock, connectTimeout, idleReadTimeout time.Duration, token string, tokenHosts []string, onRequest func(*http.Request)) *client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyFromEnvironment
	transport.DialContext = (&net.Dialer{Timeout: connectTimeout}).DialContext
	// file:// bases (the conformance suite's file transport, §2 of the
	// guide) are served through the same retry/redirect/Accept-header
	// pipeline as http(s): registering it here, rather than special-casing
	// the scheme anywhere else, is what keeps every other file in this
	// package transport-agnostic. The root is "/": a file base is already
	// an absolute path (file:///abs/...), and http.Dir resolves the
	// request's URL.Path against it unchanged.
	transport.RegisterProtocol("file", http.NewFileTransport(http.Dir("/")))
	return &client{
		http: &http.Client{
			Transport: transport,
			// Redirects are followed by this package's own loop (dropping
			// Authorization cross-origin), never by net/http's default
			// policy, which would resend every header unchanged.
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		clock:           clock,
		connectTimeout:  connectTimeout,
		idleReadTimeout: idleReadTimeout,
		token:           token,
		tokenHosts:      tokenHosts,
		onRequest:       onRequest,
	}
}

func sameOrigin(a, b *url.URL) bool {
	return a.Scheme == b.Scheme && a.Host == b.Host
}

func (c *client) sendsTokenTo(host string) bool {
	for _, h := range c.tokenHosts {
		if h == host {
			return true
		}
	}
	return false
}

// bearerToken, when set on the initial request, carries an anonymous
// registry bearer token obtained from a 401 challenge (the "anon-token-flow"
// case) rather than the static CHTYPES_DOWNLOAD_TOKEN.
type requestOptions struct {
	bearerToken string // overrides the static token for this one request, when set
	maxBytes    int64  // 0 means no cap
	// extraRetryStatuses adds to RetryStatuses for this one request; used
	// only by the digest-fetch-on-last-base path (§7 A5: digest 404 retries
	// on the last base rather than failing immediately).
	extraRetryStatuses []int
	// accept, when set, becomes the request's Accept header. Every
	// manifests/<ref> GET (by tag or by digest) sets manifestAccept: our
	// host ignores it, but a mirror may need it for content negotiation
	// (the delivery side's review).
	accept string
}

// manifestAccept is the Accept header every GET …/manifests/<ref> sends,
// whether <ref> is a tag or a digest.
var manifestAccept = MediaTypeIndex + ", " + MediaTypeManifest

// doGet performs one logical GET against rawURL: redirects, retries,
// Retry-After and the token policy, all in one place. It returns the final
// result whose status may be any value the caller must interpret (200, 401,
// 403, 404, …) — only a transport failure or an exhausted retry budget
// produces an error, always a *FetchError.
// permanentTransportError marks a failure that retrying the SAME base will
// never fix: a connection refused (nothing is listening), a redirect with
// no Location, or more hops than limits.max_redirects. doGet returns on the
// first one of these, consuming none of the retry schedule.
type permanentTransportError struct{ err error }

func (e *permanentTransportError) Error() string { return e.err.Error() }
func (e *permanentTransportError) Unwrap() error { return e.err }

func isPermanentTransportError(err error) bool {
	var perm *permanentTransportError
	return errors.As(err, &perm)
}

func (c *client) doGet(ctx context.Context, rawURL string, opts requestOptions) (*httpResult, error) {
	policy := defaultRetryPolicy()
	var lastErr error
	triedAnonToken := false
	for attempt := 0; attempt < policy.attempts; attempt++ {
		attemptCtx, cancel := context.WithTimeout(ctx, c.connectTimeout+c.idleReadTimeout)
		result, status, retryAfter, err := c.attemptWithRedirects(attemptCtx, rawURL, opts)
		cancel()

		// A permanent, base-specific condition (connection refused — no
		// server to retry against; too many redirects or a malformed
		// Location — a protocol violation that will recur identically on
		// every retry) is never worth sleeping for: it fails this base
		// immediately, consuming no part of the retry schedule, so
		// fetchAcrossBases moves on to the next base (if any) without
		// delay — same treatment as a 404 (§2: "tried in order on a
		// temporary error … never on a verification failure", and a
		// permanent local error is not something retrying the same base
		// could ever fix).
		if isPermanentTransportError(err) {
			if errors.Is(err, errBodyTooLarge) {
				return nil, newError(CodeArtifactCorrupt, "", "", rawURL, err, "fetching %s: %v", rawURL, err)
			}
			return nil, newError(CodeSourceUnreachable, "", "", rawURL, err, "fetching %s: %v", rawURL, err)
		}

		// The anonymous Bearer-token flow (mirrors): a 401 with no static
		// CHTYPES_DOWNLOAD_TOKEN configured and a Bearer challenge is an
		// invitation to fetch a token and retry, not a final failure. A 401
		// with a static token already attached is a real auth failure
		// (download-token-401) and skips straight to CHTYPES_SOURCE_
		// UNAUTHORIZED in the caller, never this exchange.
		if err == nil && status == http.StatusUnauthorized && !triedAnonToken && c.token == "" && opts.bearerToken == "" {
			if challenge := result.headers.Get("WWW-Authenticate"); strings.HasPrefix(strings.TrimSpace(challenge), "Bearer ") {
				if tok, terr := c.anonymousToken(ctx, challenge); terr == nil && tok != "" {
					opts.bearerToken = tok
					triedAnonToken = true
					attempt--
					continue
				}
			}
		}

		if err == nil {
			if !retryableStatus(status, opts.extraRetryStatuses...) {
				return result, nil
			}
			lastErr = fmt.Errorf("status %d", status)
		} else {
			lastErr = err
		}
		if attempt == policy.attempts-1 {
			return nil, newError(CodeSourceUnreachable, "", "", rawURL, lastErr,
				"exhausted %d attempts fetching %s: %v", policy.attempts, rawURL, lastErr)
		}
		wait := policy.wait(attempt)
		if retryAfter != nil {
			if *retryAfter > policy.totalBudget() {
				return nil, newError(CodeSourceUnreachable, "", "", rawURL, nil,
					"refusing Retry-After of %s for %s: exceeds the %s retry budget",
					*retryAfter, rawURL, policy.totalBudget())
			}
			wait = *retryAfter
		}
		c.clock.Sleep(ctx, wait)
	}
	return nil, newError(CodeSourceUnreachable, "", "", rawURL, lastErr, "exhausted retries fetching %s", rawURL)
}

// attemptWithRedirects performs exactly one attempt of doGet's retry loop,
// following up to MaxRedirects hops and stripping Authorization on any
// cross-origin hop. It returns the status code and, when the status asks for
// one, a parsed Retry-After even when the caller will not retry on it
// (harmless: doGet only consults retryAfter when it has already decided to
// retry).
func (c *client) attemptWithRedirects(ctx context.Context, rawURL string, opts requestOptions) (*httpResult, int, *time.Duration, error) {
	current := rawURL
	includeAuth := true
	for hop := 0; ; hop++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, current, nil)
		if err != nil {
			return nil, 0, nil, err
		}
		u, err := url.Parse(current)
		if err != nil {
			return nil, 0, nil, err
		}
		if includeAuth {
			c.setAuth(req, u, opts)
		}
		if opts.accept != "" {
			req.Header.Set("Accept", opts.accept)
		}
		// Every hop (first request, redirects, token exchanges) is built
		// here, so this is the one place the agent is set.
		req.Header.Set("User-Agent", userAgent())
		if c.onRequest != nil {
			c.onRequest(req)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			if errors.Is(err, syscall.ECONNREFUSED) {
				return nil, 0, nil, &permanentTransportError{err}
			}
			return nil, 0, nil, err
		}
		if resp.StatusCode >= 300 && resp.StatusCode < 400 {
			loc := resp.Header.Get("Location")
			_ = resp.Body.Close()
			if loc == "" {
				return nil, 0, nil, &permanentTransportError{errors.New("redirect response with no Location header")}
			}
			if hop+1 >= MaxRedirects {
				return nil, 0, nil, &permanentTransportError{fmt.Errorf("more than %d redirects", MaxRedirects)}
			}
			next, err := u.Parse(loc)
			if err != nil {
				return nil, 0, nil, err
			}
			includeAuth = sameOrigin(u, next)
			current = next.String()
			continue
		}
		body, readErr := readLimited(resp.Body, opts.maxBytes)
		closeErr := resp.Body.Close()
		if readErr != nil {
			if errors.Is(readErr, errBodyTooLarge) {
				// A real response the server sent, just oversized — a
				// content problem, never a connection one. Retrying would
				// read the identical oversized body again, so this is
				// permanent, but it is CHTYPES_ARTIFACT_CORRUPT rather
				// than CHTYPES_SOURCE_UNREACHABLE: doGet checks for this
				// sentinel before applying the generic permanent-transport
				// classification.
				return nil, 0, nil, &permanentTransportError{readErr}
			}
			return nil, 0, nil, readErr
		}
		if closeErr != nil {
			return nil, 0, nil, closeErr
		}
		var retryAfter *time.Duration
		if honorsRetryAfter(resp.StatusCode) {
			if ra, ok := parseRetryAfter(resp.Header, retryAfterBaseline(resp.Header, c.clock.Now())); ok {
				retryAfter = &ra
			}
		}
		return &httpResult{status: resp.StatusCode, headers: resp.Header, body: body, url: current}, resp.StatusCode, retryAfter, nil
	}
}

// setAuth attaches exactly one bearer credential, in priority order: a
// per-request anonymous token (opts.bearerToken), else the static
// CHTYPES_DOWNLOAD_TOKEN when this host is a configured base (exact match),
// never across a redirect (callers only pass includeAuth through same-origin
// hops) and never to an unconfigured mirror.
func (c *client) setAuth(req *http.Request, u *url.URL, opts requestOptions) {
	if opts.bearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+opts.bearerToken)
		return
	}
	if c.token != "" && c.sendsTokenTo(u.Host) {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
}

// errBodyTooLarge is readLimited's sentinel for an oversized response body
// (errors.Is-able), so doGet can tell "the server sent real content, just
// too much of it" apart from every other transport failure and map it to
// CHTYPES_ARTIFACT_CORRUPT rather than CHTYPES_SOURCE_UNREACHABLE.
var errBodyTooLarge = errors.New("response exceeds the byte cap")

// readLimited reads at most maxBytes+1 bytes and fails if that is exceeded,
// so a caller can distinguish "within the cap" from "over" without buffering
// an attacker-controlled amount of data. maxBytes<=0 means unlimited.
func readLimited(r io.Reader, maxBytes int64) ([]byte, error) {
	if maxBytes <= 0 {
		return io.ReadAll(r)
	}
	limited := io.LimitReader(r, maxBytes+1)
	b, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > maxBytes {
		return nil, fmt.Errorf("%w: %d bytes over the %d byte cap", errBodyTooLarge, int64(len(b))-maxBytes, maxBytes)
	}
	return b, nil
}

// anonymousToken implements the anonymous Bearer-token flow mirrors use
// (WWW-Authenticate: Bearer realm="…",service="…",scope="…"): it fetches a
// token from the realm and returns it for the caller to retry the original
// request with.
func (c *client) anonymousToken(ctx context.Context, challenge string) (string, error) {
	params := parseWWWAuthenticate(challenge)
	realm := params["realm"]
	if realm == "" {
		return "", errors.New("WWW-Authenticate challenge names no realm")
	}
	u, err := url.Parse(realm)
	if err != nil {
		return "", fmt.Errorf("WWW-Authenticate realm %q: %w", realm, err)
	}
	q := u.Query()
	if s := params["service"]; s != "" {
		q.Set("service", s)
	}
	if s := params["scope"]; s != "" {
		q.Set("scope", s)
	}
	u.RawQuery = q.Encode()
	result, err := c.doGet(ctx, u.String(), requestOptions{maxBytes: BundleMaxBytes})
	if err != nil {
		return "", err
	}
	if result.status != http.StatusOK {
		return "", fmt.Errorf("token endpoint %s returned %d", u.Redacted(), result.status)
	}
	tok, err := extractToken(result.body)
	if err != nil {
		return "", err
	}
	return tok, nil
}

// parseWWWAuthenticate reads the Bearer challenge's comma-separated
// key="value" parameters (RFC 6750 §3).
func parseWWWAuthenticate(header string) map[string]string {
	out := map[string]string{}
	header = strings.TrimSpace(header)
	const prefix = "Bearer "
	if !strings.HasPrefix(header, prefix) {
		return out
	}
	rest := header[len(prefix):]
	for _, part := range splitAuthParams(rest) {
		kv := strings.SplitN(part, "=", 2)
		if len(kv) != 2 {
			continue
		}
		key := strings.TrimSpace(kv[0])
		val := strings.Trim(strings.TrimSpace(kv[1]), `"`)
		out[key] = val
	}
	return out
}

// extractToken reads the registry token-endpoint response: either {"token":
// "…"} or the older {"access_token": "…"} (distribution-spec allows both).
func extractToken(body []byte) (string, error) {
	var doc struct {
		Token       string `json:"token"`
		AccessToken string `json:"access_token"`
	}
	if err := strictUnmarshal(body, &doc); err != nil {
		return "", fmt.Errorf("token response is not valid JSON: %w", err)
	}
	if doc.Token != "" {
		return doc.Token, nil
	}
	if doc.AccessToken != "" {
		return doc.AccessToken, nil
	}
	return "", errors.New("token response carries neither token nor access_token")
}

// splitAuthParams splits on commas that are not inside a quoted value.
func splitAuthParams(s string) []string {
	var parts []string
	var buf strings.Builder
	inQuotes := false
	for _, r := range s {
		switch r {
		case '"':
			inQuotes = !inQuotes
			buf.WriteRune(r)
		case ',':
			if inQuotes {
				buf.WriteRune(r)
			} else {
				parts = append(parts, buf.String())
				buf.Reset()
			}
		default:
			buf.WriteRune(r)
		}
	}
	if buf.Len() > 0 {
		parts = append(parts, buf.String())
	}
	return parts
}
