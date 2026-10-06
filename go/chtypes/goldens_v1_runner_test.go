package chtypes

// goldens_v1_runner_test.go — the Go goldens runner (docs/guides/goldens-v1.md
// section 2). It EXECUTES the cases of a release's goldens document and
// RECORDS what the library returned, byte for byte. It never compares:
// scripts/goldens-v1/compare.py does that, once, for all four bindings.
//
// It lives in this package, not in an internal one, for one reason: the
// "public decoder" half of the contract (decoded_ok) is the decoder a user's
// call runs on the library's document, and that is the unexported decode
// functions of this package, which Schema.Row, Rows, Describe and Filter.Rows
// call after the buffer is copied. Going through the exported calls instead
// would re-encode the settings and column list the document names as bytes.
//
// How it runs, in one paragraph. TestGoldensV1Runner is both the parent and the
// child. The parent fetches the release and its goldens document in-process
// (ocifetch.Ensure, then ocifetch.FetchSigned with the platform manifest's
// digest: the referrer selection is the fetch layer's, never this file's),
// writes the document's exact bytes, and re-executes this test binary once per
// setup with CHTYPES_GOLDENS_SETUP=<id>. Each child is a fresh process, so
// Setup (chs_initialize, chs_set_defaults) runs exactly once in it, before its
// first case, and never twice in one process. The parent merges the children's
// answers into the report.
//
// Inputs, from the environment:
//
//	CHTYPES_GOLDENS_REGISTRY_BASE  the registry base, repository path included
//	CHTYPES_GOLDENS_VERSION        the exact four-part version
//	CHTYPES_GOLDENS_PLATFORM       linux-amd64 | linux-arm64 | darwin-arm64
//	CHTYPES_GOLDENS_REPORT         where the report is written
//	CHTYPES_GOLDENS_DOCUMENT       where the document's exact bytes are written
//
// Without them this test SKIPS LOUDLY by name and a plain `go test ./...`
// stays green; that is the only skip.
//
// The stub path, which bypasses the fetch (for the SDK's own checks, before a
// release's goldens exist): set CHTYPES_GOLDENS_LIBRARY to a library file and
// CHTYPES_GOLDENS_DOCUMENT_IN to a goldens document, plus the REPORT and
// DOCUMENT paths, and CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 (the library opens
// through OpenUnverified, with no signature). CHTYPES_GOLDENS_PLATFORM then
// defaults to this host's.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// ---- the goldens document: only the members a runner needs

type goldensDoc struct {
	ClickHouseVersion string `json:"clickhouse_version"`
	Build             string `json:"build"`
	Revision          int64  `json:"revision"`
	Setups            []struct {
		ID        string        `json:"id"`
		ImageZone []byte        `json:"image_zone_b64"`
		Defaults  []byte        `json:"defaults_b64"`
		Cases     []goldensCase `json:"cases"`
	} `json:"setups"`
}

type goldensCase struct {
	ID        string   `json:"id"`
	Call      string   `json:"call"`
	Platforms []string `json:"platforms"`
	Schema    struct {
		CreateTable []byte `json:"create_table_b64"`
		Settings    []byte `json:"settings_b64"`
	} `json:"schema"`
	Body *struct {
		Format       int32  `json:"format"`
		Body         []byte `json:"body_b64"`
		Settings     []byte `json:"settings_b64"`
		Columns      []byte `json:"columns_b64"`
		ExportFormat *int32 `json:"export_format"`
		DocFlags     *int32 `json:"doc_flags"`
	} `json:"body"`
	Filter *struct {
		Expr        []byte `json:"expr_b64"`
		QueryParams []byte `json:"query_params_b64"`
		Settings    []byte `json:"settings_b64"`
	} `json:"filter"`
}

// ---- what a child writes for the parent

type childOut struct {
	Artifact map[string]string `json:"artifact"`
	Cases    []map[string]any  `json:"cases"`
}

const (
	envBase      = "CHTYPES_GOLDENS_REGISTRY_BASE"
	envVersion   = "CHTYPES_GOLDENS_VERSION"
	envPlatform  = "CHTYPES_GOLDENS_PLATFORM"
	envReport    = "CHTYPES_GOLDENS_REPORT"
	envDocument  = "CHTYPES_GOLDENS_DOCUMENT"
	envLibrary   = "CHTYPES_GOLDENS_LIBRARY"
	envDocIn     = "CHTYPES_GOLDENS_DOCUMENT_IN"
	envChildID   = "CHTYPES_GOLDENS_SETUP"
	envChildOut  = "CHTYPES_GOLDENS_CHILD_OUT"
	exportNone   = -1
	docFlagsAll  = 7
	statusOKName = "CHS_OK"
)

