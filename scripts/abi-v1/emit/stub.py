"""The test stub: a real, tiny implementation of D2's handle rules and D3's
status/error shape, generated from the same spec/abi-v1/abi.json every other
emitter reads, that echoes its inputs instead of doing anything ClickHouse.

This emitter produces NO committed output (`outputs()` returns `[]`); the
stub's C source is a BUILD-TIME file, read by `gen.py --render stub --out DIR`
and compiled by scripts/abi-v1/build-stubs.sh into one .so per
`emit._stubshared.plan(model)` entry. Nothing here is checked by
`gen.py --check`.

WHAT THE STUB REALLY DOES (not a mock: these are the real rules, D2/D3,
exercised against real memory).

  * Every handle (chs_buf, chs_error, chs_schema, chs_filter, chs_block) is
    the SAME underlying layout, `struct chs_handle_common` as its first
    member (the standard C "common initial sequence" trick), tagged with its
    kind (an interned string literal, compared by pointer since the whole
    program is one translation unit) and its IMAGE (the address of a static
    local to this compiled unit, so two separately dlopen'd copies of the
    identical stub source get two different, comparable addresses — this is
    what "ok" and "ok-b" are for). A real atomic refcount starts at 1 and a
    `holds` list of up to 4 strong parent references implements D2's "a child
    holds its parents alive" rule for real: freeing a schema that a filter
    still holds does not free the schema.
  * `chs_X_free` on NULL is a no-op; on an already-freed handle it is also a
    no-op (any free order is safe); otherwise it decrements the refcount and,
    at zero, releases its held parents (recursively) and marks the handle
    poisoned. Memory is never actually `free()`d (a deliberate stub
    simplification: this process is short-lived and the point is to prove
    the COUNTING and TAGGING logic, not to avoid a leak) — this also lets a
    "use after free" check keep reading the poison flag instead of touching
    freed memory.
  * Every handle- or bytes_in-taking function validates its inputs BEFORE
    doing anything else: a wrong-kind, freed, or cross-image handle, or a
    NULL bytes_in pointer with a nonzero length, is CHS_INVALID_ARGUMENT
    naming the parameter — the one rule every binding's conformance cases
    probe per function.
  * STATUS INJECTION. If a function's first bytes_in parameter (in
    declaration order) begins with the literal `!S:`, the rest up to the
    next two `:` separators is read as `<chs_status name>:<ch_code
    integer>:<ch_name>` and everything after that third `:` to the end of
    the buffer (which may itself contain `:`) is the message; the function
    returns that status with an error carrying those fields and an empty
    column. This is how one cases.json case per `may_return` entry is
    produced without the stub knowing anything about ClickHouse.
  * ECHO. Absent an injected status, a function that produces an
    `out_handle` whose type is `chs_buf` fills it with a small JSON object
    `{"fn": "<this function's name>", "out": "<the out parameter's name>",
    "args": [...]}`, one entry per INPUT parameter in declaration order:
    a scalar or enum as a bare number, a bytes_in as
    `{"len":N,"sha256":"<hex>","head_hex":"<hex>"}` (never the bytes
    themselves — so NUL and invalid UTF-8 round-trip through length and hash
    alone), and a const handle as `{"kind":"<handle type>","id":<serial>}`.
    The JSON is valid but its key order is whatever this emitter writes
    textually; a conformance case compares the PARSED document structurally,
    never the raw bytes, so no binding's JSON encoder needs to match another
    byte for byte. A function whose out_handle is some other handle kind
    instead gets a freshly minted handle of that kind, holding whichever
    const handle input parameters match its `holds` list (by kind; today
    that is always exactly one, enforced at generation time).
  * THE IMAGE ZONE. `chs_initialize` keeps the library's process-once rule
    for real (_stubshared.IMAGE_ZONE): one sentinel bad zone is refused with
    CHS_REJECTED and commits nothing, the first other spelling is committed,
    and after that the same spelling is CHS_OK and a different one is
    CHS_INVALID_ARGUMENT, per image (each dlopen'd copy has its own state).
    A probe (_stubshared.ZONE_PROBE) reads the committed zone back through
    `chs_type_validate`, so a public-API test can see which zone step 7 used.
  * `chs_live_handles` reports this image's own live count per handle kind
    from one atomic counter per kind (incremented on mint, decremented the
    instant a handle's refcount reaches zero), never by walking a registry.
  * CLOSED INPUT DOCUMENTS (generation 2, rule r1). A bytes_in parameter
    whose content is `input:<name>` is checked after the status injection
    (_stubshared.INPUT_DOCUMENT): a document that is not a JSON object, or a
    top-level key the named document's schema does not list, is
    CHS_INVALID_ARGUMENT naming the key. Length 0 is `{}`.
  * A NULLABLE PARENT. A handle the minted one `holds` may come from a
    nullable parameter (chs_schema_create's `server`): the new handle holds
    it only when the caller passed one.

THE TWELVE HAND-WRITTEN FUNCTIONS (today; `classify()` reports the exact
split against the tree's own description). Each has return-value semantics,
or a job, a generic template cannot express — mostly "what does a function
return on a BAD handle when it has no status/error channel at all" — and is
written out directly, by name, in `_special()`: the three handshake calls
(`chs_abi_version`, `chs_clickhouse_version`, and `chs_build_info`, the last
of which also switches on `CHS_STUB_BUILD_INFO_MODE` in its own section), the
tombstone (`chs_abi_revision`), `chs_buf_data`/`chs_buf_len` (NULL/0 on a bad
buffer), `chs_error_status` (CHS_INVALID_ARGUMENT on a bad error) and
`chs_error_ch_code` (0 on a bad error), the three owned-buffer text accessors
(`chs_error_ch_name`/`chs_error_message`/`chs_error_column`, NULL on a bad
error or allocation failure), and `chs_live_handles` (the real per-kind
counters, not an echo of its own argument-less call). Every free function
(five handles plus `chs_shutdown`) is ALSO generated, by one shared
structural template (`_gen_free`/`is_free_like`), not hand-written, since all
six share one shape. Everything else — about HALF of the described functions
today (20 of 38) — goes through ONE generic template, `_gen_generic`, driven
purely by the function's `params`/`returns` shape, so a new provisional call
needs no new emitter code as long as it stays within the closed
parameter-kind vocabulary model.py already enforces.
"""

from __future__ import annotations

from model import BUF_HANDLE, ERROR_HANDLE, STATUS_ENUM

from . import _stubshared

# Every major some binding speaks (emit/__init__.py, SHARED): each major's
# conformance runners need their own cases and stubs.
MAJORS = (1, 2)
SHARED = True

HEAD_BYTES = _stubshared.HEAD_BYTES


def outputs(model):  # noqa: ARG001 - no committed output; see module docstring
    return []


# --------------------------------------------------------------------- C text


def _c_str(s: str) -> str:
    """A C string literal for `s` (ASCII-only input is all this ever needs)."""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _omit_guard(symbol: str) -> tuple[str, str]:
    return f"#if !defined({_stubshared.omit_define(symbol)})", "#endif"


def _kind_const(handle_name: str) -> str:
    """CHS_STUB_KIND_<X> for a handle type name `chs_X` (every handle name is
    the frozen `chs_` prefix plus the kind word, so stripping it once here
    keeps every call site and the enum declaration itself in exact
    agreement, instead of two independently-typed spellings risking drift)."""
    assert handle_name.startswith("chs_"), handle_name
    return f"CHS_STUB_KIND_{handle_name[len('chs_'):].upper()}"


def _c_fixed_error(status: str, message: str, *, indent: str = "    ") -> list[str]:
    """A `chs_stub_set_err(err, ...); return <status>;` block whose message is
    a C string literal, with its length taken from `sizeof(...) - 1` rather
    than a hand-computed Python integer, so there is no length arithmetic to
    get wrong (and nothing to drift if the message text is ever edited)."""
    lit = _c_str(message)
    return [
        f"{indent}{{",
        f'{indent}    static const char msg[] = {lit};',
        f"{indent}    chs_stub_set_err(err, chs_stub_make_error({status}, 0, \"\", 0, msg, sizeof(msg) - 1));",
        f"{indent}    return {status};",
        f"{indent}}}",
    ]


# ------------------------------------------------------------------- preamble


