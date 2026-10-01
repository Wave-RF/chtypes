// finalizer_test.go — the GC backstop for LoadedSchema, LoadedFilter and
// LoadedBlock objects nobody Close()'d (issues #302 and #375), and the
// explicit Close paths it must not disturb.
//
// Every assertion here is made through C frees, never through timing: the
// package reports each chs_schema_free / chs_filter_free / chs_block_free it
// makes to go/internal/testhook.NativeFreed, with the handle it freed, and
// these tests count those reports. A test that waited on the Go object alone
// would pass for a schema whose object was collected while its C handle
// leaked.
package chtypes

import (
	"errors"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wave-rf/chtypes/go/internal/testhook"
)

// reclaimN is how many objects each GC test abandons. Large enough that a
// leak cannot hide inside one lucky collection, small enough to compile in
// well under a second.
const reclaimN = 100

// freeKey is one native handle as testhook.NativeFreed reports it.
type freeKey struct {
	kind   testhook.NativeKind
	handle uintptr
}

// freeLog records every native free the package reports while a test runs,
// each with its position in one global sequence, so ordering (children before
// their schema) is checkable as well as counts.
type freeLog struct {
	mu  sync.Mutex
	seq int
	at  map[freeKey][]int
}

// observeFrees installs a free observer for the rest of t.
func observeFrees(t *testing.T) *freeLog {
	t.Helper()
	l := &freeLog{at: map[freeKey][]int{}}
	fn := func(kind testhook.NativeKind, h uintptr) {
		l.mu.Lock()
		l.seq++
		k := freeKey{kind, h}
		l.at[k] = append(l.at[k], l.seq)
		l.mu.Unlock()
	}
	if !testhook.NativeFreed.CompareAndSwap(nil, &fn) {
		t.Fatal("another free observer is already installed")
	}
	t.Cleanup(func() { testhook.NativeFreed.Store(nil) })
	return l
}

// now is the current position in the sequence: a free recorded at or before
// it happened before whatever the caller does next.
func (l *freeLog) now() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.seq
}

// freesAfter answers how many times (kind, h) was freed after position born,
// and the position of the first such free (0 when none). Frees at or before
// born belong to an earlier allocation at the same address, never to the
// handle the caller is asking about: two live handles cannot share one.
func (l *freeLog) freesAfter(kind testhook.NativeKind, h uintptr, born int) (n, first int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, at := range l.at[freeKey{kind, h}] {
		if at > born {
			if n == 0 {
				first = at
			}
			n++
		}
	}
	return n, first
}

// family is one schema's native handles, with its children's when it has
// them (0 when it does not), and the sequence position before which none of
// them existed.
type family struct {
	schema, filter, block uintptr
	born                  int
}

// gcLibrary is the library every test here builds on: the first line the
// test registry holds, which must carry the filter and block groups.
func gcLibrary(t *testing.T) *Library {
	t.Helper()
	libs := testRegistry(t).Libraries()
	if len(libs) == 0 {
		t.Fatal("testRegistry opened no libraries")
	}
	return libs[0]
}

// openFamily compiles one schema and, when children is true, an open filter
// and an open block over it, recording their native handles.
func openFamily(t *testing.T, lib *Library, log *freeLog, children bool) (*LoadedSchema, *LoadedFilter, *LoadedBlock, family) {
	t.Helper()
	fam := family{born: log.now()}
	s, err := lib.CompileDDL("x UInt8, y String")
	if err != nil {
		t.Fatalf("%s: CompileDDL: %v", lib.Version, err)
	}
	fam.schema = schemaHandleOf(s)
	if !children {
		return s, nil, nil, fam
	}
	f, err := s.CompileFilter("x > 3")
	if err != nil {
		var u *UnsupportedError
		if errors.As(err, &u) {
			t.Skipf("%s: %v", lib.Version, err)
		}
		t.Fatalf("%s: CompileFilter: %v", lib.Version, err)
	}
	b, err := s.ParseBlock(JSONEachRow, []byte(`{"x":4,"y":"a"}`+"\n"+`{"x":1,"y":"b"}`+"\n"), nil)
	if err != nil {
		var u *UnsupportedError
		if errors.As(err, &u) {
			t.Skipf("%s: %v", lib.Version, err)
		}
		t.Fatalf("%s: ParseBlock: %v", lib.Version, err)
	}
	fam.filter, fam.block = filterHandleOf(f), blockHandleOf(b)
	return s, f, b, fam
}

// abandonFamilies opens n families and drops every reference to them before
// returning, so nothing but the GC can free them.
func abandonFamilies(t *testing.T, lib *Library, log *freeLog, n int, children bool) []family {
	t.Helper()
	fams := make([]family, 0, n)
	for i := 0; i < n; i++ {
		_, _, _, fam := openFamily(t, lib, log, children)
		fams = append(fams, fam)
	}
	return fams
}

// reclaimed is what a family's frees say about it: each handle's free count
// and whether every child went before the schema.
type reclaimed struct {
	schemas, filters, blocks int // handles freed at least once
	extra                    int // frees beyond the first, any kind
	outOfOrder               int // a child freed after its schema, or never while the schema was
}

