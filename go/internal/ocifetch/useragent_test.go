package ocifetch

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestUserAgentShape(t *testing.T) {
	got := userAgent()
	if !userAgentPattern.MatchString(got) {
		t.Fatalf("user agent %q does not match %s", got, userAgentPattern)
	}
	if want := "chtypes-go/" + bindingVersion; got != want {
		t.Fatalf("user agent = %q, want %q", got, want)
	}
	if bindingVersion == "" {
		t.Fatal("bindingVersion is empty; use userAgentDevVersion, never omit the header")
	}
}

// Every hop of a redirect chain carries the agent, not only the first request.
func TestUserAgentOnEveryHop(t *testing.T) {
	var seen []string
	mux := http.NewServeMux()
	mux.HandleFunc("/start", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get("User-Agent"))
		http.Redirect(w, r, "/end", http.StatusFound)
	})
	mux.HandleFunc("/end", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get("User-Agent"))
		_, _ = w.Write([]byte("ok"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c, _ := newTestClient()
	if _, err := c.doGet(context.Background(), srv.URL+"/start", requestOptions{maxBytes: 1 << 10}); err != nil {
		t.Fatal(err)
	}
	if len(seen) != 2 {
		t.Fatalf("saw %d requests, want 2: %v", len(seen), seen)
	}
	for i, ua := range seen {
		if ua != userAgent() {
			t.Fatalf("hop %d sent User-Agent %q, want %q", i, ua, userAgent())
		}
	}
}