def _preamble(model) -> str:
    handle_names = list(model.handles)  # insertion order from abi.json: buf, error, schema, filter, block
    kind_enum = ", ".join(_kind_const(h) for h in handle_names)
    kind_count = len(handle_names)
    kind_name_table = ",\n    ".join(_c_str(h) for h in handle_names)
    status_table = "\n".join(
        f"    if (chs_stub_slice_eq(p, n, {_c_str(v.name)})) {{ *out = {v.name}; return 1; }}"
        for v in model.enums[STATUS_ENUM].values
    )
    # chs_buf and chs_error carry their own payload (defined further below);
    # every other handle kind is identity-only for the stub's purposes: a
    # chs_handle_common plus whatever it holds. The header already supplies
    # `typedef struct chs_X chs_X;` (opaque); this only fills in the struct
    # body the typedef names, so it never redeclares the typedef itself.
    plain_structs = "\n\n".join(
        f"struct {h} {{\n    chs_handle_common common;\n}};"
        for h in handle_names
        if h not in (BUF_HANDLE, ERROR_HANDLE)
    )
    return f'''/* GENERATED BUILD-TIME FILE by scripts/abi-v1/gen.py --render stub, from spec/abi-v1/abi.json
   (CHS_ABI_FINGERPRINT {model.fingerprint}). Never committed; build-stubs.sh writes this fresh
   on every run, then compiles it into one .so per scripts/abi-v1/emit/_stubshared.py's variant
   plan. See scripts/abi-v1/emit/stub.py's module docstring for what this file implements and
   why: real D2 handle discipline (refcounts, kind and image tags, "holds" keeps a parent alive),
   D3 status injection via a magic `!S:` prefix on a function's first bytes_in argument, and an
   echo of every other call's inputs into its chs_buf output, so a conformance case can assert a
   specific, adversarial-bytes-safe result without any ClickHouse behavior in this file at all. */
#include "chtypes.h"

#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------- handle kinds */

enum {{ {kind_enum}, CHS_STUB_KIND_COUNT = {kind_count} }};

static const char *const CHS_STUB_KIND_NAMES[CHS_STUB_KIND_COUNT] = {{
    {kind_name_table}
}};

static atomic_llong chs_stub_live[CHS_STUB_KIND_COUNT];
static atomic_ullong chs_stub_next_id = 1;

/* The address of this per-translation-unit static is this stub BUILD's own
   identity: two separately dlopen'd copies of the identical source (RTLD_LOCAL
   gives each its own static storage) get two different addresses, which is
   exactly what a cross-image check compares. */
static const int chs_stub_image_marker = 0;
#define CHS_STUB_IMAGE (&chs_stub_image_marker)

#define CHS_STUB_HOLDS_MAX 4

typedef struct chs_handle_common {{
    int kind;                 /* index into CHS_STUB_KIND_NAMES */
    const void *image;
    atomic_int refcount;      /* starts at 1; a free() path decrements it */
    atomic_int freed;         /* 1 once refcount has reached 0 (poisoned) */
    unsigned long long id;    /* a per-image serial, for the echo's "id" field */
    struct chs_handle_common *holds[CHS_STUB_HOLDS_MAX];
    int holds_count;
}} chs_handle_common;

static void chs_stub_common_init(chs_handle_common *c, int kind) {{
    c->kind = kind;
    c->image = CHS_STUB_IMAGE;
    atomic_init(&c->refcount, 1);
    atomic_init(&c->freed, 0);
    c->id = atomic_fetch_add(&chs_stub_next_id, 1);
    c->holds_count = 0;
    atomic_fetch_add(&chs_stub_live[kind], 1);
}}

/* Release this reference; if it is the last one, release every held parent
   (recursively, through the same function) and mark this handle poisoned.
   Never actually frees the struct's memory — see the module docstring. */
static void chs_stub_release(chs_handle_common *c) {{
    if (c == NULL || atomic_load(&c->freed)) return; /* NULL or double-free: a no-op */
    if (atomic_fetch_sub(&c->refcount, 1) != 1) return; /* another reference remains */
    atomic_store(&c->freed, 1);
    atomic_fetch_sub(&chs_stub_live[c->kind], 1);
    for (int i = 0; i < c->holds_count; i++) chs_stub_release(c->holds[i]);
}}

static void chs_stub_hold(chs_handle_common *child, chs_handle_common *parent) {{
    atomic_fetch_add(&parent->refcount, 1);
    child->holds[child->holds_count++] = parent;
}}

/* NULL on a required-but-absent handle is reported by the caller (the
   required/nullable distinction is per parameter); this validates a
   NON-NULL handle's kind, image and liveness. Returns NULL on success, or a
   literal problem description (never freed; stub errors leak, deliberately). */
static const char *chs_stub_check_handle(const chs_handle_common *c, int want_kind, const char *param) {{
    if (atomic_load((atomic_int *) &c->freed)) {{
        static char buf[160];
        snprintf(buf, sizeof buf, "%s: a freed handle", param);
        return buf;
    }}
    if (c->image != CHS_STUB_IMAGE) {{
        static char buf[160];
        snprintf(buf, sizeof buf, "%s: a cross-image handle", param);
        return buf;
    }}
    if (c->kind != want_kind) {{
        static char buf[160];
        snprintf(buf, sizeof buf, "%s: wrong handle kind (%s, want %s)", param,
                  CHS_STUB_KIND_NAMES[c->kind], CHS_STUB_KIND_NAMES[want_kind]);
        return buf;
    }}
    return NULL;
}}

{plain_structs}
/* ------------------------------------------------------------------- errors */

typedef struct chs_error {{
    chs_handle_common common;
    chs_status status;
    int32_t ch_code;
    char *ch_name; size_t ch_name_len;
    char *message; size_t message_len;
    char *column; size_t column_len;
}} chs_error;

static chs_error *chs_stub_make_error(chs_status status, int32_t ch_code,
                                       const char *name, size_t name_len,
                                       const char *msg, size_t msg_len) {{
    chs_error *e = (chs_error *) malloc(sizeof *e);
    chs_stub_common_init(&e->common, {_kind_const(ERROR_HANDLE)});
    e->status = status;
    e->ch_code = ch_code;
    e->ch_name = name_len ? (char *) malloc(name_len) : NULL;
    if (e->ch_name) memcpy(e->ch_name, name, name_len);
    e->ch_name_len = name_len;
    e->message = msg_len ? (char *) malloc(msg_len) : NULL;
    if (e->message) memcpy(e->message, msg, msg_len);
    e->message_len = msg_len;
    e->column = NULL;
    e->column_len = 0;
    return e;
}}

static void chs_stub_set_err(chs_error **err, chs_error *e) {{
    if (err != NULL) *err = e; else chs_stub_release((chs_handle_common *) e);
}}

/* ---------------------------------------------------------- status injection */

static int chs_stub_slice_eq(const uint8_t *p, size_t n, const char *lit) {{
    size_t l = strlen(lit);
    return n == l && memcmp(p, lit, l) == 0;
}}

static int chs_stub_parse_status(const uint8_t *p, size_t n, chs_status *out) {{
{status_table}
    return 0;
}}

/* If `p[0:n)` begins "!S:<status>:<ch_code>:<ch_name>:<message>", parse it and
   return 1 with *status and *err set; the message runs to the end of the buffer
   and may itself contain ':'. Otherwise return 0 and touch nothing. */
static int chs_stub_try_inject(const uint8_t *p, size_t n, chs_status *status, chs_error **err) {{
    static const char PFX[] = "!S:";
    size_t pfx = sizeof(PFX) - 1;
    if (p == NULL || n < pfx || memcmp(p, PFX, pfx) != 0) return 0;
    size_t i = pfx, s0 = i;
    while (i < n && p[i] != ':') i++;
    if (i >= n) return 0;
    chs_status st;
    if (!chs_stub_parse_status(p + s0, i - s0, &st)) return 0;
    i++; /* skip ':' */
    size_t c0 = i;
    while (i < n && p[i] != ':') i++;
    if (i >= n) return 0;
    long code;
    {{
        char tmp[32];
        size_t clen = i - c0;
        if (clen >= sizeof tmp) clen = sizeof tmp - 1;
        memcpy(tmp, p + c0, clen);
        tmp[clen] = 0;
        code = strtol(tmp, NULL, 10);
    }}
    i++; /* skip ':' */
    size_t name0 = i;
    while (i < n && p[i] != ':') i++;
    if (i >= n) return 0;
    size_t name_len = i - name0;
    i++; /* skip ':': the rest is the message, verbatim, to the end */
    size_t msg0 = i, msg_len = n - i;
    *status = st;
    *err = chs_stub_make_error(st, (int32_t) code, (const char *) p + name0, name_len,
                                (const char *) p + msg0, msg_len);
    return 1;
}}

/* --------------------------------------------------------------------- bufs */

typedef struct chs_buf {{
    chs_handle_common common;
    uint8_t *data;
    size_t len;
}} chs_buf;

static chs_buf *chs_stub_make_buf(uint8_t *data, size_t len) {{
    chs_buf *b = (chs_buf *) malloc(sizeof *b);
    chs_stub_common_init(&b->common, {_kind_const(BUF_HANDLE)});
    b->data = data;
    b->len = len;
    return b;
}}

/* --------------------------------------------------------- the sha256 used
   by the echo's bytes field (RFC 6234); stubtest.c proves it against the
   standard test vectors before anything else in this file is trusted. */

typedef struct {{ uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t buflen; }} chs_sha256_ctx;

static const uint32_t CHS_SHA256_K[64] = {{
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
}};

static uint32_t chs_rotr(uint32_t x, int n) {{ return (x >> n) | (x << (32 - n)); }}

static void chs_sha256_block(chs_sha256_ctx *c, const uint8_t *p) {{
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t) p[i*4] << 24) | ((uint32_t) p[i*4+1] << 16) | ((uint32_t) p[i*4+2] << 8) | p[i*4+3];
    for (int i = 16; i < 64; i++) {{
        uint32_t s0 = chs_rotr(w[i-15], 7) ^ chs_rotr(w[i-15], 18) ^ (w[i-15] >> 3);
        uint32_t s1 = chs_rotr(w[i-2], 17) ^ chs_rotr(w[i-2], 19) ^ (w[i-2] >> 10);
        w[i] = w[i-16] + s0 + w[i-7] + s1;
    }}
    uint32_t a=c->h[0],b=c->h[1],cc=c->h[2],d=c->h[3],e=c->h[4],f=c->h[5],g=c->h[6],hh=c->h[7];
    for (int i = 0; i < 64; i++) {{
        uint32_t S1 = chs_rotr(e,6) ^ chs_rotr(e,11) ^ chs_rotr(e,25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = hh + S1 + ch + CHS_SHA256_K[i] + w[i];
        uint32_t S0 = chs_rotr(a,2) ^ chs_rotr(a,13) ^ chs_rotr(a,22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + maj;
        hh=g; g=f; f=e; e=d+t1; d=cc; cc=b; b=a; a=t1+t2;
    }}
    c->h[0]+=a; c->h[1]+=b; c->h[2]+=cc; c->h[3]+=d; c->h[4]+=e; c->h[5]+=f; c->h[6]+=g; c->h[7]+=hh;
}}

static void chs_sha256_init(chs_sha256_ctx *c) {{
    static const uint32_t iv[8] = {{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                                     0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}};
    memcpy(c->h, iv, sizeof iv);
    c->len = 0;
    c->buflen = 0;
}}

static void chs_sha256_update(chs_sha256_ctx *c, const uint8_t *p, size_t n) {{
    c->len += (uint64_t) n * 8;
    while (n > 0) {{
        size_t take = 64 - c->buflen;
        if (take > n) take = n;
        memcpy(c->buf + c->buflen, p, take);
        c->buflen += take; p += take; n -= take;
        if (c->buflen == 64) {{ chs_sha256_block(c, c->buf); c->buflen = 0; }}
    }}
}}

static void chs_sha256_final(chs_sha256_ctx *c, uint8_t out[32]) {{
    uint8_t pad[72];
    size_t padlen = 0;
    pad[padlen++] = 0x80;
    while ((c->buflen + padlen) % 64 != 56) pad[padlen++] = 0x00;
    for (int i = 7; i >= 0; i--) pad[padlen++] = (uint8_t) (c->len >> (i * 8));
    chs_sha256_update(c, pad, padlen);
    /* chs_sha256_update's own padding call must not re-enter padding, so
       buflen is now exactly 0: both blocks above were flushed by the loop. */
    for (int i = 0; i < 8; i++) {{
        out[i*4]   = (uint8_t) (c->h[i] >> 24);
        out[i*4+1] = (uint8_t) (c->h[i] >> 16);
        out[i*4+2] = (uint8_t) (c->h[i] >> 8);
        out[i*4+3] = (uint8_t) (c->h[i]);
    }}
}}

static void chs_sha256_hex(const uint8_t *p, size_t n, char out[65]) {{
    chs_sha256_ctx c; chs_sha256_init(&c); chs_sha256_update(&c, p, n);
    uint8_t digest[32]; chs_sha256_final(&c, digest);
    static const char hexd[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {{ out[i*2] = hexd[digest[i] >> 4]; out[i*2+1] = hexd[digest[i] & 0xf]; }}
    out[64] = 0;
}}

static void chs_bytes_hex(const uint8_t *p, size_t n, char *out) {{
    static const char hexd[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {{ out[i*2] = hexd[p[i] >> 4]; out[i*2+1] = hexd[p[i] & 0xf]; }}
    out[n*2] = 0;
}}

/* ---------------------------------------------------------------- a growable
   string builder, for the echo JSON (and nothing else needs one). */

typedef struct {{ char *buf; size_t len, cap; }} chs_sb;

static void chs_sb_init(chs_sb *sb) {{ sb->cap = 256; sb->len = 0; sb->buf = (char *) malloc(sb->cap); sb->buf[0] = 0; }}

static void chs_sb_reserve(chs_sb *sb, size_t extra) {{
    if (sb->len + extra + 1 <= sb->cap) return;
    while (sb->len + extra + 1 > sb->cap) sb->cap *= 2;
    sb->buf = (char *) realloc(sb->buf, sb->cap);
}}

static void chs_sb_cat(chs_sb *sb, const char *s) {{
    size_t n = strlen(s);
    chs_sb_reserve(sb, n);
    memcpy(sb->buf + sb->len, s, n + 1);
    sb->len += n;
}}

static void chs_sb_fmt(chs_sb *sb, const char *fmt, ...) {{
    char tmp[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    chs_sb_cat(sb, tmp);
}}

static void chs_sb_bytes_field(chs_sb *sb, const uint8_t *p, size_t n) {{
    char hex[65];
    chs_sha256_hex(p, n, hex);
    size_t head = n < {HEAD_BYTES} ? n : {HEAD_BYTES};
    char headhex[{HEAD_BYTES} * 2 + 1];
    chs_bytes_hex(p, head, headhex);
    chs_sb_fmt(sb, "{{\\"head_hex\\":\\"%s\\",\\"len\\":%zu,\\"sha256\\":\\"%s\\"}}", headhex, n, hex);
}}

static void chs_sb_handle_field(chs_sb *sb, const chs_handle_common *c) {{
    chs_sb_fmt(sb, "{{\\"id\\":%llu,\\"kind\\":\\"%s\\"}}", c->id, CHS_STUB_KIND_NAMES[c->kind]);
}}

static chs_buf *chs_stub_finish_buf(chs_sb *sb) {{
    return chs_stub_make_buf((uint8_t *) sb->buf, sb->len);
}}

/* ------------------------------------------------- the byte_strings rule, in
   C, for document mode (scripts/abi-v1/emit/_stubshared.py DOC_TEMPLATES).
   Deliberately its own code: a conformance case compares what this produces
   with what Python's own codec and base64 produce for the same bytes. */

static void chs_sb_append(chs_sb *sb, const void *p, size_t n) {{
    chs_sb_reserve(sb, n);
    memcpy(sb->buf + sb->len, p, n);
    sb->len += n;
    sb->buf[sb->len] = 0;
}}

/* Strict UTF-8, as RFC 3629 defines it: no overlong form, no surrogate, nothing
   above U+10FFFF. */
static int chs_stub_utf8_valid(const uint8_t *p, size_t n) {{
    size_t i = 0;
    while (i < n) {{
        uint8_t c = p[i];
        size_t need;
        uint8_t lo = 0x80, hi = 0xBF;
        if (c < 0x80) {{ i++; continue; }}
        else if (c >= 0xC2 && c <= 0xDF) need = 1;
        else if (c == 0xE0) {{ need = 2; lo = 0xA0; }}
        else if (c >= 0xE1 && c <= 0xEC) need = 2;
        else if (c == 0xED) {{ need = 2; hi = 0x9F; }}
        else if (c >= 0xEE && c <= 0xEF) need = 2;
        else if (c == 0xF0) {{ need = 3; lo = 0x90; }}
        else if (c >= 0xF1 && c <= 0xF3) need = 3;
        else if (c == 0xF4) {{ need = 3; hi = 0x8F; }}
        else return 0;
        if (i + need >= n) return 0; /* truncated sequence */
        if (p[i + 1] < lo || p[i + 1] > hi) return 0;
        for (size_t k = 2; k <= need; k++)
            if (p[i + k] < 0x80 || p[i + k] > 0xBF) return 0;
        i += need + 1;
    }}
    return 1;
}}

/* A JSON string of valid UTF-8 bytes: quote, backslash and control bytes
   escaped (a NUL as JSON's six-character escape for U+0000), every other byte kept. */
static void chs_sb_json_utf8(chs_sb *sb, const uint8_t *p, size_t n) {{
    static const char hexd[] = "0123456789abcdef";
    chs_sb_append(sb, "\\"", 1);
    for (size_t i = 0; i < n; i++) {{
        uint8_t c = p[i];
        if (c == '"' || c == '\\\\') {{
            char e[2] = {{'\\\\', (char) c}};
            chs_sb_append(sb, e, 2);
        }} else if (c < 0x20) {{
            char e[6] = {{'\\\\', 'u', '0', '0', hexd[c >> 4], hexd[c & 15]}};
            chs_sb_append(sb, e, 6);
        }} else {{
            chs_sb_append(sb, &p[i], 1);
        }}
    }}
    chs_sb_append(sb, "\\"", 1);
}}

/* A JSON string holding the standard base64 (RFC 4648 section 4, padded). */
static void chs_sb_base64(chs_sb *sb, const uint8_t *p, size_t n) {{
    static const char a[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    chs_sb_append(sb, "\\"", 1);
    size_t i = 0;
    for (; i + 3 <= n; i += 3) {{
        uint32_t v = ((uint32_t) p[i] << 16) | ((uint32_t) p[i + 1] << 8) | p[i + 2];
        char q[4] = {{a[(v >> 18) & 63], a[(v >> 12) & 63], a[(v >> 6) & 63], a[v & 63]}};
        chs_sb_append(sb, q, 4);
    }}
    if (n - i == 1) {{
        uint32_t v = (uint32_t) p[i] << 16;
        char q[4] = {{a[(v >> 18) & 63], a[(v >> 12) & 63], '=', '='}};
        chs_sb_append(sb, q, 4);
    }} else if (n - i == 2) {{
        uint32_t v = ((uint32_t) p[i] << 16) | ((uint32_t) p[i + 1] << 8);
        char q[4] = {{a[(v >> 18) & 63], a[(v >> 12) & 63], a[(v >> 6) & 63], '='}};
        chs_sb_append(sb, q, 4);
    }}
    chs_sb_append(sb, "\\"", 1);
}}

/* The byte_strings rule: "key":"<text>" for valid UTF-8, else "key_b64":"<base64>". */
static void chs_sb_bytes_member(chs_sb *sb, const char *key, const uint8_t *p, size_t n) {{
    chs_sb_append(sb, "\\"", 1);
    chs_sb_cat(sb, key);
    if (chs_stub_utf8_valid(p, n)) {{
        chs_sb_append(sb, "\\":", 2);
        chs_sb_json_utf8(sb, p, n);
    }} else {{
        chs_sb_append(sb, "_b64\\":", 6);
        chs_sb_base64(sb, p, n);
    }}
}}

/* payload ++ suffix, in a buffer the caller frees. */
static uint8_t *chs_stub_concat(const uint8_t *p, size_t n, const char *suffix, size_t sn, size_t *out_n) {{
    uint8_t *b = (uint8_t *) malloc(n + sn + 1);
    if (n) memcpy(b, p, n);
    if (sn) memcpy(b + n, suffix, sn);
    *out_n = n + sn;
    return b;
}}
'''


