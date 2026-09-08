/* chtypes.h — C API over ClickHouse's own type machinery.
 *
 * Everything behind this header is ClickHouse's real C++ (DataTypeFactory,
 * ISerialization, ReadHelpers), so coercion is exact by construction rather
 * than reimplemented.
 *
 * Ownership: every `char *` returned is malloc'd by the library and must be
 * released with chs_free(). Handles are released with chs_schema_free().
 * The library is thread-safe for concurrent chs_row() calls on distinct
 * handles; a single handle must not be used from two threads at once.
 *
 * -------------------------------------------------------------------------
 * STABILITY, PRE-1.0. Nothing has published this library yet, so this ABI is
 * NOT additive-only: until the first publish, a deliberate consolidation
 * cycle MAY change any signature, delete any function, or renumber anything
 * that is not marked frozen below. Versioned duplicates (`_v2` names) are
 * explicitly NOT how this ABI evolves — the second shape replaces the first
 * and every artifact is relinked in the same cycle. What IS frozen, because
 * it is identity rather than versioning:
 *
 *   * the library base name `libchtypes` (.so / .dylib);
 *   * the `chs_` symbol prefix, and the rule that NOTHING else is exported;
 *   * the numeric values in `enum chs_format` — bindings pass integers.
 *
 * A loader therefore MUST pair an artifact with the header it was built
 * from: symbol PRESENCE proves a function exists, never that its signature
 * matches this file. (The presence probe stays because a Registry may load a
 * third-party-built artifact; see spec/c-abi.md §ABI identity.) After the
 * first publish this section is replaced by a compatibility policy and the
 * usual semantic-version rules begin.
 * -------------------------------------------------------------------------
 */
#ifndef CHTYPES_H
#define CHTYPES_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The C API is the only exported surface: the library is compiled with
 * -fvisibility=hidden and linked with an exported-symbols list, so none of
 * ClickHouse's C++ symbols escape into the host process. */
#define CHS_API __attribute__((visibility("default")))

/* Input formats, matching chtypes.Format in the Go API.
 *
 * The RowBinary family reads the value in ClickHouse's own storage encoding
 * (ISerialization::deserializeBinary — the exact reader BinaryRowInputFormat
 * uses), so every parse-time text guard is bypassed exactly as it is on a real
 * server (docs/type-coverage.md §11): one error code (33) for any framing
 * fault, all-or-nothing batches, input_format_allow_errors_* never applies.
 * CHS_ROW_BINARY_WITH_DEFAULTS adds the measured per-column marker byte
 * (any nonzero byte = compute the column's DEFAULT, read no value bytes).
 *
 * CHS_NATIVE is COLUMN-oriented, not row-oriented, and is the format every
 * ClickHouse client library sends on INSERT. Its wire contract, as
 * DB::NativeReader reads it at server_revision 0 — the revision
 * NativeInputFormat passes for `INSERT ... FORMAT Native`
 * (Impl/NativeFormat.cpp:20-25 on 24.8, :20-25 on 26.7 — the literal `0`
 * argument) — is, per BLOCK:
 *
 *     varuint  n_columns
 *     varuint  n_rows
 *     n_columns x {
 *         string   column name           (varuint length + bytes)
 *         string   column type name      (ditto; a type EXPRESSION, e.g.
 *                                         "Nullable(Decimal(18, 4))")
 *         bytes    the whole column, via ISerialization::
 *                  deserializeBinaryBulkWithMultipleStreams
 *     }
 *
 * and the body is a sequence of blocks terminated by end-of-input or by a
 * block declaring zero columns. There is NO per-column custom-serialization
 * flag byte at revision 0: NativeReader reads one only when
 * server_revision >= DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION
 * (NativeReader.cpp:182 on 24.8, :211 on 26.7), which the FORMAT path never
 * reaches. Blocks captured off a live TCP connection ARE revision-tagged and
 * are therefore NOT this format; see spec/c-abi.md.
 *
 * Because the stream carries names and types, this is a format where the
 * declared schema and the payload can DISAGREE — and ClickHouse's own
 * resolution of that disagreement is neither obvious nor uniformly an error
 * (input_format_native_allow_types_conversion defaults to TRUE on every
 * vendored tag 24.8-26.7, so a type mismatch is CAST, not refused). The
 * wrapper drives NativeReader itself so that resolution is the server's own,
 * never a re-implementation.
 *
 * CHS_BUFFERS is Native's column encoding under a different, self-describing
 * FRAME — and nothing else. Its wire contract, as DB::BuffersReader documents
 * it (src/Formats/BuffersReader.h:13-24, byte-identical on 26.5, 26.6 and
 * 26.7), is, per BLOCK:
 *
 *     uint64le  n_columns
 *     uint64le  n_rows
 *     n_columns x {
 *         uint64le  byte size of this column's serialized data
 *         bytes     the column, EXACTLY as the Native format writes it
 *     }
 *
 * The difference that matters is stated in that same comment: "The schema
 * (names, types, order) is taken from the header passed in the constructor;
 * the stream itself does not contain names or types." Native carries a name
 * and a type expression per column and can therefore reconcile a producer
 * against a consumer; Buffers carries neither. A producer/consumer schema
 * disagreement is consequently NOT detectable in band. BuffersReader.cpp:76-84
 * compares only the DECLARED byte size against what the declared type actually
 * consumed, so a disagreement of EQUAL width passes every check and lands as a
 * silently reinterpreted value — a UInt32 producer's 4294967295 stored as -1
 * by an Int32 consumer, a Float32 1.5 stored as 1069547520 by a UInt32 one.
 * A disagreement of different width is caught, as INCORRECT_DATA (117) when
 * the size accounting disagrees or CANNOT_READ_ALL_DATA (33) when the reader
 * runs off the end. This library reports what the server reports; it cannot
 * report more, because the bytes do not carry more.
 *
 * ARRIVAL: 26.5. `factory.registerInputFormat("Buffers")` first appears at
 * Impl/BuffersFormat.cpp:88 on 26.5 (:89 on 26.6 and 26.7), reached from
 * registerFormats.cpp:179 / :184 / :185; no tag at 25.10 or earlier contains
 * the string, the reader, or the register function at all. On those lines this
 * library answers 73 UNKNOWN_FORMAT, which is what the server answers. */
