// conformance_lifecycle_test.go: the hand-written runner for the "lifecycle"
// and "concurrent" case kinds (spec/abi-v2/schema/cases.schema.json). The
// generated invoke() mints a fresh handle for every handle argument, which is
// exactly what these two kinds must not do (a `ref` names a handle an earlier
// step bound; a concurrent case mints each handle once and shares it), so
// this runner resolves handle arguments itself and calls the generated
// *Table call wrappers directly. It holds no symbol lookup and no table.
package abi2

import (
	"fmt"
	"runtime"
	"strings"
	"sync"
	"time"
)

// rArg is a case argument including the `ref` form invoke()'s own Arg lacks.
type rArg struct {
	Arg
	Ref string `json:"ref,omitempty"`
}

type callJSON struct {
	Fn   string `json:"fn"`
	Args []rArg `json:"args"`
}

type stepJSON struct {
	Let       string                 `json:"let,omitempty"`
	Call      *callJSON              `json:"call,omitempty"`
	Expect    map[string]interface{} `json:"expect,omitempty"`
	Free      string                 `json:"free,omitempty"`
	LiveDelta map[string]int64       `json:"live_delta,omitempty"`
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

// resolveArgs evaluates every argument once: a ref reads env, a nested handle
// is minted through the generated invoke (and recorded in owned so the caller
// can free it), anything else is a literal.
func resolveArgs(t *Table, env map[string]interface{}, args []rArg, owned *[]handleCloser) ([]resolved, error) {
	out := make([]resolved, len(args))
	for i, a := range args {
		switch {
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
	case "chs_schema_create":
		if err := need(2); err != nil {
			return nil, err
		}
		h, ce := t.SchemaCreate(a[0].b(), a[1].b())
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
		if err := need(5); err != nil {
			return nil, err
		}
		o, ce := t.PreviewRow(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	case "chs_preview_batch":
		if err := need(8); err != nil {
			return nil, err
		}
		o, x, ce := t.PreviewBatch(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b(), a[5].filter(), int32(a[6].i()), uint32(a[7].i()))
		return finishResult(t, fn, ce, map[string]*Buf{"out": o, "out_export": x})
	case "chs_filter_create":
		if err := need(4); err != nil {
			return nil, err
		}
		h, ce := t.FilterCreate(a[0].schema(), a[1].b(), a[2].b(), a[3].b())
		r, err := finishResult(t, fn, ce, nil)
		if r != nil {
			r.Handle = h
		}
		return r, err
	case "chs_filter_eval_body":
		if err := need(4); err != nil {
			return nil, err
		}
		o, ce := t.FilterEvalBody(a[0].filter(), int32(a[1].i()), a[2].b(), a[3].b())
		return finishResult(t, fn, ce, map[string]*Buf{"out": o})
	case "chs_block_create":
		if err := need(5); err != nil {
			return nil, err
		}
		h, ce := t.BlockCreate(a[0].schema(), int32(a[1].i()), a[2].b(), a[3].b(), a[4].b())
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
