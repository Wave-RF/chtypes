package chtypes

// cancel.go — the context variants' cancel plumbing (spec/abi-v2/docs.md,
// chs_cancel). The public source of a cancellation is a context.Context; the
// C token (chs_cancel) is per call and per image, and never leaves this file.
//
// With a context whose Done is nil (context.Background, context.TODO) nothing
// changes: no token is created, no goroutine is started and no extra ABI call
// is made, so RowContext(context.Background(), ...) is Row(...). Otherwise one
// call is:
//
//  1. create the token (chs_cancel_create);
//  2. arrange that the context's cancellation fires it (context.AfterFunc),
//     or fire it at once when the context is already done, so the library
//     answers CHS_CANCELED at entry and reads no byte of the body;
//  3. put {"cancel": id} into the call's options document, merged with the
//     members the call already sets;
//  4. make the call;
//  5. stop the arrangement, and when stop reports the fire already running,
//     wait for it to finish;
//  6. free the token.
//
// Step 5 is what makes fire-before-free hold: the token is never fired after
// it is freed. CHS_CANCELED maps to *CanceledError with Cause set to
// context.Cause(ctx), so errors.Is(err, context.Canceled) and
// errors.Is(err, context.DeadlineExceeded) hold. Any other answer is the
// normal answer, even when the context was canceled after the call's last
// check point: a complete answer is never turned into a cancellation.

import (
	"context"
	"errors"
	"runtime"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

// cancelSkipDrain is a test-only switch: true frees the token without waiting
// for a fire already in progress, the planted defect the soak test proves its
// fire-after-free probe can see. It is never set outside a test.
var cancelSkipDrain bool

// cancelArm is one call's token and the arrangement that fires it.
type cancelArm struct {
	tbl   *abi2.Table
	ctx   context.Context
	tok   *abi2.Cancel
	stop  func() bool   // nil when the context was already done at arm
	fired chan struct{} // closed once the arranged fire returned
}

// armCancel prepares one call's cancellation from ctx and sets the cancel
// member of doc. It returns nil (and touches nothing) for a context that can
// never be canceled. A nil context is a *UsageError.
func armCancel(ctx context.Context, tbl *abi2.Table, doc *optionDoc) (*cancelArm, error) {
	if ctx == nil {
		return nil, usageError("the context is nil")
	}
	if ctx.Done() == nil {
		return nil, nil
	}
	tok, idBuf, cerr := tbl.CancelCreate()
	if cerr != nil {
		tbl.Take(idBuf)
		_ = tok.Close()
		return nil, callError(cerr)
	}
	id := string(tbl.Take(idBuf))
	a := &cancelArm{tbl: tbl, ctx: ctx, tok: tok}
	if ctx.Err() != nil {
		// Already done: fire now, on this goroutine, so the call sees a
		// pre-fired token whatever the scheduler does.
		tbl.CancelFire(tok)
	} else {
		a.fired = make(chan struct{})
		a.stop = context.AfterFunc(ctx, func() {
			tbl.CancelFire(tok)
			close(a.fired)
		})
	}
	doc.set("cancel", id)
	return a, nil
}

// finish stops the arrangement, waits for a fire already in progress, frees
// the token and maps err: a *CanceledError gains the context's cause. It is
// nil-receiver-safe and returns err unchanged then.
func (a *cancelArm) finish(err error) error {
	if a == nil {
		return err
	}
	if a.stop != nil && !a.stop() && !cancelSkipDrain {
		<-a.fired // the fire started before stop: it must return before the free
	}
	// The free is the generated wrapper's, not Cancel.Close, so the handle's
	// pointer is not rewritten under a fire that (only with the test switch)
	// may still be running; the finalizer is dropped so nothing frees it twice.
	runtime.SetFinalizer(a.tok, nil)
	a.tbl.CancelFree(a.tok)
	var ce *CanceledError
	if errors.As(err, &ce) {
		ce.Cause = context.Cause(a.ctx)
	}
	return err
}