def _reader_rule_build_info_formats(model) -> str:
    """Generation 2's r2/r3 build_info shapes: unknown members
    (CHS_STUB_R2_MEMBERS) and an unlisted value in every capabilities list
    (CHS_STUB_R3_CAPABILITIES). Empty for ABI v1. Each is a printf format whose
    four conversions are the same as CHS_STUB_BUILD_INFO_FMT's."""
    if model.abi < 2:
        return ""
    import json

    def c_line(text: str) -> str:
        return '    "' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    head = [
        '{"schema":1,"abi":%d,"abi_fingerprint":"%s","clickhouse_version":"26.8.15.10",',
        '"channel":"lts","clickhouse_minor":"26.8","clickhouse_commit":"' + "a" * 40 + '",',
        '"core_commit":"' + "b" * 40 + '","build":"20261001.000000","inputs_sha256":"' + "c" * 64 + '",',
        '"os":"%s","arch":"%s","toolchain":{"cc":"stub"},',
    ]
    r3 = head + ['"capabilities":' + json.dumps(_stubshared.R3_CAPABILITIES, separators=(",", ":")) + "}"]
    r2 = head + [
        '"capabilities":{"input_formats":["JSONEachRow"],"export_formats":["JSONEachRow"],"doc_flags":["values"],',
        '"features":["default_generators","x_future_feature"],"x_future":1,"x_future_obj":{"a":[1]}},',
        '"x_future":1,"x_future_obj":{"a":[1,{"b":null}]},"x_future_null":null}',
    ]
    for lines in (r2, r3):
        json.loads("".join(lines).replace("%d", "2").replace("%s", "x"))  # each is one JSON object
    return (
        "/* r3 (generation 2): an unlisted value in every capabilities list (CHS_STUB_R3_CAPABILITIES). */\n"
        "#define CHS_STUB_BUILD_INFO_FMT_R3 \\\n" + " \\\n".join(c_line(x) for x in r3) + "\n\n"
        "/* r2 (generation 2): members no description names, at the top level and inside\n"
        "   capabilities (CHS_STUB_R2_MEMBERS). */\n"
        "#define CHS_STUB_BUILD_INFO_FMT_R2 \\\n" + " \\\n".join(c_line(x) for x in r2) + "\n\n"
    )


