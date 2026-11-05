// conformance_lifecycle_test.go: the hand-written runner for the "lifecycle"
// and "concurrent" case kinds (spec/abi-v2/schema/cases.schema.json). The
// generated invoke() mints a fresh handle for every handle argument, which is
// exactly what these two kinds must not do (a `ref` names a handle an earlier
// step bound; a concurrent case mints each handle once and shares it), so
// this runner resolves handle arguments itself and calls the generated
// *Table call wrappers directly. It holds no symbol lookup and no table.
package abi2

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// cancelIDKey is where a `cancel` step records a token's id in the case's env,
// beside the token itself (a name no `let` can bind: it holds a space).
func cancelIDKey(name string) string { return "id " + name }

var cancelIDShape = regexp.MustCompile(`^c[0-9a-f]{16}$`)

// cancelOptions is the options document a `cancel_options` argument names:
// {"cancel":"<id>"}, the id of the token bound to name.
func cancelOptions(env map[string]interface{}, name string) ([]byte, error) {
	id, ok := env[cancelIDKey(name)].(string)
	if !ok {
		return nil, fmt.Errorf("cancel_options %q: no token is bound to that name", name)
	}
	return []byte(`{"cancel":"` + id + `"}`), nil
}

// substituteCancelEcho replaces each {"cancel_echo": name} in an expectation
// with the echo the stub gives of the cancel_options bytes for that name:
// {head_hex, len, sha256}, computed here because the id is minted at run time.
func substituteCancelEcho(env map[string]interface{}, v interface{}) (interface{}, error) {
	switch x := v.(type) {
	case map[string]interface{}:
		if name, ok := x["cancel_echo"].(string); ok && len(x) == 1 {
			b, err := cancelOptions(env, name)
			if err != nil {
				return nil, err
			}
			sum := sha256.Sum256(b)
			head := b
			if len(head) > 16 {
				head = head[:16]
			}
			return map[string]interface{}{
				"head_hex": hex.EncodeToString(head),
				"len":      json.Number(strconv.Itoa(len(b))),
				"sha256":   hex.EncodeToString(sum[:]),
			}, nil
		}
		out := make(map[string]interface{}, len(x))
		for k, vv := range x {
			s, err := substituteCancelEcho(env, vv)
			if err != nil {
				return nil, err
			}
			out[k] = s
		}
		return out, nil
	case []interface{}:
		out := make([]interface{}, len(x))
		for i, vv := range x {
			s, err := substituteCancelEcho(env, vv)
			if err != nil {
				return nil, err
			}
			out[i] = s
		}
		return out, nil
	}
	return v, nil
}

// isNilHandle is true for an absent handle, including a typed nil pointer
// inside the interface (a canceled create returns a nil *Schema, not nil).
func isNilHandle(h interface{}) bool {
	if h == nil {
		return true
	}
	v := reflect.ValueOf(h)
	return v.Kind() == reflect.Pointer && v.IsNil()
}

// cancelInflight reads the stub's cancel probe: how many calls sit blocked in
// its directive now.
func cancelInflight(t *Table) (int64, error) {
	probe := hex.EncodeToString([]byte("!C:"))
	r, err := invoke(t, "chs_type_validate", []Arg{{BytesHex: &probe}})
	if err != nil {
		return 0, err
	}
	if r.Error != nil {
		return 0, r.Error
	}
	doc, ok := r.Outputs["out"].(map[string]interface{})
	if !ok {
		return 0, fmt.Errorf("the cancel probe answered no document")
	}
	n, ok := doc["inflight"].(json.Number)
	if !ok {
		return 0, fmt.Errorf("the cancel probe has no inflight count: %v", doc)
	}
	return n.Int64()
}

// rArg is a case argument including the `ref` and `cancel_options` forms
// invoke()'s own Arg lacks.
type rArg struct {
	Arg
	Ref           string `json:"ref,omitempty"`
	CancelOptions string `json:"cancel_options,omitempty"`
}

