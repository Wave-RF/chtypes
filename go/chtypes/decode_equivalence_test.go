package chtypes

// decode_equivalence_test.go — the batch decoder against its reference.
//
// decodeBatch (decode_batch.go) must return, for every document, exactly what
// the generic reader it replaced returns: the same BatchResult under
// reflect.DeepEqual, which tells nil from empty, and the same error text. The
// reference is decodeBatchReference in decode_reference_test.go. Three
// document sets are compared:
//
//   - the real documents under testdata/ (decoded at doc_flags 0 and 7);
//   - every batch and row document the decoders' own tests use, the row ones
//     wrapped as the rows of a batch;
//   - mutations of those: a key repeated, a type changed, a name dropped or
//     doubled, a value that is not base64, an unknown key, a null where an
//     object is wanted, trailing data, truncation, and seeded random edits.
//
// A comparison that always fell back to the generic reader would pass by
// comparing it with itself, so the well-formed documents must also be answered
// by the fast path itself (decodeBatchFast), and a document that no longer is
// fails the test.
//
// One class of document is left out on purpose: a key that differs from a
// known key only in letter case. encoding/json matches struct fields
// case-insensitively, so below the top level the fast path reads it as the
// known key where the generic reader ignores it as unknown (decode_batch.go
// says so); the library never emits one, and the random edits below never
// create one because they only write structural bytes.

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

// batchFixtures is every batch document the decoder tests use, by name.
var batchFixtures = map[string]string{
	"empty object":        `{}`,
	"minimal":             `{"outcome":"accepted"}`,
	"big integer":         `{"outcome":"accepted","rows_read":"18446744073709551615"}`,
	"unknown outcome":     `{"outcome":"something_new"}`,
	"framing known":       `{"outcome":"accepted","rows":[],"unconsumed":[],"framing":{"bom_skipped":false,"container":"array","header":{"consumed":true,"lines":1,"names":[{"name":"a"},{"name_b64":"/w=="}]}}}`,
	"framing unknown":     `{"outcome":"accepted","rows":[],"framing":{"bom_skipped":null,"container":null,"header":null}}`,
	"framing empty":       `{"outcome":"accepted","framing":{}}`,
	"rows empty and null": `{"outcome":"accepted","rows":null,"transformed":null,"unconsumed":[],"row_spans":null,"engine_rows":[null,[]]}`,
	"every top-level field": `{
	  "outcome": "accepted_poisoned", "code": 0, "err": "",
	  "rows_read": 2, "rows_skipped": 1,
	  "rows": [{"outcome": "accepted", "input_span": {"off": 0, "len": 3}}, {"outcome": "skipped", "input_span": {"off": 3, "len": 4}, "err": "bad"}],
	  "transformed": [{"column": "a", "input": "1", "stored": "2", "reason": "ttl_expired", "row": 1}],
	  "engine_rows": [[{"name":"a","stored":"1","null":false},{"name_b64":"/w==","stored_b64":"/wA=","value_b64":"/wA=","null":false},{"name":"n","null":true}]],
	  "row_spans": [{"off": 0, "len": 5}],
	  "export_declined": "",
	  "rows_passed": 1, "rows_cut": 0,
	  "partition_count": 4,
	  "unconsumed": [{"off": 9, "len": 2}],
	  "framing": {"bom_skipped": null, "container": null, "header": null}
	}`,
	"byte-safe batch fields": `{"outcome":"rejected","code":27,"err_b64":"/w==","export_declined_b64":"/gA=","rows_read":"7"}`,
	"storage transforms":     `{"outcome":"accepted","storage_transforms":[{"row":0,"column":"a","stored":"1"}]}`,
}

