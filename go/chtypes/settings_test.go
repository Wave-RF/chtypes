package chtypes

import (
	"errors"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestCallSettings(t *testing.T) {
	// Nothing to send is nil: the ABI reads length 0 as "none".
	if b, err := callSettings(nil, nil); err != nil || b != nil {
		t.Errorf("empty = %q, %v", b, err)
	}
	b, err := callSettings(map[string]string{"b": "2", "a": "x<y"}, nil)
	if err != nil || string(b) != `{"a":"x<y","b":"2"}` {
		t.Errorf("settings = %q, %v: a JSON object of strings, no HTML escaping, values untouched", b, err)
	}
	// The per-call zone is one settings key, verbatim, never validated.
	z := "Not/AZone"
	b, err = callSettings(map[string]string{"a": "1"}, &z)
	if err != nil || string(b) != `{"a":"1","session_timezone":"Not/AZone"}` {
		t.Errorf("settings with a zone = %q, %v", b, err)
	}
	b, err = callSettings(nil, &z)
	if err != nil || string(b) != `{"session_timezone":"Not/AZone"}` {
		t.Errorf("a zone alone = %q, %v", b, err)
	}
	// Given twice is misuse whether or not the two agree.
	for _, v := range []string{"Not/AZone", "UTC"} {
		_, err = callSettings(map[string]string{"session_timezone": v}, &z)
		var ue *UsageError
		if !errors.As(err, &ue) {
			t.Errorf("zone twice (%s) = %v, want a *UsageError", v, err)
		}
	}
	// The caller's map is never modified.
	in := map[string]string{"a": "1"}
	if _, err := callSettings(in, &z); err != nil || len(in) != 1 {
		t.Errorf("the caller's map was changed: %v", in)
	}
}

// A non-UTF-8 key, and a non-UTF-8 value where the input has no object form for
// one, is refused before any call, naming the input and the key in escaped form;
// stock encoding/json would have replaced it with U+FFFD.
func TestNonUTF8InputIsRefused(t *testing.T) {
	z := "UTC"
	bad := "\xff"
	cases := []struct {
		name string
		call func() ([]byte, error)
		want []string
	}{
		{"settings key", func() ([]byte, error) { return callSettings(map[string]string{"k" + bad: "v"}, nil) }, []string{"settings", `the key "k\xff"`}},
		{"settings key beside a zone", func() ([]byte, error) { return callSettings(map[string]string{"k" + bad: "v"}, &z) }, []string{"settings", `the key "k\xff"`}},
		{"query parameters key", func() ([]byte, error) { return stringMapJSON("query parameters", map[string]string{bad: "v"}) }, []string{"query parameters", `the key "\xff"`}},
		{"default settings key", func() ([]byte, error) { return stringMapJSON("default settings", map[string]string{"d" + bad: "1"}) }, []string{"default settings", `"d\xff"`}},
		{"server profile macro", func() ([]byte, error) { return serverProfileJSON(ServerProfile{Macros: map[string]string{"m": bad}}) }, []string{"server profile macros", `"m"`}},
		{"server profile settings key", func() ([]byte, error) { return serverProfileJSON(ServerProfile{Settings: map[string]string{bad: "1"}}) }, []string{"server profile settings", "key"}},
		{"server profile zone", func() ([]byte, error) { return serverProfileJSON(ServerProfile{Timezone: bad}) }, []string{"timezone"}},
	}
	for _, c := range cases {
		b, err := c.call()
		var ue *UsageError
		if !errors.As(err, &ue) || b != nil {
			t.Errorf("%s: = %q, %v, want a *UsageError and no bytes", c.name, b, err)
			continue
		}
		msg := err.Error()
		for _, w := range append(c.want, "not valid UTF-8", "cannot carry") {
			if !strings.Contains(msg, w) {
				t.Errorf("%s: message %q lacks %q", c.name, msg, w)
			}
		}
		if !utf8.ValidString(msg) {
			t.Errorf("%s: message is not valid UTF-8: %q", c.name, msg)
		}
	}
}

// A settings or query-parameter value that is not valid UTF-8 is the object form
// {"b64": ...} (content json_object_byte_values), standard base64 with padding;
// a valid one stays a JSON string. The per-call zone is a value too.
func TestNonUTF8ValueIsTheB64Form(t *testing.T) {
	z := "\xffZ"
	for _, c := range []struct {
		name string
		got  func() ([]byte, error)
		want string
	}{
		{"settings", func() ([]byte, error) { return callSettings(map[string]string{"k": "\xff\xfe", "a": "ok"}, nil) }, `{"a":"ok","k":{"b64":"//4="}}`},
		{"the zone", func() ([]byte, error) { return callSettings(nil, &z) }, `{"session_timezone":{"b64":"/1o="}}`},
		{"query parameters", func() ([]byte, error) { return stringMapJSON("query parameters", map[string]string{"p": "\xff"}) }, `{"p":{"b64":"/w=="}}`},
		{"defaults", func() ([]byte, error) { return stringMapJSON("default settings", map[string]string{"d": "\x00\xff"}) }, `{"d":{"b64":"AP8="}}`},
		{"server profile settings", func() ([]byte, error) {
			return serverProfileJSON(ServerProfile{Settings: map[string]string{"s": "\xff"}})
		}, `{"settings":{"s":{"b64":"/w=="}}}`},
	} {
		b, err := c.got()
		if err != nil || string(b) != c.want {
			t.Errorf("%s = %s, %v, want %s", c.name, b, err, c.want)
		}
	}
}

// Valid UTF-8 serializes byte for byte as before: non-ASCII is written as
// itself, in keys and values, and U+FFFD that the caller really wrote stays.
func TestValidNonASCIIInputIsUnchanged(t *testing.T) {
	z := "Asia/Tokyo"
	b, err := callSettings(map[string]string{"é": "日本", "r": "\ufffd"}, &z)
	want := "{\"r\":\"\ufffd\",\"session_timezone\":\"Asia/Tokyo\",\"é\":\"日本\"}"
	if err != nil || string(b) != want {
		t.Errorf("settings = %q, %v, want %q", b, err, want)
	}
	b, err = stringMapJSON("query parameters", map[string]string{"p": "é日本"})
	if err != nil || string(b) != "{\"p\":\"é日本\"}" {
		t.Errorf("params = %q, %v", b, err)
	}
	b, err = serverProfileJSON(ServerProfile{Timezone: "日本", Settings: map[string]string{"é": "日本"}})
	if err != nil || !strings.Contains(string(b), "日本") {
		t.Errorf("profile = %q, %v", b, err)
	}
}

func TestColumnsJSON(t *testing.T) {
	if b, err := columnsJSON(nil, false); err != nil || b != nil {
		t.Errorf("absent columns = %q, %v", b, err)
	}
	b, err := columnsJSON([]string{"a", "b\x00c", "\xff\xfe"}, true)
	want := `[{"name":"a"},{"name":"b\u0000c"},{"name_b64":"//4="}]`
	if err != nil || string(b) != want {
		t.Errorf("columns = %s, %v, want %s", b, err, want)
	}
	b, err = columnsJSON([]string{}, true)
	if err != nil || string(b) != `[]` {
		t.Errorf("an explicit empty list = %s, %v", b, err)
	}
}

func TestOptionsApply(t *testing.T) {
	f := &Filter{}
	c := rowsConfig([]RowsOption{
		WithSettings(map[string]string{"a": "b"}), WithSessionTimezone("UTC"),
		WithColumns([]string{"x"}), WithRowFilter(f), WithExport(CSV), WithDocFlags(DocValues | DocTransforms),
	})
	if c.settings["a"] != "b" || c.timezone == nil || *c.timezone != "UTC" || !c.haveColumns || c.filter != f ||
		c.export != CSV || c.docFlags != DocValues|DocTransforms {
		t.Errorf("config = %+v", c)
	}
	d := rowsConfig(nil)
	if d.export != ExportNone || d.docFlags != DocAll {
		t.Errorf("defaults = export %d, doc flags %d: want ExportNone and DocAll", d.export, d.docFlags)
	}
	// One value implements all six interfaces; each of the six applies it.
	o := WithSettings(map[string]string{"k": "v"})
	var _ CompileOption = o
	var _ RowOption = o
	var _ RowsOption = o
	var _ BlockOption = o
	var _ FilterOption = o
	var _ EvalOption = o
	for _, c := range []*callConfig{
		compileConfig([]CompileOption{o}), rowConfig([]RowOption{o}), rowsConfig([]RowsOption{o}),
		blockConfig([]BlockOption{o}), filterConfig([]FilterOption{o}), evalConfig([]EvalOption{o}),
	} {
		if c.settings["k"] != "v" {
			t.Errorf("an option did not apply: %+v", c)
		}
	}
	if p := filterConfig([]FilterOption{WithFilterParams(map[string]string{"p": "1"})}); p.params["p"] != "1" {
		t.Errorf("params = %v", p.params)
	}
}

func resetSetup(t *testing.T) {
	t.Helper()
	setup.mu.Lock()
	setup.recorded, setup.latched, setup.opts = false, false, SetupOptions{}
	setup.mu.Unlock()
	t.Cleanup(func() {
		setup.mu.Lock()
		setup.recorded, setup.latched, setup.opts = false, false, SetupOptions{}
		setup.mu.Unlock()
	})
}

func TestSetupProcessOnce(t *testing.T) {
	resetSetup(t)
	if err := Setup(SetupOptions{Timezone: "Europe/Paris", Defaults: map[string]string{"a": "1"}}); err != nil {
		t.Fatal(err)
	}
	// The same setup again, byte for byte, is a no-op.
	if err := Setup(SetupOptions{Timezone: "Europe/Paris", Defaults: map[string]string{"a": "1"}}); err != nil {
		t.Errorf("the same setup twice: %v", err)
	}
	// A different zone, or different defaults, is a UsageError naming both.
	for _, o := range []SetupOptions{
		{Timezone: "UTC", Defaults: map[string]string{"a": "1"}},
		{Timezone: "Europe/Paris", Defaults: map[string]string{"a": "2"}},
		{Timezone: "Europe/Paris"},
	} {
		err := Setup(o)
		var ue *UsageError
		if !errors.As(err, &ue) {
			t.Errorf("Setup(%+v) = %v, want a *UsageError", o, err)
			continue
		}
		if !strings.Contains(err.Error(), "Europe/Paris") || !strings.Contains(err.Error(), o.Timezone) {
			t.Errorf("the error does not name both setups: %v", err)
		}
	}
	// The first setup stands.
	zone, defaults, err := commitSetup()
	if err != nil || string(zone) != "Europe/Paris" || string(defaults) != `{"a":"1"}` {
		t.Errorf("the setup in effect = %q %q, %v", zone, defaults, err)
	}
}

func TestFirstOpenCommitsTheEmptySetup(t *testing.T) {
	resetSetup(t)
	zone, defaults, err := commitSetup()
	if err != nil || len(zone) != 0 || defaults != nil {
		t.Fatalf("the committed empty setup = %q %q, %v", zone, defaults, err)
	}
	if err := Setup(SetupOptions{}); err != nil {
		t.Errorf("the empty setup after the commit: %v", err)
	}
	if err := Setup(SetupOptions{Timezone: "UTC"}); err == nil {
		t.Error("a setup after the first open commit must be refused: it is not the one in effect")
	}
}

// TestAFailedFetchBeforeStep7UnlocksTheSetup: a registry open that attempted
// to resolve and failed (nothing installed, autofetch off) unlocks the setup
// record while no image has completed step 7, so a different setup replaces it.
// A refused version spelling is misuse, refused before anything is attempted,
// and unlocks nothing.
func TestAFailedFetchBeforeStep7UnlocksTheSetup(t *testing.T) {
	resetSetup(t)
	if err := Setup(SetupOptions{Timezone: "Asia/Tokyo"}); err != nil {
		t.Fatal(err)
	}
	reg, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: t.TempDir(), SystemDirs: []string{t.TempDir()}, Offline: true}), WithAutoFetch(false))
	if err != nil {
		t.Fatal(err)
	}
	var ue *UsageError
	// A refused spelling first: it unlocks nothing.
	if _, err := reg.For("v26.8"); !errors.As(err, &ue) {
		t.Fatalf("For(v26.8) = %v, want a *UsageError", err)
	}
	if err := Setup(SetupOptions{Timezone: "UTC"}); !errors.As(err, &ue) {
		t.Errorf("a different setup after a refused spelling = %v, want a *UsageError", err)
	}
	if _, err := reg.For("26.8"); !errors.Is(err, ErrArtifactMissing) {
		t.Fatalf("For on an empty cache = %v, want ErrArtifactMissing", err)
	}
	// The failed open kept the record (a retry runs under it, never the empty
	// setup) and made it replaceable.
	setup.mu.Lock()
	kept, replaceable := setup.recorded && setup.opts.Timezone == "Asia/Tokyo", setup.replaceable
	setup.mu.Unlock()
	if !kept || !replaceable {
		t.Errorf("after a failed open: record kept = %v, replaceable = %v; want both", kept, replaceable)
	}
	if err := Setup(SetupOptions{Timezone: "UTC"}); err != nil {
		t.Errorf("a different setup after a failed open = %v, want it to replace the record", err)
	}
	// The replaced record is locked again.
	if err := Setup(SetupOptions{Timezone: "Europe/Berlin"}); !errors.As(err, &ue) {
		t.Errorf("a second, different setup after the replacement = %v, want a *UsageError", err)
	}
}