type callJSON struct {
	Fn   string `json:"fn"`
	Args []rArg `json:"args"`
}

type stepJSON struct {
	Let         string                 `json:"let,omitempty"`
	Call        *callJSON              `json:"call,omitempty"`
	Expect      map[string]interface{} `json:"expect,omitempty"`
	Free        string                 `json:"free,omitempty"`
	LiveDelta   map[string]int64       `json:"live_delta,omitempty"`
	Cancel      string                 `json:"cancel,omitempty"`
	Fire        string                 `json:"fire,omitempty"`
	Fired       string                 `json:"fired,omitempty"`
	Want        *int64                 `json:"want,omitempty"`
	BlockedCall *blockedCallJSON       `json:"blocked_call,omitempty"`
}

// blockedCallJSON is a `blocked_call` step: a call that blocks in the stub's
// directive until `fire` is fired from this goroutine.
type blockedCallJSON struct {
	Call   callJSON               `json:"call"`
	Fire   string                 `json:"fire"`
	Expect map[string]interface{} `json:"expect"`
}

func plainArgs(in []rArg) []Arg {
	out := make([]Arg, len(in))
	for i, a := range in {
		out[i] = a.Arg
	}
	return out
}

// resolved is one evaluated argument: a handle (possibly nil), or the literal.
type resolved struct {
	handle interface{}
	arg    Arg
}

type handleCloser interface{ Close() error }

// resolveArgs evaluates every argument once: a ref reads env, a
// cancel_options builds the options document naming the bound token's id, a
// nested handle is minted through the generated invoke (and recorded in owned
// so the caller can free it), anything else is a literal.
func resolveArgs(t *Table, env map[string]interface{}, args []rArg, owned *[]handleCloser) ([]resolved, error) {
	out := make([]resolved, len(args))
	for i, a := range args {
		switch {
		case a.CancelOptions != "":
			b, err := cancelOptions(env, a.CancelOptions)
			if err != nil {
				return nil, fmt.Errorf("arg %d: %w", i, err)
			}
			h := hex.EncodeToString(b)
			out[i] = resolved{arg: Arg{BytesHex: &h}}
		case a.Ref != "":
			h, ok := env[a.Ref]
			if !ok {
				return nil, fmt.Errorf("arg %d: ref %q is not bound", i, a.Ref)
			}
			out[i] = resolved{handle: h}
		case a.Handle != nil:
			r, err := invoke(t, a.Handle.Fn, a.Handle.Args)
			if err != nil {
				return nil, fmt.Errorf("arg %d: %w", i, err)
			}
			if r.Error != nil {
				return nil, fmt.Errorf("arg %d: minting via %s: %w", i, a.Handle.Fn, r.Error)
			}
			if c, ok := r.Handle.(handleCloser); ok {
				*owned = append(*owned, c)
			}
			out[i] = resolved{handle: r.Handle}
		default:
			out[i] = resolved{arg: a.Arg}
		}
	}
	return out, nil
}

func (r resolved) server() *Server { s, _ := r.handle.(*Server); return s }
func (r resolved) schema() *Schema { s, _ := r.handle.(*Schema); return s }
func (r resolved) filter() *Filter { f, _ := r.handle.(*Filter); return f }
func (r resolved) block() *Block   { b, _ := r.handle.(*Block); return b }
func (r resolved) i() int64 {
	if r.arg.Int != nil {
		return *r.arg.Int
	}
	return 0
}
func (r resolved) b() []byte {
	if r.arg.BytesHex == nil {
		return nil
	}
	b, _ := argBytes(r.arg)
	return b
}

func finishResult(t *Table, fn string, callErr *CallError, outs map[string]*Buf) (*InvokeResult, error) {
	res := &InvokeResult{Error: callErr, Outputs: map[string]interface{}{}}
	if callErr != nil {
		res.Status = callErr.Status
	} else {
		res.Status = statusName(0)
	}
	for name, b := range outs {
		if b == nil {
			continue
		}
		doc, err := bufJSON(t, b)
		if err != nil {
			return nil, fmt.Errorf("%s: %s: %w", fn, name, err)
		}
		res.Outputs[name] = doc
	}
	return res, nil
}

