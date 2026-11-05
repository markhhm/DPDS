/* SPDX-License-Identifier: Apache-2.0 */
/* Deterministic clock-read ordering; process tests cover real clock correlation. */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <rte_eal.h>
#include "../common/common.h"
static unsigned mode, phase;
static uint64_t base, previous_epoch;
int __real_clock_gettime(clockid_t id, struct timespec *t);
uint64_t __real_host_now_ns(void);
int __wrap_clock_gettime(clockid_t id, struct timespec *t)
{
    if (!mode || id != CLOCK_MONOTONIC) return __real_clock_gettime(id, t);
    uint64_t ns;
    if (!phase) { phase = 1; ns = base; }
    else {
        assert(phase == (mode == 1 ? 1u : 2u));
        if (mode == 1) assert(host.epoch_cycles != previous_epoch);
        phase = 3; ns = base + 100;
    }
    *t = (struct timespec){.tv_sec = ns / UINT64_C(1000000000), .tv_nsec = ns % UINT64_C(1000000000)};
    return 0;
}
uint64_t __wrap_host_now_ns(void)
{
    if (mode != 2) return __real_host_now_ns();
    assert(phase == 1); phase = 2;
    return 123;
}
static uint64_t field(const char *name)
{
    FILE *f = tmpfile(); assert(f);
    host_diagnostics_report(f); rewind(f);
    char line[512], prefix[128];
    snprintf(prefix, sizeof(prefix), "%s=", name);
    uint64_t value = 0; bool found = false;
    while (fgets(line, sizeof(line), f))
        if (!strncmp(line, prefix, strlen(prefix))) {
            assert(sscanf(line + strlen(prefix), "%"SCNu64, &value) == 1);
            found = true; break;
        }
    fclose(f); assert(found); return value;
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("config.json"));
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    uint64_t anchor = 0;
    for (unsigned i = 0; i < 2; ++i) {
        mode = 1; phase = 0; previous_epoch = host.epoch_cycles;
        base = UINT64_C(1000000000000) + i * 1000;
        host_diagnostics_epoch_anchor();
        assert(phase == 3);
        assert(field("native_observer_epoch_monotonic_ns") == base + 100);
        assert(field("native_observer_epoch_monotonic_ns") > anchor);
        anchor = field("native_observer_epoch_monotonic_ns");
        assert(field("native_observer_epoch_read_gap_ns") == 100);
        assert(field("native_observer_timer_hz") == host.cycles_hz);
    }
    assert(field("port7_close_ns") == 0 && field("port7_close_monotonic_ns") == 0);
    mode = 2; phase = 0; base += 1000;
    host_diagnostics_port_closed(0);
    assert(phase == 3 && field("port7_close_ns") == 123);
    assert(field("port7_close_monotonic_ns") == base + 100 && field("port7_close_read_gap_ns") == 100);
    assert(field("port13_close_ns") == 0 && field("port13_close_monotonic_ns") == 0);
    mode = 0;
    host_cleanup();
    assert(rte_eal_cleanup() == 0);
    puts("monotonic anchors, exact bracket gap, timer/monotonic read order and per-port publication: PASS");
}
