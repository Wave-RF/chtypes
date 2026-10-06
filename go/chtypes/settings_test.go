package chtypes

import (
	"errors"
	"strings"
	"testing"
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
