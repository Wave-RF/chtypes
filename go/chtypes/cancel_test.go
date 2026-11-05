package chtypes

// cancel_test.go — the context variants over the ABI v2 test stub, whose
// cancel token is real (scripts/abi-v1/emit/_stubshared.py CANCEL): a body
// beginning `!B:` blocks inside the call until its token fires, and the probe
// ValidateType("!C:") reads the stub's own counters, the calls blocked in that
// directive (inflight) and the fires that reached a freed token
// (fire_after_free). The tests sequence on the probe, never on a sleep.
// Without CHTYPES_ABI2_STUBS every test here skips LOUDLY by name.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

type cancelCounters struct {
	FireAfterFree int64 `json:"fire_after_free"`
	Inflight      int64 `json:"inflight"`
}

func cancelProbe(t *testing.T, lib *Library) cancelCounters {
	t.Helper()
	out, err := lib.ValidateType("!C:")
	if err != nil {
		t.Fatalf("the cancel probe: %v", err)
	}
	var c cancelCounters
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatalf("the cancel probe answered %q: %v", out, err)
	}
	return c
}

// waitInflight returns once the stub reads exactly want calls blocked in its
// directive. The bound only stops a broken stub from hanging the suite; the
// sequencing is the probe's.
func waitInflight(t *testing.T, lib *Library, want int64) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		got := cancelProbe(t, lib).Inflight
		if got == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the stub reads %d calls in flight, want %d", got, want)
		}
		runtime.Gosched()
	}
}

// cancelFixture is a stub image with a schema and a filter on it.
type cancelFixture struct {
	lib    *Library
	schema *Schema
	filter *Filter
}

func newCancelFixture(t *testing.T) cancelFixture {
	t.Helper()
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { schema.Close() })
	f, err := schema.CompileFilter("x < 5")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return cancelFixture{lib, schema, f}
}

// cancelCall is one cancelable call: it runs under ctx over body and reports
// whether it produced any output alongside its error.
type cancelCall struct {
	name string
	run  func(ctx context.Context, body []byte) (output bool, err error)
}

func (fx cancelFixture) calls() []cancelCall {
	return []cancelCall{
		{"RowContext", func(ctx context.Context, body []byte) (bool, error) {
			r, err := fx.schema.RowContext(ctx, JSONEachRow, body)
			return !reflect.DeepEqual(r, RowResult{}), err
		}},
		{"RowsContext", func(ctx context.Context, body []byte) (bool, error) {
			r, err := fx.schema.RowsContext(ctx, JSONEachRow, body, WithExport(CSV))
			return !reflect.DeepEqual(r, BatchResult{}), err
		}},
		{"InsertContext", func(ctx context.Context, body []byte) (bool, error) {
			_, rows, err := fx.schema.insertRaw(ctx, JSONEachRow, body, CSV)
			return rows != nil, err
		}},
		{"ParseBlockContext", func(ctx context.Context, body []byte) (bool, error) {
			b, err := fx.schema.ParseBlockContext(ctx, JSONEachRow, body)
			if b != nil {
				b.Close()
			}
			return b != nil, err
		}},
		{"Filter.RowsContext", func(ctx context.Context, body []byte) (bool, error) {
			r, err := fx.filter.RowsContext(ctx, JSONEachRow, body)
			return !reflect.DeepEqual(r, FilterResult{}), err
		}},
		{"CompileTableContext", func(ctx context.Context, body []byte) (bool, error) {
			s, err := fx.lib.CompileTableContext(ctx, string(body))
			if s != nil {
				s.Close()
			}
			return s != nil, err
		}},
	}
}

