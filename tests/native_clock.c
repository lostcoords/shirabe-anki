#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

static int oracle_clock_gettime(clockid_t clock, struct timespec *value) {
    const char *stamp = getenv("SHIRABE_ORACLE_TIME");
    if (clock == CLOCK_REALTIME && stamp && *stamp) {
        value->tv_sec = strtoll(stamp, NULL, 10);
        value->tv_nsec = 0;
        return 0;
    }
    return clock_gettime(clock, value);
}

static int oracle_gettimeofday(struct timeval *value, void *zone) {
    const char *stamp = getenv("SHIRABE_ORACLE_TIME");
    if (stamp && *stamp) {
        value->tv_sec = strtoll(stamp, NULL, 10);
        value->tv_usec = 0;
        return 0;
    }
    return gettimeofday(value, zone);
}

__attribute__((used, section("__DATA,__interpose")))
static const struct {
    const void *replacement;
    const void *original;
} oracle_clocks[] = {
    { (const void *)oracle_clock_gettime, (const void *)clock_gettime },
    { (const void *)oracle_gettimeofday, (const void *)gettimeofday }
};