func (l *freeLog) tally(fams []family) reclaimed {
	var r reclaimed
	for _, fam := range fams {
		sn, sAt := l.freesAfter(testhook.FreedSchema, fam.schema, fam.born)
		if sn > 0 {
			r.schemas++
			r.extra += sn - 1
		}
		for _, c := range []struct {
			kind  testhook.NativeKind
			h     uintptr
			count *int
		}{{testhook.FreedFilter, fam.filter, &r.filters}, {testhook.FreedBlock, fam.block, &r.blocks}} {
			if c.h == 0 {
				continue
			}
			n, at := l.freesAfter(c.kind, c.h, fam.born)
			if n > 0 {
				*c.count++
				r.extra += n - 1
			}
			if sn > 0 && (n == 0 || at > sAt) {
				r.outOfOrder++
			}
		}
	}
	return r
}

// awaitReclaim runs the GC until every family is freed or the bound runs out,
// and answers the final tally. The bound is a retry count, not an assertion:
// the verdict is the tally.
func awaitReclaim(log *freeLog, fams []family, children bool) reclaimed {
	var r reclaimed
	for i := 0; i < 200; i++ {
		r = log.tally(fams)
		if r.schemas == len(fams) && (!children || (r.filters == len(fams) && r.blocks == len(fams))) {
			return r
		}
		runtime.GC()
		time.Sleep(5 * time.Millisecond)
	}
	return log.tally(fams)
}

// TestAbandonedSchemaIsReclaimed: a schema nobody ever Close()'d, with nothing
// open on it, has its C handle freed by the GC backstop (issue #302).
func TestAbandonedSchemaIsReclaimed(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	fams := abandonFamilies(t, lib, log, reclaimN, false)
	r := awaitReclaim(log, fams, false)
	t.Logf("%s: %d abandoned schemas, %d freed", lib.Version, reclaimN, r.schemas)
	if r.schemas != reclaimN || r.extra != 0 {
		t.Fatalf("%d of %d abandoned schemas freed (%d extra frees); want every one exactly once", r.schemas, reclaimN, r.extra)
	}
}

// TestAbandonedSchemaWithOpenFilterAndBlockIsReclaimed is issue #375: a schema
// abandoned with an open filter AND an open block used to leak all three C
// handles for the life of the process, because each child pointed back at
// the schema, the schema tracked its children, and every one of them carried
// a runtime.SetFinalizer — a cycle Go does not promise to collect. All three
// must now be freed, each exactly once, children before their schema.
func TestAbandonedSchemaWithOpenFilterAndBlockIsReclaimed(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	fams := abandonFamilies(t, lib, log, reclaimN, true)
	r := awaitReclaim(log, fams, true)
	t.Logf("%s: %d abandoned families; freed %d schemas, %d filters, %d blocks", lib.Version, reclaimN, r.schemas, r.filters, r.blocks)
	if r.schemas != reclaimN || r.filters != reclaimN || r.blocks != reclaimN {
		t.Fatalf("freed %d schemas, %d filters, %d blocks of %d abandoned families; want all of them", r.schemas, r.filters, r.blocks, reclaimN)
	}
	if r.extra != 0 {
		t.Fatalf("%d handles were freed more than once", r.extra)
	}
	if r.outOfOrder != 0 {
		t.Fatalf("%d children were freed after their schema", r.outOfOrder)
	}
}

// TestOpenChildKeepsItsSchemaAlive: dropping the schema while a filter and a
// block over it are still held must free nothing — the children keep the
// schema reachable, so they stay usable — and dropping the children too must
// free all three.
func TestOpenChildKeepsItsSchemaAlive(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	held := make([]*LoadedFilter, 0, reclaimN)
	blocks := make([]*LoadedBlock, 0, reclaimN)
	fams := make([]family, 0, reclaimN)
	for i := 0; i < reclaimN; i++ {
		_, f, b, fam := openFamily(t, lib, log, true)
		held, blocks, fams = append(held, f), append(blocks, b), append(fams, fam)
	}
	for i := 0; i < 20; i++ {
		runtime.GC()
		time.Sleep(2 * time.Millisecond)
	}
	if r := log.tally(fams); r.schemas+r.filters+r.blocks != 0 {
		t.Fatalf("with every child still held, the GC freed %d schemas, %d filters, %d blocks", r.schemas, r.filters, r.blocks)
	}
	for i, f := range held {
		res, err := f.Eval(blocks[i])
		if err != nil {
			t.Fatalf("Eval on a held filter whose schema was dropped: %v", err)
		}
		if len(res.Verdicts) != 2 || res.Verdicts[0] != VerdictTrue || res.Verdicts[1] != VerdictFalse {
			t.Fatalf("Eval verdicts = %v, want [true false]", res.Verdicts)
		}
	}
	clear(held)
	clear(blocks)
	r := awaitReclaim(log, fams, true)
	if r.schemas != reclaimN || r.filters != reclaimN || r.blocks != reclaimN || r.extra != 0 || r.outOfOrder != 0 {
		t.Fatalf("after dropping the children: %+v, want %d of each, no extra, none out of order", r, reclaimN)
	}
}

