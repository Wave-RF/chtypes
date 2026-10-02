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
  * `chs_live_handles` reports this image's own live count per handle kind
    from five atomic counters (incremented on mint, decremented the instant
    a handle's refcount reaches zero), never by walking a registry.

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
'''


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
    "\\"capabilities\\":{{\\"input_formats\\":[\\"JSONEachRow\\"],\\"export_formats\\":[\\"JSONEachRow\\"],\\"doc_flags\\":[\\"values\\"]}}}}"

{model.function("chs_build_info").prototype().rstrip(";")} {{
    static char buf[768];
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
#  else
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
                *[f"{indent}chs_stub_hold((chs_handle_common *) nh, (chs_handle_common *) {hp.name});" for hp in parents],
                f"{indent}*{p.name} = nh;",
            ]
            if p.nullable:
                body.append("    }")
            lines += body
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
    parts = [_preamble(model), _build_info_section(model), _ctor_section(), _unbound_section()]
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
            continue  # already emitted in _build_info_section, unconditionally present
        if kinds[fn.name] == "special":
            body_by_name[fn.name] = specials[fn.name]
        elif kinds[fn.name] == "free":
            body_by_name[fn.name] = _gen_free(fn) if fn.params else f"{fn.prototype().rstrip(';')} {{\n}}"
        else:
            body_by_name[fn.name] = _gen_generic(model, fn)

    out = parts
    out.append("\n/* ----------------------------------------------------------- functions */\n")
    out.append(
        "/* chs_build_info is defined unconditionally above (its own CHS_STUB_BUILD_INFO_MODE\n"
        "   variants cover its malformations; it has no missing-symbol variant worth a separate\n"
        "   build since every other variant already proves step 4 runs on whatever it returns). */"
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