def _reader_rule_build_info_calls(model, fp_ok: str) -> str:
    if model.abi < 2:
        return ""
    return (
        "#  elif defined(CHS_STUB_R2_MEMBERS)\n"
        f"    snprintf(buf, sizeof buf, CHS_STUB_BUILD_INFO_FMT_R2, {model.abi}, {fp_ok}, CHS_STUB_OS, CHS_STUB_ARCH);\n"
        "#  elif defined(CHS_STUB_R3_CAPABILITIES)\n"
        f"    snprintf(buf, sizeof buf, CHS_STUB_BUILD_INFO_FMT_R3, {model.abi}, {fp_ok}, CHS_STUB_OS, CHS_STUB_ARCH);\n"
    )


def _build_info_section(model) -> str:
    """chs_build_info()'s four variant bodies, keyed by CHS_STUB_BUILD_INFO_MODE
    (0 = normal, 1 = NULL, 2 = malformed JSON, 3 = a duplicate key,
    4 = a non-ASCII byte) and CHS_STUB_FINGERPRINT_OTHER. The field values
    other than os/arch/abi_fingerprint are arbitrary but schema-valid
    constants; build-stubs.sh's predicate.json (rendered alongside this file)
    carries the SAME values, so the "ok" variant's build_info and predicate
    agree field for field, and a future loader's step 5 cross-check passes."""
    fp_ok = _c_str(model.fingerprint)
    fp_other = _c_str("sha256:" + "0" * 64)
    return f'''
/* ------------------------------------------------------------------- build_info */

#if defined(__linux__)
#  define CHS_STUB_OS "linux"
#elif defined(__APPLE__)
#  define CHS_STUB_OS "darwin"
#else
#  error "unsupported OS"
#endif
#if defined(__aarch64__) || defined(__arm64__)
#  define CHS_STUB_ARCH "arm64"
#elif defined(__x86_64__) || defined(__amd64__)
#  define CHS_STUB_ARCH "amd64"
#else
#  error "unsupported architecture"
#endif

/* Kept in lockstep with build-stubs.sh's predicate.json by construction: both
   are rendered from the one dict in emit/stub.py's _BUILD_INFO_FIELDS. */
#define CHS_STUB_BUILD_INFO_FMT \\
    "{{\\"schema\\":1,\\"abi\\":%d,\\"abi_fingerprint\\":\\"%s\\",\\"clickhouse_version\\":\\"26.8.15.10\\"," \\
    "\\"channel\\":\\"lts\\",\\"clickhouse_minor\\":\\"26.8\\",\\"clickhouse_commit\\":\\"{"a" * 40}\\"," \\
    "\\"core_commit\\":\\"{"b" * 40}\\",\\"build\\":\\"20261001.000000\\",\\"inputs_sha256\\":\\"{"c" * 64}\\"," \\
    "\\"os\\":\\"%s\\",\\"arch\\":\\"%s\\",\\"toolchain\\":{{\\"cc\\":\\"stub\\"}}," \\
    "\\"capabilities\\":{{\\"input_formats\\":[\\"JSONEachRow\\"],\\"export_formats\\":[\\"JSONEachRow\\"],\\"doc_flags\\":[\\"values\\"]," \\
    "\\"features\\":[\\"default_generators\\"]}}}}"

{_reader_rule_build_info_formats(model)}{model.function("chs_build_info").prototype().rstrip(";")} {{
    static char buf[{768 if model.abi < 2 else 2048}];
    static int ready = 0;
    if (ready) return buf;
#if defined(CHS_STUB_BUILD_INFO_MODE) && CHS_STUB_BUILD_INFO_MODE == 1
    return NULL;
#elif defined(CHS_STUB_BUILD_INFO_MODE) && CHS_STUB_BUILD_INFO_MODE == 2
    snprintf(buf, sizeof buf, "{{not json");
#elif defined(CHS_STUB_BUILD_INFO_MODE) && CHS_STUB_BUILD_INFO_MODE == 3
    snprintf(buf, sizeof buf, "{{\\"schema\\":1,\\"schema\\":1}}");
#elif defined(CHS_STUB_BUILD_INFO_MODE) && CHS_STUB_BUILD_INFO_MODE == 4
    snprintf(buf, sizeof buf, "{{\\"schema\\":1,\\"non_ascii\\":\\"\\xc3\\x28\\"}}");
#else
#  if defined(CHS_STUB_FINGERPRINT_OTHER)
    snprintf(buf, sizeof buf, CHS_STUB_BUILD_INFO_FMT, {model.abi}, {fp_other}, CHS_STUB_OS, CHS_STUB_ARCH);
{_reader_rule_build_info_calls(model, fp_ok)}#  else
    snprintf(buf, sizeof buf, CHS_STUB_BUILD_INFO_FMT, {model.abi}, {fp_ok}, CHS_STUB_OS, CHS_STUB_ARCH);
#  endif
#endif
    ready = 1;
    return buf;
}}
'''


def _ctor_section() -> str:
    return '''
/* "ctor-marker": a constructor runs only once the dynamic linker has
   FINISHED loading this image, i.e. strictly after dlopen() returns. If a
   loader's glibc check (step 1) correctly refuses before ever calling
   dlopen, this file is never created, whatever this library contains. */
#if defined(CHS_STUB_CTOR_MARKER)
#ifndef CHS_STUB_CTOR_MARKER_PATH
#error "CHS_STUB_CTOR_MARKER needs CHS_STUB_CTOR_MARKER_PATH"
#endif
__attribute__((constructor)) static void chs_stub_ctor_marker(void) {
    FILE *f = fopen(CHS_STUB_CTOR_MARKER_PATH, "w");
    if (f) { fputs("loaded\\n", f); fclose(f); }
}
#endif
'''


def _unbound_section() -> str:
    return '''
/* "unbound": an external symbol that is never defined anywhere. Under
   RTLD_NOW a dlopen() of this library must fail outright; under RTLD_LAZY it
   would succeed, because nothing here calls chs_stub_unbound_external()
   eagerly — only a loader's own startup-time strict-binding probe can tell
   the difference, which is the point of this variant (the TS RTLD_LAZY
   trap). The symbol is merely referenced via a non-static function pointer
   the linker cannot optimize away, so it stays in this library's dynamic
   symbol table requiring resolution at load time. */
#if defined(CHS_STUB_UNBOUND)
extern void chs_stub_unbound_external(void);
void (*volatile chs_stub_unbound_ref)(void) = chs_stub_unbound_external;
#endif
'''


# --------------------------------------------------------- per-function bodies