// requireFreedOnceInOrder asserts fam's three handles were freed exactly once
// each, children first.
func requireFreedOnceInOrder(t *testing.T, log *freeLog, fam family) {
	t.Helper()
	r := log.tally([]family{fam})
	if r.schemas != 1 || r.filters != 1 || r.blocks != 1 || r.extra != 0 || r.outOfOrder != 0 {
		t.Fatalf("frees = %+v, want one schema, one filter, one block, no extra, children first", r)
	}
}

// requireClosedErrors asserts every call on the three objects answers the
// closed error rather than reaching C.
func requireClosedErrors(t *testing.T, s *LoadedSchema, f *LoadedFilter, b *LoadedBlock) {
	t.Helper()
	if _, err := s.Rows(JSONEachRow, []byte(`{"x":1,"y":"a"}`), nil); err == nil || !strings.Contains(err.Error(), "schema is closed") {
		t.Fatalf("Rows on a closed schema: err = %v", err)
	}
	if _, err := f.Rows(JSONEachRow, []byte(`{"x":1,"y":"a"}`), nil); err == nil || !strings.Contains(err.Error(), "filter is closed") {
		t.Fatalf("Rows on a closed filter: err = %v", err)
	}
	if _, err := f.Eval(b); err == nil || !strings.Contains(err.Error(), "is closed") {
		t.Fatalf("Eval on a closed filter and block: err = %v", err)
	}
}

// TestCloseChildrenFirst: closing the filter and block, then the schema, frees
// each once, and repeating every Close frees nothing more.
func TestCloseChildrenFirst(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	s, f, b, fam := openFamily(t, lib, log, true)
	f.Close()
	b.Close()
	s.Close()
	requireFreedOnceInOrder(t, log, fam)
	f.Close()
	b.Close()
	s.Close()
	requireFreedOnceInOrder(t, log, fam)
	requireClosedErrors(t, s, f, b)
}

// TestCloseSchemaFirst: closing the schema with a filter and block still open
// frees them first, in the same call, and their own later Close is a no-op.
func TestCloseSchemaFirst(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	s, f, b, fam := openFamily(t, lib, log, true)
	s.Close()
	requireFreedOnceInOrder(t, log, fam)
	f.Close()
	b.Close()
	s.Close()
	requireFreedOnceInOrder(t, log, fam)
	requireClosedErrors(t, s, f, b)
}

// TestChildCloseAfterSchemaReleaseIsSafe: when a schema's native half is
// released before its children's — what the schema's GC cleanup does when it
// happens to run first — the children are freed by that release, first, and
// their own later Close (or their own cleanup) frees nothing and answers
// "closed" rather than touching a freed handle. A held child keeps its schema
// reachable, so the GC alone cannot produce this order with the child still
// in hand; calling the cleanup's own function is how the test gets there.
func TestChildCloseAfterSchemaReleaseIsSafe(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	s, f, b, fam := openFamily(t, lib, log, true)
	s.n.release() // exactly what the schema's runtime.AddCleanup runs
	requireFreedOnceInOrder(t, log, fam)
	f.n.release() // exactly what the filter's cleanup runs
	b.n.release() // and the block's
	f.Close()
	b.Close()
	s.Close()
	requireFreedOnceInOrder(t, log, fam)
	requireClosedErrors(t, s, f, b)
}

// TestConcurrentCloseFreesOnce races the three Close calls (and a call on the
// filter) against each other, many times over: whatever order they land in,
// each handle is freed once and the children go first. Meant for -race.
func TestConcurrentCloseFreesOnce(t *testing.T) {
	lib := gcLibrary(t)
	log := observeFrees(t)
	for i := 0; i < 50; i++ {
		s, f, b, fam := openFamily(t, lib, log, true)
		var wg sync.WaitGroup
		start := make(chan struct{})
		for _, fn := range []func(){
			s.Close, f.Close, b.Close, s.Close,
			s.n.release, f.n.release, b.n.release, // the three GC cleanups' own functions
			func() { _, _ = f.Eval(b) },
			func() { _, _ = s.Rows(JSONEachRow, []byte(`{"x":1,"y":"a"}`), nil) },
		} {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				fn()
			}()
		}
		close(start)
		wg.Wait()
		requireFreedOnceInOrder(t, log, fam)
	}
}

// The native handles, read under the lock that guards them.
func schemaHandleOf(s *LoadedSchema) uintptr {
	unlock := s.lock()
	defer unlock()
	return uintptr(s.n.handle)
}

func filterHandleOf(f *LoadedFilter) uintptr {
	unlock := f.schema.lock()
	defer unlock()
	return uintptr(f.n.handle)
}

func blockHandleOf(b *LoadedBlock) uintptr {
	unlock := b.schema.lock()
	defer unlock()
	return uintptr(b.n.handle)
}
