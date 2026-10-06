package ocifetch

// conformance_test.go — TestConformanceV1, the runner v1.yml's
// v1-conformance job invokes (`go test -count=1 -run '^TestConformanceV1$'
// ./internal/ocifetch/`; docs/guides/fetch-v1.md §10). It reads
// CHTYPES_V1_CONFORMANCE and skips loudly when unset, so a bare-copy
// `go test ./...` (scripts/check-standalone.sh) still passes with nothing to
// fetch.

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
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
// binding (docs/guides/fetch-v1.md §10).
func TestConformanceV1(t *testing.T) {
	fixturesDir := os.Getenv("CHTYPES_V1_CONFORMANCE")
	if fixturesDir == "" {
		t.Skip("CHTYPES_V1_CONFORMANCE is not set; skipping the v1 conformance suite (docs/guides/fetch-v1.md §10)")
	}
	// This package's tests run under the v1 fetch contract (main_test.go):
	// the cases are its specification. The ABI v2 dev channel narrows it, and
	// its own rules (r5, r6) are TestDevChannel* in this package.
	t.Logf("fetch contract: %s (the fetch-v1 cases' own; the dev channel this binding ships is tested by TestDevChannel*)", ChannelName())
	if ChannelName() != "v1" {
		t.Fatalf("the fetch-v1 conformance cases must run under the v1 contract, not %q", ChannelName())
	}
	abs, err := filepath.Abs(fixturesDir)
	if err == nil {
		fixturesDir = abs
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
	// v1.yml's matrix names the Go leg by minor line ("go1.27.x") and
	// parity.py requires that exact string, while runtime.Version() is
	// the patch release ("go1.27.1"); report the matrix's spelling.
	if v := os.Getenv("CHTYPES_V1_TOOLCHAIN"); v != "" {
		return v
	}
	v := runtime.Version()
	if m := goMinorRE.FindStringSubmatch(v); m != nil {
		return m[1] + ".x"
	}
	return v
}

var goMinorRE = regexp.MustCompile(`^(go\d+\.\d+)(?:\.\d+|rc\d+|beta\d+)?`)

// startFixtureServer starts the fixtures lane's scripted server (under
// scripts/fetch-v1/) and parses its "LISTENING <port> <port2>" line.
func startFixtureServer(t *testing.T, fixturesDir string) (port, port2 int, stop func()) {
	t.Helper()
	scriptPath := findServerScript(t)
	cmd := exec.CommandContext(context.Background(), "python3", scriptPath, "--fixtures", fixturesDir, "--port", "0")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("StdoutPipe: %v", err)
	}
	cmd.Stderr = os.Stderr
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
	// go/internal/ocifetch -> repository root -> scripts/fetch-v1/ -> server.py
	candidate := filepath.Join("..", "..", "..", "scripts", "fetch-v1", "server.py")
	if _, err := os.Stat(candidate); err != nil {
		t.Fatalf("the fixtures lane's scripted server was not found at %s: %v", candidate, err)
	}
	return candidate
}