// callResolved makes one call through the generated wrappers with already
// resolved arguments, for the functions the lifecycle and concurrent cases use.
func callResolved(t *Table, fn string, a []resolved) (*InvokeResult, error) {
	need := func(n int) error {
		if len(a) != n {
			return fmt.Errorf("%s: want %d args, got %d", fn, n, len(a))
		}
		return nil
	}
	switch fn {
	case "chs_server_create":
		if err := need(2); err != nil {
			return nil, err
		}
		h, ce := t.ServerCreate(a[0].b(), a[1].b())
		r, err := finishResult(t, fn, ce, nil)
		if r != nil {
			r.Handle = h
		}
		return r, err
	case "chs_schema_create":
		if err := need(4); err != nil {
			return nil, err
		}
		h, ce := t.SchemaCreate(a[0].server(), a[1].b(), a[2].b(), a[3].b())
		r, err := finishResult(t, fn, ce, nil)
		if r != nil {
			r.Handle = h
		}
		return r, err
	case "chs_schema_describe":
		if err := need(1); err != nil {
			return nil, err
		}
		o, ce := t.SchemaDescribe(a[0].schema())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	case "chs_preview_row":
		if err := need(6); err != nil {
			return nil, err
		}
		o, ce := t.PreviewRow(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	case "chs_preview_batch":
		if err := need(9); err != nil {
			return nil, err
		}
		o, x, ce := t.PreviewBatch(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].filter(), int32(a[6].i()), uint32(a[7].i()), a[8].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o, "out_export": x})
	case "chs_preview_insert":
		if err := need(8); err != nil {
			return nil, err
		}
		o, x, ce := t.PreviewInsert(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].filter(), int32(a[6].i()), a[7].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o, "out_rows": x})
	case "chs_filter_create":
		if err := need(5); err != nil {
			return nil, err
		}
		h, ce := t.FilterCreate(a[0].schema(), a[1].b(), a[2].b(), a[3].b(), a[4].b())
		r, err := finishResult(t, fn, ce, nil)
		if r != nil {
			r.Handle = h
		}
		return r, err
	case "chs_filter_eval_body":
		if err := need(6); err != nil {
			return nil, err
		}
		o, ce := t.FilterEvalBody(a[0].filter(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	case "chs_block_create":
		if err := need(6); err != nil {
			return nil, err
		}
		h, ce := t.BlockCreate(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].b())
		r, err := finishResult(t, fn, ce, nil)
		if r != nil {
			r.Handle = h
		}
		return r, err
	case "chs_filter_eval_block":
		if err := need(2); err != nil {
			return nil, err
		}
		o, ce := t.FilterEvalBlock(a[0].filter(), a[1].block())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	}
	return nil, fmt.Errorf("lifecycle/concurrent runner: no support for %s", fn)
}

// liveCounts reads chs_live_handles' document as kind -> count.
func liveCounts(t *Table) (map[string]int64, error) {
	r, err := invoke(t, "chs_live_handles", nil)
	if err != nil {
		return nil, err
	}
	if r.Error != nil {
		return nil, r.Error
	}
	doc, ok := r.Outputs["out"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("chs_live_handles: document is not an object")
	}
	counts := map[string]int64{}
	for k, v := range doc {
		n, ok := v.(interface{ Int64() (int64, error) })
		if !ok {
			return nil, fmt.Errorf("chs_live_handles: %s is not a number", k)
		}
		x, err := n.Int64()
		if err != nil {
			return nil, err
		}
		counts[k] = x
	}
	return counts, nil
}