enum chs_format
{
    CHS_JSON_EACH_ROW = 0,
    CHS_CSV = 1,
    CHS_TSV = 2,
    CHS_VALUES = 3,
    CHS_JSON_COMPACT_EACH_ROW = 4,
    CHS_ROW_BINARY = 5,
    CHS_ROW_BINARY_WITH_DEFAULTS = 6,
    CHS_ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS = 7,
    CHS_NATIVE = 8,
    CHS_BUFFERS = 9
};

/* The ClickHouse release this library was vendored from, e.g. "25.8.28.1". */
CHS_API const char * chs_clickhouse_version(void);

/* ---------------------------------------------------------------- ABI identity
 * The revision of THIS HEADER's shape — every declaration below, taken
 * together. It is NOT the ClickHouse version (chs_clickhouse_version), NOT a
 * semantic version of the product, and NOT a feature bitmap.
 *
 * It exists because of the rule stated at the top of this file: symbol
 * PRESENCE proves a function exists, never that its signature matches. Before
 * this revision existed, a loader holding a header and an artifact built from
 * a different cycle had no way to discover the mismatch except by calling
 * through it, which is undefined behaviour. Now it can ask.
 *
 * THE RULE, and it is the whole rule:
 *
 *   * CHS_ABI_REVISION is the value a caller COMPILED against (this header).
 *   * chs_abi_revision() is the value the loaded artifact was BUILT from.
 *   * Equal            -> the declarations match; call freely.
 *   * Different        -> do not call anything. A binding MUST refuse to load
 *                         the artifact and say both numbers.
 *   * Symbol ABSENT    -> the artifact predates this probe. Report revision 0
 *                         and keep the pre-existing degradation rules
 *                         (spec/artifact.md §Loading step 5); absence is not a
 *                         claim of incompatibility, only of ignorance.
 *
 * It increments by exactly ONE per consolidation cycle that changes any
 * existing declaration — a changed signature, a deleted function, a renumbered
 * constant. Purely ADDITIVE cycles (a new function, nothing else touched) also
 * increment it, because a caller compiled against the newer header may call
 * the new function and an older artifact does not have it; the presence probe
 * still handles that case gracefully, so the bump is about honesty, not safety.
 *
 * Revision 1 is the first: the ABI as of the settings-shape cycle, 2026-08-25.
 * Revision 2 is the Buffers cycle, 2026-08-25: `CHS_BUFFERS = 9` joins
 * `enum chs_format`. Nothing existing was renumbered, no signature changed and
 * no function was deleted — this is the purely ADDITIVE case the rule above
 * covers explicitly, and it increments for the reason given there. Note what
 * that means in practice and do not soften it: a caller compiled against
 * revision 2 loading a revision 1 artifact takes the "Different" branch, not
 * the "Symbol ABSENT" one, so it MUST refuse the artifact. Degradation is for
 * ignorance (revision 0) alone; a revision that is merely OLDER is still a
 * refusal, and the cycle that bumps this number relinks every artifact in
 * the registry so that no such pairing survives it.
 *
 * Revision 3 is the export/filter cycle, 2026-08-31: chs_rows gains
 * export_format / doc_flags / out_bytes (see its comment), chs_bytes and the
 * CHS_EXPORT_NONE / CHS_DOC_* constants join the header, and the
 * chs_filter_compile / chs_filter_free / chs_filter_rows trio joins the
 * surface (25 exported functions). This is the SIGNATURE-CHANGE case: calling
 * either era's chs_rows through the other's declaration is undefined
 * behaviour, which is exactly what this gate refuses.
 *
 * Revision 4 is the filter phase-2 cycle, 2026-08-31 (same day, second
 * cycle): chs_filter_compile gains params_json ({name:Type} query
 * parameters, the server's own ReplaceQueryParameterVisitor substitution —
 * see its comment), and the block-parse twin joins the surface: chs_block,
 * chs_block_parse, chs_block_free, chs_filter_eval (28 exported functions).
 * Both cases at once — a changed signature and additions — so the gate
 * refuses either pairing of header and artifact across the boundary.
 *
 * There is no revision 0 artifact — 0 is reserved for "the symbol was absent".
 */
#define CHS_ABI_REVISION 4
CHS_API int chs_abi_revision(void);