def _args_echo_code(fn) -> list[str]:
    """C statements that append this function's INPUT parameters (in
    declaration order) to `chs_sb args`, as a JSON array body (no brackets)."""
    lines = []
    first = True
    for p in fn.params:
        if p.is_out:
            continue
        if not first:
            lines.append('    chs_sb_cat(&args, ",");')
        first = False
        if p.kind in ("scalar", "enum"):
            if p.kind == "enum" or p.type in ("int32", "int64"):
                fmt, cast = "%ld", "(long)"
            else:  # uint32, uint64, size
                fmt, cast = "%llu", "(unsigned long long)"
            lines.append(f'    chs_sb_fmt(&args, "{fmt}", {cast}{p.name});')
        elif p.kind == "bytes_in":
            lines.append(f"    chs_sb_bytes_field(&args, {p.name}, {p.name}_len);")
        elif p.kind == "handle":
            lines.append(
                f"    if ({p.name} == NULL) chs_sb_cat(&args, \"null\"); "
                f"else chs_sb_handle_field(&args, (const chs_handle_common *) {p.name});"
            )
    return lines


def _input_checks(model, fn) -> list[str]:
    lines = []
    for p in fn.params:
        if p.is_out:
            continue
        if p.kind == "handle":
            kind_const = _kind_const(p.type)
            cast = f"(const chs_handle_common *) {p.name}"
            if p.nullable:
                lines.append(f"    if ({p.name} != NULL) {{")
                indent = "        "
                closers = ["    }"]
            else:
                lines.append(f"    if ({p.name} == NULL) {{")
                lines += _c_fixed_error("CHS_INVALID_ARGUMENT", f"{p.name}: required", indent="        ")
                lines.append("    }")
                lines.append("    {")
                indent = "        "
                closers = ["    }"]
            lines.append(f"{indent}const char *problem = chs_stub_check_handle({cast}, {kind_const}, {_c_str(p.name)});")
            lines.append(f"{indent}if (problem != NULL) {{")
            lines.append(
                f"{indent}    chs_stub_set_err(err, chs_stub_make_error(CHS_INVALID_ARGUMENT, 0, \"\", 0, "
                "problem, strlen(problem)));"
            )
            lines.append(f"{indent}    return CHS_INVALID_ARGUMENT;")
            lines.append(f"{indent}}}")
            lines += closers
        elif p.kind == "bytes_in":
            lines.append(f"    if ({p.name} == NULL && {p.name}_len > 0) {{")
            lines += _c_fixed_error("CHS_INVALID_ARGUMENT", f"{p.name}: NULL with a nonzero length", indent="        ")
            lines.append("    }")
    return lines


def _input_document_scanner(model) -> str:
    """The C reader of a closed input document's top-level keys
    (_stubshared.INPUT_DOCUMENT). Emitted only when the description has an
    input document (generation 2 on), so ABI v1's stub is unchanged."""
    if not model.inputs:
        return ""
    return """
/* ------------------------------------------------------ closed input documents
   (generation 2, rule r1: scripts/abi-v1/emit/_stubshared.py INPUT_DOCUMENT).
   A test double: only the top-level keys are read, a key is compared as raw
   bytes (no escape decoded), and a value is skipped by tracking strings and
   nesting, never validated. */

static size_t chs_stub_json_ws(const uint8_t *p, size_t n, size_t i) {
    while (i < n && (p[i] == ' ' || p[i] == '\\t' || p[i] == '\\n' || p[i] == '\\r')) i++;
    return i;
}

/* Past the closing quote of the JSON string that opens at p[i], or 0. */
static size_t chs_stub_json_string_end(const uint8_t *p, size_t n, size_t i) {
    for (i++; i < n; i++) {
        if (p[i] == '\\\\') { i++; continue; }
        if (p[i] == '"') return i + 1;
    }
    return 0;
}

/* Past the JSON value that starts at p[i] (strings and nesting tracked), or 0. */
static size_t chs_stub_json_value_end(const uint8_t *p, size_t n, size_t i) {
    int depth = 0;
    while (i < n) {
        uint8_t c = p[i];
        if (c == '"') {
            size_t e = chs_stub_json_string_end(p, n, i);
            if (e == 0) return 0;
            i = e;
            if (depth == 0) return i;
            continue;
        }
        if (c == '{' || c == '[') { depth++; i++; continue; }
        if (c == '}' || c == ']') {
            if (depth == 0) return i;
            depth--; i++;
            if (depth == 0) return i;
            continue;
        }
        if (depth == 0 && c == ',') return i;
        i++;
    }
    return depth == 0 ? i : 0;
}

/* 0 when p[0:n) is a JSON object (length 0 counts as `{}`) whose every
   top-level key is one of allowed[0:na). Otherwise 1, with *key / *key_len
   the first key that is not, or *key NULL when the document is not an object. */
static int chs_stub_closed_keys(const uint8_t *p, size_t n, const char *const *allowed, size_t na,
                                const uint8_t **key, size_t *key_len) {
    *key = NULL;
    *key_len = 0;
    if (n == 0) return 0;
    size_t i = chs_stub_json_ws(p, n, 0);
    if (i >= n || p[i] != '{') return 1;
    i = chs_stub_json_ws(p, n, i + 1);
    if (i < n && p[i] == '}') return chs_stub_json_ws(p, n, i + 1) == n ? 0 : 1;
    for (;;) {
        if (i >= n || p[i] != '"') return 1;
        size_t e = chs_stub_json_string_end(p, n, i);
        if (e == 0) return 1;
        const uint8_t *k = p + i + 1;
        size_t kn = e - i - 2;
        int known = 0;
        for (size_t a = 0; a < na; a++)
            if (strlen(allowed[a]) == kn && memcmp(allowed[a], k, kn) == 0) known = 1;
        if (!known) {
            *key = k;
            *key_len = kn;
            return 1;
        }
        i = chs_stub_json_ws(p, n, e);
        if (i >= n || p[i] != ':') return 1;
        i = chs_stub_json_ws(p, n, i + 1);
        size_t v = chs_stub_json_value_end(p, n, i);
        if (v == 0 || v == i) return 1;
        i = chs_stub_json_ws(p, n, v);
        if (i < n && p[i] == ',') { i = chs_stub_json_ws(p, n, i + 1); continue; }
        if (i < n && p[i] == '}') return chs_stub_json_ws(p, n, i + 1) == n ? 0 : 1;
        return 1;
    }
}
"""


def _input_documents(model, fn) -> list[str]:
    """Rule r1's stand-in (_stubshared.INPUT_DOCUMENT): every bytes_in
    parameter that carries an input document refuses a document that is not a
    JSON object, or a top-level key its schema does not list, naming the key.
    After the input checks and the status injection."""
    rule = _stubshared.INPUT_DOCUMENT
    lines: list[str] = []
    for p in fn.params:
        d = model.input_of(p)
        if d is None:
            continue
        keys = list(d.schema.get("properties", {}))
        prefix, _, suffix = _stubshared.input_document_message(p.name, d.name, "\0").partition("\0")
        allowed = (
            f"        static const char *const allowed[] = {{{', '.join(_c_str(k) for k in keys)}}};"
            if keys
            else "        static const char *const *const allowed = NULL;"
        )
        lines += [
            "    {",
            f"        /* rule r1's closed input document {d.name}: see scripts/abi-v1/emit/_stubshared.py INPUT_DOCUMENT */",
            allowed,
            "        const uint8_t *key; size_t key_len;",
            f"        if (chs_stub_closed_keys({p.name}, {p.name}_len, allowed, {len(keys)}, &key, &key_len)) {{",
            "            chs_sb m; chs_sb_init(&m);",
            "            if (key != NULL) {",
            f"                chs_sb_cat(&m, {_c_str(prefix)});",
            "                chs_sb_append(&m, key, key_len);",
            f"                chs_sb_cat(&m, {_c_str(suffix)});",
            "            } else {",
            f"                chs_sb_cat(&m, {_c_str(_stubshared.input_document_message(p.name, d.name, None))});",
            "            }",
            f"            chs_stub_set_err(err, chs_stub_make_error({rule['status']}, 0, \"\", 0, m.buf, m.len));",
            "            free(m.buf);",
            f"            return {rule['status']};",
            "        }",
            "    }",
        ]
    return lines


def _status_injection(fn) -> list[str]:
    first_bytes = next((p for p in fn.params if p.kind == "bytes_in"), None)
    if first_bytes is None or fn.returns.kind != "status":
        return []
    return [
        "    {",
        "        chs_status inj_status; chs_error *inj_err = NULL;",
        f"        if (chs_stub_try_inject({first_bytes.name}, {first_bytes.name}_len, &inj_status, &inj_err)) {{",
        "            chs_stub_set_err(err, inj_err);",
        "            return inj_status;",
        "        }",
        "    }",
    ]


def _one_create(fn) -> list[str]:
    """The stub's stand-in for the one-CREATE rule (_stubshared.ONE_CREATE):
    after the input checks and any status injection, a `;` followed by
    anything but ASCII whitespace or another `;` is refused with the rule's
    status and error. Only the function the rule names gets it."""
    rule = _stubshared.ONE_CREATE
    if fn.name != rule["fn"]:
        return []
    p = rule["param"]
    if not any(q.name == p and q.kind == "bytes_in" for q in fn.params):
        raise ValueError(f"{fn.name}: _stubshared.ONE_CREATE names {p!r}, which is not one of its bytes_in parameters")
    name_lit = _c_str(rule["ch_name"])
    msg_lit = _c_str(rule["message"])
    return [
        "    {",
        "        /* the one-CREATE rule's stand-in: see scripts/abi-v1/emit/_stubshared.py ONE_CREATE */",
        "        size_t i = 0;",
        f"        while (i < {p}_len && {p}[i] != ';') i++;",
        f"        while (i < {p}_len && ({p}[i] == ';' || {p}[i] == ' ' || {p}[i] == '\\t' || {p}[i] == '\\n' || {p}[i] == '\\r')) i++;",
        f"        if (i < {p}_len) {{",
        f"            static const char name[] = {name_lit};",
        f"            static const char msg[] = {msg_lit};",
        f"            chs_stub_set_err(err, chs_stub_make_error({rule['status']}, {rule['ch_code']}, name, sizeof(name) - 1, msg, sizeof(msg) - 1));",
        f"            return {rule['status']};",
        "        }",
        "    }",
    ]


