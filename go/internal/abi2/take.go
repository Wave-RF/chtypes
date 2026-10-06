// take.go: the one way hand-written code reads a generated call's chs_buf.
// It is plain Go over the generated BufData and BufLen (never a symbol of its
// own), so check-no-hand-decls has nothing to object to.
package abi2

import "unsafe"

// Take copies a returned chs_buf's bytes and frees the buffer: the generated
// call wrappers hand back the live buffer, and no public caller ever holds
// one past this call. A nil buffer (an absent optional output) is nil bytes;
// a present, empty buffer is a non-nil empty slice, so "emitted empty" stays
// distinguishable from "absent".
func (t *Table) Take(b *Buf) []byte {
	if b == nil {
		return nil
	}
	out := copyBytes(t.BufData(b), int(t.BufLen(b)))
	b.Close()
	return out
}

// TakeInto is Take with the destination chosen by the caller: it copies the
// buffer's bytes into dst[:0] (growing it only when it is too small), frees the
// buffer and returns the copy. The result is Go memory and never aliases the
// library's: it is for a document the caller consumes at once and does not
// hand on, where a reused dst saves the allocation Take would make. A nil
// buffer returns nil, as Take does; a present, empty one returns dst[:0] (nil
// only when dst is).
func (t *Table) TakeInto(b *Buf, dst []byte) []byte {
	if b == nil {
		return nil
	}
	n := int(t.BufLen(b))
	if cap(dst) < n {
		dst = make([]byte, n)
	}
	dst = dst[:n]
	if n > 0 {
		copy(dst, unsafe.Slice((*byte)(t.BufData(b)), n))
	}
	b.Close()
	return dst
}