// rowFixtures are documents of one row, as decodeRow reads them; each also
// becomes the single row of a batch.
var rowFixtures = map[string]string{
	"row, every field":        rowDoc,
	"row, minimal":            `{"outcome":"accepted"}`,
	"row, empty lists":        `{"outcome":"accepted","cols":[],"computed":[],"transformed":[],"unknown_fields":[],"unsupported_settings":[]}`,
	"row, null lists":         `{"outcome":"accepted","cols":null,"computed":null,"transformed":null,"unknown_fields":null,"unsupported_settings":null,"input_span":null,"verdict":null,"partition_id":null}`,
	"row, partition id bytes": `{"outcome":"accepted","partition_id_b64":"/w==","err_b64":"/gA=","verdict_err_b64":"/w==","verdict_code":3}`,
	"row, string value":       `{"outcome":"accepted","cols":[{"name":"s","src":"input","null":false,"stored":"\"a\\\"b\"","value_b64":"YSJi","type":"String","ref":"x"}]}`,
	"row, unknown outcome":    `{"outcome":"something_new"}`,
}

// refusalFixtures are the row refusals of TestDecodeRowRefusals, run as the
// rows of a batch too.
var refusalFixtures = map[string]string{
	"unknown field as a bare string": `{"unknown_fields": ["u"]}`,
	"duplicate key":                  `{"outcome": "accepted", "outcome": "rejected"}`,
	"duplicate key, nested":          `{"cols": [{"name": "a", "name": "b", "src": "input"}]}`,
	"name both ways":                 `{"cols": [{"name": "a", "name_b64": "YQ==", "src": "input"}]}`,
	"name neither way":               `{"cols": [{"src": "input"}]}`,
	"name, bad base64":               `{"cols": [{"name_b64": "!!", "src": "input"}]}`,
	"wrong type":                     `{"code": "7x"}`,
	"cols not an array":              `{"cols": {"name": "a"}}`,
	"source not in the list":         `{"cols": [{"name": "a", "src": "no_such_source"}]}`,
	"source absent":                  `{"cols": [{"name": "a"}]}`,
	"not an object":                  `[1]`,
	"stored both ways":               `{"cols": [{"name": "a", "src": "input", "stored": "1", "stored_b64": "MQ=="}]}`,
}

func batchOfRows(rows ...string) string {
	return `{"outcome":"accepted","rows":[` + strings.Join(rows, ",") + `]}`
}

