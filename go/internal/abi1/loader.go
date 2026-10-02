// loader.go: the hand-written S3 loader (plan §3.2/§3.3 for Go), over the
// generated primitives abi_gen.go exposes (OpenLibrary, GlibcVersion,
// Table.Handshake, Table.ResolveAll, Table.BuildInfo, ChsAbiVersion,
// ChsAbiFingerprint). It never declares or looks up a chs_* symbol itself --
// scripts/abi-v1/check-no-hand-decls.py --scope v1 enforces that by scanning
// for `dlsym(` and `C.chs_*` outside the generated layer, and this file has
// neither.
//
// Nothing here is a cgo file: the cgo preamble is the generated one
// (abi_gen.go's own doc comment), and this is plain Go over it.
package abi1

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
)

// LoadInput is the loader's own small input, decoupled from the fetch
// module's eventual Resolved type (plan §3.1's seam) so this lane needs no
// fetch binding PR to merge first; wave C's adapter is a three-line
// Resolved -> LoadInput translation. Predicate is the verified predicate,
// passed VERBATIM: raw JSON bytes, parsed here, never re-derived or
// re-interpreted beyond the nine fields sdk.json's cross_check names.
type LoadInput struct {
	LibraryPath string
	Predicate   []byte // nil only through OpenUnverified; steps 1 and 5 then skip
	Platform    string // informational ("linux-amd64", ...); the predicate's
	// own os/arch fields are what step 5 actually compares
}

// LoadError is a step 1-6 refusal. Each one carries sdk.json's own reason
// vocabulary (loader.refusals), the path, and want/got where either is
// meaningful. Until wave C wires the public fetch-error mapping (D3/§3.4),
// this stays abi1-local: waves A and B touch no v0 file.
type LoadError struct {
	Reason string
	Path   string
	Want   string
	Got    string
}

func (e *LoadError) Error() string {
	if e.Want != "" || e.Got != "" {
		return fmt.Sprintf("abi1: %s: %s (want %q, got %q)", e.Path, e.Reason, e.Want, e.Got)
	}
	return fmt.Sprintf("abi1: %s: %s", e.Path, e.Reason)
}

// Class is the sdk.json errors.loader refusal class this reason maps to
// (loader.refusals in spec/abi-v1/sdk.json, reproduced here by hand: a step 5
// or build_info_malformed refusal is "artifact_corrupt" -- the signed
// statement and the bytes disagree -- every other one is
// "artifact_incompatible"). Wave C's public mapping reads this to pick
// CHTYPES_ARTIFACT_CORRUPT or CHTYPES_ARTIFACT_INCOMPATIBLE.
func (e *LoadError) Class() string {
	if e.Reason == "build_info_malformed" || strings.HasPrefix(e.Reason, "build_info_mismatch:") {
		return "artifact_corrupt"
	}
	return "artifact_incompatible"
}

// unverifiedEnv is sdk.json's loader.unverified_env.
const unverifiedEnv = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY"

var (
	unverifiedWarnedMu sync.Mutex
	unverifiedWarned   = map[string]bool{}
)

func warnUnverifiedOnce(path string) {
	unverifiedWarnedMu.Lock()
	defer unverifiedWarnedMu.Unlock()
	if unverifiedWarned[path] {
		return
	}
	unverifiedWarned[path] = true
	fmt.Fprintf(os.Stderr,
		"chtypes/abi1: loading %s UNVERIFIED (no predicate, no signature) -- never the default; "+
			"only for a local build or the linked smoke path\n", path)
}

// crossCheckFields is spec/abi-v1/sdk.json's cross_check table (step 5):
// nine build_info fields, each compared byte-for-byte against the SAME name
// in the predicate. Kept by hand, like the rest of this file -- a Friday
// confirmation that changes it is a one-line edit here, cited back to the
// description it mirrors.
var crossCheckFields = []string{
	"abi",
	"abi_fingerprint",
	"clickhouse_version",
	"channel",
	"build",
	"os",
	"arch",
	"core_commit",
	"inputs_sha256",
}

// Load runs steps 1-7 against a VERIFIED predicate (in.Predicate must be
// non-nil; a caller with no predicate wants OpenUnverified, explicitly).
func Load(in LoadInput) (*Table, *LoadError) {
	if in.Predicate == nil {
		return nil, &LoadError{
			Reason: "predicate_malformed",
			Path:   in.LibraryPath,
			Want:   "a verified predicate",
			Got:    "none (use OpenUnverified explicitly for an unverified load)",
		}
	}
	return load(in)
}