/* -------------------------------------------------------------------------
 * STATELESSNESS. Every entry point below is a pure function of
 * (build version, schema text, row bytes, settings, clock instant). No
 * cross-call state exists: a chs_schema is derived data, and the process-wide
 * pieces (the global Context, the evaluation-guard pool) are caches that never
 * influence an answer. ClickHouse has no auto-increment/sequence DEFAULT — a
 * DEFAULT reaches only same-row columns and functions, and
 * tools/gen_function_flags.py re-proves per build that the admitted volatile
 * set is exactly the four clock reads (generateSerialID and
 * generateSnowflakeID, the closest things to a sequence, are refused by the
 * same flags that refuse rand). The clock is an explicit input
 * (chtypes_now_epoch_nanos), not hidden state.
 *
 * RESOURCE ENVELOPE. Evaluating a DEFAULT runs tenant-authored code in this
 * process, so every evaluation is bounded:
 *   chtypes_default_eval_memory_bytes   default 256 MiB (0 disables)
 *   chtypes_default_eval_wall_nanos     default 1 s     (0 disables)
 * set process-wide via chs_set_default_settings. A schema whose DEFAULT
 * exceeds a ceiling is REFUSED AT chs_schema_compile, naming the column and
 * the budget (CHS_CODE_UNSUPPORTED — a real server might accept it, so it is
 * never a fabricated rejection). A row-dependent DEFAULT (range(a)) is cheap
 * at admission and bounded again per row; a row that trips comes back
 * "unsupported". Memory is enforced mid-allocation (ClickHouse's own
 * MemoryTracker, thread-private, code 241 cleanly unwound) and covers
 * Allocator-routed memory — column buffers, arenas — on every platform; Linux
 * additionally sees plain operator-new through the mandatory new_delete
 * archive. The wall ceiling is detection after the fact; sleep() is refused
 * outright via function_sleep_max_microseconds_per_block=1.
 * ------------------------------------------------------------------------- */

/* One-time process setup. Call before anything else.
 *
 * `timezone`         server timezone for bare DateTime/DateTime64 columns.
 *                    Pass NULL for "UTC" (what a stock ClickHouse container
 *                    uses); without this the host's TZ leaks into results.
 * `unsafe_families`  comma-separated type families this build must refuse
 *                    instead of constructing, because they dereference a
 *                    global Context that does not exist here and would
 *                    segfault the process. Generated by
 *                    tools/gen_unsafe_families.py — never hand-written.
 *                    Pass NULL to disable the guard.
 * `out_err`          optional (may be NULL): on failure receives ClickHouse's
 *                    own message, malloc'd — free with chs_free(). The one
 *                    reachable failure is an unknown `timezone`, and a bare
 *                    code cannot say which name was rejected.
 *
 * Returns 0 on success, ClickHouse's own error code on failure. */
CHS_API int chs_init(const char * timezone, const char * unsafe_families, char ** out_err);

/* Seed the settings every later call starts from, as a JSON object.
 *
 * ClickHouse gates several type families at column-creation time —
 * `LowCardinality(UInt32)` needs allow_suspicious_low_cardinality_types,
 * `Time`/`Time64` need allow_experimental_time_time64_type, and so on. Those
 * gates are properties of the *server the table lives on*, not of the row being
 * validated, so a gateway knows them once and should not have to repeat them
 * per row. Anything a per-call settings map sets still wins.
 *
 * Pass NULL or "{}" for ClickHouse's own defaults. `out_err` is optional (may
 * be NULL) and receives the server's own message on refusal — including its
 * did-you-mean hint, which names the setting and is the whole diagnostic value
 * of a 115; free it with chs_free(). Returns 0 on success.
 *
 * A setting name the version's server would refuse is refused HERE, wholesale:
 * the server's own name gate (builtin + obsolete + registered custom prefixes,
 * AccessControl::checkSettingNameIsAllowed) runs over the whole payload, and
 * on any unknown name the call returns the ClickHouse code (115) and commits
 * NOTHING. The registered prefixes start as the version's shipped config.xml
 * value ("SQL_" on every vendored era) and are re-declared with the
 * `chtypes_custom_settings_prefixes` key, comma-separated, mirroring the
 * server's own custom_settings_prefixes element (absent key = unchanged).
 * Per-call settings run the same gate: an unknown name rejects that call with
 * code 115, exactly as a server rejects the whole query (spec/c-abi.md,
 * Settings rule 2). */
CHS_API int chs_set_default_settings(const char * settings_json, char ** out_err);

/* Newline-separated list of every type family in ClickHouse's runtime
 * registry. Used by the generator above; also the answer to "does this build
 * track upstream type families without a table to maintain?". */
CHS_API char * chs_registered_families(void);

/* TSV audit of every registered function's volatility flags, one per line:
 *   name \t deterministic \t deterministic_in_query \t server_constant
 *        \t stateful \t resolver_error_code
 * ClickHouse's own answers, off this build's own registry. Drives
 * tools/gen_function_flags.py, which regenerates the statelessness evidence
 * (build/function_flags.tsv) on every build instead of trusting a list.
 * Requires chs_init. */
CHS_API char * chs_function_flags(void);

/* Distinguished code for "this build refuses to answer" (see chs_init).
 * Never a real ClickHouse error code. */
#define CHS_CODE_UNSUPPORTED (-2)

/* Release a string returned by any chs_* entry point. */
CHS_API void chs_free(char * p);

/* Stop the background work the DEFAULT evaluator starts, and join its threads.
 *
 * Resolving an expression constructs Context's external loaders, whose
 * constructors start a periodic-reload thread on the global pool. Nothing joins
 * it until the global pool's own static destructor does, at which point it is
 * still looping and the process HANGS after printing everything.
 *
 * chs_init registers this with atexit(), so a normal process needs no call.
 * Call it explicitly when the library is dlclose()d, when the host controls its
 * own teardown order, or from a test that must not depend on atexit. Idempotent
 * and safe to call when chs_init was never reached. */
