package ocifetch

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const contractBody = `{"errors":[{"code":"DENIED","message":"chtypes/v1 is retired: use chtypes/v2 (this registry no longer serves chtypes/v1)","detail":{"retired":"chtypes/v1","successor":"chtypes/v2"}}]}`

const contractMessage = "chtypes/v1 is retired: use chtypes/v2 (this registry no longer serves chtypes/v1)"

// TestRetiredMessageSharedTable runs retiredMessage over the one table all
// four bindings read (tests/fixtures/retired-message/cases.json), so the four
// give one answer for every body in it.
func TestRetiredMessageSharedTable(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "tests", "fixtures", "retired-message", "cases.json"))
	if os.IsNotExist(err) {
		// A bare copy of go/ (scripts/check-standalone.sh) has no fixtures:
		// skip by name, as every other fixture-driven test here does.
		t.Skipf("SKIPPED: the shared retired-message table is not beside this checkout (%v)", err)
	}
	if err != nil {
		t.Fatalf("reading the shared table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name     string  `json:"name"`
			BodyText *string `json:"body_text"`
			BodyHex  *string `json:"body_hex"`
			Message  *string `json:"message"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parsing the shared table: %v", err)
	}
	seen := map[string]bool{}
	for _, c := range table.Cases {
		seen[c.Name] = true
		var body []byte
		switch {
		case c.BodyText != nil:
			body = []byte(*c.BodyText)
		case c.BodyHex != nil:
			if body, err = hex.DecodeString(*c.BodyHex); err != nil {
				t.Fatalf("%s: body_hex: %v", c.Name, err)
			}
		default:
			t.Fatalf("%s: neither body_text nor body_hex", c.Name)
		}
		want := ""
		if c.Message != nil {
			want = *c.Message
		}
		if got := retiredMessage(body); got != want {
			t.Errorf("%s: retiredMessage = %q, want %q", c.Name, got, want)
		}
	}
	// The kinds the table must hold (public issue #571's list): a removed
	// table row would otherwise pass here in silence.
	for _, name := range []string{"c0-escape-sequence", "bidi-override", "over-cap-300", "non-bmp-is-the-256th", "empty-string", "message-number"} {
		if !seen[name] {
			t.Errorf("the shared table has no %q row", name)
		}
	}
}

func retiredServer(t *testing.T, handler http.HandlerFunc) (*httptest.Server, *int) {
	t.Helper()
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func wantRetired(t *testing.T, err error, url string) *FetchError {
	t.Helper()
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeSourceRetired {
		t.Fatalf("error = %v, want a CHTYPES_SOURCE_RETIRED FetchError", err)
	}
	if !errors.Is(err, ErrSourceRetired) {
		t.Fatalf("errors.Is(err, ErrSourceRetired) = false")
	}
	if !strings.Contains(err.Error(), url) {
		t.Fatalf("error %q does not name the URL that answered, %s", err, url)
	}
	if ExitCode(err) != 10 {
		t.Fatalf("ExitCode = %d, want 10", ExitCode(err))
	}
	return fe
}

// A 410 is answered once: no retry, no sleep, the registry's message in the
// error, even when the next answer would have been a 200.
func TestDoGet410IsRetiredAndNeverRetried(t *testing.T) {
	srv, calls := retiredServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/after" {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusGone)
		_, _ = w.Write([]byte(contractBody))
	})
	c, sleeps := newTestClient()
	_, err := c.doGet(context.Background(), srv.URL+"/v2/chtypes/v1/manifests/26.9", requestOptions{maxBytes: 16})
	wantRetired(t, err, srv.URL+"/v2/chtypes/v1/manifests/26.9")
	if !strings.Contains(err.Error(), "the registry says: "+contractMessage) {
		t.Fatalf("error %q does not carry the registry's message (read past the request's own 16-byte cap)", err)
	}
	if *calls != 1 || len(*sleeps) != 0 {
		t.Fatalf("calls = %d, sleeps = %v; want 1 call and no sleep", *calls, *sleeps)
	}
}

// A 410 reached through a redirect names the URL that answered it.
func TestDoGet410AfterRedirect(t *testing.T) {
	srv, _ := retiredServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/gone" {
			w.WriteHeader(http.StatusGone)
			return
		}
		http.Redirect(w, r, "/gone", http.StatusFound)
	})
	c, _ := newTestClient()
	_, err := c.doGet(context.Background(), srv.URL+"/start", requestOptions{})
	wantRetired(t, err, srv.URL+"/gone")
	for _, bad := range []string{"null", "<nil>", "registry says"} {
		if strings.Contains(err.Error(), bad) {
			t.Fatalf("error %q, with no body, contains %q", err, bad)
		}
	}
}

// Only RetiredBodyMaxBytes of a 410's body is read: a longer one is not an
// error of its own, it just carries no message.
func TestDoGet410ReadsAtMostTheCap(t *testing.T) {
	long := `{"errors":[{"message":"` + strings.Repeat("x", RetiredBodyMaxBytes) + `"}]}`
	srv, _ := retiredServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
		_, _ = w.Write([]byte(long))
	})
	c, _ := newTestClient()
	_, err := c.doGet(context.Background(), srv.URL+"/x", requestOptions{})
	wantRetired(t, err, srv.URL+"/x")
	if strings.Contains(err.Error(), "registry says") {
		t.Fatalf("error %q carries a message from a body past the cap", err)
	}
}

// A 410 on the first base ends the loop: the second base, which would serve,
// is never asked, under every 404 policy.
func TestFetchAcrossBasesStopsAtRetired(t *testing.T) {
	for _, policy := range []notFoundPolicy{notFoundUnpublished, notFoundRetryOnLast, notFoundAlias} {
		gone, _ := retiredServer(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusGone)
			_, _ = w.Write([]byte(contractBody))
		})
		serving, servingCalls := retiredServer(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		})
		c, sleeps := newTestClient()
		s := &session{client: c}
		_, _, err := s.fetchAcrossBases(context.Background(), []string{gone.URL + "/chtypes/v1", serving.URL + "/chtypes/v1"}, "manifests/26.9", policy, requestOptions{})
		wantRetired(t, err, gone.URL+"/v2/chtypes/v1/manifests/26.9")
		if *servingCalls != 0 || len(*sleeps) != 0 {
			t.Fatalf("policy %d: the second base was asked %d time(s), sleeps %v; want 0 and none", policy, *servingCalls, *sleeps)
		}
	}
}
