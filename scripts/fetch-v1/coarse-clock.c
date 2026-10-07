/*
 * coarse-clock.c: an LD_PRELOAD shim that rounds the wall clock DOWN to a
 * coarse grain, so the concurrent-install legs of
 * scripts/fetch-v1/cache-interop.py can run each binding's real, unmodified
 * CLI as if the host clock were coarse (public issues #482 and #516: a name
 * drawn from a clock reading repeats across processes when the clock is
 * coarse, and darwin's is whole microseconds).
 *
 *   cc -shared -fPIC -O2 -o coarse-clock.so scripts/fetch-v1/coarse-clock.c -ldl
 *   LD_PRELOAD=/abs/coarse-clock.so COARSE_CLOCK_NS=100000 COARSE_CLOCK_LOG=/abs/log <command>
 *
 * It interposes clock_gettime() and gettimeofday(). A CLOCK_REALTIME or
 * CLOCK_REALTIME_COARSE reading, and every gettimeofday() reading, is rounded
 * down to a multiple of COARSE_CLOCK_NS nanoseconds (default 100000, which is
 * 100 microseconds; the grain must divide one second). Every other clock
 * passes through unchanged, so timeouts and elapsed-time arithmetic are not
 * disturbed. 0 turns the rounding off, which is what the driver's selftest
 * plants to prove its own control can fail.
 *
 * Its reach is not assumed. A program that reads the clock without calling
 * libc (Go's runtime reads it from the vDSO itself) is not coarsened, and a
 * statically linked binary never loads the shim at all. So with
 * COARSE_CLOCK_LOG set it appends, per process image:
 *
 *   loaded pid=<pid> exe=<path>    from its constructor
 *   rounded pid=<pid> exe=<path>   at the first reading it rounded
 *
 * and the driver reads that log to say, for every CLI process, whether the
 * CLI's own image loaded the shim and whether its clock was coarsened.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef int (*clock_gettime_fn)(clockid_t, struct timespec *);
typedef int (*gettimeofday_fn)(struct timeval *, void *);

static clock_gettime_fn real_clock_gettime;
static gettimeofday_fn real_gettimeofday;
static long grain_ns = 100000;
static int rounded_logged;

static void log_event(const char *event) {
    const char *path = getenv("COARSE_CLOCK_LOG");
    if (path == NULL || *path == '\0') {
        return;
    }
    char exe[4096];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n < 0) {
        n = 0;
    }
    exe[n] = '\0';
    char line[4352];
    int len = snprintf(line, sizeof line, "%s pid=%ld exe=%s\n", event, (long)getpid(), exe);
    if (len <= 0) {
        return;
    }
    if (len >= (int)sizeof line) {
        len = (int)sizeof line - 1;
    }
    int fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0644);
    if (fd < 0) {
        return;
    }
    ssize_t written = write(fd, line, (size_t)len);
    (void)written;
    close(fd);
}

static void resolve(void) {
    if (real_clock_gettime == NULL) {
        real_clock_gettime = (clock_gettime_fn)dlsym(RTLD_NEXT, "clock_gettime");
    }
    if (real_gettimeofday == NULL) {
        real_gettimeofday = (gettimeofday_fn)dlsym(RTLD_NEXT, "gettimeofday");
    }
}

static void note_rounded(void) {
    if (!__atomic_exchange_n(&rounded_logged, 1, __ATOMIC_SEQ_CST)) {
        log_event("rounded");
    }
}

__attribute__((constructor)) static void coarse_clock_init(void) {
    const char *grain = getenv("COARSE_CLOCK_NS");
    if (grain != NULL && *grain != '\0') {
        grain_ns = strtol(grain, NULL, 10);
    }
    resolve();
    log_event("loaded");
}

int clock_gettime(clockid_t id, struct timespec *ts) {
    resolve();
    int rc = real_clock_gettime(id, ts);
    if (rc == 0 && grain_ns > 0 && (id == CLOCK_REALTIME || id == CLOCK_REALTIME_COARSE)) {
        ts->tv_nsec -= ts->tv_nsec % grain_ns;
        note_rounded();
    }
    return rc;
}

int gettimeofday(struct timeval *restrict tv, void *restrict tz) {
    resolve();
    int rc = real_gettimeofday(tv, tz);
    long grain_us = grain_ns / 1000;
    if (rc == 0 && grain_us > 0) {
        tv->tv_usec -= tv->tv_usec % grain_us;
        note_rounded();
    }
    return rc;
}