CHS_API void chs_shutdown(void);

/* Parse and canonicalise one type expression.
 * Returns 0 and sets *out_canonical on success; on failure returns the error
 * code (a ClickHouse code, or CHS_CODE_UNSUPPORTED) and sets *out_code /
 * *out_err. Every out param is optional (may be NULL) — the return value
 * carries the code, so a caller that wants only the verdict needs no slots. */
CHS_API int chs_validate_type(const char * type_expr, char ** out_canonical, int * out_code, char ** out_err);

/* A compiled schema: a ClickHouse column-declaration list, e.g.
 *   "a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()"
 * Parsed with ClickHouse's own ParserColumnDeclarationList, so DEFAULT
 * expressions are validated as real SQL. Freed with chs_schema_free(). */
typedef struct chs_schema chs_schema;

/* The compile MODE — how the profile handed to chs_schema_compile relates to
 * the settings this build compiles under. Numeric values are part of the ABI
 * (bindings pass an int), exactly like enum chs_format. */
enum chs_compile_mode
{
    CHS_COMPILE_DECLARED = 0
};

/* Compile a column-declaration list, optionally under a DECLARED settings
 * profile — the settings the deployment's server runs, fixed into the handle at
 * compile time exactly as a real CREATE TABLE fixes them into the table.
 *
 * `settings_json`  a JSON object of ClickHouse query settings (string values,
 *                  as everywhere on this ABI). NULL or "{}" means "compile
 *                  under this build's own compile base", which is the common
 *                  case and takes a code path that makes no Context copy at
 *                  all.
 * `mode`           CHS_COMPILE_DECLARED (0), the only mode this build
 *                  accepts: every
 *                  setting the profile names takes the caller's value; every
 *                  setting it does not name keeps the library's own compile
 *                  base (build defaults plus the derived permissive type-gate
 *                  list — see spec/c-abi.md §Compile-time settings). A partial
 *                  profile can therefore admit a schema the server might
 *                  refuse, but can never fabricate a rejection.
 *                  Any other value is refused loudly (-2), reserved for a
 *                  future COMPLETE-profile mode.
 *
 * The profile's setting NAMES run the server's own gate (the same one per-call
 * settings run): an unknown name fails the compile with the server's code 115
 * and its own message; a known name with an unparseable value fails with the
 * server's own code for that. The chtypes_* keys are per-call/process keys,
 * not ClickHouse settings, so they are refused here too (115).
 *
 * What the profile changes at compile, this cycle: `flatten_nested` — at 1
 * (the default) a Nested(a,b) column compiles to the flattened `n.a`/`n.b`
 * Array columns, exactly as before; at 0 it stays ONE column `n` of type
 * Nested(a,b) = Array(Tuple(a,b)), exactly as the server's CREATE does under
 * that setting (InterpreterCreateQuery gates ColumnsDescription::flattenNested
 * on it). Every downstream shape — column introspection, JSONEachRow name
 * lookup, positional arity, the RowBinary wire — follows the compiled shape.
 * CREATE-time type validation (checkAllTypesAreAllowedInTable) runs on the
 * column set AS THE FLATTEN DECISION SHAPES IT, as the server validates it.
 *
 * Per-call settings still govern row parsing, and only row parsing; a handle's
 * compile profile is immutable for the handle's life. The DEFAULT-evaluation
 * admission budgets remain process policy (chs_set_default_settings) and are
 * re-pinned after the profile, so a profile cannot lift the sleep() refusal.
 *
 * Returns a handle, or NULL on failure with *out_code / *out_err set (both
 * optional; free the message with chs_free). */
CHS_API chs_schema * chs_schema_compile(
    const char * columns_sql, const char * settings_json, int mode,
    int * out_code, char ** out_err);

CHS_API void chs_schema_free(chs_schema * s);

/* Declare the table's engine, so chs_rows can apply the engine's own
 * insert-time semantics — the single-block merge every INSERT runs under the
 * server's default `optimize_on_insert = 1` (MergeTreeDataWriter::mergeBlock):
 * CollapsingMergeTree refusing an invalid Sign with code 117 before anything
 * is stored, SummingMergeTree summing equal keys and dropping all-zero rows,
 * ReplacingMergeTree deduplicating within the block. `engine` is the SHOW
 * CREATE spelling ("CollapsingMergeTree(sign)"); `order_by` the sorting key
 * ("tuple()", "id", "(day, key)").
 *
 * Modelled: MergeTree, Replacing, Collapsing, VersionedCollapsing, Summing,
 * Aggregating — exactly mergeBlock's switch minus Graphite/Coalescing. Without
 * this call (or with "MergeTree") chs_rows behaves as if no engine were
 * declared.
 *
 * `merge_tree_settings_json`  the table's MergeTree-NAMESPACE settings — the
 *                             SETTINGS clause after the engine, which
 *                             DB::Settings cannot carry (allow_nullable_key,
 *                             allow_floating_point_partition_key, ... live
 *                             here). A JSON object with string values; NULL or
 *                             "{}" declares none. Names are validated by the
 *                             server's own MergeTreeSettings object, never a
 *                             hand list. A known name declared at a NON-default
 *                             value is refused (-2, naming it): no MergeTree
 *                             setting's behaviour is modelled by this build
 *                             yet, and silently ignoring a declared value would
 *                             mean the declared profile is not the profile. A
 *                             name declared AT its default is inert and
 *                             accepted — the server behaves identically with or
 *                             without it.
 *
 * Returns, and these are three DIFFERENT verdicts a caller must not conflate:
 *   0    accepted;
 *   -2   CHS_CODE_UNSUPPORTED — engine or sorting key not modelled, or a
 *        non-default MergeTree setting. "A real server might well accept
 *        this; I decline to guess";
 *   115  UNKNOWN_SETTING — the server's own REJECTION of a MergeTree setting
 *        name it does not have, with the server's own message. This is a real
 *        ClickHouse code and is not a decline;
 *   -1   a guarded exception (measured: a clock-reading TTL).
 * *out_err says why in every nonzero case; free it with chs_free. */