def _image_zone_state() -> str:
    """The image zone's state, file scope: chs_initialize's process-once rule
    (_image_zone) writes it, and the zone probe (_zone_probe) reads it. Static
    storage, so each dlopen'd copy of the stub (each image) has its own."""
    return """
/* ------------------------------------------------------------- the image zone
   (scripts/abi-v1/emit/_stubshared.py IMAGE_ZONE and ZONE_PROBE) */

static atomic_flag chs_stub_zone_lock = ATOMIC_FLAG_INIT;
static uint8_t *chs_stub_zone = NULL;
static size_t chs_stub_zone_len = 0;
static int chs_stub_zone_set = 0;

static void chs_stub_zone_acquire(void) {
    while (atomic_flag_test_and_set(&chs_stub_zone_lock)) { /* spin: the critical sections are a few instructions */ }
}

static void chs_stub_zone_release(void) {
    atomic_flag_clear(&chs_stub_zone_lock);
}
"""


def _image_zone(fn) -> list[str]:
    """The stub's stand-in for the image zone's process-once rule
    (_stubshared.IMAGE_ZONE): after the input checks and any status
    injection, the sentinel bad zone is refused and commits nothing; the
    first other spelling is committed; after that, the same spelling is
    accepted and a different one is refused. Only the function the rule names
    gets it."""
    rule = _stubshared.IMAGE_ZONE
    if fn.name != rule["fn"]:
        return []
    p = rule["param"]
    if not any(q.name == p and q.kind == "bytes_in" for q in fn.params):
        raise ValueError(f"{fn.name}: _stubshared.IMAGE_ZONE names {p!r}, which is not one of its bytes_in parameters")
    return [
        "    {",
        "        /* the image zone's process-once rule: see scripts/abi-v1/emit/_stubshared.py IMAGE_ZONE */",
        f"        static const char bad[] = {_c_str(rule['bad_zone'])};",
        f"        if ({p}_len == sizeof(bad) - 1 && memcmp({p}, bad, sizeof(bad) - 1) == 0) {{",
        f"            static const char name[] = {_c_str(rule['ch_name'])};",
        f"            static const char msg[] = {_c_str(rule['message'])};",
        f"            chs_stub_set_err(err, chs_stub_make_error({rule['status']}, {rule['ch_code']}, name, sizeof(name) - 1, msg, sizeof(msg) - 1));",
        f"            return {rule['status']};",
        "        }",
        "        int conflict = 0;",
        "        chs_stub_zone_acquire();",
        "        if (!chs_stub_zone_set) {",
        f"            chs_stub_zone = {p}_len ? (uint8_t *) malloc({p}_len) : NULL;",
        f"            if (chs_stub_zone) memcpy(chs_stub_zone, {p}, {p}_len);",
        f"            chs_stub_zone_len = {p}_len;",
        "            chs_stub_zone_set = 1;",
        f"        }} else if (chs_stub_zone_len != {p}_len || ({p}_len > 0 && memcmp(chs_stub_zone, {p}, {p}_len) != 0)) {{",
        "            conflict = 1;",
        "        }",
        "        chs_stub_zone_release();",
        "        if (conflict) {",
        f"            static const char msg[] = {_c_str(rule['conflict_message'])};",
        f"            chs_stub_set_err(err, chs_stub_make_error({rule['conflict_status']}, 0, \"\", 0, msg, sizeof(msg) - 1));",
        f"            return {rule['conflict_status']};",
        "        }",
        "    }",
    ]


def _zone_probe(fn) -> list[str]:
    """The image zone probe (_stubshared.ZONE_PROBE): when the named parameter
    is exactly the probe, the call returns the zone this image holds (empty
    when none was committed) in its output, instead of the echo. Only the
    function the probe names gets it."""
    rule = _stubshared.ZONE_PROBE
    if fn.name != rule["fn"]:
        return []
    p, out = rule["param"], rule["out"]
    if not any(q.name == p and q.kind == "bytes_in" for q in fn.params):
        raise ValueError(f"{fn.name}: _stubshared.ZONE_PROBE names {p!r}, which is not one of its bytes_in parameters")
    if not any(q.name == out and q.kind == "out_handle" and q.type == BUF_HANDLE and not q.nullable for q in fn.params):
        raise ValueError(f"{fn.name}: _stubshared.ZONE_PROBE names {out!r}, which is not its required chs_buf output")
    return [
        "    {",
        "        /* the image zone probe: see scripts/abi-v1/emit/_stubshared.py ZONE_PROBE */",
        f"        static const char probe[] = {_c_str(rule['probe'])};",
        f"        if ({p}_len == sizeof(probe) - 1 && memcmp({p}, probe, sizeof(probe) - 1) == 0) {{",
        f"            if ({out} == NULL) {{",
        *_c_fixed_error("CHS_INVALID_ARGUMENT", f"{out}: required", indent="                "),
        "            }",
        "            chs_stub_zone_acquire();",
        "            size_t n = chs_stub_zone_len;",
        "            uint8_t *copy = (uint8_t *) malloc(n + 1);",
        "            if (n) memcpy(copy, chs_stub_zone, n);",
        "            chs_stub_zone_release();",
        f"            *{out} = chs_stub_make_buf(copy, n);",
        "            chs_stub_set_err(err, NULL);",
        "            return CHS_OK;",
        "        }",
        "    }",
    ]


def _c_bytes_lit(data: bytes) -> str:
    """A C string literal for arbitrary bytes, every byte octal-escaped."""
    return '"' + "".join(f"\\{b:03o}" for b in data) + '"'


def _document_mode(model, fn) -> list[str]:
    """Document mode (_stubshared.DOC_FUNCTIONS / DOC_TEMPLATES): when the
    named parameter begins the prefix, the call returns the template's
    document in its `out` instead of the echo."""
    entry = _stubshared.DOC_FUNCTIONS.get(fn.name)
    if entry is None:
        return []
    param, template = entry
    if not any(q.name == param and q.kind == "bytes_in" for q in fn.params):
        raise ValueError(f"{fn.name}: DOC_FUNCTIONS names {param!r}, which is not one of its bytes_in parameters")
    outs = [q for q in fn.params if q.kind == "out_handle" and q.type == BUF_HANDLE and not q.nullable]
    if len(outs) != 1:
        raise ValueError(f"{fn.name}: document mode needs exactly one required chs_buf output")
    out = outs[0].name
    pre = _stubshared.DOC_PREFIX
    lines = [
        "    {",
        "        /* document mode: see scripts/abi-v1/emit/_stubshared.py DOC_TEMPLATES */",
        f"        static const char pfx[] = {_c_bytes_lit(pre)};",
        f"        if ({param} != NULL && {param}_len >= sizeof(pfx) - 1 && memcmp({param}, pfx, sizeof(pfx) - 1) == 0) {{",
        f"            if ({out} == NULL) {{",
        *_c_fixed_error("CHS_INVALID_ARGUMENT", f"{out}: required", indent="                "),
        "            }",
        f"            const uint8_t *pl = {param} + (sizeof(pfx) - 1);",
        f"            size_t pln = {param}_len - (sizeof(pfx) - 1);",
        "            (void) pl; (void) pln;",
        "            chs_sb sb; chs_sb_init(&sb);",
    ]

    def src_code(src) -> tuple[list[str], str, str, bool]:
        """(setup statements, pointer expr, length expr, needs free)."""
        if src[0] == "lit":
            return [], f"(const uint8_t *) {_c_bytes_lit(src[1])}", str(len(src[1])), False
        if not src[1]:
            return [], "pl", "pln", False
        return (
            [f"            size_t tn; uint8_t *tb = chs_stub_concat(pl, pln, {_c_bytes_lit(src[1])}, {len(src[1])}, &tn);"],
            "tb",
            "tn",
            True,
        )

    for step in _stubshared.DOC_TEMPLATES[template]:
        op = step[0]
        if op == "raw":
            lines.append(f"            chs_sb_cat(&sb, {_c_str(step[1])});")
        elif op == "len":
            lines.append(f'            chs_sb_fmt(&sb, "%zu", {param}_len);')
        elif op in ("member", "b64"):
            src = step[2] if op == "member" else step[1]
            setup, ptr, n, needs_free = src_code(src)
            lines.append("            {")
            lines += setup
            if op == "member":
                lines.append(f"            chs_sb_bytes_member(&sb, {_c_str(step[1])}, {ptr}, {n});")
            else:
                lines.append(f"            chs_sb_base64(&sb, {ptr}, {n});")
            if needs_free:
                lines.append("            free(tb);")
            lines.append("            }")
        else:
            raise ValueError(f"DOC_TEMPLATES: unknown step {op!r}")
    lines += [
        f"            *{out} = chs_stub_finish_buf(&sb);",
        "            chs_stub_set_err(err, NULL);",
        "            return CHS_OK;",
        "        }",
        "    }",
    ]
    return lines


