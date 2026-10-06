/* Positive control for the ABI-additions tolerance probe: dlopen a stub variant
   and print every document it answers. Scratch only; never committed. */
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include "chtypes.h"

#define SYM(h, name) __typeof__(&name) p_##name = (__typeof__(&name)) dlsym(h, #name)
#define B(s) (const uint8_t *) (s), strlen(s)

static void *h;

static void show(const char *what, chs_buf *b) {
    SYM(h, chs_buf_data); SYM(h, chs_buf_len);
    if (b == NULL) { printf("%s: (null)\n", what); return; }
    printf("%s: %.*s\n", what, (int) p_chs_buf_len(b), (const char *) p_chs_buf_data(b));
}

int main(int argc, char **argv) {
    h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!h) { printf("dlopen: %s\n", dlerror()); return 1; }
    printf("dlsym chs_abi_level: %s\n", dlsym(h, "chs_abi_level") ? "present" : "absent");
    printf("dlsym chs_abi_additions: %s\n", dlsym(h, "chs_abi_additions") ? "present" : "absent");
    SYM(h, chs_build_info); SYM(h, chs_initialize); SYM(h, chs_live_handles); SYM(h, chs_error_codes);
    SYM(h, chs_schema_create); SYM(h, chs_schema_describe); SYM(h, chs_preview_row); SYM(h, chs_preview_batch);
    SYM(h, chs_filter_create); SYM(h, chs_filter_eval_body); SYM(h, chs_discover_columns);
    printf("build_info: %s\n", p_chs_build_info());
    chs_error *err = NULL; chs_buf *out = NULL, *exp = NULL;
    p_chs_initialize(B(""), &err);
    p_chs_live_handles(&out, &err); show("live_handles", out);
    p_chs_error_codes(&out, &err); show("error_code_table", out);
    chs_schema *s = NULL;
    p_chs_schema_create(B("CREATE TABLE t (x Int32)"), B("{}"), &s, &err);
    p_chs_schema_describe(s, &out, &err); show("schema_description", out);
    p_chs_preview_row(s, 0, B("{\"x\":1}"), B("{}"), B("[]"), &out, &err); show("row", out);
    p_chs_preview_batch(s, 0, B("{\"x\":1}"), B("{}"), B("[]"), NULL, 0, 0, &out, &exp, &err);
    show("batch", out); show("batch export", exp);
    chs_filter *f = NULL;
    p_chs_filter_create(s, B("x > 1"), B("{}"), B("{}"), &f, &err);
    p_chs_filter_eval_body(f, 0, B("{\"x\":1}"), B("{}"), &out, &err); show("filter_result", out);
    p_chs_discover_columns(B("{}"), &out, &err); show("discovery", out);
    return 0;
}