CHS_API int chs_schema_engine(chs_schema * s, const char * engine, const char * order_by,
                              const char * merge_tree_settings_json, char ** out_err);

/* Declare the table's rows TTL — the `TTL ...` clause after the engine, e.g.
 * "ts + INTERVAL 30 DAY". Parsed and validated by ClickHouse's own
 * TTLTableDescription::parse (the CREATE path), evaluated per batch by its own
 * TTLDeleteAlgorithm against the batch's one clock instant, with force=true —
 * OPTIMIZE FINAL's posture, which is also how the acceptance rig's ground
 * truth is captured since its determinism fix (the racy read-after-insert
 * truth was measured 0/1/1 across three identical runs; under FINAL it is 0
 * eight-of-eight). An expired row is reported NOT STORED, with a
 * `storage_transforms` entry (reason "ttl_expired") so a gateway can tell the
 * tenant rather than silently previewing a row the table will never hold.
 *
 * Column-level TTLs need no call: they are part of the declaration list and
 * are captured at chs_schema_compile. An expired column value resets to the
 * column's DEFAULT — TTLColumnAlgorithm with TTLTransform's own
 * build_default_expr — and is reported as "ttl_column_expired" with the
 * post-reset value.
 *
 * Refused (-2), never guessed: WHERE / GROUP BY TTLs, TO DISK/VOLUME moves,
 * RECOMPRESS, and any TTL expression the DEFAULT scanner refuses — including
 * clock reads (now() + INTERVAL): a merge evaluates those against the
 * server's clock at merge time, which no preview owns. TTL evaluation runs
 * under the same admission budgets as DEFAULT evaluation. */
CHS_API int chs_schema_ttl(chs_schema * s, const char * ttl_sql, char ** out_err);

CHS_API int chs_schema_column_count(const chs_schema * s);
/* Borrowed pointers, valid until chs_schema_free(). */
CHS_API const char * chs_schema_column_name(const chs_schema * s, int i);
CHS_API const char * chs_schema_column_type(const chs_schema * s, int i);          /* canonical */
CHS_API const char * chs_schema_column_default_kind(const chs_schema * s, int i);  /* "" | DEFAULT | MATERIALIZED | ALIAS | EPHEMERAL */
CHS_API const char * chs_schema_column_default_expr(const chs_schema * s, int i);  /* "" if none */
/* 1 when the DEFAULT expression is a plain literal this build can apply
 * without the expression interpreter; 0 when it needs FunctionFactory. */
CHS_API int chs_schema_column_default_is_literal(const chs_schema * s, int i);

