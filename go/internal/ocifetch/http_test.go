package ocifetch

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestRetryPolicyWaitsAndBudget(t *testing.T) {
	p := retryPolicy{attempts: 5, firstWait: 4 * time.Second, multiplier: 2}
	want := []time.Duration{4 * time.Second, 8 * time.Second, 16 * time.Second, 32 * time.Second}
	for i, w := range want {
		if got := p.wait(i); got != w {
			t.Errorf("wait(%d) = %v, want %v", i, got, w)
		}
	}
	if got, want := p.totalBudget(), 60*time.Second; got != want {
		t.Errorf("totalBudget() = %v, want %v", got, want)
	}
}

func TestParseRetryAfterSeconds(t *testing.T) {
	h := http.Header{"Retry-After": []string{"2"}}
	d, ok := parseRetryAfter(h, time.Now())
	if !ok || d != 2*time.Second {
		t.Fatalf("parseRetryAfter(seconds) = %v, %v; want 2s, true", d, ok)
	}
}

func TestParseRetryAfterHTTPDate(t *testing.T) {
	base := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	target := base.Add(3 * time.Second)
	h := http.Header{"Retry-After": []string{target.Format(http.TimeFormat)}}
	d, ok := parseRetryAfter(h, base)
	if !ok || d != 3*time.Second {
		t.Fatalf("parseRetryAfter(http-date) = %v, %v; want 3s, true", d, ok)
	}
}

func TestRetryAfterBaselineUsesResponseDate(t *testing.T) {
	respDate := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	h := http.Header{"Date": []string{respDate.Format(http.TimeFormat)}}
	got := retryAfterBaseline(h, time.Now())
	if !got.Equal(respDate) {
		t.Fatalf("retryAfterBaseline = %v, want %v (the response's own Date header)", got, respDate)
	}
}

func TestRetryAfterBaselineFallsBackToClock(t *testing.T) {
	now := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	got := retryAfterBaseline(http.Header{}, now)
	if !got.Equal(now) {
		t.Fatalf("retryAfterBaseline with no Date header = %v, want the clock's now() %v", got, now)
	}
}

func TestRetryableStatus(t *testing.T) {
	for _, s := range []int{408, 429, 500, 502, 503, 504} {
		if !retryableStatus(s) {
			t.Errorf("retryableStatus(%d) = false, want true", s)
		}
	}
	for _, s := range []int{200, 400, 401, 403, 404} {
		if retryableStatus(s) {
			t.Errorf("retryableStatus(%d) = true, want false", s)
		}
	}
	if !retryableStatus(404, http.StatusNotFound) {
		t.Errorf("retryableStatus(404) with 404 as an extra status = false, want true")
	}
}

func TestHonorsRetryAfter(t *testing.T) {
	if !honorsRetryAfter(429) || !honorsRetryAfter(503) {
		t.Fatalf("honorsRetryAfter should be true for 429 and 503")
	}
	if honorsRetryAfter(500) {
		t.Fatalf("honorsRetryAfter(500) = true, want false (only 429/503 per constants_gen.go)")
	}
}

// newTestClient builds a client with an instant, no-op sleeper so retry
// tests run in milliseconds and can assert the exact sleep sequence.
func newTestClient() (*client, *[]time.Duration) {
	var sleeps []time.Duration
	clock := Clock{
		Now: time.Now,
		Sleep: func(_ context.Context, d time.Duration) {
			sleeps = append(sleeps, d)
		},
	}
	return newClient(clock, 2*time.Second, 2*time.Second, "", nil), &sleeps
}

func TestDoGetRetries5xxThenSucceeds(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	defer srv.Close()

	c, sleeps := newTestClient()
	res, err := c.doGet(context.Background(), srv.URL, requestOptions{})
	if err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if string(res.body) != "ok" {
		t.Fatalf("body = %q, want ok", res.body)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3", calls)
	}
	if len(*sleeps) != 2 {
		t.Fatalf("sleeps = %v, want 2 entries (4s, 8s)", *sleeps)
	}
}

func TestDoGetHonorsRetryAfterSeconds(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.Header().Set("Retry-After", "2")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c, sleeps := newTestClient()
	if _, err := c.doGet(context.Background(), srv.URL, requestOptions{}); err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if len(*sleeps) != 1 || (*sleeps)[0] != 2*time.Second {
		t.Fatalf("sleeps = %v, want [2s]", *sleeps)
	}
}

func TestDoGetRefusesRetryAfterOverBudget(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "9999")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()

	c, sleeps := newTestClient()
	_, err := c.doGet(context.Background(), srv.URL, requestOptions{})
	if err == nil {
		t.Fatalf("doGet with an over-budget Retry-After = nil error, want CHTYPES_SOURCE_UNREACHABLE")
	}
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeSourceUnreachable {
		t.Fatalf("error = %v, want a CHTYPES_SOURCE_UNREACHABLE FetchError", err)
	}
	if len(*sleeps) != 0 {
		t.Fatalf("sleeps = %v, want none: an over-budget Retry-After is refused before sleeping at all", *sleeps)
	}
}

func TestDoGetNoRetryOn400(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer srv.Close()

	c, _ := newTestClient()
	res, err := c.doGet(context.Background(), srv.URL, requestOptions{})
	if err != nil {
		t.Fatalf("doGet should return the 400 status, not an error: %v", err)
	}
	if res.status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", res.status)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want exactly 1 (400 is never retried)", calls)
	}
}

