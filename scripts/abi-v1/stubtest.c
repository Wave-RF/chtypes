/* stubtest.c: the stub's own self-test, driving scripts/abi-v1/emit/stub.py's
 * generated handle-rule logic entirely THROUGH the real chs_* ABI, through
 * dlopen+dlsym, never by reaching into the stub's internals. That matters
 * for one check in particular: the SHA-256 the stub's echo reports is proven
 * correct by calling a real function with a known-plaintext argument and
 * checking the well-known digest appears in its output, rather than by
 * calling the stub's internal sha256 routine directly with a hand-picked
 * input — the same "drive it from the real computation, not a hand-set
 * value" rule that governs every other test in this tree.
 *
 * Hand-written (not generated): the acceptance criterion is "the D2
 * self-test passes", and this is that self-test.
 *
 *     scripts/abi-v1/stubtest <dir>
 *
 * `<dir>` must contain ok.so and ok-b.so (two separately dlopen'd, byte
 * identical builds of the stub, which is what proves cross-image handling:
 * each gets its own static image-identity marker). unbound.so, if present,
 * is checked for refusal under RTLD_NOW. Exits 0 and prints a summary line
 * on success; on any failure, prints each failing check to stderr and exits
 * 1 — never a zero-run: main() counts both passes and failures and refuses
 * to report success on zero checks.
 */
#include "chtypes.h"

#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_pass = 0, g_fail = 0;

#define CHECK(cond, ...)                                                                                             \
    do {                                                                                                              \
        if (cond) {                                                                                                   \
            g_pass++;                                                                                                 \
        } else {                                                                                                      \
            g_fail++;                                                                                                 \
            fprintf(stderr, "stubtest: FAIL %s:%d: ", __FILE__, __LINE__);                                            \
            fprintf(stderr, __VA_ARGS__);                                                                             \
            fprintf(stderr, "\n");                                                                                    \
        }                                                                                                              \
    } while (0)

/* ----------------------------------------------------------- symbol tables */

typedef int32_t (*fn_abi_version)(void);
typedef const char *(*fn_cstr_static)(void);
typedef int (*fn_abi_revision)(void);
typedef const uint8_t *(*fn_buf_data)(const chs_buf *);
typedef size_t (*fn_buf_len)(const chs_buf *);
typedef void (*fn_buf_free)(chs_buf *);
typedef chs_status (*fn_error_status)(const chs_error *);
typedef int32_t (*fn_error_ch_code)(const chs_error *);
typedef chs_buf *(*fn_error_text)(const chs_error *);
typedef void (*fn_error_free)(chs_error *);
typedef chs_status (*fn_live_handles)(chs_buf **, chs_error **);
typedef chs_status (*fn_quote_string)(const uint8_t *, size_t, chs_buf **, chs_error **);
#if CHS_ABI_VERSION >= 2
/* Generation 2: chs_schema_create takes a nullable server first and an options
   document after the settings. Every call below passes NULL and none, which is
   generation 1's behavior; test_server_profile covers the server itself. */
typedef chs_status (*fn_schema_create)(const chs_server *, const uint8_t *, size_t, const uint8_t *, size_t,
                                       const uint8_t *, size_t, chs_schema **, chs_error **);
typedef chs_status (*fn_server_create)(const uint8_t *, size_t, const uint8_t *, size_t, chs_server **, chs_error **);
typedef void (*fn_server_free)(chs_server *);
#define SCHEMA_CREATE(l, sql, n, settings, settings_n, out, err)                                                    \
    (l)->schema_create(NULL, (sql), (n), (settings), (settings_n), NULL, 0, (out), (err))
#else
typedef chs_status (*fn_schema_create)(const uint8_t *, size_t, const uint8_t *, size_t, chs_schema **, chs_error **);
#define SCHEMA_CREATE(l, sql, n, settings, settings_n, out, err)                                                    \
    (l)->schema_create((sql), (n), (settings), (settings_n), (out), (err))
#endif
typedef void (*fn_schema_free)(chs_schema *);
typedef chs_status (*fn_filter_create)(
    const chs_schema *, const uint8_t *, size_t, const uint8_t *, size_t, const uint8_t *, size_t, chs_filter **,
    chs_error **);
typedef void (*fn_filter_free)(chs_filter *);
typedef chs_status (*fn_filter_eval_body)(
    const chs_filter *, chs_format, const uint8_t *, size_t, const uint8_t *, size_t, chs_buf **, chs_error **);
