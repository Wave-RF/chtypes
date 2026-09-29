package chtypes

// Revision 6's partition key: the two result fields, the setter's sign rule,
// and — with a revision-6 artifact — the key end to end.
//
// The unit half parses documents through the SAME rowResultOf / batchResultOf
// every Row/Rows call uses, and drives partitionByError, which both
// SetPartitionBy paths (dlopen'd and linked) return through. The artifact
// half skips LOUDLY, by name, without a revision-6 registry.

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestPartitionFieldsParse(t *testing.T) {
	js := `{"outcome":"accepted","code":0,"err":"","rows_read":3,"rows_skipped":0,"partition_count":2,
	  "rows":[
	    {"outcome":"accepted","cols":[],"partition_id":"202601"},
	    {"outcome":"accepted","cols":[],"partition_id":"202602"},
	    {"outcome":"rejected","code":27,"err":"x","cols":[]}
	  ]}`
	res, err := batchResultOf(js)
	if err != nil {
		t.Fatalf("batchResultOf: %v", err)
	}
	if res.PartitionCount != 2 {
		t.Errorf("PartitionCount = %d, want 2", res.PartitionCount)
	}
	for i, want := range []string{"202601", "202602", ""} {
		if got := res.Rows[i].PartitionID; got != want {
			t.Errorf("Rows[%d].PartitionID = %q, want %q", i, got, want)
		}
	}
}

// Absent is the zero value, and the zero value is exactly the revision-5
// answer: a handle with no key declared gets byte-identical documents.
func TestPartitionFieldsAbsentMeanZero(t *testing.T) {
	res, err := batchResultOf(`{"outcome":"accepted","rows":[{"outcome":"accepted","cols":[]}]}`)
	if err != nil {
		t.Fatalf("batchResultOf: %v", err)
	}
	if res.PartitionCount != 0 || res.Rows[0].PartitionID != "" {
		t.Fatalf("PartitionCount = %d, PartitionID = %q; want 0 and \"\"", res.PartitionCount, res.Rows[0].PartitionID)
	}
	var doc rowDoc
	if err := json.Unmarshal([]byte(`{"outcome":"accepted","cols":[],"partition_id":"all"}`), &doc); err != nil {
		t.Fatal(err)
	}
	if got := rowResultOf(doc).PartitionID; got != "all" {
		t.Fatalf("a single row's PartitionID = %q, want \"all\"", got)
	}
}

// 252 is an ordinary rejection: the batch verdict and code, and nothing new.
func TestTooManyPartsIsAnOrdinaryRejection(t *testing.T) {
	res, err := batchResultOf(`{"outcome":"rejected","code":252,"err":"Too many partitions for single INSERT block","rows_read":2,
	  "rows":[{"outcome":"accepted","cols":[],"partition_id":"1"},{"outcome":"accepted","cols":[],"partition_id":"2"}]}`)
	if err != nil {
		t.Fatalf("batchResultOf: %v", err)
	}
	if res.Outcome != Rejected || res.ErrCode != 252 {
		t.Fatalf("Outcome = %v, ErrCode = %d; want Rejected, 252", res.Outcome, res.ErrCode)
	}
	if len(res.Rows) != 2 {
		t.Fatalf("the rows stay itemized: got %d, want 2", len(res.Rows))
	}
}

// The setter follows chs_schema_engine's SIGN rule, not chs_schema_ttl's.
func TestSetPartitionBySignRule(t *testing.T) {
	if err := partitionByError(0, ""); err != nil {
		t.Fatalf("rc 0: %v", err)
	}
	var se *SchemaError
	if err := partitionByError(549, "the server's own message"); !errors.As(err, &se) || se.Code != 549 || se.Msg != "the server's own message" {
		t.Fatalf("rc 549: %v, want *SchemaError{549, the server's own message}", err)
	}
	for _, rc := range []int{-1, -2, -3} {
		err := partitionByError(rc, "why")
		var ue *UnsupportedError
		if !errors.As(err, &ue) {
			t.Fatalf("rc %d: %v, want *UnsupportedError", rc, err)
		}
		if errors.As(err, &se) {
			t.Fatalf("rc %d: a decline also satisfied *SchemaError", rc)
		}
	}
	var ue *UnsupportedError
	if err := partitionByError(-3, ""); !errors.As(err, &ue) || ue.Msg == "" {
		t.Fatalf("rc -3 (the symbol is missing) must name why: %v", err)
	}
}