func b64(b []byte) string { return base64.StdEncoding.EncodeToString(b) }

// goldensPlatform is the report's platform, "os/arch", from the key the
// workflow passes ("linux-amd64"), or this host's.
func goldensPlatform() (key, osArch string) {
	key = os.Getenv(envPlatform)
	if key == "" {
		key = runtime.GOOS + "-" + runtime.GOARCH
	}
	return key, strings.Replace(key, "-", "/", 1)
}

func TestGoldensV1Runner(t *testing.T) {
	if os.Getenv(envLibrary) == "" {
		// Against a registry, the runner fetches exactly as a non-test
		// binary of this 2.0.0-dev SDK does: the ABI v2 dev channel, its
		// staging base and its staging key only, whatever base the caller
		// names (spec/abi-v2/docs.md, rule r6). The goldens are an OCI
		// referrer of the platform manifest signed with that key, and the
		// highest verified revision wins (docs/guides/goldens-v1.md).
		t.Cleanup(ocifetch.UseDevChannelForTests())
	}
	if id := os.Getenv(envChildID); id != "" {
		runGoldensChild(t, id)
		return
	}
	stub := os.Getenv(envLibrary) != ""
	if stub {
		for _, k := range []string{envDocIn, envReport, envDocument} {
			if os.Getenv(k) == "" {
				t.Fatalf("%s is set, so %s must be too", envLibrary, k)
			}
		}
	} else {
		missing := []string{}
		for _, k := range []string{envBase, envVersion, envPlatform} {
			if os.Getenv(k) == "" {
				missing = append(missing, k)
			}
		}
		if len(missing) > 0 {
			t.Skipf("SKIPPED: the v1 goldens runner needs %s (and %s, %s); the goldens did not run", strings.Join(missing, ", "), envReport, envDocument)
		}
		for _, k := range []string{envReport, envDocument} {
			if os.Getenv(k) == "" {
				t.Fatalf("%s must be set (where the runner writes)", k)
			}
		}
	}

	// 1. The document's exact bytes.
	var docBytes []byte
	if stub {
		b, err := os.ReadFile(os.Getenv(envDocIn))
		if err != nil {
			t.Fatal(err)
		}
		docBytes = b
	} else {
		docBytes = fetchGoldens(t)
	}
	if err := os.WriteFile(os.Getenv(envDocument), docBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(docBytes)
	var doc goldensDoc
	if err := json.Unmarshal(docBytes, &doc); err != nil {
		t.Fatalf("the goldens document does not parse: %v", err)
	}

	// 2. One OS process per setup.
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	var artifact map[string]string
	var cases []map[string]any
	for _, s := range doc.Setups {
		out := filepath.Join(t.TempDir(), "setup-"+s.ID+".json")
		cmd := exec.CommandContext(context.Background(), self, "-test.run", "^TestGoldensV1Runner$", "-test.count=1")
		cmd.Env = append(os.Environ(), envChildID+"="+s.ID, envChildOut+"="+out)
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		cmd.Stdout = &stderr
		if err := cmd.Run(); err != nil {
			t.Fatalf("the process for setup %q failed: %v\n%s", s.ID, err, stderr.String())
		}
		raw, err := os.ReadFile(out)
		if err != nil {
			t.Fatalf("setup %q wrote no answers: %v\n%s", s.ID, err, stderr.String())
		}
		var co childOut
		if err := json.Unmarshal(raw, &co); err != nil {
			t.Fatalf("setup %q: %v", s.ID, err)
		}
		if artifact != nil && !equalStrings(artifact, co.Artifact) {
			t.Fatalf("setup %q loaded a different library than an earlier setup", s.ID)
		}
		artifact = co.Artifact
		cases = append(cases, co.Cases...)
	}
	if len(cases) == 0 {
		t.Fatal("the goldens document ran zero cases")
	}

	// 3. The report.
	_, osArch := goldensPlatform()
	report := map[string]any{
		"report_version":  1,
		"binding":         "go",
		"binding_version": bindingVersion(),
		"platform":        osArch,
		"goldens": map[string]any{
			"clickhouse_version": doc.ClickHouseVersion,
			"build":              doc.Build,
			"revision":           doc.Revision,
			"sha256":             hex.EncodeToString(sum[:]),
		},
		"artifact": map[string]any{
			"build_info_b64":  artifact["build_info_b64"],
			"abi_fingerprint": artifact["abi_fingerprint"],
		},
		"cases": cases,
	}
	out, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv(envReport), append(out, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Logf("recorded %d cases across %d setups in %s", len(cases), len(doc.Setups), os.Getenv(envReport))
}

func equalStrings(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// bindingVersion is the binding's own version as it reports it: a Go module
// carries none in its source, so the build's own VCS revision, or "devel".
func bindingVersion() string {
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, s := range bi.Settings {
			if s.Key == "vcs.revision" && len(s.Value) >= 12 {
				return "devel+" + s.Value[:12]
			}
		}
	}
	return "devel"
}

// fetchGoldens ensures the release for the platform, then asks the fetch layer
// for its goldens: with the goldens predicate type, fetch_signed takes the
// PLATFORM manifest's digest and does the referrer selection itself (the
// highest verified revision), so this file looks up nothing.
func fetchGoldens(t *testing.T) []byte {
	t.Helper()
	key, _ := goldensPlatform()
	opts := &ocifetch.Options{Bases: []string{os.Getenv(envBase)}}
	res, err := ocifetch.Ensure(context.Background(), ocifetch.Request{Spelling: os.Getenv(envVersion), Platform: key}, opts)
	if err != nil {
		t.Fatalf("ensure %s for %s: %v", os.Getenv(envVersion), key, err)
	}
	sa, err := ocifetch.FetchSigned(context.Background(), "", string(res.Digests.Manifest), ocifetch.PredicateTypeGoldens, opts)
	var fe *ocifetch.FetchError
	if errors.As(err, &fe) && fe.Code == ocifetch.CodeArtifactUnpublished && ocifetch.ChannelName() == "v2-dev" {
		// The build is published (ensure above succeeded) and carries no
		// goldens referrer: the first v2-dev builds ship none. A skip BY
		// NAME, never a pass: the workflows read this line.
		t.Skipf("SKIPPED: no v2-dev goldens published yet: %s %s (%s) has no goldens referrer on %s; the goldens did not run (%v)",
			os.Getenv(envVersion), key, res.Digests.Manifest, ocifetch.DevChannelBase, err)
	}
	if err != nil {
		t.Fatalf("fetch_signed (goldens) for %s: %v", res.Digests.Manifest, err)
	}
	return sa.Bytes
}

// ---- the child: one setup, one process

func runGoldensChild(t *testing.T, setupID string) {
	t.Helper()
	raw, err := os.ReadFile(os.Getenv(envDocument))
	if err != nil {
		t.Fatal(err)
	}
	var doc goldensDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	si := -1
	for i, s := range doc.Setups {
		if s.ID == setupID {
			si = i
		}
	}
	if si < 0 {
		t.Fatalf("no setup %q in the document", setupID)
	}
	setupDef := doc.Setups[si]

	// chs_initialize and chs_set_defaults, once, through the process setup the
	// loader applies at step 7: the zone bytes as given, and the defaults as
	// the strings the document holds (none: chs_set_defaults is not called).
	var defaults map[string]string
	if len(bytes.TrimSpace(setupDef.Defaults)) > 0 {
		if err := json.Unmarshal(setupDef.Defaults, &defaults); err != nil {
			t.Fatalf("setup %q: defaults are not a JSON object of strings: %v", setupID, err)
		}
	}
	if err := Setup(SetupOptions{Timezone: string(setupDef.ImageZone), Defaults: defaults}); err != nil {
		t.Fatal(err)
	}

	var lib *Library
	if path := os.Getenv(envLibrary); path != "" {
		lib, err = OpenUnverified(path, true)
	} else {
		reg, rerr := NewRegistry(WithFetchOptions(FetchOptions{Bases: []string{os.Getenv(envBase)}}))
		if rerr != nil {
			t.Fatal(rerr)
		}
		lib, err = reg.For(os.Getenv(envVersion))
	}
	if err != nil {
		t.Fatalf("opening the library: %v", err)
	}

	_, osArch := goldensPlatform()
	var out childOut
	out.Artifact = map[string]string{
		"build_info_b64":  b64(lib.BuildInfo().Raw),
		"abi_fingerprint": lib.BuildInfo().ABIFingerprint,
	}
	for _, c := range setupDef.Cases {
		rec := map[string]any{"id": c.ID, "setup": setupID}
		if !containsString(c.Platforms, osArch) {
			rec["result"] = "skipped"
			rec["skip_reason"] = fmt.Sprintf("the case's platforms (%s) exclude %s", strings.Join(c.Platforms, ", "), osArch)
		} else {
			rec["result"] = "ran"
			runGoldensCase(t, lib, c, rec)
		}
		out.Cases = append(out.Cases, rec)
	}
	b, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv(envChildOut), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// stopAt records the step a call sequence stopped at and why.
func stopAt(rec map[string]any, step string, ce *abi2.CallError) {
	rec["at"] = step
	rec["status"] = ce.Status
	rec["error"] = map[string]any{
		"ch_code":     ce.ChCode,
		"ch_name_b64": b64([]byte(ce.ChName)),
		"message_b64": b64([]byte(ce.Message)),
		"column_b64":  b64([]byte(ce.Column)),
	}
	delete(rec, "document_b64")
	rec["decoded_ok"] = nil
}

// runGoldensCase makes exactly the chs_* sequence the case's call names,
// through the generated call layer, and records the raw bytes it got.
func runGoldensCase(t *testing.T, lib *Library, c goldensCase, rec map[string]any) {
	t.Helper()
	tbl := lib.tbl
	sch, cerr := tbl.SchemaCreate(c.Schema.CreateTable, c.Schema.Settings)
	if cerr != nil {
		stopAt(rec, "schema_create", cerr)
		return
	}
	defer sch.Close()

	var doc, export []byte
	var haveExport bool
	switch c.Call {
	case "schema_create":
		buf, ce := tbl.SchemaDescribe(sch)
		if ce != nil {
			stopAt(rec, "call", ce)
			return
		}
		doc = tbl.Take(buf)
	case "preview_row":
		buf, ce := tbl.PreviewRow(sch, c.Body.Format, c.Body.Body, c.Body.Settings, c.Body.Columns)
		if ce != nil {
			stopAt(rec, "call", ce)
			return
		}
		doc = tbl.Take(buf)
	case "preview_batch":
		ef, df := int32(exportNone), int32(docFlagsAll)
		if c.Body.ExportFormat != nil {
			ef = *c.Body.ExportFormat
		}
		if c.Body.DocFlags != nil {
			df = *c.Body.DocFlags
		}
		dbuf, ebuf, ce := tbl.PreviewBatch(sch, c.Body.Format, c.Body.Body, c.Body.Settings, c.Body.Columns, nil, ef, uint32(df))
		if ce != nil {
			stopAt(rec, "call", ce)
			return
		}
		doc = tbl.Take(dbuf)
		if ef != exportNone {
			// An absent buffer is nil; a present, empty one is not.
			if export = tbl.Take(ebuf); export != nil {
				haveExport = true
			}
		}
	case "filter_eval_body":
		f, ce := tbl.FilterCreate(sch, c.Filter.Expr, c.Filter.QueryParams, c.Filter.Settings)
		if ce != nil {
			stopAt(rec, "filter_create", ce)
			return
		}
		defer f.Close()
		buf, ce := tbl.FilterEvalBody(f, c.Body.Format, c.Body.Body, c.Body.Settings)
		if ce != nil {
			stopAt(rec, "call", ce)
			return
		}
		doc = tbl.Take(buf)
	default:
		t.Fatalf("case %s names the call %q, which this runner does not know", c.ID, c.Call)
	}

	rec["at"] = "call"
	rec["status"] = statusOKName
	rec["document_b64"] = b64(doc)
	if haveExport {
		rec["export_b64"] = b64(export)
	}
	// The public decoder: the call a user makes decodes these same bytes.
	var derr error
	switch c.Call {
	case "schema_create":
		_, derr = decodeSchemaDescription(doc)
	case "preview_row":
		_, derr = decodeRow(doc)
	case "preview_batch":
		_, derr = decodeBatch(doc, export)
	case "filter_eval_body":
		_, derr = decodeFilterResult(doc)
	}
	rec["decoded_ok"] = derr == nil
	if derr != nil {
		t.Logf("case %s: the public decoder refused the library's document: %v", c.ID, derr)
	}
}

// ---- the stub self-check: the runner against the ABI test stub, judged by the
// real comparator, and a planted wrong byte refused by it.

func TestGoldensV1StubSelfCheck(t *testing.T) {
	if os.Getenv(envChildID) != "" {
		t.Skip("child process")
	}
	dir := os.Getenv("CHTYPES_ABI2_STUBS")
	if dir == "" {
		t.Skip("SKIPPED: CHTYPES_ABI2_STUBS is not set (scripts/abi-v1/build-stubs.sh --out DIR); the goldens runner's stub self-check did not run")
	}
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("SKIPPED: python3 is not on PATH, so the comparator did not run; the goldens runner's stub self-check did not run")
	}
	compare, err := filepath.Abs("../../scripts/goldens-v1/compare.py")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(compare); err != nil {
		t.Skipf("SKIPPED: %v: the comparator is not beside this checkout; the self-check did not run", err)
	}
	tmp := t.TempDir()
	// The hand-written document names the stub's identity as the stubs'
	// own manifest gives it (stubs.json, rendered from the description the
	// stub is built from), so an UNSTABLE description's moving fingerprint
	// never needs a hand edit here; the comparator then checks it against
	// the identity the loaded stub reports.
	doc := stubGoldensDocument(t, dir, tmp)
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	report := filepath.Join(tmp, "report.json")
	cmd := exec.CommandContext(context.Background(), self, "-test.run", "^TestGoldensV1Runner$", "-test.count=1")
	cmd.Env = append(os.Environ(),
		envLibrary+"="+filepath.Join(dir, "ok.so"), envDocIn+"="+doc, envReport+"="+report,
		envDocument+"="+filepath.Join(tmp, "document.json"), "CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("the runner: %v\n%s", err, out)
	}

	judge := func(report string) ([]byte, error) {
		return exec.CommandContext(context.Background(), py, compare, "--goldens", doc, "--report", report).CombinedOutput()
	}
	if out, err := judge(report); err != nil {
		t.Fatalf("the comparator refused the runner's report: %v\n%s", err, out)
	}

	// A planted wrong byte in the recorded document must be refused.
	raw, err := os.ReadFile(report)
	if err != nil {
		t.Fatal(err)
	}
	var whole map[string]any
	if err := json.Unmarshal(raw, &whole); err != nil {
		t.Fatal(err)
	}
	planted := false
	for _, c := range whole["cases"].([]any) {
		m := c.(map[string]any)
		if m["id"] != "stub-wire-csv-binary-string" {
			continue
		}
		b, err := base64.StdEncoding.DecodeString(m["document_b64"].(string))
		if err != nil {
			t.Fatal(err)
		}
		i := bytes.Index(b, []byte("accepted"))
		if i < 0 {
			t.Fatal("the stub's document has no outcome to plant a byte in")
		}
		b[i] ^= 1
		m["document_b64"] = b64(b)
		planted = true
	}
	if !planted {
		t.Fatal("the planted case is missing from the report")
	}
	out, err := json.Marshal(whole)
	if err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(tmp, "planted.json")
	if err := os.WriteFile(bad, out, 0o644); err != nil {
		t.Fatal(err)
	}
	if o, err := judge(bad); err == nil {
		t.Fatalf("the comparator accepted a report with a wrong byte in a document:\n%s", o)
	}
}

// stubGoldensDocument writes testdata/goldens-v1-stub.json to tmp with its
// abi and abi_fingerprint taken from the "ok" stub's predicate in stubs.json.
func stubGoldensDocument(t *testing.T, stubs, tmp string) string {
	t.Helper()
	var manifest struct {
		Variants map[string]struct {
			Predicate map[string]any `json:"predicate"`
		} `json:"variants"`
	}
	raw, err := os.ReadFile(filepath.Join(stubs, "stubs.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	pred := manifest.Variants["ok"].Predicate
	if pred["abi"] == nil || pred["abi_fingerprint"] == nil {
		t.Fatalf("stubs.json's ok predicate names no abi or abi_fingerprint: %v", pred)
	}
	src, err := os.ReadFile("testdata/goldens-v1-stub.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(src, &doc); err != nil {
		t.Fatal(err)
	}
	doc["abi"], doc["abi_fingerprint"] = pred["abi"], pred["abi_fingerprint"]
	out, err := json.MarshalIndent(doc, "", " ")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(tmp, "goldens-stub.json")
	if err := os.WriteFile(path, out, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}