// TestAFailedOpenUnlocksOnlyItsOwnRecordAndNeverALatchedOne: a failed open
// never unlocks a setup recorded after it began, and once an image has
// completed step 7 a failure unlocks nothing.
func TestAFailedOpenUnlocksOnlyItsOwnRecordAndNeverALatchedOne(t *testing.T) {
	resetSetup(t)
	began := setupGeneration()
	if err := Setup(SetupOptions{Timezone: "Asia/Tokyo"}); err != nil {
		t.Fatal(err)
	}
	failedOpen(began) // began before Asia/Tokyo was recorded
	if err := Setup(SetupOptions{Timezone: "UTC"}); err == nil {
		t.Error("a failed open unlocked a setup recorded after it began")
	}
	latchSetup()
	failedOpen(setupGeneration())
	if err := Setup(SetupOptions{Timezone: "UTC"}); err == nil {
		t.Error("a failed open unlocked a latched setup")
	}
	if err := Setup(SetupOptions{Timezone: "Asia/Tokyo"}); err != nil {
		t.Errorf("the latched setup again = %v, want a no-op", err)
	}
}

func TestInsertOptionsDocument(t *testing.T) {
	// No member set: no document (length 0).
	if b, err := rowConfig(nil).insertOpts.json("insert options"); err != nil || b != nil {
		t.Errorf("empty = %q, %v", b, err)
	}
	// A nil list sets nothing; a non-nil empty list is sent.
	if b, _ := rowConfig([]RowOption{WithInsertableColumns(nil), WithColumnDefaults(nil)}).insertOpts.json("x"); b != nil {
		t.Errorf("nil lists set a member: %s", b)
	}
	c := rowsConfig([]RowsOption{
		WithInsertableColumns([]string{"a", "\xff"}),
		WithColumnDefaults([]ColumnDefault{{Name: "n", Value: "v"}, {Name: "\xfe", Value: "\xff"}}),
		WithVerdictBasis(VerdictBasisExport),
		WithDeferDefaults(DeferDefaultsServer),
		WithInsertOption("future_member", []byte(`{"x":[1,2]}`)),
	})
	b, err := c.insertOpts.json("insert options")
	want := `{"column_defaults":[{"name":"n","value":"v"},{"name_b64":"/g==","value_b64":"/w=="}],"defer_defaults":"server","future_member":{"x":[1,2]},"insertable_columns":[{"name":"a"},{"name_b64":"/w=="}],"verdict_basis":"export"}`
	if err != nil || string(b) != want {
		t.Errorf("insert options = %s, %v, want %s", b, err, want)
	}
	// The same option applies to all four calls that model an INSERT.
	for name, cfg := range map[string]*callConfig{
		"row":   rowConfig([]RowOption{WithDeferDefaults(DeferDefaultsNone)}),
		"block": blockConfig([]BlockOption{WithDeferDefaults(DeferDefaultsNone)}),
		"eval":  evalConfig([]EvalOption{WithDeferDefaults(DeferDefaultsNone), WithColumns([]string{"a"})}),
	} {
		if b, _ := cfg.insertOpts.json("x"); string(b) != `{"defer_defaults":"none"}` {
			t.Errorf("%s: insert options = %s", name, b)
		}
	}
	if c := evalConfig([]EvalOption{WithColumns([]string{"a"})}); !c.haveColumns || len(c.columns) != 1 {
		t.Errorf("an eval does not take the column list: %+v", c)
	}
	// A value that is not JSON is misuse, raised before any call.
	bad := rowConfig([]RowOption{WithInsertOption("k", []byte(`{`))})
	if _, err := bad.insertOpts.json("insert options"); err == nil {
		t.Error("invalid JSON passed through")
	} else if _, ok := err.(*UsageError); !ok {
		t.Errorf("invalid JSON = %T, want *UsageError", err)
	}
}

func TestFilterAndSchemaOptionsDocuments(t *testing.T) {
	f := filterConfig([]FilterOption{WithParamEncoding(ParamEncodingRaw), WithFilterOption("k", []byte(`1`))})
	if b, err := f.filterOpts.json("filter options"); err != nil || string(b) != `{"k":1,"param_encoding":"raw"}` {
		t.Errorf("filter options = %s, %v", b, err)
	}
	c := compileConfig([]CompileOption{WithPosture(PostureLoad), WithSchemaOption("z", []byte(`"v"`))})
	if b, err := c.schemaOpts.json("schema options"); err != nil || string(b) != `{"posture":"load","z":"v"}` {
		t.Errorf("schema options = %s, %v", b, err)
	}
	if b, _ := compileConfig(nil).schemaOpts.json("x"); b != nil {
		t.Errorf("an empty schema options document = %s", b)
	}
}
