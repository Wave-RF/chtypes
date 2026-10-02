package ocifetch

// conformance_test.go — TestConformanceV1, the runner v1.yml's
// v1-conformance job invokes (`go test -count=1 -run '^TestConformanceV1$'
// ./internal/ocifetch/`; plan §3.3). It reads CHTYPES_V1_CONFORMANCE and
// skips loudly when unset, so a bare-copy `go test ./...` (scripts/check-
// standalone.sh) still passes with nothing to fetch.
//
// PROVISIONAL: lane 0B (fixtures, the scripted HTTP server, the parity gate)
// has not landed on this branch yet, so cases.json and
// scripts/fetch-v1/server.py do not exist to test this against. This file
// implements the documented contract (plan §3.2's case shape,
// spec/fetch-v1/schema/cases.schema.json and report.schema.json) as
// precisely as it can be read today; expect to iterate once 0B merges and a
// real conformance run can drive it — see MERGE NOTES.

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

type confCase struct {
	ID         string            `json:"id"`
	Tree       string            `json:"tree"`
	Transports []string          `json:"transports"`
	HTTPScript *string           `json:"http_script"`
	Setup      confSetup         `json:"setup"`
	Request    confRequest       `json:"request"`
	Env        map[string]string `json:"env"`
	Expect     confExpect        `json:"expect"`
}

type confSetup struct {
	Cache                 string   `json:"cache"`
	SystemDirs            []string `json:"system_dirs"`
	Lock                  *string  `json:"lock"`
	BeforeIndexRenameHook *string  `json:"before_index_rename_hook"`
}

type confRequest struct {
	Spelling      string   `json:"spelling"`
	Platform      string   `json:"platform"`
	Offline       bool     `json:"offline"`
	Frozen        bool     `json:"frozen"`
	LockWrite     bool     `json:"lock_write"`
	Update        bool     `json:"update"`
	AllowUnsigned bool     `json:"allow_unsigned"`
	Trust         string   `json:"trust"`
	Bases         []string `json:"bases"`
}

type confExpect struct {
	OK            bool               `json:"ok"`
	Version       *string            `json:"version"`
	Build         *string            `json:"build"`
	Manifest      *string            `json:"manifest"`
	LibrarySHA256 *string            `json:"library_sha256"`
	Code          *string            `json:"code"`
	Sleeps        []float64          `json:"sleeps"`
	Warnings      []string           `json:"warnings"`
	Requests      confRequestsExpect `json:"requests"`
	LockAfter     *string            `json:"lock_after"`
}

type confRequestsExpect struct {
	Max                *int     `json:"max"`
	NoneMatching       []string `json:"none_matching"`
	AuthOnSecondOrigin bool     `json:"auth_on_second_origin"`
}

type casesFile struct {
	Schema int        `json:"schema"`
	Cases  []confCase `json:"cases"`
}

type reportResult struct {
	ID        string `json:"id"`
	Transport string `json:"transport"`
	Verdict   string `json:"verdict"`
	Detail    string `json:"detail"`
}

type confReport struct {
	Schema      int            `json:"schema"`
	Binding     string         `json:"binding"`
	Toolchain   string         `json:"toolchain"`
	CasesSHA256 string         `json:"cases_sha256"`
	Results     []reportResult `json:"results"`
}

// TestConformanceV1 is the v1 fetch-layer conformance runner for the Go
// binding (plan §3.3).
func TestConformanceV1(t *testing.T) {
	fixturesDir := os.Getenv("CHTYPES_V1_CONFORMANCE")
	if fixturesDir == "" {
		t.Skip("CHTYPES_V1_CONFORMANCE is not set; skipping the v1 conformance suite (docs/guides/fetch-v1.md §10)")
	}

	raw, err := os.ReadFile(filepath.Join(fixturesDir, "cases.json"))
	if err != nil {
		t.Fatalf("reading cases.json under %s: %v", fixturesDir, err)
	}
	sum := sha256.Sum256(raw)
	casesSHA256 := hex.EncodeToString(sum[:])

	var cf casesFile
	if err := strictUnmarshal(raw, &cf); err != nil {
		t.Fatalf("parsing cases.json: %v", err)
	}
	if len(cf.Cases) == 0 {
		t.Fatalf("cases.json names zero cases")
	}

	port, port2, stop := startFixtureServer(t, fixturesDir)
	defer stop()

	registryBase := os.Getenv("CHTYPES_V1_REGISTRY_BASE") // set only by the v1-network job

	report := confReport{Schema: 1, Binding: "go", Toolchain: goToolchainID(), CasesSHA256: casesSHA256}

	for _, c := range cf.Cases {
		for _, transport := range c.Transports {
			if transport == "registry" && registryBase == "" {
				continue // the v1-network job produces registry-transport results separately
			}
			result := runOneCase(t, fixturesDir, port, port2, registryBase, c, transport)
			report.Results = append(report.Results, result)
		}
	}

	if path := os.Getenv("CHTYPES_V1_REPORT"); path != "" {
		out, err := json.MarshalIndent(&report, "", "  ")
		if err != nil {
			t.Fatalf("marshaling the report: %v", err)
		}
		if err := os.WriteFile(path, out, 0o644); err != nil {
			t.Fatalf("writing the report to %s: %v", path, err)
		}
	}

	for _, r := range report.Results {
		if r.Verdict != "pass" {
			t.Errorf("%s [%s]: %s", r.ID, r.Transport, r.Detail)
		}
	}
}