// checkCanceled asserts a canceled call: a *CanceledError carrying the
// library's reason, with Cause the context's, and no output.
func checkCanceled(t *testing.T, name string, output bool, err error, cause error) {
	t.Helper()
	var ce *CanceledError
	if !errors.As(err, &ce) {
		t.Fatalf("%s: err = %v, want a *CanceledError", name, err)
	}
	if ce.Status != StatusCanceled || ce.Reason != "call-canceled" || ce.ChCode != 0 {
		t.Errorf("%s: status %v, reason %q, ch_code %d; want CHS_CANCELED, call-canceled, 0", name, ce.Status, ce.Reason, ce.ChCode)
	}
	if !errors.Is(err, cause) || !errors.Is(ce.Cause, cause) {
		t.Errorf("%s: errors.Is(err, %v) is false (Cause = %v)", name, cause, ce.Cause)
	}
	if output {
		t.Errorf("%s: a canceled call returned output", name)
	}
	if c, ok := AsCallError(err); !ok || c.Status != StatusCanceled {
		t.Errorf("%s: AsCallError = %v, %v", name, c, ok)
	}
}

func TestStubCancelAlreadyFired(t *testing.T) {
	fx := newCancelFixture(t)
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	why := errors.New("the request went away")
	caused, cancelCaused := context.WithCancelCause(context.Background())
	cancelCaused(why)
	for _, tc := range []struct {
		ctx   context.Context
		cause error
	}{{canceled, context.Canceled}, {expired, context.DeadlineExceeded}, {caused, why}} {
		for _, c := range fx.calls() {
			// A body the call would answer: the pre-fired token answers first.
			body := []byte(`{"x":1}`)
			if c.name == "CompileTableContext" {
				body = []byte("CREATE TABLE t (x UInt8) ENGINE = Memory")
			}
			out, err := c.run(tc.ctx, body)
			checkCanceled(t, c.name, out, err, tc.cause)
		}
	}
	if h, err := fx.lib.LiveHandles(); err != nil || h["chs_cancel"] != 0 {
		t.Errorf("live chs_cancel after the calls = %v, %v; want 0", h, err)
	}
}

func TestStubCancelDuringTheCall(t *testing.T) {
	fx := newCancelFixture(t)
	for _, c := range fx.calls() {
		base := cancelProbe(t, fx.lib).Inflight
		ctx, cancel := context.WithCancel(context.Background())
		type res struct {
			out bool
			err error
		}
		done := make(chan res, 1)
		go func() {
			out, err := c.run(ctx, []byte("!B:block"))
			done <- res{out, err}
		}()
		waitInflight(t, fx.lib, base+1)
		cancel()
		r := <-done
		checkCanceled(t, c.name, r.out, r.err, context.Canceled)
		waitInflight(t, fx.lib, base)
	}
}

// A cancel that arrives after the library's last check point finds a complete
// answer: the call returns it, never an error ("a complete answer is never
// turned into a cancel"). The stub's !L: body passes its last check point,
// waits for the token, then answers the echo.
func TestCancelAfterLastCheckPoint(t *testing.T) {
	fx := newCancelFixture(t)
	body := []byte("!L:late")
	want, err := fx.schema.Row(JSONEachRow, body)
	if err != nil {
		t.Fatal(err)
	}
	base := cancelProbe(t, fx.lib).Inflight
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type res struct {
		row RowResult
		err error
	}
	done := make(chan res, 1)
	go func() {
		r, err := fx.schema.RowContext(ctx, JSONEachRow, body)
		done <- res{r, err}
	}()
	waitInflight(t, fx.lib, base+1)
	cancel()
	r := <-done
	if r.err != nil {
		t.Fatalf("a cancel after the last check point returned %v; want the complete answer", r.err)
	}
	if !reflect.DeepEqual(r.row, want) {
		t.Errorf("RowContext = %+v; want the answer with no cancel source %+v", r.row, want)
	}
	waitInflight(t, fx.lib, base)
}

