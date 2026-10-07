package main

// model.go — the cases.json shape (spec/fetch-v1/schema/cases.schema.json),
// mirrored as Go structs. Field order here is the field order emitted by
// encoding/json for structs, which is what keeps --check's output stable
// across runs: nothing here is produced from map iteration order.

// CasesFile is the top-level tests/fixtures/fetch-v1/cases.json document.
type CasesFile struct {
	Schema int    `json:"schema"`
	Source Source `json:"source"`
	Cases  []Case `json:"cases"`
}

// Source is cases.json's own provenance block: the generator commit this
// file was produced at, and the sha256 of genfixtures' own go.sum, so a
// stale checkout (generator code changed, fixtures not regenerated) is
// detectable without re-running the generator.
type Source struct {
	GeneratorCommit string `json:"generator_commit"`
	GoSumSHA256     string `json:"go_sum_sha256"`
}

// Case is one row of cases.json: spec/fetch-v1/schema/cases.schema.json#/$defs/case.
type Case struct {
	ID         string            `json:"id"`
	Tree       string            `json:"tree"`
	Transports []string          `json:"transports"`
	HTTPScript *string           `json:"http_script"`
	Setup      Setup             `json:"setup"`
	Request    Request           `json:"request"`
	Env        map[string]string `json:"env"`
	Expect     Expect            `json:"expect"`
}

type Setup struct {
	Cache                 string   `json:"cache"`
	SystemDirs            []string `json:"system_dirs"`
	Lock                  *string  `json:"lock"`
	BeforeIndexRenameHook *string  `json:"before_index_rename_hook"`
}

type Request struct {
	Spelling      string   `json:"spelling"`
	Platform      string   `json:"platform"`
	Offline       bool     `json:"offline"`
	Frozen        bool     `json:"frozen"`
	LockWrite     bool     `json:"lock_write"`
	Update        bool     `json:"update"`
	AllowUnsigned bool     `json:"allow_unsigned"`
	Trust         string   `json:"trust"`
	Bases         []string `json:"bases"`
	// OwnFingerprint, when set, is the 64-hex fingerprint the runner hands
	// its binding's alias seam: the case runs with the dev channel's alias
	// step (docs/guides/fetch-v1.md §3) under a fixture fingerprint. nil runs
	// it with none, as the v1 contract and every production channel do.
	OwnFingerprint *string `json:"own_fingerprint"`
}

type ExpectRequests struct {
	Max                *int     `json:"max"`
	NoneMatching       []string `json:"none_matching"`
	AuthOnSecondOrigin bool     `json:"auth_on_second_origin"`
}

type Expect struct {
	OK            bool           `json:"ok"`
	Version       *string        `json:"version"`
	Build         *string        `json:"build"`
	Manifest      *string        `json:"manifest"`
	LibrarySHA256 *string        `json:"library_sha256"`
	Code          *string        `json:"code"`
	Sleeps        []float64      `json:"sleeps"`
	Warnings      []string       `json:"warnings"`
	Requests      ExpectRequests `json:"requests"`
	LockAfter     *string        `json:"lock_after"`
	// Tags is what a `list-tags-` case's listing must return, in order: the
	// tree's own tags/list less every alias (aliascases.go). nil for every
	// other case.
	Tags []string `json:"tags"`
	// RecordsIntact names installed manifests whose unpacked/sha256/<hex>/
	// directory must be byte for byte the same after the call as before it
	// (aliascases.go: another fingerprint's build in a shared cache).
	RecordsIntact []string `json:"records_intact"`
	// MessageContains is text the failed call's error message must contain
	// exactly: a retired repository's sanitized message (retiredcases.go).
	// nil for every other case.
	MessageContains *string `json:"message_contains"`
	// MessageExcludes is text the failed call's error message must not
	// contain anywhere (retiredcases.go: no `null` or `undefined` where a
	// registry sent no message). Empty for every other case.
	MessageExcludes []string `json:"message_excludes"`
}

// newCase returns a Case with every schema-required array/object field
// non-nil (encoding/json renders a nil slice as `null`, which
// additionalProperties:false-but-type:array schema fields never allow as a
// value — cases.schema.json types every one of these as a bare array, never
// nullable).
func newCase(id, tree string, transports ...string) Case {
	return Case{
		ID:         id,
		Tree:       tree,
		Transports: transports,
		HTTPScript: nil,
		Setup: Setup{
			Cache:                 "empty",
			SystemDirs:            []string{},
			Lock:                  nil,
			BeforeIndexRenameHook: nil,
		},
		Request: Request{
			Platform: "linux-arm64",
			Trust:    "test",
			Bases:    []string{"{base}"},
		},
		Env: map[string]string{},
		Expect: Expect{
			OK:              true,
			Sleeps:          []float64{},
			Warnings:        []string{},
			RecordsIntact:   []string{},
			MessageExcludes: []string{},
			Requests: ExpectRequests{
				NoneMatching: []string{},
			},
		},
	}
}

func strp(s string) *string { return &s }
func intp(i int) *int       { return &i }