typedef chs_status (*fn_preview_row)(
    const chs_schema *, chs_format, const uint8_t *, size_t, const uint8_t *, size_t, const uint8_t *, size_t,
    chs_buf **, chs_error **);

typedef struct {
    void *handle;
    const char *path;
    fn_abi_version abi_version;
    fn_cstr_static build_info;
    fn_cstr_static clickhouse_version;
    fn_abi_revision abi_revision;
    fn_buf_data buf_data;
    fn_buf_len buf_len;
    fn_buf_free buf_free;
    fn_error_status error_status;
    fn_error_ch_code error_ch_code;
    fn_error_text error_message;
    fn_error_free error_free;
    fn_live_handles live_handles;
    fn_quote_string quote_string;
    fn_schema_create schema_create;
    fn_schema_free schema_free;
    fn_filter_create filter_create;
    fn_filter_free filter_free;
    fn_filter_eval_body filter_eval_body;
    fn_preview_row preview_row;
#if CHS_ABI_VERSION >= 2
    fn_server_create server_create;
    fn_server_free server_free;
#endif
} lib_t;

#define SYM(l, field, name)                                                                                           \
    do {                                                                                                              \
        (l).field = (void *) dlsym((l).handle, name);                                                                 \
        if ((l).field == NULL) {                                                                                      \
            fprintf(stderr, "stubtest: %s: dlsym(%s) failed: %s\n", (l).path, name, dlerror());                       \
            exit(2);                                                                                                  \
        }                                                                                                              \
    } while (0)

static lib_t load_lib(const char *path) {
    lib_t l;
    memset(&l, 0, sizeof l);
    l.path = path;
    l.handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (l.handle == NULL) {
        fprintf(stderr, "stubtest: dlopen(%s) failed: %s\n", path, dlerror());
        exit(2);
    }
    SYM(l, abi_version, "chs_abi_version");
    SYM(l, build_info, "chs_build_info");
    SYM(l, clickhouse_version, "chs_clickhouse_version");
    SYM(l, abi_revision, "chs_abi_revision");
    SYM(l, buf_data, "chs_buf_data");
    SYM(l, buf_len, "chs_buf_len");
    SYM(l, buf_free, "chs_buf_free");
    SYM(l, error_status, "chs_error_status");
    SYM(l, error_ch_code, "chs_error_ch_code");
    SYM(l, error_message, "chs_error_message");
    SYM(l, error_free, "chs_error_free");
    SYM(l, live_handles, "chs_live_handles");
    SYM(l, quote_string, "chs_quote_string");
    SYM(l, schema_create, "chs_schema_create");
    SYM(l, schema_free, "chs_schema_free");
    SYM(l, filter_create, "chs_filter_create");
    SYM(l, filter_free, "chs_filter_free");
    SYM(l, filter_eval_body, "chs_filter_eval_body");
    SYM(l, preview_row, "chs_preview_row");
#if CHS_ABI_VERSION >= 2
    SYM(l, server_create, "chs_server_create");
    SYM(l, server_free, "chs_server_free");
#endif
    return l;
}

/* A tiny reader for this stub's own flat, single-level JSON (never a general
   parser): finds "key": and parses the integer that follows. */
static long json_field_int(const char *json, const char *key) {
    char needle[128];
    snprintf(needle, sizeof needle, "\"%s\":", key);
    const char *p = strstr(json, needle);
    if (p == NULL) {
        fprintf(stderr, "stubtest: json_field_int: no %s in %s\n", needle, json);
        exit(2);
    }
    return strtol(p + strlen(needle), NULL, 10);
}

static long live_count(lib_t *l, const char *kind) {
    chs_buf *out = NULL;
    chs_error *err = NULL;
    chs_status st = l->live_handles(&out, &err);
    CHECK(st == CHS_OK && out != NULL, "chs_live_handles: status %d", (int) st);
    char tmp[512];
    size_t n = l->buf_len(out);
    if (n >= sizeof tmp) n = sizeof tmp - 1;
    memcpy(tmp, l->buf_data(out), n);
    tmp[n] = 0;
    long v = json_field_int(tmp, kind);
    l->buf_free(out);
    (void) err;
    return v;
}

/* --------------------------------------------------------------- the checks */