func TestStubCancelNeverFiredIsTheSyncCall(t *testing.T) {
	fx := newCancelFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// A context that is never done: the stub's real row document is the same
	// with and without the token.
	body := []byte("!D:a\x00b")
	want, err := fx.schema.Row(JSONEachRow, body)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []context.Context{context.Background(), ctx} {
		got, err := fx.schema.RowContext(c, JSONEachRow, body)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("RowContext = %+v, %v; want %+v", got, err, want)
		}
	}
	// context.Background is exactly the call without a source: the options
	// argument on the wire is empty, as Insert sends it, and no token is made.
	before, _ := fx.lib.LiveHandles()
	_, plain, err := fx.schema.insertRaw(context.Background(), JSONEachRow, []byte("ab"), CSV)
	if err != nil {
		t.Fatal(err)
	}
	if o := wireBytes(t, wireOf(t, InsertResult{Output: plain})[7]); o != "" {
		t.Errorf("options on the wire with context.Background = %q, want none", o)
	}
	after, _ := fx.lib.LiveHandles()
	if !reflect.DeepEqual(before, after) {
		t.Errorf("live handles %v -> %v", before, after)
	}
	// A cancelable context sends the token's id, merged with the members the
	// call already sets.
	_, withTok, err := fx.schema.insertRaw(ctx, JSONEachRow, []byte("ab"), CSV, WithMemoryLimit(7))
	if err != nil {
		t.Fatal(err)
	}
	var e struct {
		Args []json.RawMessage `json:"args"`
	}
	if err := json.Unmarshal(withTok, &e); err != nil {
		t.Fatal(err)
	}
	if o := wireBytes(t, e.Args[7]); !strings.HasPrefix(o, `{"cancel":"c`) {
		t.Errorf("options on the wire with a cancelable context start %q, want the cancel member", o)
	}
	var o7 struct {
		Len int `json:"len"`
	}
	_ = json.Unmarshal(e.Args[7], &o7)
	// {"cancel":"c<16 hex>","memory_limit_bytes":"7"}
	if want := len(`{"cancel":"c0123456789abcdef","memory_limit_bytes":"7"}`); o7.Len != want {
		t.Errorf("options document is %d bytes, want %d", o7.Len, want)
	}
	// Every other call answers normally under a never-fired context.
	for _, c := range fx.calls() {
		body := []byte(`{"x":1}`)
		if c.name == "CompileTableContext" {
			body = []byte("CREATE TABLE t (x UInt8) ENGINE = Memory")
		}
		if _, err := c.run(ctx, body); err != nil {
			t.Errorf("%s under a never-fired context: %v", c.name, err)
		}
	}
	if h, _ := fx.lib.LiveHandles(); h["chs_cancel"] != 0 {
		t.Errorf("live chs_cancel = %d, want 0", h["chs_cancel"])
	}
}

func TestStubCancelOneSourceCancelsEveryCall(t *testing.T) {
	fx := newCancelFixture(t)
	calls := fx.calls()
	base := cancelProbe(t, fx.lib).Inflight
	ctx, cancel := context.WithCancel(context.Background())
	errs := make([]error, len(calls))
	outs := make([]bool, len(calls))
	var wg sync.WaitGroup
	for i, c := range calls {
		wg.Add(1)
		go func() {
			defer wg.Done()
			outs[i], errs[i] = c.run(ctx, []byte("!B:block"))
		}()
	}
	waitInflight(t, fx.lib, base+int64(len(calls)))
	cancel()
	wg.Wait()
	for i, c := range calls {
		checkCanceled(t, c.name, outs[i], errs[i], context.Canceled)
	}
}

func TestStubCancelIndependentSources(t *testing.T) {
	fx := newCancelFixture(t)
	base := cancelProbe(t, fx.lib).Inflight
	ctx1, cancel1 := context.WithCancel(context.Background())
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	done1, done2 := make(chan error, 1), make(chan error, 1)
	go func() { _, err := fx.schema.RowContext(ctx1, JSONEachRow, []byte("!B:one")); done1 <- err }()
	go func() { _, err := fx.schema.RowContext(ctx2, JSONEachRow, []byte("!B:two")); done2 <- err }()
	waitInflight(t, fx.lib, base+2)
	cancel1()
	checkCanceled(t, "the first call", false, <-done1, context.Canceled)
	// The second call is still blocked on its own token.
	waitInflight(t, fx.lib, base+1)
	select {
	case err := <-done2:
		t.Fatalf("the second call returned when only the first source was canceled: %v", err)
	default:
	}
	cancel2()
	checkCanceled(t, "the second call", false, <-done2, context.Canceled)
}

