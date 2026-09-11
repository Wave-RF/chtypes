//go:build chtypes_linked

// linked_abi_check.go — the dlopen-only build hardcodes the ABI's frozen
// numbers (docs/reference/c-abi.md §Frozen: enum chs_format, CHS_DOC_*,
// CHS_CODE_UNSUPPORTED, CHS_EXPORT_NONE, CHS_ABI_REVISION) exactly as the
// Python, TypeScript and Rust bindings do, because it never sees chtypes.h.
// This file, compiled only with the linked path, pins every one of those
// literals to the header: a constant expression that comes out negative
// cannot convert to uint, so each pair below fails to COMPILE the moment
// either side moves. That is how `just test` (which builds with the tag)
// keeps the consumer-facing build honest without the consumer ever needing
// the header.
package chtypes

/*
#include "chtypes.h"
*/
import "C"

const (
	// Every pair is checked in both directions; the conversions strip the Go
	// side's named type so the subtraction is a plain untyped constant.
	_ = uint(int(C.CHS_ABI_REVISION) - int(ABIRevision))
	_ = uint(int(ABIRevision) - int(C.CHS_ABI_REVISION))
	_ = uint(int(C.CHS_CODE_UNSUPPORTED) - int(CodeUnsupported))
	_ = uint(int(CodeUnsupported) - int(C.CHS_CODE_UNSUPPORTED))
	_ = uint(int(C.CHS_EXPORT_NONE) - int(ExportNone))
	_ = uint(int(ExportNone) - int(C.CHS_EXPORT_NONE))
	_ = uint(int(C.CHS_DOC_VALUES) - int(DocValues))
	_ = uint(int(DocValues) - int(C.CHS_DOC_VALUES))
	_ = uint(int(C.CHS_DOC_TRANSFORMS) - int(DocTransforms))
	_ = uint(int(DocTransforms) - int(C.CHS_DOC_TRANSFORMS))
	_ = uint(int(C.CHS_DOC_DEFAULTS) - int(DocDefaults))
	_ = uint(int(DocDefaults) - int(C.CHS_DOC_DEFAULTS))
	_ = uint(int(C.CHS_DOC_ALL) - int(DocAll))
	_ = uint(int(DocAll) - int(C.CHS_DOC_ALL))
	_ = uint(int(C.CHS_COMPILE_DECLARED) - int(CompileDeclared))
	_ = uint(int(CompileDeclared) - int(C.CHS_COMPILE_DECLARED))
	_ = uint(int(C.CHS_JSON_EACH_ROW) - int(JSONEachRow))
	_ = uint(int(JSONEachRow) - int(C.CHS_JSON_EACH_ROW))
	_ = uint(int(C.CHS_CSV) - int(CSV))
	_ = uint(int(CSV) - int(C.CHS_CSV))
	_ = uint(int(C.CHS_TSV) - int(TSV))
	_ = uint(int(TSV) - int(C.CHS_TSV))
	_ = uint(int(C.CHS_VALUES) - int(Values))
	_ = uint(int(Values) - int(C.CHS_VALUES))
	_ = uint(int(C.CHS_JSON_COMPACT_EACH_ROW) - int(JSONCompactEachRow))
	_ = uint(int(JSONCompactEachRow) - int(C.CHS_JSON_COMPACT_EACH_ROW))
	_ = uint(int(C.CHS_ROW_BINARY) - int(RowBinary))
	_ = uint(int(RowBinary) - int(C.CHS_ROW_BINARY))
	_ = uint(int(C.CHS_ROW_BINARY_WITH_DEFAULTS) - int(RowBinaryWithDefaults))
	_ = uint(int(RowBinaryWithDefaults) - int(C.CHS_ROW_BINARY_WITH_DEFAULTS))
	_ = uint(int(C.CHS_ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS) - int(RowBinaryWithNamesAndTypesAndDefaults))
	_ = uint(int(RowBinaryWithNamesAndTypesAndDefaults) - int(C.CHS_ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS))
	_ = uint(int(C.CHS_NATIVE) - int(Native))
	_ = uint(int(Native) - int(C.CHS_NATIVE))
	_ = uint(int(C.CHS_BUFFERS) - int(Buffers))
	_ = uint(int(Buffers) - int(C.CHS_BUFFERS))
)