/* Validate + coerce one row. `settings_json` is a JSON object of ClickHouse
 * format settings (may be NULL or "{}"). Returns a malloc'd JSON document:
 *
 * {"outcome":"accepted"|"rejected"|"accepted_poisoned",
 *  "code":0,"err":"",
 *  "computed":[{"name":"m","kind":"MATERIALIZED","stored":11}],
 *  "cols":[{"name":"x","type":"UInt8","base":"UInt8","nullable":false,
 *           "src":"input"|"default"|"default_substituted"|"absent"|"skipped",
 *           "input":"256",
 *           "stored":"0",          // ClickHouse's JSON text of the stored value
 *           "ref":"256",           // same input through a widened reference type
 *           "ref_type":"Int256",   // "" when no reference type applies
 *           "poison":false}]}
 *
 * `stored` is authoritative ClickHouse output. `ref` is the same bytes parsed
 * by a structurally identical type with widened leaves, which is how the Go
 * layer detects silent transformation without reimplementing any parsing.
 *
 * One additive field, absent unless true: `"ref_unclassified":true` marks a
 * column whose leaf is numeric/temporal by ClickHouse's own predicate but has
 * no entry in this build's reference ladder — the detectors must then treat a
 * visible change as LOSSY, never as `reformat`. The build asserts the ladder
 * covers every registered family (tools/gen_reference_ladder.py), so the
 * field never appears from an artifact whose build gate ran; it exists so a
 * new upstream family can only ever degrade toward over-reporting.
 *
 * ---------------------------------------------------------------------------
 * VOLATILE DEFAULTS. `"src":"default_substituted"` marks a column whose DEFAULT
 * is now() / now64(n) / today() / yesterday(), or an expression over one. This
 * library resolved it, from its OWN clock, ONCE FOR THE WHOLE BATCH.
 *
 * THE CALLER MUST SEND EVERY SUCH COLUMN AS AN EXPLICIT VALUE IN THE INSERT.
 * That is the mechanism, not a nicety: if the server evaluates the expression
 * instead, preview and stored differ always at now64 resolution (2-60 ms even
 * back to back) and sometimes at now() resolution, because ClickHouse reads the
 * clock once per BLOCK — a 200-row insert at max_insert_block_size=10 stamps 20
 * distinct now64(9) values. Emit `stored` verbatim: a tick count re-encoded as
 * a JSON float is a hard reject (code 27), not a coercion.
 *
 * Three keys, recognised on `settings_json` alongside ClickHouse's own, are the
 * caller's entire interface to clock skew:
 *
 *   chtypes_now_epoch_nanos       pin the batch instant outright
 *   chtypes_clock_offset_nanos    measured (server - client), added to the read
 *   chtypes_max_clock_skew_nanos  refuse to substitute past |offset|; 0 = none
 *
 * With none of them the tolerated skew is UNBOUNDED. Past the budget the row is
 * `unsupported`, which is the only safe direction: a substituted timestamp too
 * far in the PAST under a `TTL` is accepted, previewed as accepted, and then
 * SILENTLY DELETED at merge time, with no error at any point.
 *
 * A DEFAULT whose value is a property of the SERVER (hostName, version, uptime,
 * timezone, serverUUID, getMacro, getScalar — everything ClickHouse itself
 * marks isServerConstant), of the SESSION (currentUser, currentDatabase,
 * getSetting) or of INSERTION ORDER (blockNumber, rowNumberInAllBlocks) is
 * `unsupported`. Resolving those here would store the gateway's answer to a
 * question about the server.
 *
 * A DEFAULT that would BLOCK -- sleep(), sleepEachRow() -- is `unsupported`,
 * refused rather than awaited. The tenant supplies the DDL, so `DEFAULT
 * sleep(3)` is otherwise a denial of service in one line of schema (measured:
 * 2.18 s of real time per case, in this process). The bound is upstream's own
 * `function_sleep_max_microseconds_per_block`, set on the evaluation context,
 * so nothing is ever parked and there is nothing to cancel. It is reported as
 * `unsupported` and NEVER as a rejection: a real server would have run it, so
 * a rejection here would be an over-reject of our own making.
 *
 * NOT bounded, and stated rather than papered over: a DEFAULT that allocates
 * without bound. `DEFAULT range(400000000)` is 10.98 GB of RSS. See README
 * "Blocking DEFAULTs" for why the obvious setting does not fix it and why it
 * cannot be fixed at all on macOS.
 *
 * ---------------------------------------------------------------------------
 * POSITIONAL FORMATS (CSV, TSV, Values, JSONCompactEachRow) address the k-th
 * column of the block an INSERT WITHOUT a column list targets —
 * getSampleBlockNonMaterialized(): ordinary + DEFAULT, i.e. everything except
 * MATERIALIZED, ALIAS *and* EPHEMERAL. Those three occupy no field position
 * and are not counted in the expected arity. (An EPHEMERAL column is in
 * ColumnsDescription::getInsertable(), but it becomes addressable only via an
 * explicit `INSERT INTO t (id, e)` column list, which no format stream
 * carries — measured on live 24.8.14.39 / 25.8.28.1 / 26.7.3.19, 2026-08-17:
 * `id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1` reads CSV `7,5` as
 * id=7, d=5, and three fields is 117 "Expected end of line".) JSONEachRow is
 * name-addressed and is unaffected; a field NAMING a MATERIALIZED / ALIAS /
 * EPHEMERAL column there is an UNKNOWN field (skipped by default, 117 under
 * input_format_skip_unknown_fields=0), exactly as those servers answer.
 *
 * ---------------------------------------------------------------------------
 * `computed` carries MATERIALIZED values. They are NOT in `cols` — `SELECT *`
 * does not return them, so a preview that mixed them into the row would
 * disagree with what a subscriber sees. They are reported because they are
 * durable: an ALTER of the expression does not touch rows already written.
 *
 * ALIAS is deliberately never reported. It is computable by the same
 * machinery, and `ALTER ... MODIFY COLUMN a ALIAS <new expr>` RETROACTIVELY
 * changes what already-inserted rows read back as, so an ALIAS is a fact about
 * the schema at read time and not about the row. This library will not present
 * one as a stored value.
 *
 * EPHEMERAL has no value at all (Code 16 in both paths). Its effect is visible
 * only through the DEFAULT columns that reference it, which are computed. */
CHS_API char * chs_row(const chs_schema * s, int format, const char * raw, size_t raw_len, const char * settings_json);

/* A counted, library-owned byte buffer: the chs_rows export channel's
 * out-param. `data` is malloc'd by the library — free it with the SAME
 * library's chs_free — and `len` counts it; `data` is not NUL-terminated by
 * contract (the length is the contract). {NULL, 0} means "nothing emitted". */
typedef struct chs_bytes
{
    char * data;
    size_t len;
} chs_bytes;

/* export_format sentinel: no export requested; out_bytes may be NULL. */
#define CHS_EXPORT_NONE (-1)

/* doc_flags bits — which document GROUPS the per-row documents carry. The
 * verdict channel (batch and per-row outcome/code/err, rows_read,
 * rows_skipped, unsupported_settings, engine_rows, storage_transforms) is
 * ALWAYS emitted and is not a flag. CHS_DOC_ALL reproduces the revision-2
 * document byte-for-byte; 0 is "lean" (verdicts only). A bit outside
 * CHS_DOC_ALL is refused loudly (the whole call answers unsupported), so a
 * future flag can never silently mean nothing. spec/c-abi.md §Document flags
 * is the full contract, including the conservative retention rule that keeps
 * TRANSFORMS-without-VALUES documents SDK-derivable and the cost asymmetry
 * (VALUES without TRANSFORMS skips the reference second-parse — real compute
 * saved; TRANSFORMS without VALUES still computes it and saves only bytes). */
