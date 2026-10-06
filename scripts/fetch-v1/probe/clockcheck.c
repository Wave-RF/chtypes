// PROBE ONLY (do not merge): exit 0 when every CLOCK_REALTIME reading is a
// multiple of 100000 ns (the coarse-clock shim is in effect), 1 otherwise.
#include <stdio.h>
#include <sys/time.h>
#include <time.h>

int main(void) {
    int coarse = 1;
    for (int i = 0; i < 8; i++) {
        struct timespec ts;
        struct timeval tv;
        clock_gettime(CLOCK_REALTIME, &ts);
        gettimeofday(&tv, NULL);
        printf("clock_gettime tv_nsec %ld, gettimeofday tv_usec %ld\n", (long)ts.tv_nsec, (long)tv.tv_usec);
        if (ts.tv_nsec % 100000 != 0 || tv.tv_usec % 100 != 0) {
            coarse = 0;
        }
    }
    return coarse ? 0 : 1;
}