func readTestdata(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// compareDecoders fails the test when the two decoders disagree on doc.
func compareDecoders(t *testing.T, label, doc string) {
	t.Helper()
	payload := []byte("payload")
	got, gotErr := decodeBatch([]byte(doc), payload)
	want, wantErr := decodeBatchReference([]byte(doc), payload)
	switch {
	case (gotErr == nil) != (wantErr == nil):
		t.Errorf("%s: error = %v, reference error = %v\ndocument: %.300s", label, gotErr, wantErr, doc)
	case gotErr != nil:
		if gotErr.Error() != wantErr.Error() {
			t.Errorf("%s: error = %q, reference error = %q\ndocument: %.300s", label, gotErr, wantErr, doc)
		}
	case !reflect.DeepEqual(got, want):
		t.Errorf("%s: result differs from the reference\n got: %+v\nwant: %+v\ndocument: %.300s", label, got, want, doc)
	}
}

// wellFormed is every document the fast path must answer by itself.
func wellFormed(t *testing.T) map[string]string {
	t.Helper()
	docs := map[string]string{
		"real, doc_flags 0": readTestdata(t, "batch-100-flags0.json"),
		"real, doc_flags 7": readTestdata(t, "batch-100-flags7.json"),
	}
	for name, doc := range batchFixtures {
		docs["batch: "+name] = doc
	}
	for name, doc := range rowFixtures {
		docs[name] = batchOfRows(doc)
	}
	docs["rows: two real rows"] = batchOfRows(rowFixtures["row, every field"], rowFixtures["row, string value"], rowFixtures["row, minimal"])
	return docs
}

func TestDecodeBatchEquivalence(t *testing.T) {
	for name, doc := range wellFormed(t) {
		compareDecoders(t, name, doc)
		if _, err := decodeBatchReference([]byte(doc), nil); err != nil {
			t.Errorf("%s: the reference refuses a document meant to be well-formed: %v", name, err)
		}
		if _, ok := decodeBatchFast([]byte(doc), nil); !ok {
			t.Errorf("%s: the fast path declined a well-formed document", name)
		}
	}
	for name, doc := range refusalFixtures {
		compareDecoders(t, "refusal: "+name, batchOfRows(doc))
	}
	// A fixture that is not a batch at all.
	for name, doc := range map[string]string{
		"empty":       ``,
		"null":        `null`,
		"array":       `[]`,
		"string":      `"x"`,
		"number":      `1`,
		"truncated":   `{"outcome":"accepted","rows":[`,
		"trailing":    `{"outcome":"accepted"} {}`,
		"trailing 2":  `{"outcome":"accepted"}x`,
		"two objects": `{} {}`,
	} {
		compareDecoders(t, "not a batch: "+name, doc)
	}
}

// TestDecodeBatchEquivalenceReal pins the real documents' content, so the
// comparison above cannot pass by agreeing on an empty result.
func TestDecodeBatchEquivalenceReal(t *testing.T) {
	for _, tc := range []struct {
		file    string
		columns bool
	}{{"batch-100-flags0.json", false}, {"batch-100-flags7.json", true}} {
		res, err := decodeBatch([]byte(readTestdata(t, tc.file)), nil)
		if err != nil {
			t.Fatalf("%s: %v", tc.file, err)
		}
		if len(res.Rows) != 100 || res.RowsRead != 100 || res.Outcome != Accepted {
			t.Fatalf("%s: outcome %q, rows_read %d, %d rows", tc.file, res.Outcome, res.RowsRead, len(res.Rows))
		}
		if got := len(res.Rows[0].Columns) > 0; got != tc.columns {
			t.Errorf("%s: row 0 has columns = %v, want %v", tc.file, got, tc.columns)
		}
		if tc.columns {
			if len(res.Transformed) == 0 || len(res.Rows[0].Computed) == 0 || res.Rows[1].Columns[1].Value == nil {
				t.Errorf("%s: no transforms, computed values or string values decoded", tc.file)
			}
		}
	}
}

// mutations returns the documents derived from doc by one targeted edit each.
func mutations(doc string) map[string]string {
	out := map[string]string{}
	// replace swaps the first occurrence of old.
	replace := func(name, old, repl string) {
		if i := strings.Index(doc, old); i >= 0 {
			out[name] = doc[:i] + repl + doc[i+len(old):]
		}
	}
	replace("top-level key twice", `"outcome":`, `"outcome":"accepted","outcome":`)
	replace("code twice", `"code":`, `"code":0,"code":`)
	replace("rows twice", `"rows":[`, `"rows":[],"rows":[`)
	replace("framing twice", `"framing":{`, `"framing":{},"framing":{`)
	replace("outcome a number", `"outcome":"accepted"`, `"outcome":5`)
	replace("outcome null", `"outcome":"accepted"`, `"outcome":null`)
	replace("code a string", `"code":0`, `"code":"abc"`)
	replace("code a big string", `"code":0`, `"code":"2147483648"`)
	replace("code a float", `"code":0`, `"code":1.5`)
	replace("code a bool", `"code":0`, `"code":true`)
	replace("rows_read negative", `"rows_read":`, `"rows_read":-`)
	replace("rows_read float", `"rows_read":100`, `"rows_read":100.0`)
	replace("rows not an array", `"rows":[`, `"rows":{},"x":[`)
	replace("row null", `"rows":[`, `"rows":[null,`)
	replace("row a number", `"rows":[`, `"rows":[5,`)
	replace("row key twice", `"cols":[`, `"cols":[],"cols":[`)
	replace("row outcome twice", `{"outcome":"accepted","code":0,"err":""`, `{"outcome":"accepted","outcome":"accepted","code":0,"err":""`)
	replace("row err both ways", `"err":""`, `"err":"","err_b64":"YQ=="`)
	replace("row err_b64 null", `"err":""`, `"err_b64":null`)
	replace("row err null", `"err":""`, `"err":null`)
	replace("row err wrong type", `"err":""`, `"err":[]`)
	replace("col key twice", `"src":"input"`, `"src":"input","src":"input"`)
	replace("col name twice", `"name":"id"`, `"name":"id","name":"id"`)
	replace("col name both ways", `"name":"id"`, `"name":"id","name_b64":"aWQ="`)
	replace("col name neither", `"name":"id",`, ``)
	replace("col name null", `"name":"id"`, `"name":null`)
	replace("col name b64", `"name":"id"`, `"name_b64":"aWQ="`)
	replace("col name bad b64", `"name":"id"`, `"name_b64":"!!"`)
	replace("col name b64 not padded", `"name":"id"`, `"name_b64":"aWQ"`)
	replace("col name b64 with a newline", `"name":"id"`, `"name_b64":"aW\nQ="`)
	replace("col name b64 escaped slash", `"name":"id"`, `"name_b64":"aW\/Q"`)
	replace("col src unknown", `"src":"input"`, `"src":"bogus"`)
	replace("col src absent", `"src":"input",`, ``)
	replace("col src a number", `"src":"input"`, `"src":1`)
	replace("col null a string", `"null":false`, `"null":"no"`)
	replace("col null as JSON null", `"null":false`, `"null":null`)
	replace("col stored both ways", `"stored":"0"`, `"stored":"0","stored_b64":"MA=="`)
	replace("col stored null", `"stored":"0"`, `"stored":null`)
	replace("col value_b64 null", `"value_b64":`, `"value_b64":null,"x":`)
	replace("col value_b64 a number", `"value_b64":"`, `"value_b64":5,"x":"`)
	replace("col value_b64 bad", `"value_b64":"`, `"value_b64":"!`)
	replace("col unknown key", `"src":"input"`, `"src":"input","future":1`)
	replace("col unknown key twice", `"src":"input"`, `"src":"input","future":1,"future":2`)
	replace("col ignored key twice", `"ref":`, `"ref":"","ref":`)
	replace("col null element", `"cols":[{`, `"cols":[null,{`)
	replace("col empty element", `"cols":[{`, `"cols":[{},{`)
	replace("col element a string", `"cols":[{`, `"cols":["x",{`)
	replace("cols a string", `"cols":[`, `"cols":"x","y":[`)
	replace("row unknown key", `"outcome":"accepted"`, `"outcome":"accepted","future":{"a":1,"a":2}`)
	replace("top-level unknown key", `"rows_read":`, `"future":[1],"rows_read":`)
	replace("computed null element", `"computed":[{`, `"computed":[null,{`)
	replace("computed name neither", `"name":"total",`, ``)
	replace("input_span a string", `"input_span":{`, `"input_span":"x","y":{`)
	replace("input_span twice", `"input_span":{`, `"input_span":{},"input_span":{`)
	replace("input_span unknown key", `"input_span":{`, `"input_span":{"z":1,`)
	replace("span off negative", `"off":0`, `"off":-1`)
	replace("span off a big string", `"off":0`, `"off":"18446744073709551615"`)
	replace("span off too big", `"off":0`, `"off":"18446744073709551616"`)
	replace("span off escaped digits", `"off":0`, `"off":"0"`)
	replace("span off exponent", `"off":0`, `"off":1e2`)
	replace("transform reason unknown", `"reason":"default_filled"`, `"reason":"a_reason_nobody_listed"`)
	replace("transform row negative", `"row":0`, `"row":-1`)
	replace("transform row too big", `"row":0`, `"row":2147483648`)
	replace("transform column neither", `"column":"label",`, ``)
	replace("transform key twice", `"reason":"default_filled"`, `"reason":"default_filled","reason":"x"`)
	replace("framing a string", `"framing":{`, `"framing":"x","y":{`)
	replace("framing container a number", `"container":"stream"`, `"container":5`)
	replace("framing container null", `"container":"stream"`, `"container":null`)
	replace("framing bom_skipped a string", `"bom_skipped":false`, `"bom_skipped":"no"`)
	replace("framing header a string", `"header":null`, `"header":"x"`)
	replace("framing header names", `"header":null`, `"header":{"consumed":true,"lines":2,"names":[{"name":"a"}]}`)
	replace("framing header names, neither", `"header":null`, `"header":{"names":[{}]}`)
	replace("framing header names twice", `"header":null`, `"header":{"names":[],"names":[]}`)
	replace("framing header lines negative", `"header":null`, `"header":{"lines":-1}`)
	replace("framing unknown key", `"bom_skipped":false`, `"bom_skipped":false,"future":1`)
	replace("unconsumed element null", `"unconsumed":[]`, `"unconsumed":[null]`)
	replace("unconsumed element empty", `"unconsumed":[]`, `"unconsumed":[{}]`)
	replace("unconsumed span", `"unconsumed":[]`, `"unconsumed":[{"off":1,"len":2},{"off":"3","len":4}]`)
	replace("engine_rows cell", `"unconsumed":[]`, `"unconsumed":[],"engine_rows":[[{"name":"a","stored":"1","null":false,"value_b64":"MQ=="}],null,[]]`)
	replace("engine_rows cell, no name", `"unconsumed":[]`, `"unconsumed":[],"engine_rows":[[{"stored":"1"}]]`)
	replace("engine_rows not arrays", `"unconsumed":[]`, `"unconsumed":[],"engine_rows":[{}]`)
	replace("partition_count", `"unconsumed":[]`, `"unconsumed":[],"partition_count":7`)
	replace("partition_count null", `"unconsumed":[]`, `"unconsumed":[],"partition_count":null`)
	replace("partition_count bad", `"unconsumed":[]`, `"unconsumed":[],"partition_count":"x"`)
	replace("storage_transforms twice", `"unconsumed":[]`, `"unconsumed":[],"storage_transforms":[],"storage_transforms":[]`)
	replace("verdict", `"outcome":"accepted"`, `"outcome":"accepted","verdict":"t","verdict_code":2`)
	replace("verdict a number", `"outcome":"accepted"`, `"outcome":"accepted","verdict":1`)
	replace("partition_id", `"outcome":"accepted"`, `"outcome":"accepted","partition_id":"p"`)
	replace("partition_id both ways", `"outcome":"accepted"`, `"outcome":"accepted","partition_id":"p","partition_id_b64":"cA=="`)
	replace("unknown_fields as strings", `"outcome":"accepted"`, `"outcome":"accepted","unknown_fields":["u"]`)
	replace("unknown_fields as names", `"outcome":"accepted"`, `"outcome":"accepted","unknown_fields":[{"name":"u"},{"name_b64":"/w=="}]`)
	replace("unsupported_settings null element", `"outcome":"accepted"`, `"outcome":"accepted","unsupported_settings":[null]`)
	out["trailing data"] = doc + ` {}`
	out["trailing garbage"] = doc + `x`
	out["leading space"] = "  \n" + doc + "\n "
	out["truncated, half"] = doc[:len(doc)/2]
	out["truncated, one byte short"] = doc[:len(doc)-1]
	return out
}

func TestDecodeBatchEquivalenceMutations(t *testing.T) {
	n := 0
	for _, file := range []string{"batch-100-flags0.json", "batch-100-flags7.json"} {
		doc := readTestdata(t, file)
		for name, m := range mutations(doc) {
			compareDecoders(t, file+": "+name, m)
			n++
		}
	}
	// Hand-written documents carry the paths the real ones do not: verdicts,
	// engine rows, names as bytes, a header.
	for name, doc := range wellFormed(t) {
		for mname, m := range mutations(doc) {
			compareDecoders(t, name+": "+mname, m)
			n++
		}
	}
	if n < 500 {
		t.Fatalf("only %d mutated documents were compared; the mutation table stopped applying", n)
	}
}

// TestDecodeBatchEquivalenceRandomEdits applies seeded random edits that write
// only structural bytes or copy or cut a span of the document itself, so none
// can turn a key into a case variant of a known one.
func TestDecodeBatchEquivalenceRandomEdits(t *testing.T) {
	rng := rand.New(rand.NewSource(456))
	structural := []byte(`{}[],:"\ 0189-.nul`)
	docs := []string{readTestdata(t, "batch-100-flags0.json"), readTestdata(t, "batch-100-flags7.json")}
	for _, name := range []string{"every top-level field", "framing known"} {
		docs = append(docs, batchFixtures[name])
	}
	docs = append(docs, batchOfRows(rowFixtures["row, every field"]))
	iterations := 2000
	if testing.Short() {
		iterations = 500
	}
	declined, answered := 0, 0
	for i := 0; i < iterations; i++ {
		b := []byte(docs[i%len(docs)])
		for edits := 1 + rng.Intn(2); edits > 0; edits-- {
			p := rng.Intn(len(b))
			switch rng.Intn(3) {
			case 0: // overwrite one byte
				b[p] = structural[rng.Intn(len(structural))]
			case 1: // cut a span
				q := p + 1 + rng.Intn(24)
				if q > len(b) {
					q = len(b)
				}
				b = append(b[:p:p], b[q:]...)
			default: // copy a span of the document to another place
				q := p + 1 + rng.Intn(48)
				if q > len(b) {
					q = len(b)
				}
				at := rng.Intn(len(b))
				span := append([]byte(nil), b[p:q]...)
				b = append(b[:at:at], append(span, b[at:]...)...)
			}
			if len(b) == 0 {
				break
			}
		}
		doc := string(b)
		compareDecoders(t, fmt.Sprintf("edit %d", i), doc)
		if _, ok := decodeBatchFast([]byte(doc), nil); ok {
			answered++
		} else {
			declined++
		}
	}
	// Both outcomes must occur, or the loop proved nothing about one of them.
	if answered == 0 || declined == 0 {
		t.Errorf("random edits: the fast path answered %d and declined %d documents", answered, declined)
	}
}

// TestJSONStringMatchesStdlib compares the string decoder's own unescaping with
// encoding/json on strings it handles itself and strings it hands over.
func TestJSONStringMatchesStdlib(t *testing.T) {
	for _, in := range []string{
		`""`, `"a"`, `"caf\u00e9"`, `"\"q\""`, `"\\"`, `"\/"`, `"\b\f\n\r\t"`,
		`"\ud83d\ude00"`, `"\ud83d"`, `"\u0000"`, "\"\xff\"", "\"a\xffb\\n\"", "\"\xe2\x82\"",
		`"` + strings.Repeat(`\"x`, 200) + `"`, `"` + strings.Repeat("é", 300) + `"`,
		`"tab\there"`, `"\u2028\u2029"`, `"\u00"`,
	} {
		var want string
		wantErr := json.Unmarshal([]byte(in), &want)
		got, gotErr := jsonString([]byte(in), false)
		if (wantErr == nil) != (gotErr == nil) || (wantErr == nil && got != want) {
			t.Errorf("%q: got %q, %v; stdlib %q, %v", in, got, gotErr, want, wantErr)
		}
		if wantErr == nil {
			if w, err := jsonString([]byte(in), true); err != nil || w != want {
				t.Errorf("%q as a word: got %q, %v; want %q", in, w, err, want)
			}
		}
	}
}

// TestInternWordBounded: the table answers equal strings alike, copies a word
// it can no longer hold, and stops growing.
func TestInternWordBounded(t *testing.T) {
	word := []byte("interned-once")
	if a := internWord(word); a != "interned-once" {
		t.Errorf("word = %q", a)
	}
	if n := testing.AllocsPerRun(100, func() { internWord(word) }); n != 0 {
		t.Errorf("a repeated word allocates %v times, want 0", n)
	}
	for i := 0; i < 2*wordMax; i++ {
		if got, want := internWord([]byte(fmt.Sprintf("w%d", i))), fmt.Sprintf("w%d", i); got != want {
			t.Fatalf("word %d = %q", i, got)
		}
	}
	if n := len(*words.Load()); n > wordMax {
		t.Errorf("the table holds %d words, the cap is %d", n, wordMax)
	}
	long := strings.Repeat("l", wordLen+1)
	if got := internWord([]byte(long)); got != long {
		t.Errorf("a long word = %q", got)
	}
}

// TestDecodeBatchConcurrent decodes the real documents from many goroutines
// at once, as the race detector wants for the shared word table and the
// decoder pool.
func TestDecodeBatchConcurrent(t *testing.T) {
	docs := wellFormed(t)
	var names []string
	for name := range docs {
		names = append(names, name)
	}
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 40; i++ {
				name := names[(g+i)%len(names)]
				got, err := decodeBatch([]byte(docs[name]), nil)
				want, werr := decodeBatchReference([]byte(docs[name]), nil)
				if (err == nil) != (werr == nil) || (err == nil && !reflect.DeepEqual(got, want)) {
					t.Errorf("%s: decoders disagree under concurrency", name)
					return
				}
			}
		}(g)
	}
	wg.Wait()
}