// OpenUnverified loads a library with NO predicate verification: steps 1 and
// 5 are skipped (plan §3.1). It is for core's own local builds and the
// linked-mode smoke path, and refuses unless BOTH explicit=true AND
// CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 are set, warning loudly -- once per
// path -- whenever it proceeds. It is never the default: nothing reaches
// this but a caller that asked for it by name.
func OpenUnverified(path string, explicit bool) (*Table, error) {
	if !explicit || os.Getenv(unverifiedEnv) != "1" {
		return nil, fmt.Errorf(
			"abi1: OpenUnverified(%s) refused: pass explicit=true AND set %s=1", path, unverifiedEnv,
		)
	}
	warnUnverifiedOnce(path)
	t, lerr := load(LoadInput{LibraryPath: path, Predicate: nil})
	if lerr != nil {
		return nil, lerr
	}
	return t, nil
}

func load(in LoadInput) (*Table, *LoadError) {
	var pred map[string]interface{}
	if in.Predicate != nil {
		m, err := decodeStrictJSONObject(in.Predicate)
		if err != nil {
			return nil, &LoadError{Reason: "predicate_malformed", Path: in.LibraryPath, Got: err.Error()}
		}
		pred = m
	}

	// Step 1: glibc, only for a predicate that declares itself linux (D3:
	// "Linux only ... darwin: skip" -- glibc has no meaning off Linux, and a
	// non-linux predicate skips this step whatever host runs the check).
	// Resolved from THIS process (GlibcVersion), never from the candidate
	// artifact, so it runs before the artifact is ever dlopen'd.
	if pred != nil {
		if predOS, _ := pred["os"].(string); predOS == "linux" {
			floor, ok := pred["glibc_floor"].(string)
			if !ok || floor == "" {
				return nil, &LoadError{
					Reason: "predicate_malformed", Path: in.LibraryPath,
					Want: "glibc_floor on a linux predicate", Got: "absent",
				}
			}
			got, present := GlibcVersion()
			if !present {
				return nil, &LoadError{Reason: "no_glibc", Path: in.LibraryPath}
			}
			if versionLess(got, floor) {
				return nil, &LoadError{Reason: "glibc_floor", Path: in.LibraryPath, Want: floor, Got: got}
			}
		}
	}

	// Step 2: dlopen (RTLD_NOW|RTLD_LOCAL, in the generated OpenLibrary).
	t, errmsg := OpenLibrary(in.LibraryPath)
	if t == nil {
		return nil, &LoadError{Reason: "dlopen", Path: in.LibraryPath, Got: errmsg}
	}

	// Step 3: chs_abi_version AND chs_build_info, resolved TOGETHER, early --
	// a judgment call (ruled on the stub's own fixtures, `inferred` not
	// `measured`): the plan's two-phase table design (§2.2) scopes step 6's
	// sweep to "api, tooling and tombstone", pointedly never "handshake", so
	// a missing handshake-class symbol is not step 6's to report. Either one
	// absent reads as "not an ABI v1+ artifact" (not_v1), the same as today's
	// chs_abi_version-only check; chs_clickhouse_version is NOT resolved here
	// (it is never called before step 6 and is left to that sweep, like
	// every non-handshake symbol -- confirmed against the stub's own
	// missing-chs_clickhouse_version case, which expects
	// "missing_symbol:chs_clickhouse_version", not "not_v1").
	if missing := t.ResolveAbiVersion(); missing != "" {
		return nil, &LoadError{Reason: "not_v1", Path: in.LibraryPath, Got: "missing " + missing}
	}
	if missing := t.ResolveBuildInfo(); missing != "" {
		return nil, &LoadError{Reason: "not_v1", Path: in.LibraryPath, Got: "missing " + missing}
	}
	if v := t.AbiVersion(); v != ChsAbiVersion {
		return nil, &LoadError{
			Reason: "abi_version", Path: in.LibraryPath,
			Want: strconv.Itoa(ChsAbiVersion), Got: strconv.Itoa(int(v)),
		}
	}

	// Step 4: chs_build_info() (already resolved above), parsed strictly and
	// fingerprint-compared.
	biRaw := t.BuildInfo()
	if biRaw == "" {
		return nil, &LoadError{Reason: "build_info_malformed", Path: in.LibraryPath, Got: "NULL"}
	}
	bi, err := decodeStrictJSONObject([]byte(biRaw))
	if err != nil {
		return nil, &LoadError{Reason: "build_info_malformed", Path: in.LibraryPath, Got: err.Error()}
	}
	if s, ok := bi["schema"].(json.Number); !ok || s.String() != "1" {
		return nil, &LoadError{
			Reason: "build_info_malformed", Path: in.LibraryPath,
			Want: "schema 1", Got: fmt.Sprintf("%v", bi["schema"]),
		}
	}
	fp, _ := bi["abi_fingerprint"].(string)
	if fp == "" {
		return nil, &LoadError{
			Reason: "build_info_malformed", Path: in.LibraryPath, Want: "abi_fingerprint", Got: "absent",
		}
	}
	if fp != ChsAbiFingerprint {
		return nil, &LoadError{Reason: "fingerprint", Path: in.LibraryPath, Want: ChsAbiFingerprint, Got: fp}
	}

	// Step 5: the nine-field cross-check against the verified predicate.
	if pred != nil {
		for _, field := range crossCheckFields {
			bv, haveB := bi[field]
			pv, haveP := pred[field]
			if !haveB || !haveP || !jsonScalarEqual(bv, pv) {
				return nil, &LoadError{
					Reason: "build_info_mismatch:" + field, Path: in.LibraryPath,
					Want: fmt.Sprintf("%v", pv), Got: fmt.Sprintf("%v", bv),
				}
			}
		}
	}

	// Step 6: resolve every described symbol (api, tooling and the
	// chs_abi_revision tombstone included, for presence only).
	if missing := t.ResolveAll(); missing != "" {
		return nil, &LoadError{Reason: "missing_symbol:" + missing, Path: in.LibraryPath}
	}

	// Step 7: on_loaded is a no-op hook until A5 settles init/context (plan
	// §3.2 row 7). Nothing to call yet.

	return t, nil
}