// settledCounts reads the live counts once earlier cases' handles are gone.
// The generated invoke() mints nested handles for echo/status cases and leaves
// their release to the handles' finalizers, which run on the runtime's own
// schedule; a lifecycle or concurrent case reads a delta, so it first forces
// collection and waits for the counts to hold still across several polls.
func settledCounts(t *Table) (map[string]int64, error) {
	var last map[string]int64
	stable := 0
	for i := 0; i < 200 && stable < 5; i++ {
		runtime.GC()
		time.Sleep(10 * time.Millisecond)
		cur, err := liveCounts(t)
		if err != nil {
			return nil, err
		}
		if last != nil && fmt.Sprint(cur) == fmt.Sprint(last) {
			stable++
		} else {
			stable = 0
		}
		last = cur
	}
	return last, nil
}

func closeHandle(h interface{}) {
	if c, ok := h.(handleCloser); ok {
		_ = c.Close()
	}
}

func runLifecycleCase(t *Table, c caseJSON) (bool, string) {
	base, err := settledCounts(t)
	if err != nil {
		return false, err.Error()
	}
	env := map[string]interface{}{}
	defer func() {
		for _, h := range env {
			closeHandle(h)
		}
	}()
	for i, st := range c.Steps {
		switch {
		case st.Cancel != "":
			tok, id, ce := t.CancelCreate()
			if ce != nil || tok == nil || id == nil {
				return false, fmt.Sprintf("step %d: chs_cancel_create: %v", i, ce)
			}
			idText := string(copyBytes(t.BufData(id), int(t.BufLen(id))))
			id.Close()
			if !cancelIDShape.MatchString(idText) {
				closeHandle(tok)
				return false, fmt.Sprintf("step %d: chs_cancel_create's id %q is not c and 16 lowercase hex digits", i, idText)
			}
			env[st.Cancel] = tok
			env[cancelIDKey(st.Cancel)] = idText
		case st.Fire != "":
			tok, ok := env[st.Fire].(*Cancel)
			if !ok {
				return false, fmt.Sprintf("step %d: fire of unbound token %q", i, st.Fire)
			}
			t.CancelFire(tok)
		case st.Fired != "":
			tok, ok := env[st.Fired].(*Cancel)
			if !ok || st.Want == nil {
				return false, fmt.Sprintf("step %d: fired of unbound token %q, or no want", i, st.Fired)
			}
			if got := int64(t.CancelFired(tok)); got != *st.Want {
				return false, fmt.Sprintf("step %d: chs_cancel_fired = %d, want %d", i, got, *st.Want)
			}
		case st.BlockedCall != nil:
			if ok, detail := runBlockedCall(t, env, *st.BlockedCall); !ok {
				return false, fmt.Sprintf("step %d: %s", i, detail)
			}
		case st.Free != "":
			h, ok := env[st.Free]
			if !ok {
				return false, fmt.Sprintf("step %d: free of unbound %q", i, st.Free)
			}
			closeHandle(h)
		case st.LiveDelta != nil:
			now, err := liveCounts(t)
			if err != nil {
				return false, fmt.Sprintf("step %d: %v", i, err)
			}
			for kind, want := range st.LiveDelta {
				if got := now[kind] - base[kind]; got != want {
					return false, fmt.Sprintf("step %d: live %s changed by %d, want %d", i, kind, got, want)
				}
			}
		case st.Call != nil:
			var owned []handleCloser
			vals, err := resolveArgs(t, env, st.Call.Args, &owned)
			if err != nil {
				return false, fmt.Sprintf("step %d: %v", i, err)
			}
			res, err := callResolved(t, st.Call.Fn, vals)
			if err != nil {
				return false, fmt.Sprintf("step %d: %v", i, err)
			}
			for _, o := range owned {
				_ = o.Close()
			}
			if st.Expect != nil {
				x, err := substituteCancelEcho(env, st.Expect)
				if err != nil {
					return false, fmt.Sprintf("step %d: %v", i, err)
				}
				st.Expect = x.(map[string]interface{})
			}
			if st.Let != "" {
				if res.Error != nil || res.Handle == nil {
					return false, fmt.Sprintf("step %d: %s produced no handle (%v)", i, st.Call.Fn, res.Error)
				}
				env[st.Let] = res.Handle
			} else {
				closeHandle(res.Handle)
			}
			if st.Expect != nil {
				if p := matchResult(st.Expect, res); len(p) > 0 {
					return false, fmt.Sprintf("step %d: %s", i, strings.Join(p, "; "))
				}
			}
		default:
			return false, fmt.Sprintf("step %d: unrecognized step", i)
		}
	}
	return true, ""
}