static void test_handshake(lib_t *l) {
    CHECK(l->abi_version() == CHS_ABI_VERSION, "chs_abi_version() = %d, want %d", l->abi_version(), CHS_ABI_VERSION);
    const char *bi = l->build_info();
    CHECK(bi != NULL && strstr(bi, "\"schema\":1") != NULL, "chs_build_info(): %s", bi ? bi : "(null)");
    CHECK(bi != NULL && strstr(bi, "\"abi_fingerprint\":\"sha256:") != NULL, "chs_build_info() has no abi_fingerprint: %s", bi);
    const char *cv = l->clickhouse_version();
    CHECK(cv != NULL && cv[0] != 0, "chs_clickhouse_version(): empty");
}

static void test_tombstone(lib_t *l) {
    CHECK(l->abi_revision() == CHS_ABI_REVISION_TOMBSTONE, "chs_abi_revision() = %d, want %d", l->abi_revision(),
          CHS_ABI_REVISION_TOMBSTONE);
}

/* Proves the stub's internal SHA-256 (used nowhere else in this file) is
   correct, by calling the REAL chs_quote_string with known plaintexts and
   checking the well-known RFC 6234 digests appear in its echoed output —
   never by calling an internal sha256 routine with a hand-picked input. */
static void test_sha256_via_echo(lib_t *l) {
    chs_buf *out = NULL;
    chs_error *err = NULL;

    CHECK(l->quote_string((const uint8_t *) "abc", 3, &out, &err) == CHS_OK, "chs_quote_string(\"abc\")");
    char tmp[1024];
    size_t n = l->buf_len(out);
    memcpy(tmp, l->buf_data(out), n < sizeof tmp ? n : sizeof tmp - 1);
    tmp[n < sizeof tmp ? n : sizeof tmp - 1] = 0;
    CHECK(strstr(tmp, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") != NULL,
          "sha256(\"abc\") not found in echo: %s", tmp);
    CHECK(strstr(tmp, "\"head_hex\":\"616263\"") != NULL, "head_hex of \"abc\" not found in echo: %s", tmp);
    l->buf_free(out);

    CHECK(l->quote_string(NULL, 0, &out, &err) == CHS_OK, "chs_quote_string(\"\")");
    n = l->buf_len(out);
    memcpy(tmp, l->buf_data(out), n < sizeof tmp ? n : sizeof tmp - 1);
    tmp[n < sizeof tmp ? n : sizeof tmp - 1] = 0;
    CHECK(strstr(tmp, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") != NULL,
          "sha256(\"\") not found in echo: %s", tmp);
    l->buf_free(out);
}

/* Regression for the chs_sb_fmt truncation bug: the wrapping
   {"args":[...],"fn":...,"out":...} object used to be built through
   chs_sb_fmt's fixed `char tmp[256]` (vsnprintf), so any echo whose args
   array alone exceeds ~230 bytes was silently cut off. chs_preview_row's
   three bytes_in arguments push the echo well past 256 bytes on their own
   (each {"head_hex","len","sha256"} object is well over 100 bytes), so this
   proves the wrapper is now built by chs_sb_cat concatenation instead. */
static void test_long_echo_not_truncated(lib_t *l) {
    chs_schema *schema = NULL;
    chs_error *err = NULL;
    CHECK(SCHEMA_CREATE(l, (const uint8_t *) "x", 1, NULL, 0, &schema, &err) == CHS_OK,
          "chs_schema_create for the long-echo test");

    const char *body = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
    const char *settings = "{\"setting_one\":\"value_one\",\"setting_two\":\"value_two\"}";
    const char *columns = "[\"col_a\",\"col_b\",\"col_c\",\"col_d\",\"col_e\"]";

    chs_buf *out = NULL;
    chs_status st = l->preview_row(
        schema, CHS_JSON_EACH_ROW, (const uint8_t *) body, strlen(body), (const uint8_t *) settings,
        strlen(settings), (const uint8_t *) columns, strlen(columns), &out, &err);
    CHECK(st == CHS_OK, "chs_preview_row for the long-echo test: status %d", (int) st);
    if (out != NULL) {
        size_t n = l->buf_len(out);
        CHECK(n > 256, "the echo was not long enough to exercise the truncation bug (got %zu bytes)", n);
        char tmp[4096];
        size_t copy = n < sizeof tmp - 1 ? n : sizeof tmp - 1;
        memcpy(tmp, l->buf_data(out), copy);
        tmp[copy] = 0;
        /* A truncated echo (the chs_sb_fmt bug) cuts this off mid-field or
           drops the closing brace entirely; all three must survive. */
        CHECK(strstr(tmp, "\"fn\":\"chs_preview_row\"") != NULL, "echo missing fn field (likely truncated): %s", tmp);
        CHECK(strstr(tmp, "\"out\":\"out\"") != NULL, "echo missing out field (likely truncated): %s", tmp);
        CHECK(tmp[copy - 1] == '}', "echo does not end with '}' (truncated): last char is '%c'", tmp[copy - 1]);
        l->buf_free(out);
    }
    l->schema_free(schema);
}

/* Drives every chs_status value through the "!S:" injection a cases.json
   case will also use, and checks all four error fields round-trip. */
static void test_status_injection(lib_t *l) {
    static const struct {
        const char *name;
        chs_status want;
    } statuses[] = {
        {"CHS_REJECTED", CHS_REJECTED},
        {"CHS_DECLINED", CHS_DECLINED},
        {"CHS_INVALID_ARGUMENT", CHS_INVALID_ARGUMENT},
        {"CHS_INTERNAL", CHS_INTERNAL},
    };
    for (size_t i = 0; i < sizeof statuses / sizeof statuses[0]; i++) {
        char msg[128];
        snprintf(msg, sizeof msg, "!S:%s:42:TEST_CODE:boom: with a colon", statuses[i].name);
        chs_buf *out = NULL;
        chs_error *err = NULL;
        chs_status st = l->quote_string((const uint8_t *) msg, strlen(msg), &out, &err);
        CHECK(st == statuses[i].want, "injected %s: got status %d", statuses[i].name, (int) st);
        CHECK(err != NULL, "injected %s: no error set", statuses[i].name);
        if (err != NULL) {
            CHECK(l->error_status(err) == statuses[i].want, "injected %s: chs_error_status mismatch", statuses[i].name);
            CHECK(l->error_ch_code(err) == 42, "injected %s: chs_error_ch_code = %d, want 42", statuses[i].name,
                  l->error_ch_code(err));
            chs_buf *message = l->error_message(err);
            CHECK(message != NULL, "injected %s: chs_error_message is NULL", statuses[i].name);
            if (message != NULL) {
                char mtmp[128];
                size_t n = l->buf_len(message);
                memcpy(mtmp, l->buf_data(message), n < sizeof mtmp ? n : sizeof mtmp - 1);
                mtmp[n < sizeof mtmp ? n : sizeof mtmp - 1] = 0;
                CHECK(strcmp(mtmp, "boom: with a colon") == 0, "injected %s: message = %s", statuses[i].name, mtmp);
                l->buf_free(message);
            }
            l->error_free(err);
        }
    }
}

/* free(NULL) and a double free are no-ops; a freed buf gives NULL/0, exactly
   as include/chtypes.h documents for chs_buf_data/chs_buf_len. */
static void test_free_rules(lib_t *l) {
    l->buf_free(NULL); /* must not crash */

    chs_buf *out = NULL;
    chs_error *err = NULL;
    CHECK(l->quote_string((const uint8_t *) "x", 1, &out, &err) == CHS_OK, "chs_quote_string(\"x\")");
    CHECK(l->buf_len(out) > 0, "a fresh buf reports zero length");
    l->buf_free(out);
    CHECK(l->buf_len(out) == 0, "chs_buf_len on a freed buf did not give 0");
    CHECK(l->buf_data(out) == NULL, "chs_buf_data on a freed buf did not give NULL");
    l->buf_free(out); /* a double free: still a no-op */
    CHECK(l->buf_len(out) == 0, "a double free changed the freed buf's reported length");
}

/* A chs_error* handed to a chs_buf-typed call is the wrong kind: chs_buf_len
   must refuse it (give 0) exactly as it does a NULL or freed buf. */
static void test_wrong_kind(lib_t *l) {
    chs_buf *out = NULL;
    chs_error *err = NULL;
    CHECK(l->quote_string((const uint8_t *) "!S:CHS_INTERNAL:0:X:boom", 24, &out, &err) == CHS_INTERNAL,
          "status injection for the wrong-kind setup failed");
    CHECK(err != NULL, "no error produced for the wrong-kind setup");
    size_t wrong_len = l->buf_len((const chs_buf *) (const void *) err);
    CHECK(wrong_len == 0, "chs_buf_len on a chs_error* (wrong kind) = %zu, want 0", wrong_len);
    l->error_free(err);
}

/* The core of D2: a filter HOLDS its schema, so freeing the schema first
   does not stop the filter from working, and both disappear from
   chs_live_handles only once every reference (the caller's AND the
   filter's) is gone. */
static void test_holds_and_live(lib_t *l) {
    long schema_before = live_count(l, "chs_schema");
    long filter_before = live_count(l, "chs_filter");

    chs_schema *schema = NULL;
    chs_error *err = NULL;
    CHECK(SCHEMA_CREATE(l, (const uint8_t *) "x", 1, NULL, 0, &schema, &err) == CHS_OK, "chs_schema_create");
    CHECK(live_count(l, "chs_schema") == schema_before + 1, "live chs_schema did not increase by one on create");

    chs_filter *filter = NULL;
    CHECK(l->filter_create(schema, (const uint8_t *) "1", 1, NULL, 0, NULL, 0, &filter, &err) == CHS_OK, "chs_filter_create");
    CHECK(live_count(l, "chs_filter") == filter_before + 1, "live chs_filter did not increase by one on create");

    l->schema_free(schema); /* the caller's own reference; the filter still holds one */
    CHECK(live_count(l, "chs_schema") == schema_before + 1,
          "live chs_schema dropped after freeing the schema while a filter still holds it");

    chs_buf *out = NULL;
    CHECK(l->filter_eval_body(filter, CHS_JSON_EACH_ROW, (const uint8_t *) "{}", 2, NULL, 0, &out, &err) == CHS_OK,
          "chs_filter_eval_body after its schema was freed by the caller");
    if (out != NULL) l->buf_free(out);

    l->filter_free(filter); /* drops the filter's own hold on the schema too */
    CHECK(live_count(l, "chs_filter") == filter_before, "live chs_filter did not return to its baseline after free");
    CHECK(live_count(l, "chs_schema") == schema_before,
          "live chs_schema did not return to its baseline once the filter that held it was also freed");
}

/* Exactly one CREATE TABLE statement: the stub's stand-in for the rule
   (scripts/abi-v1/emit/_stubshared.py ONE_CREATE) refuses a second
   statement with its error intact, and a trailing semicolon is not one. */
static void test_one_create(lib_t *l) {
    static const char two[] = "CREATE TABLE a (x Int32) ENGINE = Memory; CREATE TABLE b (y Int32) ENGINE = Memory";
    static const char one[] = "CREATE TABLE a (x Int32) ENGINE = Memory;\n";
    chs_schema *schema = NULL;
    chs_error *err = NULL;
    chs_status st = SCHEMA_CREATE(l, (const uint8_t *) two, sizeof two - 1, NULL, 0, &schema, &err);
    CHECK(st == CHS_REJECTED, "two CREATE statements gave status %d, want CHS_REJECTED", (int) st);
    CHECK(schema == NULL, "two CREATE statements still produced a schema");
    CHECK(err != NULL && l->error_ch_code(err) == 62, "two CREATE statements: ch_code %d, want 62",
          err != NULL ? l->error_ch_code(err) : -1);
    if (err != NULL) l->error_free(err);
    err = NULL;
    st = SCHEMA_CREATE(l, (const uint8_t *) one, sizeof one - 1, NULL, 0, &schema, &err);
    CHECK(st == CHS_OK && schema != NULL, "one CREATE statement with a trailing semicolon gave status %d", (int) st);
    if (schema != NULL) l->schema_free(schema);
}

/* Decision 1: a `shared` call is safe from many threads at once on the SAME
   handle. Eight threads each preview rows and compile filters over one
   schema, and evaluate one filter; every call must succeed, and once every
   thread's own handles are freed the live counts are back where they were. */
#define CONC_THREADS 8
#define CONC_CALLS 200

typedef struct {
    lib_t *l;
    const chs_schema *schema;
    const chs_filter *filter;
    int failures;
} conc_arg;

static void *conc_worker(void *p) {
    conc_arg *a = (conc_arg *) p;
    static const char body[] = "{\"x\":1}";
    static const char settings[] = "{\"session_timezone\":\"America/Los_Angeles\"}";
    for (int i = 0; i < CONC_CALLS; i++) {
        chs_buf *out = NULL;
        chs_error *err = NULL;
        if (a->l->preview_row(a->schema, CHS_JSON_EACH_ROW, (const uint8_t *) body, sizeof body - 1,
                              (const uint8_t *) settings, sizeof settings - 1, NULL, 0, &out, &err) != CHS_OK) {
            a->failures++;
        }
        if (out != NULL) a->l->buf_free(out);
        chs_filter *f = NULL;
        if (a->l->filter_create(a->schema, (const uint8_t *) "x = 1", 5, NULL, 0, (const uint8_t *) settings,
                                sizeof settings - 1, &f, &err) != CHS_OK) {
            a->failures++;
        }
        if (f != NULL) a->l->filter_free(f);
        out = NULL;
        if (a->l->filter_eval_body(a->filter, CHS_JSON_EACH_ROW, (const uint8_t *) body, sizeof body - 1, NULL, 0,
                                   &out, &err) != CHS_OK) {
            a->failures++;
        }
        if (out != NULL) a->l->buf_free(out);
    }
    return NULL;
}

static void test_concurrent_shared(lib_t *l) {
    long schema_before = live_count(l, "chs_schema");
    long filter_before = live_count(l, "chs_filter");
    long buf_before = live_count(l, "chs_buf");

    chs_schema *schema = NULL;
    chs_error *err = NULL;
    CHECK(SCHEMA_CREATE(l, (const uint8_t *) "x", 1, NULL, 0, &schema, &err) == CHS_OK, "chs_schema_create");
    chs_filter *filter = NULL;
    CHECK(l->filter_create(schema, (const uint8_t *) "1", 1, NULL, 0, NULL, 0, &filter, &err) == CHS_OK,
          "chs_filter_create");

    pthread_t threads[CONC_THREADS];
    conc_arg args[CONC_THREADS];
    for (int i = 0; i < CONC_THREADS; i++) {
        args[i] = (conc_arg){l, schema, filter, 0};
        CHECK(pthread_create(&threads[i], NULL, conc_worker, &args[i]) == 0, "pthread_create %d", i);
    }
    int failures = 0;
    for (int i = 0; i < CONC_THREADS; i++) {
        pthread_join(threads[i], NULL);
        failures += args[i].failures;
    }
    CHECK(failures == 0, "%d of %d concurrent shared calls failed", failures, CONC_THREADS * CONC_CALLS * 3);

    l->filter_free(filter);
    l->schema_free(schema);
    CHECK(live_count(l, "chs_schema") == schema_before, "live chs_schema did not return to its baseline");
    CHECK(live_count(l, "chs_filter") == filter_before, "live chs_filter did not return to its baseline");
    CHECK(live_count(l, "chs_buf") == buf_before, "live chs_buf did not return to its baseline");
}

/* The byte_strings rule, at the byte level: document mode
   (scripts/abi-v1/emit/_stubshared.py DOC_TEMPLATES) on chs_preview_row.
   Each member is in exactly ONE of its two forms, the plain one only for
   strict UTF-8 (RFC 3629: no overlong form, no surrogate, nothing above
   U+10FFFF), and value_b64 is always there. */
static int doc_has(const char *doc, size_t n, const char *needle) {
    size_t k = strlen(needle);
    for (size_t i = 0; i + k <= n; i++)
        if (memcmp(doc + i, needle, k) == 0) return 1;
    return 0;
}

static void test_document_mode(lib_t *l) {
    static const struct {
        const char *what;
        const char *value;
        size_t len;
        int utf8;
        const char *stored; /* the exact member expected */
        const char *b64;    /* value_b64's exact member */
    } cases[] = {
        {"binary FF 00 80", "\xff\x00\x80", 3, 0, "\"stored_b64\":\"/wCA\"", "\"value_b64\":\"/wCA\""},
        {"UTF-8 with a NUL", "a\000b", 3, 1, "\"stored\":\"a\\u0000b\"", "\"value_b64\":\"YQBi\""},
        {"a two-byte character", "\xc3\xa9", 2, 1, "\"stored\":\"\xc3\xa9\"", "\"value_b64\":\"w6k=\""},
        {"U+10FFFF", "\xf4\x8f\xbf\xbf", 4, 1, "\"stored\":\"\xf4\x8f\xbf\xbf\"", "\"value_b64\":\"9I+/vw==\""},
        {"an overlong NUL", "\xc0\x80", 2, 0, "\"stored_b64\":\"wIA=\"", "\"value_b64\":\"wIA=\""},
        {"a surrogate", "\xed\xa0\x80", 3, 0, "\"stored_b64\":\"7aCA\"", "\"value_b64\":\"7aCA\""},
        {"above U+10FFFF", "\xf4\x90\x80\x80", 4, 0, "\"stored_b64\":\"9JCAgA==\"", "\"value_b64\":\"9JCAgA==\""},
        {"a truncated sequence", "a\xe2\x82", 3, 0, "\"stored_b64\":\"YeKC\"", "\"value_b64\":\"YeKC\""},
    };
    chs_schema *schema = NULL;
    chs_error *err = NULL;
    CHECK(SCHEMA_CREATE(l, (const uint8_t *) "x", 1, NULL, 0, &schema, &err) == CHS_OK, "chs_schema_create");
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        uint8_t body[64];
        memcpy(body, "!D:", 3);
        memcpy(body + 3, cases[i].value, cases[i].len);
        chs_buf *out = NULL;
        chs_status st = l->preview_row(schema, CHS_JSON_EACH_ROW, body, 3 + cases[i].len, NULL, 0, NULL, 0, &out, &err);
        CHECK(st == CHS_OK && out != NULL, "document mode, %s: status %d", cases[i].what, (int) st);
        if (out == NULL) continue;
        const char *doc = (const char *) l->buf_data(out);
        size_t n = l->buf_len(out);
        CHECK(doc_has(doc, n, cases[i].stored), "document mode, %s: no %s in %.*s", cases[i].what, cases[i].stored,
              (int) n, doc);
        CHECK(doc_has(doc, n, cases[i].b64), "document mode, %s: no %s", cases[i].what, cases[i].b64);
        CHECK(doc_has(doc, n, "\"stored\":") == cases[i].utf8, "document mode, %s: plain stored present = %d",
              cases[i].what, !cases[i].utf8);
        CHECK(doc_has(doc, n, "\"stored_b64\":") == !cases[i].utf8, "document mode, %s: stored_b64 present = %d",
              cases[i].what, cases[i].utf8);
        CHECK(doc_has(doc, n, "\"input\":") == cases[i].utf8 && doc_has(doc, n, "\"input_b64\":") == !cases[i].utf8,
              "document mode, %s: input is not in exactly its one form", cases[i].what);
        l->buf_free(out);
    }
    l->schema_free(schema);
}

/* Two SEPARATELY dlopen'd, byte-identical builds (RTLD_LOCAL: each gets its
   own static image-identity marker) must refuse each other's handles. */
static void test_cross_image(lib_t *a, lib_t *b) {
    chs_buf *out = NULL;
    chs_error *err = NULL;
    CHECK(a->quote_string((const uint8_t *) "y", 1, &out, &err) == CHS_OK, "chs_quote_string on lib a");
    CHECK(b->buf_len(out) == 0, "lib b's chs_buf_len accepted lib a's buf (cross-image not caught)");
    a->buf_free(out);

    chs_schema *schema = NULL;
    CHECK(SCHEMA_CREATE(a, (const uint8_t *) "x", 1, NULL, 0, &schema, &err) == CHS_OK, "chs_schema_create on lib a");
    chs_filter *filter = NULL;
    chs_status st = b->filter_create(schema, (const uint8_t *) "1", 1, NULL, 0, NULL, 0, &filter, &err);
    CHECK(st == CHS_INVALID_ARGUMENT, "lib b's chs_filter_create over lib a's schema gave %d, want CHS_INVALID_ARGUMENT",
          (int) st);
    if (err != NULL) b->error_free(err);
    a->schema_free(schema);
}

#if CHS_ABI_VERSION >= 2
/* Generation 2: rule r1's closed input documents
   (scripts/abi-v1/emit/_stubshared.py INPUT_DOCUMENT) through the reader the
   stub compiles, on inputs whose values would trip a reader that does not
   track strings and nesting; and a schema holding the server it was created
   on (D2), so the server outlives the caller's free of it. */
static void test_server_profile(lib_t *l) {
    static const struct {
        const char *what;
        const char *profile;
        chs_status want;
        const char *message; /* the exact message, for a refusal */
    } cases[] = {
        {"length 0", "", CHS_OK, NULL},
        {"an empty object", " { } ", CHS_OK, NULL},
        {"every member, values holding quotes, braces and commas",
         "{\"settings\":{\"a\":\"x\\\"},{y\",\"b\":\"[1,{\"},\"macros\":{\"shard\":\"01\"},\"timezone\":\"UTC\"}",
         CHS_OK, NULL},
        {"an unknown key after the known ones", "{\"timezone\":\"UTC\", \"zz\" : 1}", CHS_INVALID_ARGUMENT,
         "profile: unknown key \"zz\" in the input document server_profile"},
        {"an array", "[]", CHS_INVALID_ARGUMENT, "profile: the input document server_profile is not a JSON object"},
        {"a trailing byte", "{} x", CHS_INVALID_ARGUMENT,
         "profile: the input document server_profile is not a JSON object"},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        chs_server *server = NULL;
        chs_error *err = NULL;
        size_t n = strlen(cases[i].profile);
        chs_status st = l->server_create(n ? (const uint8_t *) cases[i].profile : NULL, n, NULL, 0, &server, &err);
        CHECK(st == cases[i].want, "chs_server_create, %s: status %d, want %d", cases[i].what, (int) st,
              (int) cases[i].want);
        CHECK((server != NULL) == (cases[i].want == CHS_OK), "chs_server_create, %s: a server iff CHS_OK",
              cases[i].what);
        if (cases[i].message != NULL && err != NULL) {
            chs_buf *m = l->error_message(err);
            size_t k = m != NULL ? l->buf_len(m) : 0;
            CHECK(k == strlen(cases[i].message) && memcmp(l->buf_data(m), cases[i].message, k) == 0,
                  "chs_server_create, %s: message %.*s", cases[i].what, (int) k,
                  m != NULL ? (const char *) l->buf_data(m) : "");
            if (m != NULL) l->buf_free(m);
        }
        if (err != NULL) l->error_free(err);
        if (server != NULL) l->server_free(server);
    }

    long server_before = live_count(l, "chs_server");
    long schema_before = live_count(l, "chs_schema");
    chs_server *server = NULL;
    chs_error *err = NULL;
    CHECK(l->server_create(NULL, 0, NULL, 0, &server, &err) == CHS_OK, "chs_server_create({})");
    chs_schema *schema = NULL;
    CHECK(l->schema_create(server, (const uint8_t *) "x", 1, NULL, 0, NULL, 0, &schema, &err) == CHS_OK,
          "chs_schema_create on a server");
    l->server_free(server); /* the caller's own reference; the schema still holds one */
    CHECK(live_count(l, "chs_server") == server_before + 1,
          "live chs_server dropped after freeing the server while a schema still holds it");
    l->schema_free(schema);
    CHECK(live_count(l, "chs_server") == server_before, "live chs_server did not return to its baseline");
    CHECK(live_count(l, "chs_schema") == schema_before, "live chs_schema did not return to its baseline");
}
#endif

static void test_unbound_refuses_rtld_now(const char *dir) {
    char path[4096];
    snprintf(path, sizeof path, "%s/unbound.so", dir);
    FILE *probe = fopen(path, "rb");
    if (probe == NULL) return; /* build-stubs.sh did not build this variant for this run; nothing to check */
    fclose(probe);
    void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    CHECK(h == NULL, "unbound.so opened under RTLD_NOW; it must fail to resolve an undefined symbol eagerly");
    if (h != NULL) dlclose(h);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: stubtest <dir containing ok.so and ok-b.so>\n");
        return 2;
    }
    char a_path[4096], b_path[4096];
    snprintf(a_path, sizeof a_path, "%s/ok.so", argv[1]);
    snprintf(b_path, sizeof b_path, "%s/ok-b.so", argv[1]);
    lib_t a = load_lib(a_path);
    lib_t b = load_lib(b_path);

    test_handshake(&a);
    test_tombstone(&a);
    test_sha256_via_echo(&a);
    test_long_echo_not_truncated(&a);
    test_status_injection(&a);
    test_free_rules(&a);
    test_wrong_kind(&a);
    test_holds_and_live(&a);
    test_one_create(&a);
    test_document_mode(&a);
    test_concurrent_shared(&a);
    test_cross_image(&a, &b);
#if CHS_ABI_VERSION >= 2
    test_server_profile(&a);
#endif
    test_unbound_refuses_rtld_now(argv[1]);

    if (g_pass == 0) {
        fprintf(stderr, "stubtest: zero checks ran; that is a failure, not a pass\n");
        return 1;
    }
    if (g_fail > 0) {
        fprintf(stderr, "stubtest: %d failed, %d passed\n", g_fail, g_pass);
        return 1;
    }
    printf("stubtest: ok (%d checks passed)\n", g_pass);
    return 0;
}