/* CHS_DOC_VALUES: cols[] (stored text + provenance) + unknown_fields.
 * CHS_DOC_TRANSFORMS: ref/ref_type + wire run and are emitted; without
 *   VALUES, cols[] keeps only entries a change detector could fire on.
 * CHS_DOC_DEFAULTS: computed[] and (without VALUES) the default_substituted
 *   entries. */
#define CHS_DOC_VALUES 0x1u
#define CHS_DOC_TRANSFORMS 0x2u
#define CHS_DOC_DEFAULTS 0x4u
#define CHS_DOC_ALL (CHS_DOC_VALUES | CHS_DOC_TRANSFORMS | CHS_DOC_DEFAULTS)

/* Validate + coerce a whole request BODY, which may hold many rows. This is
 * the shape WaveHouse actually ingests, and it is not "chs_row in a loop":
 * row separation is format-specific (a quoted CSV field may contain a
 * newline), and ClickHouse's input_format_allow_errors_num / _ratio decide
 * whether a bad row is skipped or aborts the batch.
 *
 * {"outcome":…, "code":0, "err":"", "rows_read":10, "rows_skipped":0,
 *  "rows":[ <one chs_row document per row the reader consumed, input order> ]}
 *
 * A row skipped under input_format_allow_errors_* keeps its place in `rows`
 * as {"outcome":"skipped","code":…,"err":…,"cols":[]} — the error is the one
 * IRowInputFormat::generate caught before resyncing (the server computes it,
 * then logs only a count), reported verbatim. The batch verdict, rows_read
 * and rows_skipped are exactly what they were when skips were silent.
 *
 * THE EXPORT CHANNEL (revision 3; docs/proposals/rows-export.md, normative
 * text in spec/c-abi.md §Rows). `export_format` is CHS_EXPORT_NONE or an
 * enum chs_format value this artifact can SERIALIZE — this revision exactly
 * CHS_JSON_COMPACT_EACH_ROW. With an export requested, out_bytes (required
 * non-NULL, always initialized to {NULL,0} at entry) receives the batch's
 * accepted rows serialized ONCE by the transcription of ClickHouse's own
 * JSONCompactEachRowRowOutputFormat (row = '[' fields ", "-joined "]\n",
 * values via serializeTextJSON under the call's resolved FormatSettings —
 * vendored writer, cited at the emission site), columns = the wire tuple
 * (declared order minus MATERIALIZED/ALIAS/EPHEMERAL), volatile DEFAULTs
 * already substituted. The document gains "row_spans": one {off,len} per
 * rows[] entry, index-aligned, len 0 for non-accepted rows; a span covers
 * the row's full line including its '\n', so slicing spans out of out_bytes
 * IS the per-row payload. Fail-closed, with the reason in the document's
 * "export_declined" and out_bytes left {NULL,0}: a batch whose verdict is
 * not exactly "accepted" (accepted_poisoned included — an unreadable value
 * cannot be honestly serialized), any accepted row whose stored block lost a
 * wire column (the full-arity guard — never emit a row with silently absent
 * columns), a serialization failure, output_format_json_validate_utf8. An
 * export_format this build cannot serialize, an unknown doc_flags bit, or a
 * NULL out_bytes with an export requested answer the WHOLE call
 * {"outcome":"unsupported","code":-2,…} and process nothing — loud, never
 * silent. With export_format = CHS_EXPORT_NONE and doc_flags = CHS_DOC_ALL
 * the document is byte-identical to revision 2's. */
CHS_API char * chs_rows(const chs_schema * s, int format, const char * body, size_t body_len,
                        const char * settings_json,
                        int export_format, unsigned doc_flags, chs_bytes * out_bytes);