def _fill_outputs(model, fn) -> list[str]:
    """Fill every out_handle. `p.nullable` here means "the pointer-TO-pointer
    itself may be NULL" (the caller does not want this output); a required
    one that IS NULL is CHS_INVALID_ARGUMENT naming the parameter, checked
    before anything dereferences it — a NULL check the generic template must
    never skip, however it then fills the handle."""
    lines = []
    const_handle_params = [p for p in fn.params if p.kind == "handle"]
    for p in fn.params:
        if p.kind != "out_handle":
            continue
        if not p.nullable:
            lines.append(f"    if ({p.name} == NULL) {{")
            lines += _c_fixed_error("CHS_INVALID_ARGUMENT", f"{p.name}: required", indent="        ")
            lines.append("    }")
        if p.type == BUF_HANDLE:
            body = []
            if p.nullable:
                body.append(f"    if ({p.name} != NULL) {{")
                indent = "        "
            else:
                indent = "    "
            body += [
                f"{indent}chs_sb args; chs_sb_init(&args);",
                *[f"{indent}{line}" for line in _args_echo_code(fn)],
                f"{indent}chs_sb out_json; chs_sb_init(&out_json);",
                # Built by direct concatenation (chs_sb_cat), NEVER chs_sb_fmt:
                # chs_sb_fmt formats through a fixed char tmp[256] (vsnprintf),
                # and args.buf is unbounded (chs_preview_row/chs_preview_batch/
                # chs_filter_eval_body's echoed bytes_in arguments alone exceed
                # 256 bytes), so a %s of it there would silently truncate the
                # echo. fn/out are always short compile-time literals, safe to
                # embed directly.
                f'{indent}chs_sb_cat(&out_json, "{{\\"args\\":[");',
                f"{indent}chs_sb_cat(&out_json, args.buf);",
                f'{indent}chs_sb_cat(&out_json, "],\\"fn\\":\\"{fn.name}\\",\\"out\\":\\"{p.name}\\"}}");',
                f"{indent}free(args.buf);",
                f"{indent}*{p.name} = chs_stub_finish_buf(&out_json);",
            ]
            if p.nullable:
                body.append("    }")
            lines += body
        else:
            held_kinds = model.handles[p.type].holds
            parents = [hp for hp in const_handle_params if hp.type in held_kinds]
            if len(parents) != len(held_kinds):
                raise ValueError(
                    f"{fn.name}: out_handle {p.name} mints a {p.type}, which holds {held_kinds}, but its "
                    f"input handle parameters do not supply exactly one of each kind (found "
                    f"{[hp.type for hp in parents]}); emit/stub.py's generic template cannot guess which "
                    "input is the parent, so this function needs a _special entry"
                )
            kind_const = _kind_const(p.type)
            body = []
            indent = "    "
            if p.nullable:
                body.append(f"    if ({p.name} != NULL) {{")
                indent = "        "
            body += [
                f"{indent}{p.type} *nh = ({p.type} *) malloc(sizeof({p.type}));",
                f"{indent}chs_stub_common_init((chs_handle_common *) nh, {kind_const});",
                *[
                    (f"{indent}if ({hp.name} != NULL) " if hp.nullable else indent)
                    + f"chs_stub_hold((chs_handle_common *) nh, (chs_handle_common *) {hp.name});"
                    for hp in parents
                ],
                f"{indent}*{p.name} = nh;",
            ]
            if p.nullable:
                body.append("    }")
            lines += body
    return lines


def _doc_out(fn):
    """The one required `document:<kind>` chs_buf output of fn, and every
    other chs_buf output, or (None, [])."""
    outs = [q for q in fn.params if q.kind == "out_handle" and q.type == BUF_HANDLE]
    docs = [q for q in outs if q.content and q.content.startswith("document:")]
    if len(docs) != 1:
        return None, []
    return docs[0], [q for q in outs if q is not docs[0]]


def _answer_doc(out, others, doc_expr: str) -> list[str]:
    """Fill `out` with the C string `doc_expr`, and every other chs_buf output
    a caller asked for with _stubshared.R2_EXPORT; then succeed."""
    lines = [
        f"        if ({out.name} == NULL) {{",
        *_c_fixed_error("CHS_INVALID_ARGUMENT", f"{out.name}: required", indent="            "),
        "        }",
    ]
    for q in others:
        lines += [
            f"        if ({q.name} != NULL) {{",
            f"            static const char rule_export[] = {_c_bytes_lit(_stubshared.R2_EXPORT)};",
            "            chs_sb xb; chs_sb_init(&xb);",
            "            chs_sb_cat(&xb, rule_export);",
            f"            *{q.name} = chs_stub_finish_buf(&xb);",
            "        }",
        ]
    lines += [
        "        chs_sb sb; chs_sb_init(&sb);",
        f"        chs_sb_cat(&sb, {doc_expr});",
        f"        *{out.name} = chs_stub_finish_buf(&sb);",
        "        chs_stub_set_err(err, NULL);",
        "        return CHS_OK;",
    ]
    return lines


def _r2_members(model, fn) -> list[str]:
    """Generation 2, rule r2: under CHS_STUB_R2_MEMBERS a call whose required
    output is a document of a kind in _stubshared.R2_DOCS answers that
    document (members no description names, at every level) instead of its
    echo, after the input checks and the status injection."""
    if model.abi < 2:
        return []
    import json

    out, others = _doc_out(fn)
    if out is None:
        return []
    doc = _stubshared.R2_DOCS.get(out.content[len("document:") :])
    if doc is None:
        return []
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True)
    return [
        "#if defined(CHS_STUB_R2_MEMBERS)",
        "    {",
        f"        static const char rule_doc[] = {_c_str(text)};",
        *_answer_doc(out, others, "rule_doc"),
        "    }",
        "#endif",
    ]


def _r3_values(model, fn) -> list[str]:
    """Generation 2, rule r3: under CHS_STUB_R3_VALUES, see
    _stubshared.R3_MUTATIONS. Emitted after the input checks and the status
    injection."""
    if model.abi < 2:
        return []
    import json

    first_bytes = next((p for p in fn.params if p.kind == "bytes_in"), None)
    if fn.name == "chs_type_validate":
        n = _stubshared.R3_UNKNOWN_STATUS
        msg = "r3: a status outside the closed set"
        return [
            "#if defined(CHS_STUB_R3_VALUES)",
            f'    if ({first_bytes.name}_len == 3 && memcmp({first_bytes.name}, "!U:", 3) == 0) {{',
            f"        chs_stub_set_err(err, chs_stub_make_error((chs_status) {n}, 0, \"\", 0, {_c_str(msg)}, {len(msg)}));",
            f"        return (chs_status) {n};",
            "    }",
            "#endif",
        ]
    if fn.name == "chs_schema_create":
        b = first_bytes.name
        return [
            "#if defined(CHS_STUB_R3_VALUES)",
            "    chs_stub_r3_create_len = 0;",
            f"    if ({b}_len > 3 && {b}_len - 3 <= sizeof chs_stub_r3_create && memcmp({b}, \"!E:\", 3) == 0) {{",
            f"        memcpy(chs_stub_r3_create, {b} + 3, {b}_len - 3);",
            f"        chs_stub_r3_create_len = {b}_len - 3;",
            "    }",
            "#endif",
        ]
    out, others = _doc_out(fn)
    if out is None:
        return []
    kind = out.content[len("document:") :]
    base = _stubshared.R3_BASE.get(kind)
    if base is None:
        return []
    muts = [(mid, _stubshared.r3_doc(mid)) for mid, (k, _, _) in _stubshared.R3_MUTATIONS.items() if k == kind]
    if first_bytes is not None:
        key_check = f'{first_bytes.name}_len > 3 && memcmp({first_bytes.name}, "!E:", 3) == 0'
        key_ptr, key_len = f"{first_bytes.name} + 3", f"{first_bytes.name}_len - 3"
    else:
        key_check = "chs_stub_r3_create_len > 0"
        key_ptr, key_len = "(const uint8_t *) chs_stub_r3_create", "chs_stub_r3_create_len"
    lines = [
        "#if defined(CHS_STUB_R3_VALUES)",
        "    {",
        f"        const char *rule_doc = {_c_str(json.dumps(base, separators=(',', ':')))};",
        f"        if ({key_check}) {{",
        f"            const uint8_t *k = {key_ptr}; size_t kn = {key_len};",
    ]
    for mid, doc in muts:
        lines += [
            f"            if (kn == {len(mid)} && memcmp(k, {_c_str(mid)}, {len(mid)}) == 0)",
            f"                rule_doc = {_c_str(json.dumps(doc, separators=(',', ':')))};",
        ]
    lines += ["        }", *_answer_doc(out, others, "rule_doc"), "    }", "#endif"]
    return lines


def _gen_generic(model, fn) -> str:
    sig = fn.prototype().rstrip(";")
    body = [sig + " {"]
    # A parameter this function's shape does not otherwise reference (an enum
    # or scalar with no validation and no out_handle to feed) would warn
    # under -Wunused-parameter; this silences it uniformly rather than
    # tracking which functions happen to use which parameters.
    body += [f"    (void) {cp.name};" for cp in fn.c_params]
    body += _input_checks(model, fn)
    body += _status_injection(fn)
    body += _input_documents(model, fn)
    body += _one_create(fn)
    body += _image_zone(fn)
    body += _zone_probe(fn)
    body += _document_mode(model, fn)
    body += _r2_members(model, fn)
    body += _r3_values(model, fn)
    body += _fill_outputs(model, fn)
    err_param = next((p for p in fn.params if p.kind == "out_error"), None)
    if err_param is not None:
        body.append(f"    chs_stub_set_err({err_param.name}, NULL);")
        body.append("    return CHS_OK;")
    else:
        body.append("    return;")
    body.append("}")
    return "\n".join(body)