// runBlockedCall starts the call on another goroutine (its first bytes_in
// begins the stub's block directive), waits until the stub's cancel probe
// reads one more call in flight than before, fires the token from here, then
// joins the call and checks it. Sequenced on the probe, never on a sleep: the
// poll below only bounds how long a broken stub can hang the case.
func runBlockedCall(t *Table, env map[string]interface{}, b blockedCallJSON) (bool, string) {
	tok, ok := env[b.Fire].(*Cancel)
	if !ok {
		return false, fmt.Sprintf("blocked_call: fire of unbound token %q", b.Fire)
	}
	before, err := cancelInflight(t)
	if err != nil {
		return false, err.Error()
	}
	var owned []handleCloser
	vals, err := resolveArgs(t, env, b.Call.Args, &owned)
	if err != nil {
		return false, err.Error()
	}
	defer func() {
		for _, o := range owned {
			_ = o.Close()
		}
	}()
	type result struct {
		res *InvokeResult
		err error
	}
	done := make(chan result, 1)
	go func() {
		r, e := callResolved(t, b.Call.Fn, vals)
		done <- result{r, e}
	}()
	entered := false
	for i := 0; i < 5000 && !entered; i++ {
		n, err := cancelInflight(t)
		if err != nil {
			t.CancelFire(tok)
			<-done
			return false, err.Error()
		}
		if n > before {
			entered = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	t.CancelFire(tok)
	r := <-done
	if !entered {
		return false, "blocked_call: the call never reached the stub's block directive"
	}
	if r.err != nil {
		return false, r.err.Error()
	}
	defer closeHandle(r.res.Handle)
	x, err := substituteCancelEcho(env, b.Expect)
	if err != nil {
		return false, err.Error()
	}
	if p := matchResult(x.(map[string]interface{}), r.res); len(p) > 0 {
		return false, strings.Join(p, "; ")
	}
	return true, ""
}

func runConcurrentCase(t *Table, c caseJSON) (bool, string) {
	base, err := settledCounts(t)
	if err != nil {
		return false, err.Error()
	}
	var owned []handleCloser
	vals, err := resolveArgs(t, nil, c.Args, &owned)
	if err != nil {
		for _, o := range owned {
			_ = o.Close()
		}
		return false, err.Error()
	}
	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		problems []string
	)
	for th := 0; th < c.Threads; th++ {
		wg.Add(1)
		go func(th int) {
			defer wg.Done()
			for n := 0; n < c.Calls; n++ {
				res, err := callResolved(t, c.Fn, vals)
				var p []string
				if err != nil {
					p = []string{err.Error()}
				} else {
					closeHandle(res.Handle)
					p = matchResult(c.Expect, res)
				}
				if len(p) > 0 {
					mu.Lock()
					problems = append(problems, fmt.Sprintf("thread %d call %d: %s", th, n, strings.Join(p, "; ")))
					mu.Unlock()
					return
				}
			}
		}(th)
	}
	wg.Wait()
	for _, o := range owned {
		_ = o.Close()
	}
	if len(problems) > 0 {
		return false, strings.Join(problems, " | ")
	}
	after, err := settledCounts(t)
	if err != nil {
		return false, err.Error()
	}
	for k, v := range base {
		if after[k] != v {
			return false, fmt.Sprintf("live %s = %d after the case, %d before", k, after[k], v)
		}
	}
	return true, ""
}