// decodeStrictJSONObject parses data as a single JSON object: ASCII-only
// bytes, no duplicate keys at any nesting level, numbers preserved as
// json.Number (never silently widened to float64) -- the same strictness
// scripts/abi-v1/jcs.py applies to abi.json, applied here to
// chs_build_info()'s own text and to the predicate, both of which a loader
// must refuse to guess about rather than parse leniently.
func decodeStrictJSONObject(data []byte) (map[string]interface{}, error) {
	for _, b := range data {
		if b > 0x7f {
			return nil, fmt.Errorf("not ASCII (byte 0x%02x)", b)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := decodeStrictValue(dec)
	if err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, fmt.Errorf("trailing data after the JSON value")
	}
	m, ok := v.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("the top-level JSON value is not an object")
	}
	return m, nil
}

func decodeStrictValue(dec *json.Decoder) (interface{}, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return tok, nil
	}
	switch delim {
	case '{':
		obj := map[string]interface{}{}
		for dec.More() {
			keyTok, err := dec.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyTok.(string)
			if !ok {
				return nil, fmt.Errorf("a non-string object key")
			}
			if _, dup := obj[key]; dup {
				return nil, fmt.Errorf("duplicate object key %q", key)
			}
			val, err := decodeStrictValue(dec)
			if err != nil {
				return nil, err
			}
			obj[key] = val
		}
		if _, err := dec.Token(); err != nil { // the closing '}'
			return nil, err
		}
		return obj, nil
	case '[':
		arr := []interface{}{}
		for dec.More() {
			val, err := decodeStrictValue(dec)
			if err != nil {
				return nil, err
			}
			arr = append(arr, val)
		}
		if _, err := dec.Token(); err != nil { // the closing ']'
			return nil, err
		}
		return arr, nil
	default:
		return nil, fmt.Errorf("unexpected delimiter %q", delim)
	}
}

// jsonScalarEqual compares two decodeStrictJSONObject values the way
// sdk.json's cross_check "int" and "bytes" comparisons mean: exact equality,
// never a numeric-tolerant or case-insensitive one.
func jsonScalarEqual(a, b interface{}) bool {
	switch av := a.(type) {
	case json.Number:
		bv, ok := b.(json.Number)
		return ok && av == bv
	case string:
		bv, ok := b.(string)
		return ok && av == bv
	case bool:
		bv, ok := b.(bool)
		return ok && av == bv
	case nil:
		return b == nil
	default:
		return false
	}
}

// versionLess compares two dot-separated, all-numeric version strings
// (glibc's "2.29", a predicate's glibc_floor) component by component; a
// missing trailing component reads as 0, and a non-numeric component reads
// as 0 too rather than panicking on an artifact that got this wrong.
func versionLess(a, b string) bool {
	as, bs := strings.Split(a, "."), strings.Split(b, ".")
	n := len(as)
	if len(bs) > n {
		n = len(bs)
	}
	for i := 0; i < n; i++ {
		var av, bv int
		if i < len(as) {
			av, _ = strconv.Atoi(as[i])
		}
		if i < len(bs) {
			bv, _ = strconv.Atoi(bs[i])
		}
		if av != bv {
			return av < bv
		}
	}
	return false
}
