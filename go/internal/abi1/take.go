// take.go: the one way hand-written code reads a generated call's chs_buf.
// It is plain Go over the generated BufData and BufLen (never a symbol of its
// own), so check-no-hand-decls has nothing to object to.
package abi1

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
