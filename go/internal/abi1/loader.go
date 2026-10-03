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
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"sync"
)

// LoadInput is the loader's own input: the three things the fetch layer's
// Resolved record gives it, plus the process setup the public layer applies at
// step 7. Predicate is the verified predicate, exactly as the fetch layer
// returned it (its parsed map, passed VERBATIM and never re-encoded); the
// loader reads only the fields sdk.json's cross_check names, plus glibc_floor
// and os on a linux predicate.
type LoadInput struct {
	LibraryPath string
	Predicate   map[string]any // nil only through OpenUnverified; steps 1 and 5 then skip
	Platform    string         // informational ("linux-amd64", ...); the predicate's
	// own os/arch fields are what step 5 actually compares

	// Timezone is the image zone chs_initialize sets at step 7: counted
	// bytes, never canonicalized; empty means UTC.
	Timezone []byte
	// Defaults is the default settings, a JSON object of strings, handed to
	// chs_set_defaults at step 7 right after chs_initialize; empty means
	// none, and chs_set_defaults is then not called at all.
	Defaults []byte
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
// non-nil; a caller with no predicate wants OpenUnverified, explicitly). A
// refusal at steps 1-6 is a *LoadError; a failure at step 7 (chs_initialize or
// chs_set_defaults) is the call's own *CallError, never a refusal reason: the
// public layer maps it by the D3 status table.
func Load(in LoadInput) (*Table, error) {
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

// UnverifiedRefusedError is OpenUnverified's refusal of a caller that did not
// opt in twice: a misuse, not an artifact problem.
type UnverifiedRefusedError struct {
	Path string
}

func (e *UnverifiedRefusedError) Error() string {
	return fmt.Sprintf("abi1: OpenUnverified(%s) refused: pass explicit=true AND set %s=1", e.Path, unverifiedEnv)
}

// OpenUnverified loads a library with NO predicate verification: steps 1 and
// 5 are skipped (plan §3.1). It is for core's own local builds and the
// linked-mode smoke path, and refuses unless BOTH explicit=true AND
// CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 are set, warning loudly -- once per
// path -- whenever it proceeds. It is never the default: nothing reaches
// this but a caller that asked for it by name. It runs step 7 like any open,
// under the timezone and defaults given.
func OpenUnverified(path string, explicit bool, timezone, defaults []byte) (*Table, error) {
	if !explicit || os.Getenv(unverifiedEnv) != "1" {
		return nil, &UnverifiedRefusedError{Path: path}
	}
	warnUnverifiedOnce(path)
	return load(LoadInput{LibraryPath: path, Predicate: nil, Timezone: timezone, Defaults: defaults})
}

// LoadLinked runs steps 3, 4, 6 and 7 over a table the linked filler
// (OpenLinked, under -tags chtypes_linked) already populated: there is no
// dlopen to run and no predicate to check (steps 1, 2 and 5), and step 6 is
// satisfied by the linker itself.
func LoadLinked(t *Table, timezone, defaults []byte) (*Table, error) {
	return loadFrom(t, LoadInput{LibraryPath: "<linked>", Timezone: timezone, Defaults: defaults}, nil, true)
}

func load(in LoadInput) (*Table, error) {
	pred := in.Predicate

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
	return loadFrom(t, in, pred, false)
}

// loadFrom runs steps 3-7 on an opened table. linked skips the resolve
// calls, which would dlsym against a table that has no dlopen handle.
func loadFrom(t *Table, in LoadInput, pred map[string]any, linked bool) (*Table, error) {
	// Step 3: chs_abi_version ALONE. PM ruling (reverses an earlier `inferred`
	// call): chs_abi_version answering 1 is what makes a library "an ABI v1+
	// artifact" at all; if it is present and correct, refusing it as not_v1
	// over a DIFFERENT missing symbol would be a false message. not_v1 stays
	// reserved for chs_abi_version alone -- every other handshake symbol,
	// chs_build_info and chs_clickhouse_version included, is reported as
	// missing_symbol:<name> at whichever step first needs it (sdk.json now
	// carries that reason at both step 4 and step 6).
	if missing := resolveIf(!linked, t.ResolveAbiVersion); missing != "" {
		return nil, &LoadError{Reason: "not_v1", Path: in.LibraryPath, Got: "missing " + missing}
	}
	if v := t.AbiVersion(); v != ChsAbiVersion {
		return nil, &LoadError{
			Reason: "abi_version", Path: in.LibraryPath,
			Want: strconv.Itoa(ChsAbiVersion), Got: strconv.Itoa(int(v)),
		}
	}

	// Step 4: chs_build_info ALONE, then parsed strictly and
	// fingerprint-compared. A missing symbol here is step 4's own
	// missing_symbol row (sdk.json), not build_info_malformed: the artifact
	// is not malformed, a described export is simply absent, the same
	// vocabulary step 6's generic sweep would use if nothing resolved it
	// sooner.
	if missing := resolveIf(!linked, t.ResolveBuildInfo); missing != "" {
		return nil, &LoadError{Reason: "missing_symbol:" + missing, Path: in.LibraryPath}
	}
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
	if missing := resolveIf(!linked, t.ResolveAll); missing != "" {
		return nil, &LoadError{Reason: "missing_symbol:" + missing, Path: in.LibraryPath}
	}

	// Step 7: chs_initialize, once, with the image zone (length 0 = UTC),
	// then chs_set_defaults once when there are defaults. A repeat of
	// chs_initialize with the same spelling answers CHS_OK, a different
	// spelling is CHS_INVALID_ARGUMENT (process_once). A failure here is the
	// call's own error, mapped by the D3 status table -- never a refusal.
	if cerr := t.Initialize(in.Timezone); cerr != nil {
		return nil, cerr
	}
	if len(in.Defaults) > 0 {
		if cerr := t.SetDefaults(in.Defaults); cerr != nil {
			return nil, cerr
		}
	}

	return t, nil
}

// resolveIf runs a resolve step, or reports it resolved when skipped.
func resolveIf(run bool, resolve func() string) string {
	if !run {
		return ""
	}
	return resolve()
}

// DecodeStrictJSON parses data as ONE JSON value with the loader's own
// strictness for its duplicate-key rule: no repeated object key at any depth
// and numbers kept as json.Number. Unlike build_info it does not require
// ASCII, because a document may carry any UTF-8 text. It is the one strict
// reader every document decoder reuses (bindings-v1.md section 5, rule 3).
func DecodeStrictJSON(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := decodeStrictValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("trailing data after the JSON value")
	}
	return v, nil
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

// jsonScalarEqual compares a build_info value (decoded with json.Number)
// against the verified predicate's value, which the fetch layer hands over as
// its own parsed map (a float64 for a number), the way sdk.json's cross_check
// "int" and "bytes" comparisons mean: exact equality, never a case-insensitive
// one. A predicate integer is compared as an integer.
func jsonScalarEqual(a, b interface{}) bool {
	switch av := a.(type) {
	case json.Number:
		switch bv := b.(type) {
		case json.Number:
			return av == bv
		case float64:
			i, err := av.Int64()
			return err == nil && float64(i) == bv && int64(bv) == i
		case int:
			i, err := av.Int64()
			return err == nil && i == int64(bv)
		case int64:
			i, err := av.Int64()
			return err == nil && i == bv
		}
		return false
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