func goToolchainID() string {
	// A short, stable identity for the report's "toolchain" field
	// (report.schema.json); v1.yml's matrix names the exact leg separately.
	return runtime.Version()
}

// startFixtureServer starts scripts/fetch-v1/server.py (lane 0B) and parses
// its "LISTENING <port> <port2>" line. It is written against the shape the
// plan documents (§3.3); it has not run against a real server.py yet.
func startFixtureServer(t *testing.T, fixturesDir string) (port, port2 int, stop func()) {
	t.Helper()
	scriptPath := findServerScript(t)
	cmd := exec.CommandContext(context.Background(), "python3", scriptPath, "--fixtures", fixturesDir, "--port", "0")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("StdoutPipe: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting %s: %v", scriptPath, err)
	}
	scanner := bufio.NewScanner(stdout)
	var line string
	ready := make(chan struct{})
	go func() {
		if scanner.Scan() {
			line = scanner.Text()
		}
		close(ready)
	}()
	select {
	case <-ready:
	case <-time.After(10 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatalf("timed out waiting for %s to print LISTENING", scriptPath)
	}
	fields := strings.Fields(line)
	if len(fields) != 3 || fields[0] != "LISTENING" {
		_ = cmd.Process.Kill()
		t.Fatalf("expected %q to print \"LISTENING <port> <port2>\", got %q", scriptPath, line)
	}
	p1, err1 := strconv.Atoi(fields[1])
	p2, err2 := strconv.Atoi(fields[2])
	if err1 != nil || err2 != nil {
		_ = cmd.Process.Kill()
		t.Fatalf("could not parse ports from %q", line)
	}
	return p1, p2, func() { _ = cmd.Process.Kill() }
}

func findServerScript(t *testing.T) string {
	t.Helper()
	// go/internal/ocifetch -> repository root -> scripts/fetch-v1/server.py
	candidate := filepath.Join("..", "..", "..", "scripts", "fetch-v1", "server.py")
	if _, err := os.Stat(candidate); err != nil {
		t.Fatalf("scripts/fetch-v1/server.py not found at %s (lane 0B has not landed on this branch): %v", candidate, err)
	}
	return candidate
}

// expandBase turns one of a case's request.bases templates ("{base}") into
// a real base URL for transport (plan §3.2).
func expandBase(template, transport, fixturesDir, tree, caseID, registryBase string, port, port2 int) (string, error) {
	_ = port2
	switch transport {
	case "file":
		abs, err := filepath.Abs(filepath.Join(fixturesDir, "trees", tree, "v2", "chtypes", "v1"))
		if err != nil {
			return "", err
		}
		return strings.ReplaceAll(template, "{base}", "file://"+abs), nil
	case "http":
		return strings.ReplaceAll(template, "{base}", fmt.Sprintf("http://127.0.0.1:%d/s-%s/chtypes/v1", port, caseID)), nil
	case "registry":
		if registryBase == "" {
			return "", fmt.Errorf("registry transport requested with no CHTYPES_V1_REGISTRY_BASE set")
		}
		return strings.ReplaceAll(template, "{base}", registryBase), nil
	default:
		return "", fmt.Errorf("unknown transport %q", transport)
	}
}