// expandBase turns one of a case's request.bases templates ("{base}", or
// "{base}" embedded in a longer string such as "{base}/does-not-exist") into
// a real base URL for transport (docs/guides/fetch-v1.md §2, §10). "{base2}"
// (http transport only, lane 0B 2026-10-02) is the same "s-<case-id>/chtypes/v1"
// suffix rooted at server.py's SECOND origin (its "LISTENING <port> <port2>"
// line) — a case needing two genuinely independent bases over the same case
// id, such as mirror-failover-5xx/-digest-404, whose base[1] is served from
// the script's own second_origin_routes with its own response-sequence
// cursor.
func expandBase(template, transport, fixturesDir, tree, caseID, registryBase string, port, port2 int) (string, error) {
	switch transport {
	case "file":
		abs, err := filepath.Abs(filepath.Join(fixturesDir, "trees", tree, "v2", "chtypes", "v1"))
		if err != nil {
			return "", err
		}
		return strings.ReplaceAll(template, "{base}", "file://"+abs), nil
	case "http":
		out := strings.ReplaceAll(template, "{base}", fmt.Sprintf("http://127.0.0.1:%d/s-%s/chtypes/v1", port, caseID))
		out = strings.ReplaceAll(out, "{base2}", fmt.Sprintf("http://127.0.0.1:%d/s-%s/chtypes/v1", port2, caseID))
		return out, nil
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
		if err := copyDir(filepath.Join(fixturesDir, "layouts", c.Setup.Cache), cacheDir); err != nil {
			row.Verdict, row.Detail = "fail", fmt.Sprintf("seeding the cache fixture %q: %v", c.Setup.Cache, err)
			return row
		}
	}

	var systemDirs []string
	for _, name := range c.Setup.SystemDirs {
		sysDir := t.TempDir()
		if err := copyDir(filepath.Join(fixturesDir, "layouts", name), sysDir); err != nil {
			row.Verdict, row.Detail = "fail", fmt.Sprintf("seeding the system-dir fixture %q: %v", name, err)
			return row
		}
		systemDirs = append(systemDirs, sysDir)
	}

	trustedKeysHex := []string{}
	if c.Request.Trust == "test" {
		trustedKeysHex = []string{testKeyHexForConformance(t, fixturesDir)}
	}
	for k, v := range c.Env {
		if k == EnvTrustedKeysName {
			trustedKeysHex = append(trustedKeysHex, strings.Split(v, ",")...)
		}
	}
	preInstallKeys, err := resolvePreInstallKeys(trustedKeysHex)
	if err != nil {
		row.Verdict, row.Detail = "fail", err.Error()
		return row
	}

	// "Cache fixtures and installed.json" (docs/guides/fetch-v1.md §10): a
	// layout fixture that carries installed.json names manifest digests
	// that must already be INSTALLED, not merely present, before the
	// case's clock starts — verified and unpacked now, offline, from local
	// blobs, for every one of this case's cache/system directories.
	for _, dir := range append([]string{cacheDir}, systemDirs...) {
		if err := preInstallFromDir(dir, preInstallKeys); err != nil {
			row.Verdict, row.Detail = "fail", err.Error()
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

	var reqMu sync.Mutex
	var reqTexts []string
	sawAuthOnSecondOrigin := false
	secondOriginHost := fmt.Sprintf("127.0.0.1:%d", port2)
	onRequest := func(req *http.Request) {
		reqMu.Lock()
		defer reqMu.Unlock()
		reqTexts = append(reqTexts, req.Method+" "+req.URL.String())
		if req.URL.Host == secondOriginHost && req.Header.Get("Authorization") != "" {
			sawAuthOnSecondOrigin = true
		}
	}

	lockPath := filepath.Join(cacheDir, "chtypes.lock")
	if c.Setup.Lock != nil {
		src, rerr := os.ReadFile(filepath.Join(fixturesDir, "locks", "inputs", *c.Setup.Lock+".json"))
		if rerr != nil {
			row.Verdict, row.Detail = "fail", fmt.Sprintf("reading input lock %q: %v", *c.Setup.Lock, rerr)
			return row
		}
		if werr := os.WriteFile(lockPath, src, 0o644); werr != nil {
			row.Verdict, row.Detail = "fail", werr.Error()
			return row
		}
	}

	opts := &Options{
		Bases:      bases,
		CacheDir:   cacheDir,
		SystemDirs: systemDirs,
		// "stall-timeout-retried" needs its stall actually detected
		// quickly: the production defaults (30s connect, 60s idle-read)
		// would blow through this function's own 30s ctx deadline on the
		// FIRST attempt, which then makes every subsequent attempt fail
		// instantly too (the parent context is already past its
		// deadline) — exhausting the retry budget without the real retry
		// ever being exercised. A short, test-only timeout keeps every
		// case's wall-clock time trivial and lets a real stall resolve
		// through a real retry instead.
		ConnectTimeout:  2 * time.Second,
		IdleReadTimeout: 2 * time.Second,
		AllowUnsigned:   c.Request.AllowUnsigned,
		Offline:         c.Request.Offline,
		Frozen:          c.Request.Frozen,
		LockWrite:       c.Request.LockWrite,
		Update:          c.Request.Update,
		LockPath:        lockPath,
		Clock:           &clock,
		OnRequest:       onRequest,
		TrustedKeys:     trustedKeysHex,
	}

	if c.Setup.BeforeIndexRenameHook != nil {
		opts.HookBeforeIndexRename = indexRenameHookFor(*c.Setup.BeforeIndexRenameHook, cacheDir)
	}

	for k, v := range c.Env {
		switch k {
		case EnvCacheName:
			opts.CacheDir = v
		case EnvTokenName:
			opts.Token = v
		case EnvAllowUnsignedName:
			opts.AllowUnsigned = v == "1"
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var resolvedManifest, resolvedLibrarySHA256 string
	var resolvedVersion, resolvedBuild *string
	var warnings []string
	var runErr error

	switch {
	case strings.HasPrefix(c.ID, "goldens-"):
		// request.spelling is the SUBJECT (a platform manifest); FetchSigned
		// itself discovers its goldens referrers, verifies each and returns
		// the highest revision (docs/guides/fetch-v1.md §9).
		signed, ferr := FetchSigned(ctx, "", c.Request.Spelling, PredicateTypeGoldens, opts)
		runErr = ferr
		if ferr == nil {
			sum := sha256.Sum256(signed.Bytes)
			resolvedManifest, resolvedLibrarySHA256 = string(signed.Digests.Manifest), hex.EncodeToString(sum[:])
			warnings = signed.Warnings
		}
	case strings.HasPrefix(c.ID, "fixtures-"):
		// The fixtures repository is a sibling path under the same base
		// (FixturesRepoSuffix), reached directly by digest — no referrer
		// indirection, unlike goldens.
		signed, ferr := FetchSigned(ctx, FixturesRepoSuffix, c.Request.Spelling, PredicateTypeFixtures, opts)
		runErr = ferr
		if ferr == nil {
			sum := sha256.Sum256(signed.Bytes)
			resolvedManifest, resolvedLibrarySHA256 = string(signed.Digests.Manifest), hex.EncodeToString(sum[:])
			warnings = signed.Warnings
		}
	case strings.HasPrefix(c.ID, "resolve-installed-"):
		// The cache-only seam entry (docs/guides/fetch-v1.md §10): a miss is
		// reported as CHTYPES_ARTIFACT_MISSING, the code --offline gives.
		resolved, ferr := ResolveInstalled(Request{Spelling: c.Request.Spelling, Platform: c.Request.Platform}, c.Request.Platform, opts)
		if ferr == nil && resolved == nil {
			ferr = newError(CodeArtifactMissing, c.Request.Spelling, c.Request.Platform, "", nil, "no installed artifact satisfies %s", c.Request.Spelling)
		}
		runErr = ferr
		if ferr == nil {
			resolvedManifest = string(resolved.Digests.Manifest)
			resolvedLibrarySHA256 = sha256FileHex(resolved.LibraryPath)
			resolvedVersion, resolvedBuild = &resolved.Version, &resolved.Build
			warnings = resolved.Warnings
		}
	default:
		resolved, ferr := Ensure(ctx, Request{Spelling: c.Request.Spelling, Platform: c.Request.Platform}, opts)
		runErr = ferr
		if ferr == nil {
			resolvedManifest = string(resolved.Digests.Manifest)
			resolvedLibrarySHA256 = sha256FileHex(resolved.LibraryPath)
			resolvedVersion, resolvedBuild = &resolved.Version, &resolved.Build
			warnings = resolved.Warnings
		}
	}

	if detail := compareOutcome(c.Expect, runErr, resolvedVersion, resolvedBuild, resolvedManifest, resolvedLibrarySHA256, warnings, sleeps); detail != "" {
		row.Verdict, row.Detail = "fail", detail
		return row
	}

	reqMu.Lock()
	reqSnapshot := append([]string(nil), reqTexts...)
	reqMu.Unlock()
	if detail := checkRequests(c.Expect.Requests, reqSnapshot, sawAuthOnSecondOrigin); detail != "" {
		row.Verdict, row.Detail = "fail", detail
		return row
	}

	if c.Expect.LockAfter != nil {
		if detail := checkLockAfter(fixturesDir, *c.Expect.LockAfter, lockPath); detail != "" {
			row.Verdict, row.Detail = "fail", detail
			return row
		}
	}

	row.Verdict = "pass"
	return row
}

// resolvePreInstallKeys resolves the trust list a case's installed.json
// pre-install step verifies against: the same keys Options.TrustedKeys will
// carry for the real call, falling back to the default release keys when
// none are configured (an installed.json fixture under "trust": "release"
// never occurs today, but this keeps the two trust resolutions consistent).
func resolvePreInstallKeys(hexKeys []string) ([]ed25519.PublicKey, error) {
	if len(hexKeys) > 0 {
		return parseHexKeys(hexKeys)
	}
	keys := make([]ed25519.PublicKey, 0, len(ReleaseKeys))
	for _, rk := range ReleaseKeys {
		pk, err := hexToPublicKey(rk.Ed25519Hex)
		if err != nil {
			return nil, err
		}
		keys = append(keys, pk)
	}
	return keys, nil
}

// preInstallFromDir reads dir/installed.json, if present, and verifies and
// unpacks every manifest digest it names, offline, from dir's own local
// blobs (docs/guides/fetch-v1.md §10).
func preInstallFromDir(dir string, trustedKeys []ed25519.PublicKey) error {
	data, err := os.ReadFile(filepath.Join(dir, "installed.json"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var doc struct {
		Installed []string `json:"installed"`
	}
	if uerr := strictUnmarshal(data, &doc); uerr != nil {
		return fmt.Errorf("parsing %s/installed.json: %w", dir, uerr)
	}
	l := newLayout(dir, false)
	for _, digestStr := range doc.Installed {
		if err := installPreseededByDigest(l, Digest(digestStr), trustedKeys); err != nil {
			return fmt.Errorf("pre-installing %s from %s/installed.json: %w", digestStr, dir, err)
		}
	}
	return nil
}

// indexRenameHookFor builds the runner-side race hook a case's
// before_index_rename_hook names (docs/guides/fetch-v1.md §10: "the name of
// a race hook the RUNNER installs"). "index-race-reapply" is the only one
// any case uses today: it simulates a second process completing its own
// index.json write between this fetch's read and its own rename, so the
// read-check-rename loop (layout.go's addIndexEntry) must observe the
// change and re-merge onto it rather than clobbering it.
func indexRenameHookFor(name, cacheDir string) func() {
	if name != "index-race-reapply" {
		return nil
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			competing := Descriptor{
				MediaType: MediaTypeManifest,
				Digest:    Digest("sha256:" + strings.Repeat("c0ffee00", 8)),
				Size:      1,
			}
			_ = newLayout(cacheDir, false).addIndexEntry(competing, nil)
		})
	}
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

func sha256FileHex(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// compareOutcome checks the outcome of one Ensure/FetchSigned call against
// expect and returns an empty string on a match, or the failed assertion
// otherwise.
func compareOutcome(expect confExpect, runErr error, version, build *string, manifest, librarySHA256 string, warnings []string, sleeps []time.Duration) string {
	if expect.OK {
		if runErr != nil {
			return fmt.Sprintf("expected ok, got error: %v", runErr)
		}
		if expect.Version != nil && (version == nil || *version != *expect.Version) {
			got := "<nil>"
			if version != nil {
				got = *version
			}
			return fmt.Sprintf("version = %q, want %q", got, *expect.Version)
		}
		if expect.Build != nil && (build == nil || *build != *expect.Build) {
			got := "<nil>"
			if build != nil {
				got = *build
			}
			return fmt.Sprintf("build = %q, want %q", got, *expect.Build)
		}
		if expect.Manifest != nil && manifest != *expect.Manifest {
			return fmt.Sprintf("manifest digest = %q, want %q", manifest, *expect.Manifest)
		}
		if expect.LibrarySHA256 != nil && librarySHA256 != *expect.LibrarySHA256 {
			return fmt.Sprintf("library_sha256 = %q, want %q", librarySHA256, *expect.LibrarySHA256)
		}
	} else if runErr == nil {
		return "expected a failure, got ok"
	} else if expect.Code != nil {
		var fe *FetchError
		if !fetchErrorAs(runErr, &fe) {
			return fmt.Sprintf("expected code %s, got a non-FetchError: %v", *expect.Code, runErr)
		}
		if string(fe.Code) != *expect.Code {
			return fmt.Sprintf("code = %s, want %s: %v", fe.Code, *expect.Code, runErr)
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
	for _, want := range expect.Warnings {
		found := false
		for _, got := range warnings {
			if strings.Contains(got, want) {
				found = true
				break
			}
		}
		if !found {
			return fmt.Sprintf("warnings %v do not contain a warning matching %q", warnings, want)
		}
	}
	return ""
}

// checkRequests applies expect.requests against the observed request log
// (every request this process itself made, across every retry and
// redirect — §10's "GET /_log/s-<id>" need, served here by an OnRequest
// hook instead, which also works for the file transport, which has no such
// endpoint).
func checkRequests(expect confRequestsExpect, reqTexts []string, sawAuthOnSecondOrigin bool) string {
	if expect.Max != nil && len(reqTexts) > *expect.Max {
		return fmt.Sprintf("made %d requests, want at most %d: %v", len(reqTexts), *expect.Max, reqTexts)
	}
	for _, pattern := range expect.NoneMatching {
		for _, text := range reqTexts {
			if noneMatchingViolated(pattern, text) {
				return fmt.Sprintf("request %q matches the forbidden pattern %q", text, pattern)
			}
		}
	}
	if expect.AuthOnSecondOrigin != sawAuthOnSecondOrigin {
		return fmt.Sprintf("auth_on_second_origin = %v, want %v", sawAuthOnSecondOrigin, expect.AuthOnSecondOrigin)
	}
	return ""
}

// noneMatchingViolated reports whether text matches pattern. Go's RE2
// engine (encoding/regexp) cannot compile a negative lookahead such as
// "GET .*/manifests/(?!sha256:)" (cases.json's one use of the shape, for
// fixtures-digest-pin-no-tag-fallback), so that one shape — "<prefix>(?!
// <negated>)" — is handled by hand: the prefix must match, and what follows
// it must not start with the negated text. Any other pattern is a plain
// RE2 regex.
func noneMatchingViolated(pattern, text string) bool {
	if idx := strings.Index(pattern, "(?!"); idx >= 0 {
		end := strings.Index(pattern[idx:], ")")
		if end < 0 {
			return false
		}
		prefix := pattern[:idx]
		negated := pattern[idx+3 : idx+end]
		re, err := regexp.Compile("^" + prefix)
		if err != nil {
			return false
		}
		loc := re.FindStringIndex(text)
		if loc == nil {
			return false
		}
		return !strings.HasPrefix(text[loc[1]:], negated)
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return false
	}
	return re.MatchString(text)
}

// checkLockAfter compares the lock file this case wrote (if any) against
// its named expected-lock fixture, structurally (field order never
// matters).
func checkLockAfter(fixturesDir, name, lockPath string) string {
	wantBytes, err := os.ReadFile(filepath.Join(fixturesDir, "locks", "expected", name+".json"))
	if err != nil {
		return fmt.Sprintf("reading expected lock %q: %v", name, err)
	}
	gotBytes, err := os.ReadFile(lockPath)
	if err != nil {
		return fmt.Sprintf("reading the written lock at %s: %v", lockPath, err)
	}
	var want, got any
	if err := json.Unmarshal(wantBytes, &want); err != nil {
		return fmt.Sprintf("expected lock %q is not valid JSON: %v", name, err)
	}
	if err := json.Unmarshal(gotBytes, &got); err != nil {
		return fmt.Sprintf("written lock is not valid JSON: %v", err)
	}
	if !reflect.DeepEqual(want, got) {
		return fmt.Sprintf("written lock does not match expected %q:\n got: %s\nwant: %s", name, gotBytes, wantBytes)
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