func TestDoGetExhaustsAndFails(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	c, sleeps := newTestClient()
	_, err := c.doGet(context.Background(), srv.URL, requestOptions{})
	if err == nil {
		t.Fatalf("doGet should fail once the retry schedule is exhausted")
	}
	if calls != RetryAttempts {
		t.Fatalf("calls = %d, want %d (RetryAttempts)", calls, RetryAttempts)
	}
	if len(*sleeps) != RetryAttempts-1 {
		t.Fatalf("sleeps = %d entries, want %d", len(*sleeps), RetryAttempts-1)
	}
}

func TestRedirectDropsAuthorizationCrossOrigin(t *testing.T) {
	var secondOriginSawAuth bool
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		secondOriginSawAuth = r.Header.Get("Authorization") != ""
		w.WriteHeader(http.StatusOK)
	}))
	defer second.Close()

	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, second.URL+"/x", http.StatusFound)
	}))
	defer first.Close()

	c, _ := newTestClient()
	c.token = "secret-token"
	u, _ := url.Parse(first.URL)
	c.tokenHosts = []string{u.Host}

	res, err := c.doGet(context.Background(), first.URL, requestOptions{})
	if err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if res.status != http.StatusOK {
		t.Fatalf("status = %d, want 200", res.status)
	}
	if secondOriginSawAuth {
		t.Fatalf("Authorization leaked across a cross-origin redirect")
	}
}

func TestSameOriginRedirectKeepsAuthorization(t *testing.T) {
	var sawAuthOnFinal bool
	mux := http.NewServeMux()
	mux.HandleFunc("/start", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/final", http.StatusFound)
	})
	mux.HandleFunc("/final", func(w http.ResponseWriter, r *http.Request) {
		sawAuthOnFinal = r.Header.Get("Authorization") != ""
		w.WriteHeader(http.StatusOK)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c, _ := newTestClient()
	c.token = "secret-token"
	u, _ := url.Parse(srv.URL)
	c.tokenHosts = []string{u.Host}

	if _, err := c.doGet(context.Background(), srv.URL+"/start", requestOptions{}); err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if !sawAuthOnFinal {
		t.Fatalf("Authorization was dropped on a same-origin redirect; it should only be dropped cross-origin")
	}
}

func TestDownloadTokenSentOnlyToConfiguredHosts(t *testing.T) {
	var sawAuth bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawAuth = r.Header.Get("Authorization") == "Bearer secret-token"
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c, _ := newTestClient()
	c.token = "secret-token"
	// tokenHosts does NOT include this server: the token must not be sent.
	c.tokenHosts = []string{"unrelated.example:1"}
	if _, err := c.doGet(context.Background(), srv.URL, requestOptions{}); err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if sawAuth {
		t.Fatalf("the static download token was sent to a host it is not configured for")
	}
}

func TestAnonymousTokenFlow(t *testing.T) {
	var tokenEndpointCalls int
	tokenSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tokenEndpointCalls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"token":"anon-xyz"}`))
	}))
	defer tokenSrv.Close()

	var sawBearer string
	registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if auth == "" {
			w.Header().Set("WWW-Authenticate", `Bearer realm="`+tokenSrv.URL+`",service="registry.test",scope="repository:chtypes/v1:pull"`)
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		sawBearer = auth
		w.WriteHeader(http.StatusOK)
	}))
	defer registry.Close()

	c, _ := newTestClient()
	res, err := c.doGet(context.Background(), registry.URL, requestOptions{})
	if err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if res.status != http.StatusOK {
		t.Fatalf("status = %d, want 200", res.status)
	}
	if sawBearer != "Bearer anon-xyz" {
		t.Fatalf("final request Authorization = %q, want Bearer anon-xyz", sawBearer)
	}
	if tokenEndpointCalls != 1 {
		t.Fatalf("token endpoint calls = %d, want 1", tokenEndpointCalls)
	}
}

func TestDoGetSendsAcceptHeaderWhenSet(t *testing.T) {
	var sawAccept string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawAccept = r.Header.Get("Accept")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c, _ := newTestClient()
	if _, err := c.doGet(context.Background(), srv.URL, requestOptions{accept: manifestAccept}); err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if sawAccept != manifestAccept {
		t.Fatalf("Accept header = %q, want %q", sawAccept, manifestAccept)
	}
}

func TestDoGetSendsNoAcceptHeaderWhenUnset(t *testing.T) {
	var sawAccept string
	var called bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		sawAccept = r.Header.Get("Accept")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c, _ := newTestClient()
	if _, err := c.doGet(context.Background(), srv.URL, requestOptions{}); err != nil {
		t.Fatalf("doGet: %v", err)
	}
	if !called {
		t.Fatalf("server was never called")
	}
	if sawAccept != "" {
		t.Fatalf("Accept header = %q, want empty (net/http's own default) when opts.accept is unset", sawAccept)
	}
}

func TestDownloadToken401SkipsAnonymousFlow(t *testing.T) {
	// A static CHTYPES_DOWNLOAD_TOKEN that gets a 401 is a real auth
	// failure: the anonymous-token exchange must never fire for it.
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("WWW-Authenticate", `Bearer realm="http://should-not-be-called.invalid"`)
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	c, _ := newTestClient()
	c.token = "static-token"
	u, _ := url.Parse(srv.URL)
	c.tokenHosts = []string{u.Host}

	res, err := c.doGet(context.Background(), srv.URL, requestOptions{})
	if err != nil {
		t.Fatalf("doGet should return the 401 status, not an error: %v", err)
	}
	if res.status != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", res.status)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want exactly 1 (no anonymous-token retry for a static-token 401)", calls)
	}
}
