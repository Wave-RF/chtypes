// PROBE ONLY (do not merge): CLOCK_REALTIME (and gettimeofday) rounded down to
// PROBE_COARSE_NS (default 100000 ns), for an UNMODIFIED binary: a coarse
// clock, the way a darwin clock is microsecond-grained. Linux: LD_PRELOAD.
// macOS: DYLD_INSERT_LIBRARIES, through __DATA,__interpose.
#define _GNU_SOURCE
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

static long step_ns(void) {
    static long step = 0;
    if (step == 0) {
        const char *s = getenv("PROBE_COARSE_NS");
        long v = s ? atol(s) : 0;
        step = v > 0 ? v : 100000;
    }
    return step;
}

static void coarsen_ts(struct timespec *ts) {
    long st = step_ns();
    ts->tv_nsec = ts->tv_nsec / st * st;
}

static void coarsen_tv(struct timeval *tv) {
    long st = step_ns() / 1000;
    if (st > 0) {
        tv->tv_usec = tv->tv_usec / st * st;
    }
}

#ifdef __APPLE__
static int probe_clock_gettime(clockid_t id, struct timespec *ts) {
    int r = clock_gettime(id, ts);
    if (r == 0 && id == CLOCK_REALTIME) {
        coarsen_ts(ts);
    }
    return r;
}

static int probe_gettimeofday(struct timeval *tv, void *tz) {
    int r = gettimeofday(tv, tz);
    if (r == 0 && tv) {
        coarsen_tv(tv);
    }
    return r;
}

__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} probe_interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)probe_clock_gettime, (const void *)clock_gettime},
    {(const void *)probe_gettimeofday, (const void *)gettimeofday},
};
#else
#include <dlfcn.h>

int clock_gettime(clockid_t id, struct timespec *ts) {
    static int (*real)(clockid_t, struct timespec *);
    if (!real) {
        real = (int (*)(clockid_t, struct timespec *))dlsym(RTLD_NEXT, "clock_gettime");
    }
    int r = real(id, ts);
    if (r == 0 && id == CLOCK_REALTIME) {
        coarsen_ts(ts);
    }
    return r;
}

int gettimeofday(struct timeval *tv, void *tz) {
    static int (*real)(struct timeval *, void *);
    if (!real) {
        real = (int (*)(struct timeval *, void *))dlsym(RTLD_NEXT, "gettimeofday");
    }
    int r = real(tv, tz);
    if (r == 0 && tv) {
        coarsen_tv(tv);
    }
    return r;
}
#endif