// ------------------------------------------------------------ with artifacts

func TestPartitionKeyEndToEnd(t *testing.T) {
	for _, lib := range rev6Libraries(t) {
		s, err := lib.CompileDDL("ts DateTime, tenant String")
		if err != nil {
			t.Fatalf("%s: CompileDDL: %v", lib.Minor, err)
		}
		body := []byte(`{"ts":"2026-01-15 10:00:00","tenant":"a"}` + "\n" +
			`{"ts":"2026-01-20 10:00:00","tenant":"b"}` + "\n" +
			`{"ts":"2026-02-01 10:00:00","tenant":"a"}` + "\n")

		// No key: the revision-5 answer.
		res, err := s.Rows(JSONEachRow, body, nil)
		if err != nil {
			t.Fatalf("%s: Rows: %v", lib.Minor, err)
		}
		if res.PartitionCount != 0 || res.Rows[0].PartitionID != "" {
			t.Fatalf("%s: no key declared, yet PartitionCount = %d, PartitionID = %q", lib.Minor, res.PartitionCount, res.Rows[0].PartitionID)
		}

		if err := s.SetPartitionBy("toYYYYMM(ts)"); err != nil {
			t.Fatalf("%s: SetPartitionBy: %v", lib.Minor, err)
		}
		res, err = s.Rows(JSONEachRow, body, nil)
		if err != nil {
			t.Fatalf("%s: Rows: %v", lib.Minor, err)
		}
		if res.Outcome != Accepted || res.PartitionCount != 2 {
			t.Fatalf("%s: Outcome = %v, PartitionCount = %d; want Accepted, 2", lib.Minor, res.Outcome, res.PartitionCount)
		}
		p0, p1, p2 := res.Rows[0].PartitionID, res.Rows[1].PartitionID, res.Rows[2].PartitionID
		if p0 == "" || p0 != p1 || p0 == p2 {
			t.Fatalf("%s: partition ids %q %q %q; want the two January rows equal and February different", lib.Minor, p0, p1, p2)
		}

		// Over the limit: the server's own 252, the rows still itemized.
		res, err = s.Rows(JSONEachRow, body, map[string]string{"max_partitions_per_insert_block": "1"})
		if err != nil {
			t.Fatalf("%s: Rows: %v", lib.Minor, err)
		}
		if res.Outcome != Rejected || res.ErrCode != 252 || len(res.Rows) != 3 {
			t.Fatalf("%s: Outcome = %v, ErrCode = %d, %d rows; want Rejected, 252, 3", lib.Minor, res.Outcome, res.ErrCode, len(res.Rows))
		}

		// "" removes the key.
		if err := s.SetPartitionBy(""); err != nil {
			t.Fatalf("%s: SetPartitionBy(\"\"): %v", lib.Minor, err)
		}
		res, err = s.Rows(JSONEachRow, body, nil)
		if err != nil {
			t.Fatalf("%s: Rows: %v", lib.Minor, err)
		}
		if res.PartitionCount != 0 || res.Rows[0].PartitionID != "" {
			t.Fatalf("%s: key removed, yet PartitionCount = %d, PartitionID = %q", lib.Minor, res.PartitionCount, res.Rows[0].PartitionID)
		}

		// A non-deterministic key is declined, never guessed.
		var ue *UnsupportedError
		if err := s.SetPartitionBy("rand()"); !errors.As(err, &ue) {
			t.Fatalf("%s: SetPartitionBy(rand()) = %v, want *UnsupportedError", lib.Minor, err)
		}
		s.Close()
	}
}