// cancelSoak races cancels against returns: each round starts calls under one
// context and cancels it from another goroutine at once, so some calls see a
// pre-fired token, some are canceled mid-call and some return first.
func cancelSoak(t *testing.T, fx cancelFixture, rounds int) {
	t.Helper()
	calls := fx.calls()
	var wg sync.WaitGroup
	for r := 0; r < rounds; r++ {
		ctx, cancel := context.WithCancel(context.Background())
		for i := 0; i < 4; i++ {
			c := calls[(r+i)%len(calls)]
			body := []byte(`{"x":1}`)
			if c.name == "CompileTableContext" {
				body = []byte("CREATE TABLE t (x UInt8) ENGINE = Memory")
			}
			if (r+i)%7 == 0 {
				body = []byte("!B:soak") // blocks until the cancel below
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, err := c.run(ctx, body)
				var ce *CanceledError
				if err != nil && !errors.As(err, &ce) {
					t.Errorf("%s in the soak: %v", c.name, err)
				}
			}()
		}
		go cancel()
		if r%64 == 63 {
			wg.Wait()
		}
	}
	wg.Wait()
}

func TestStubCancelSoakNoFireAfterFree(t *testing.T) {
	fx := newCancelFixture(t)
	runtime.GC() // settle finalizers of earlier tests before the baseline
	baseLive, err := fx.lib.LiveHandles()
	if err != nil {
		t.Fatal(err)
	}
	base := cancelProbe(t, fx.lib)
	cancelSoak(t, fx, 2000)
	got := cancelProbe(t, fx.lib)
	if got.FireAfterFree != base.FireAfterFree {
		t.Errorf("fire_after_free %d -> %d: a token was fired after it was freed", base.FireAfterFree, got.FireAfterFree)
	}
	if got.Inflight != base.Inflight {
		t.Errorf("inflight %d -> %d after the soak", base.Inflight, got.Inflight)
	}
	runtime.GC()
	live, err := fx.lib.LiveHandles()
	if err != nil {
		t.Fatal(err)
	}
	if live["chs_cancel"] != 0 || fmt.Sprint(live) != fmt.Sprint(baseLive) {
		t.Errorf("live handles after the soak = %v, want the baseline %v", live, baseLive)
	}
}

// TestStubCancelPlantedRedFreeWithoutDrain proves the soak's probe can see the
// defect it guards against: with the test-only switch that frees the token
// without waiting for a fire already in progress, fire_after_free goes above
// 0. The switch is restored before the test returns.
func TestStubCancelPlantedRedFreeWithoutDrain(t *testing.T) {
	fx := newCancelFixture(t)
	base := cancelProbe(t, fx.lib).FireAfterFree
	cancelSkipDrain = true
	defer func() { cancelSkipDrain = false }()
	for i := 0; i < 20 && cancelProbe(t, fx.lib).FireAfterFree == base; i++ {
		cancelSoak(t, fx, 1000)
	}
	cancelSkipDrain = false
	// Fires the switch left running land later still; read once more.
	if got := cancelProbe(t, fx.lib).FireAfterFree; got == base {
		t.Fatalf("planted red: freeing without the drain left fire_after_free at %d; the probe cannot see the defect", got)
	} else {
		t.Logf("planted red went red: fire_after_free %d -> %d", base, got)
	}
}

func TestCancelNilContextIsUsage(t *testing.T) {
	var ue *UsageError
	//nolint:staticcheck // a nil context is the misuse under test
	if _, err := armCancel(nil, nil, &optionDoc{}); !errors.As(err, &ue) {
		t.Errorf("a nil context = %v, want a *UsageError", err)
	}
	// A context that can never be done makes no token: armCancel touches
	// neither the table (nil here) nor the document.
	doc := optionDoc{}
	if a, err := armCancel(context.Background(), nil, &doc); a != nil || err != nil || len(doc) != 0 {
		t.Errorf("context.Background = %v, %v, document %v; want nothing", a, err, doc)
	}
	var a *cancelArm
	sentinel := errors.New("x")
	if err := a.finish(sentinel); !errors.Is(err, sentinel) {
		t.Error("a nil arm's finish must return the error unchanged")
	}
}