// runOneCase executes c against one transport and returns its report row.
// It configures a session from c.Request/c.Setup/c.Env exactly as the
// corresponding real binding code would, drives Ensure (or ResolveInstalled
// for an offline-only case), and compares the outcome to c.Expect.
func runOneCase(t *testing.T, fixturesDir string, port, port2 int, registryBase string, c confCase, transport string) reportResult {
	t.Helper()
	row := reportResult{ID: c.ID, Transport: transport}

	bases := make([]string, 0, len(c.Request.Bases))
	for _, tmpl := range c.Request.Bases {
		b, err := expandBase(tmpl, transport, fixturesDir, c.Tree, c.ID, registryBase, port, port2)
		if err != nil {
			row.Verdict, row.Detail = "fail", err.Error()
			return row
		}
		bases = append(bases, b)
	}

	cacheDir := t.TempDir()
	if c.Setup.Cache != "" && c.Setup.Cache != "empty" {
		if err := seedCache(fixturesDir, c.Setup.Cache, cacheDir); err != nil {
			row.Verdict, row.Detail = "fail", fmt.Sprintf("seeding the cache fixture %q: %v", c.Setup.Cache, err)
			return row
		}
	}

	var sleeps []time.Duration
	clock := Clock{
		Now: time.Now,
		Sleep: func(_ context.Context, d time.Duration) {
			sleeps = append(sleeps, d)
		},
	}

	opts := &Options{
		Bases:         bases,
		CacheDir:      cacheDir,
		AllowUnsigned: c.Request.AllowUnsigned,
		Offline:       c.Request.Offline,
		Frozen:        c.Request.Frozen,
		LockWrite:     c.Request.LockWrite,
		Update:        c.Request.Update,
		Clock:         &clock,
	}
	if c.Request.Trust == "test" {
		opts.TrustedKeys = []string{testKeyHexForConformance(t, fixturesDir)}
	}
	if c.Setup.Lock != nil {
		lockPath := filepath.Join(cacheDir, "chtypes.lock")
		src, err := os.ReadFile(filepath.Join(fixturesDir, "locks", *c.Setup.Lock+".json"))
		if err == nil {
			_ = os.WriteFile(lockPath, src, 0o644)
			opts.LockPath = lockPath
		}
	}
	for k, v := range c.Env {
		switch k {
		case EnvCacheName:
			opts.CacheDir = v
		case EnvTokenName:
			opts.Token = v
		case EnvTrustedKeysName:
			opts.TrustedKeys = append(opts.TrustedKeys, strings.Split(v, ",")...)
		case EnvAllowUnsignedName:
			opts.AllowUnsigned = v == "1"
		}
	}

	req := Request{Spelling: c.Request.Spelling, Platform: c.Request.Platform}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	resolved, err := Ensure(ctx, req, opts)

	if detail := compareOutcome(c.Expect, resolved, err, sleeps); detail != "" {
		row.Verdict, row.Detail = "fail", detail
		return row
	}
	row.Verdict = "pass"
	return row
}

// seedCache copies a named cache fixture (tests/fixtures/fetch-v1/layouts/
// or .../caches/<name>/) into dir. The exact fixture directory layout is
// lane 0B's to define; this best-effort join covers the shape the plan
// names (plan §3.2's "layouts/<name>/").
func seedCache(fixturesDir, name, dir string) error {
	src := filepath.Join(fixturesDir, "layouts", name)
	return copyDir(src, dir)
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, b, 0o644)
	})
}

func testKeyHexForConformance(t *testing.T, fixturesDir string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(fixturesDir, "test-key", "public.hex"))
	if err != nil {
		t.Fatalf("reading the fixtures' test-key/public.hex: %v", err)
	}
	return strings.TrimSpace(string(b))
}

// compareOutcome checks (resolved, err, sleeps) against expect and returns
// an empty string on a match, or the failed assertion otherwise.
func compareOutcome(expect confExpect, resolved *Resolved, err error, sleeps []time.Duration) string {
	if expect.OK {
		if err != nil {
			return fmt.Sprintf("expected ok, got error: %v", err)
		}
		if expect.Version != nil && resolved.Version != *expect.Version {
			return fmt.Sprintf("version = %q, want %q", resolved.Version, *expect.Version)
		}
		if expect.Build != nil && resolved.Build != *expect.Build {
			return fmt.Sprintf("build = %q, want %q", resolved.Build, *expect.Build)
		}
		if expect.Manifest != nil && string(resolved.Digests.Manifest) != *expect.Manifest {
			return fmt.Sprintf("manifest digest = %q, want %q", resolved.Digests.Manifest, *expect.Manifest)
		}
	} else if err == nil {
		return "expected a failure, got ok"
	} else if expect.Code != nil {
		var fe *FetchError
		if !fetchErrorAs(err, &fe) {
			return fmt.Sprintf("expected code %s, got a non-FetchError: %v", *expect.Code, err)
		}
		if string(fe.Code) != *expect.Code {
			return fmt.Sprintf("code = %s, want %s", fe.Code, *expect.Code)
		}
	}
	if expect.Sleeps != nil {
		if len(sleeps) != len(expect.Sleeps) {
			return fmt.Sprintf("sleeps = %v, want %v", sleeps, expect.Sleeps)
		}
		for i, want := range expect.Sleeps {
			if sleeps[i] != time.Duration(want*float64(time.Second)) {
				return fmt.Sprintf("sleeps[%d] = %v, want %gs", i, sleeps[i], want)
			}
		}
	}
	return ""
}

func fetchErrorAs(err error, target **FetchError) bool {
	for err != nil {
		if fe, ok := err.(*FetchError); ok {
			*target = fe
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}