/* ------------------------------------------------------------------ filters
 * Phase 2 (revision 4): one boolean SQL expression over a compiled schema's
 * columns, evaluated per row of a body — with {name:Type} query parameters,
 * and a parse-once/eval-many twin (chs_block_parse + chs_filter_eval,
 * below). spec/c-abi.md §Filters and §Blocks are the full contract; the
 * header states the load-bearing parts.
 *
 * chs_filter_compile parses expr_sql with ClickHouse's own ParserExpression
 * (server limits), substitutes query parameters, and compiles the result
 * with the SAME TreeRewriter + ExpressionAnalyzer pipeline the CONSTRAINT …
 * CHECK path runs, over the schema's PHYSICAL columns (ordinary +
 * MATERIALIZED) — so comparison semantics are WHERE-side by construction
 * (x = 256 over UInt8 promotes and is false for every row; it never wraps).
 *
 * PARAMETERS. `params_json` is a JSON object of parameter name -> value
 * STRING (the settings_json convention); NULL or "{}" declares none.
 * Substitution is the server's own ReplaceQueryParameterVisitor (vendored),
 * run over the parsed AST BEFORE analysis, exactly where executeQuery runs
 * it: each value string is deserialized by the DECLARED type's own
 * deserializeTextEscaped and injected as a typed literal — so a value is
 * never SQL text and injection safety is BY CONSTRUCTION, not by escaping.
 * An unbound {name:Type} is the server's own 456 UNKNOWN_QUERY_PARAMETER
 * ("Substitution `name` is not set"); an unparseable value is the server's
 * own 457 BAD_QUERY_PARAMETER (or the serialization's own code); a bound
 * name the expression never uses is ignored, as a live server ignores an
 * unused param_* (all three probed live on 25.8.28.1, 2026-08-31). The
 * revision-3 "-2, render literals" refusal is REPLACED by real
 * substitution. Handle identity is per-(schema, expr, params) — values are
 * baked in at compile; a caller compiling filters from tenant-influenced
 * values MUST bound its cache (LRU) and its compile rate per principal
 * (spec/c-abi.md §Filters, Query parameters).
 *
 * Refused (-2), never guessed: non-deterministic expressions (clock reads —
 * now() > ts —, rand(), server-constants, insertion-order functions; the
 * scanDefaultExpr scan the CHECK/TTL paths run, AFTER substitution — a
 * parameter value is a literal and can never smuggle one in). Real
 * expression errors return NULL with ClickHouse's own code/message in
 * *out_code / *out_err (chs_free).
 *
 * LIFETIME: a chs_filter REFERENCES its schema handle — no copy, no
 * refcount. Keep the schema alive for every filter compiled from it and free
 * filters BEFORE their schema; freeing the schema first is use-after-free.
 *
 * THREADS: the per-handle rule, twice — one chs_filter must not be used from
 * two threads at once, and a chs_filter call is ALSO a use of its schema
 * handle (two filters over one schema must not run concurrently either).
 *
 * chs_filter_rows returns a malloc'd JSON document (chs_free):
 *   {"outcome":"ok","code":0,"err":"","rows_read":N,
 *    "unsupported_settings":[],"verdicts":"tfde",
 *    "errors":[{"row":2,"code":386,"err":"…"}]}
 * One verdict character per row, input order: 't' truthy (non-NULL non-zero
 * — the CHECK path's own rule), 'f' false OR NULL (WHERE's "NULL is not
 * true"), 'e' the predicate THREW on this row's values (e.g. NO_COMMON_TYPE
 * 386 from "s = 257" over String — the server would fail the WHOLE query
 * here; a security-enforcing caller MUST fail closed), 'd' this library
 * declines (row unparseable under the schema, poisoned, or the admission
 * envelope tripped — fail closed here too). Rows are evaluated
 * independently (no INSERT to abort; allow_errors does not apply); a
 * call-level failure (settings 115, framing, binary decode fault) answers
 * outcome rejected/unsupported with "verdicts":"". Evaluation runs under the
 * DEFAULT-evaluation admission budgets; volatile DEFAULTs resolve against
 * one clock instant per call. NOTHING may enforce read-side security on this
 * API until the WHERE-truth rig gates green (spec/c-abi.md §Filters) — the
 * twin below is call-shape, not an enforcement opening. */
typedef struct chs_filter chs_filter;

CHS_API chs_filter * chs_filter_compile(
    const chs_schema * s, const char * expr_sql, const char * params_json,
    int * out_code, char ** out_err);

CHS_API void chs_filter_free(chs_filter * f);

CHS_API char * chs_filter_rows(
    const chs_filter * f, int format, const char * body, size_t body_len, const char * settings_json);

/* ------------------------------------------------------------------- blocks
 * The parse-once/eval-many twin (revision 4). chs_block_parse runs the SAME
 * machinery chs_filter_rows runs — body split, per-row parse + coercion, the
 * syncAfterError resync, MATERIALIZED filled (the CHECK path's pre-eval),
 * per-row parse failures recorded IN the block with code and message — once,
 * minus document emission. chs_filter_eval then evaluates a compiled filter
 * over the already-parsed block and returns the SAME document
 * chs_filter_rows returns (same fields, same 't'/'f'/'e'/'d' + errors[]; a
 * row recorded unparseable at parse answers 'd' with the recorded error).
 *
 * CONTRACT: chs_filter_eval(f, chs_block_parse(s, fmt, body, len, settings))
 * ≡ chs_filter_rows(f, fmt, body, len, settings) for every verdict class —
 * exactly, when the clock is pinned or no volatile DEFAULT exists (volatile
 * DEFAULTs resolve against the PARSE call's one clock instant). This is the
 * live-SSE call shape: parse an event once, evaluate K per-principal
 * filters against the block with no re-parse.
 *
 * A call-level failure (settings 115, framing, binary decode fault, the
 * deferred JSONEachRow suffix verdict) returns NULL with the code/message in
 * *out_code / *out_err (both optional; chs_free the message): a malformed
 * body yields no block and no partial answers. `settings_json` is the
 * PARSE-side map; evaluation takes none — it is a pure function of
 * (filter, block).
 *
 * LIFETIME: a chs_block REFERENCES its schema handle (same non-owning rule
 * as filters): keep the schema alive, free blocks BEFORE their schema. A
 * block may be evaluated by MANY filters, sequentially; evaluation does not
 * mutate it. Filter and block MUST come from the SAME schema handle —
 * chs_filter_eval on a mismatched pair answers a rejected document (1002),
 * loudly, never undefined behaviour.
 *
 * THREADS: a chs_filter_eval call is a use of BOTH handles — the filter's
 * rule applies (and a filter use is a use of its schema handle, as
 * everywhere), and one chs_block must not be used from two threads at once.
 * chs_block_parse is a use of the schema handle, like any parse. */
typedef struct chs_block chs_block;

CHS_API chs_block * chs_block_parse(
    const chs_schema * s, int format, const char * body, size_t body_len,
    const char * settings_json, int * out_code, char ** out_err);

CHS_API void chs_block_free(chs_block * b);

CHS_API char * chs_filter_eval(const chs_filter * f, const chs_block * b);

/* Diagnostics: the reference (widened) type this build would use for a type. */
CHS_API char * chs_reference_type(const char * type_expr);

#ifdef __cplusplus
}
#endif

#endif /* CHTYPES_H */
