//go:build chtypes_linked

// linked.go — the two demonstrations only the STATICALLY LINKED Go shape can
// give (spec/bindings.md makes that shape optional; the Registry is the
// product path). Built only with `-tags chtypes_linked`, which needs the core
// repository's lib/build on CGO_LDFLAGS (`chplay.sh go` sets both when the
// sibling is present). Without the tag, linked_stub.go prints what was skipped.
package main

import (
	"fmt"
	"strings"

	"github.com/wave-rf/chtypes/go/chtypes"
)

// section9Static is the second half of section 9: the library-defaults layer,
// which the dlopen'd Registry path deliberately cannot reach.
func section9Static(iso []byte, basic, bestEffort map[string]string) {
	kv("static artifact", string(chtypes.BuiltVersion())+"  (the lib/build this binary linked)")

	plain, err := chtypes.CompileDDL(chtypes.BuiltVersion(), "ts DateTime")
	if err != nil {
		fatal("%v", err)
	}
	defer plain.Close()
	profiled, err := chtypes.CompileDDL(chtypes.BuiltVersion(), "ts DateTime", chtypes.WithCompileSettings(bestEffort))
	if err != nil {
		fatal("%v", err)
	}
	defer profiled.Close()

	r1, _ := plain.Rows(chtypes.JSONEachRow, iso, nil)
	kv("1. ClickHouse defaults", verdict(r1))
	note("nothing declared anywhere — the stock default decides (it rejected")
	note("ISO-8601 through 25.10 and accepts it from 26.5)")
	r2, _ := plain.Rows(chtypes.JSONEachRow, iso, basic)
	kv("2.  + per-call basic", verdict(r2))
	r3, _ := profiled.Rows(chtypes.JSONEachRow, iso, nil)
	kv("3. handle profile best_effort", verdict(r3))
	note("the profile declared at COMPILE reaches every later row call")
	r4, _ := profiled.Rows(chtypes.JSONEachRow, iso, basic)
	kv("4.  + per-call basic", verdict(r4))
	note("3 vs 4 is the requirement, measured: the same row PASSES under the")
	note("handle profile and FAILS when the per-call value overrides it")
	blank()

	// The library-defaults layer, and its two safety properties: the seed is
	// REPLACED wholesale on every call, and a payload with any unknown name
	// is refused wholesale — nothing committed.
	kv("SetDefaultSettings", "the library-defaults layer, measured")
	must(chtypes.SetDefaultSettings(bestEffort))
	r5, _ := plain.Rows(chtypes.JSONEachRow, iso, nil)
	kv("5. seed best_effort", verdict(r5))
	must(chtypes.SetDefaultSettings(basic))
	r6, _ := plain.Rows(chtypes.JSONEachRow, iso, nil)
	kv("6. seed basic", verdict(r6))
	note("each call REPLACES the whole seed — 5's value did not linger")
	r7, _ := plain.Rows(chtypes.JSONEachRow, iso, bestEffort)
	kv("7. seed basic + per-call best_effort", verdict(r7))
	note("per-call still outranks the seed")
	err = chtypes.SetDefaultSettings(map[string]string{"made_up_setting_xyz": "1"})
	kv("8. seed an unknown name", errStr(err))
	r8, _ := plain.Rows(chtypes.JSONEachRow, iso, nil)
	kv("   verdict after refusal", verdict(r8)+"   <- unchanged: NOTHING was committed")
	note("the 115 and its message are the server's own; a gateway can never")
	note("believe a default profile is in force when part of it never applied")
	must(chtypes.SetDefaultSettings(map[string]string{}))
	r9, _ := plain.Rows(chtypes.JSONEachRow, iso, nil)
	kv("9. seed {} (reset)", verdict(r9))
}

func section14() {
	section(14, "The static path (Go only)")
	kv("chtypes.BuiltVersion()", string(chtypes.BuiltVersion())+"  (the lib/build artifact this binary linked)")
	kv("chtypes.Timezone", chtypes.Timezone+"  (package default for bare DateTime; set before first call)")

	canon, err := chtypes.ValidateType(chtypes.BuiltVersion(), "Decimal(18,4)")
	kv("ValidateType", "Decimal(18,4) -> "+okOr(err, canon))

	// ParseSchema is the typed alternative to CompileDDL: a Schema struct in,
	// the same compiled handle out.
	ps, err := chtypes.ParseSchema(chtypes.BuiltVersion(), chtypes.Schema{Columns: []chtypes.Column{
		{Name: "a", Type: "UInt8"},
		{Name: "s", Type: "String", Default: "'x'", DefaultKind: chtypes.KindDefault},
	}})
	if err != nil {
		fatal("%v", err)
	}
	kv("ParseSchema", fmt.Sprintf("%d columns; s = %s DEFAULT %s", len(ps.Columns), ps.Columns[1].Type, ps.Columns[1].Default))
	r, err := ps.Row(chtypes.JSONEachRow, []byte(`{"a":300}`))
	if err == nil {
		kv("  Row {\"a\":300}", verdictRow(r))
		for _, t := range r.Transformed {
			raw(fmt.Sprintf("      ~ %s: %s -> %s (%s)", t.Column, t.Input, t.Stored, t.Reason))
		}
		note("the same verdicts as the Registry path — same C functions, same")
		note("vendored ClickHouse, different loader (300 wrapped to 44, reported)")
	}
	ps.Close()

	fams, err := chtypes.RegisteredFamilies()
	if err == nil {
		kv("RegisteredFamilies", fmt.Sprintf("%d type families in this build's own registry (e.g. %s)", len(fams), strings.Join(fams[:3], ", ")))
		note("the answer to 'does this track upstream type families?' — it is")
		note("read from the build itself, so there is no list to maintain")
	}
	kv("SetDefaultSettings", "already measured in section 9 (static path only in Go)")
}