def _gen_free(fn) -> str:
    sig = fn.prototype().rstrip(";")
    p = fn.params[0]
    return "\n".join(
        [
            sig + " {",
            f"    if ({p.name} == NULL) return;",
            f"    chs_stub_release((chs_handle_common *) {p.name});",
            "}",
        ]
    )


def _special(model) -> dict[str, str]:
    def sig(name: str) -> str:
        return model.function(name).prototype().rstrip(";")

    s: dict[str, str] = {}
    s["chs_abi_version"] = f"{sig('chs_abi_version')} {{\n    return CHS_ABI_VERSION;\n}}"
    s["chs_clickhouse_version"] = f'{sig("chs_clickhouse_version")} {{\n    return "26.8.15.10-lts";\n}}'
    s["chs_abi_revision"] = f"{sig('chs_abi_revision')} {{\n    return CHS_ABI_REVISION_TOMBSTONE;\n}}"
    # Not a generic echo: this is the one function whose whole job is to
    # report real state (the five live-kind counters), which is exactly what
    # a zero-live-after-close conformance case reads.
    s["chs_live_handles"] = "\n".join(
        [
            f"{sig('chs_live_handles')} {{",
            "    if (out == NULL) {",
            *_c_fixed_error("CHS_INVALID_ARGUMENT", "out: required", indent="        "),
            "    }",
            "    chs_sb sb; chs_sb_init(&sb);",
            "    chs_sb_cat(&sb, \"{\");",
            "    for (int i = 0; i < CHS_STUB_KIND_COUNT; i++) {",
            '        if (i) chs_sb_cat(&sb, ",");',
            '        chs_sb_fmt(&sb, "\\"%s\\":%lld", CHS_STUB_KIND_NAMES[i], (long long) atomic_load(&chs_stub_live[i]));',
            "    }",
            *(
                ["#if defined(CHS_STUB_R2_MEMBERS)", '    chs_sb_cat(&sb, ",\\"x_future\\":0");', "#endif"]
                if model.abi >= 2
                else []
            ),
            '    chs_sb_cat(&sb, "}");',
            "    *out = chs_stub_finish_buf(&sb);",
            "    chs_stub_set_err(err, NULL);",
            "    return CHS_OK;",
            "}",
        ]
    )
    s["chs_buf_data"] = "\n".join(
        [
            f"{sig('chs_buf_data')} {{",
            f"    if (buf == NULL || chs_stub_check_handle((const chs_handle_common *) buf, {_kind_const(BUF_HANDLE)}, "
            '"buf") != NULL) return NULL;',
            "    return buf->data;",
            "}",
        ]
    )
    s["chs_buf_len"] = "\n".join(
        [
            f"{sig('chs_buf_len')} {{",
            f"    if (buf == NULL || chs_stub_check_handle((const chs_handle_common *) buf, {_kind_const(BUF_HANDLE)}, "
            '"buf") != NULL) return 0;',
            "    return buf->len;",
            "}",
        ]
    )
    s["chs_error_status"] = "\n".join(
        [
            f"{sig('chs_error_status')} {{",
            "    if (error == NULL || chs_stub_check_handle((const chs_handle_common *) error, "
            f'{_kind_const(ERROR_HANDLE)}, "error") != NULL) return CHS_INVALID_ARGUMENT;',
            "    return error->status;",
            "}",
        ]
    )
    s["chs_error_ch_code"] = "\n".join(
        [
            f"{sig('chs_error_ch_code')} {{",
            "    if (error == NULL || chs_stub_check_handle((const chs_handle_common *) error, "
            f'{_kind_const(ERROR_HANDLE)}, "error") != NULL) return 0;',
            "    return error->ch_code;",
            "}",
        ]
    )
    for name, field_ptr, field_len in (
        ("chs_error_ch_name", "ch_name", "ch_name_len"),
        ("chs_error_message", "message", "message_len"),
        ("chs_error_column", "column", "column_len"),
    ):
        s[name] = "\n".join(
            [
                f"{sig(name)} {{",
                "    if (error == NULL || chs_stub_check_handle((const chs_handle_common *) error, "
                f'{_kind_const(ERROR_HANDLE)}, "error") != NULL) return NULL;',
                f"    uint8_t *copy = error->{field_len} ? (uint8_t *) malloc(error->{field_len}) : NULL;",
                f"    if (copy) memcpy(copy, error->{field_ptr}, error->{field_len});",
                f"    return chs_stub_make_buf(copy, error->{field_len});",
                "}",
            ]
        )
    return s


def is_free_like(fn) -> bool:
    """True for the structural free-function shape (chs_X_free, and
    chs_shutdown's no-param case): void return, at most one plain (non-const)
    handle parameter. Exported so emit/cases.py's classification of "which
    functions echo their inputs" can never drift from what render_stub_c
    actually emits for each function."""
    return fn.returns.kind == "void" and len(fn.params) <= 1 and (
        not fn.params or (fn.params[0].kind == "handle" and not fn.params[0].const)
    )


def classify(model) -> dict[str, str]:
    """name -> "special" | "free" | "generic" for every described function
    (chs_build_info is reported as "special" too: it is unconditionally
    hand-written, just not inside the `_special()` dict because its body
    also has to switch on CHS_STUB_BUILD_INFO_MODE). Only "generic" functions
    implement the echo/status-injection behavior emit/cases.py's echo and
    status cases assume."""
    specials = _special(model)
    out: dict[str, str] = {}
    for fn in model.functions:
        if fn.name == "chs_build_info" or fn.name in specials:
            out[fn.name] = "special"
        elif is_free_like(fn):
            out[fn.name] = "free"
        else:
            out[fn.name] = "generic"
    return out


def render_stub_c(model) -> str:
    bi_begin, bi_end = _omit_guard("chs_build_info")
    parts = [
        _preamble(model),
        f"{bi_begin}\n{_build_info_section(model)}\n{bi_end}",
        _ctor_section(),
        _unbound_section(),
        _image_zone_state(),
        _input_document_scanner(model),
    ]
    # chs_build_info and chs_abi_version need their own omit/override handling,
    # woven around the hand-written and special bodies below.
    specials = _special(model)
    abi_version_body = (
        "#if defined(CHS_STUB_ABI_VERSION_OVERRIDE)\n"
        f"{model.function('chs_abi_version').prototype().rstrip(';')} {{\n"
        "    return CHS_STUB_ABI_VERSION_OVERRIDE;\n"
        "}\n"
        "#else\n" + specials["chs_abi_version"] + "\n#endif"
    )
    kinds = classify(model)
    body_by_name: dict[str, str] = {}
    for fn in model.functions:
        if fn.name == "chs_abi_version":
            body_by_name[fn.name] = abi_version_body
            continue
        if fn.name == "chs_build_info":
            continue  # already emitted above, inside its own omit guard
        if kinds[fn.name] == "special":
            body_by_name[fn.name] = specials[fn.name]
        elif kinds[fn.name] == "free":
            body_by_name[fn.name] = _gen_free(fn) if fn.params else f"{fn.prototype().rstrip(';')} {{\n}}"
        else:
            body_by_name[fn.name] = _gen_generic(model, fn)

    out = parts
    if model.abi >= 2:
        out.append(
            "\n/* Generation 2, rule r3: the mutation chs_schema_describe answers (CHS_STUB_R3_VALUES). */\n"
            "#if defined(CHS_STUB_R3_VALUES)\n"
            "static char chs_stub_r3_create[128];\n"
            "static size_t chs_stub_r3_create_len = 0;\n"
            "#endif\n"
        )
    out.append("\n/* ----------------------------------------------------------- functions */\n")
    out.append(
        "/* chs_build_info is defined above (inside its own omit guard, alongside its\n"
        "   CHS_STUB_BUILD_INFO_MODE malformation variants and the missing-chs_build_info\n"
        "   variant that omits it entirely), not down here with the rest. */"
    )
    for fn in model.functions:
        if fn.name == "chs_build_info":
            continue
        begin, end = _omit_guard(fn.name)
        out.append(f"\n{begin}\n{body_by_name[fn.name]}\n{end}")
    return "\n".join(out) + "\n"


_PREDICATE_FIELDS = {
    "schema": 1,
    "clickhouse_version": "26.8.15.10",
    "channel": "lts",
    "clickhouse_minor": "26.8",
    "clickhouse_commit": "a" * 40,
    "core_commit": "b" * 40,
    "build": "20261001.000000",
    "inputs_sha256": "c" * 64,
    "os": "__OS__",
    "arch": "__ARCH__",
}


def render_predicate_json(model) -> str:
    """The predicate build-stubs.sh uses for every variant except
    ctor-marker (which overrides glibc_floor): field for field, the values
    chs_build_info() in the normal ("ok"/"ok-b") build reports. __OS__ and
    __ARCH__ are substituted by build-stubs.sh for the host it is running
    on, with the identical linux/darwin and amd64/arm64 spelling the stub's
    own `#if defined(__linux__)`-style preprocessor branches pick."""
    import json

    fields = dict(_PREDICATE_FIELDS)
    fields["abi"] = model.abi
    fields["abi_fingerprint"] = model.fingerprint
    return json.dumps(fields, indent=2, sort_keys=True) + "\n"


def render_files(model) -> dict[str, str]:
    return {"stub.c": render_stub_c(model), "predicate.json": render_predicate_json(model)}
