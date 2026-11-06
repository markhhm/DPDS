/* SPDX-License-Identifier: Apache-2.0 */
#include <errno.h>
#include <inttypes.h>
#include <time.h>
#include <rte_atomic.h>
#include <rte_cycles.h>
#include <rte_lcore.h>
#include <rte_telemetry.h>
#include "common.h"

static _Thread_local enum host_role current_role;
static _Thread_local unsigned current_owner;
static const char *role_name[] = {"main", "rx", "scheduler", "tx", "combined"};

static uint64_t monotonic_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + t.tv_nsec;
}

void host_diagnostics_epoch_anchor(void)
{
    uint64_t before = monotonic_ns();
    /* Fence the timer sample inside the monotonic bracket (LFENCE on x86). */
    rte_rmb();
    host.epoch_cycles = rte_get_timer_cycles();
    rte_rmb();
    uint64_t after = monotonic_ns();
    host.diagnostics.epoch_monotonic_ns = after;
    host.diagnostics.epoch_read_gap_ns = after - before;
}

void host_diagnostics_port_closed(unsigned index)
{
    struct port_close_diagnostics *d = &host.diagnostics.ports[index];
    uint64_t before = monotonic_ns();
    rte_rmb();
    d->native_ns = host_now_ns();
    rte_rmb();
    d->monotonic_ns = monotonic_ns();
    d->read_gap_ns = d->monotonic_ns - before;
    atomic_store_explicit(&d->recorded, true, memory_order_release);
}

void host_diagnostics_bind(enum host_role role, unsigned owner)
{
    current_role = role;
    current_owner = owner;
}

void host_diagnostics_invalid(const char *reason)
{
    /* Only the winner of host.invalid's exchange writes this immutable record. */
    struct host_diagnostics *d = &host.diagnostics;
    d->invalid_mono_ns = monotonic_ns();
    d->invalid_at_ns = host.cycles_hz ? host_now_ns() : 0;
    d->invalid_lcore = rte_lcore_id();
    snprintf(d->invalid_reason, sizeof(d->invalid_reason), "%s", reason);
    snprintf(d->invalid_owner, sizeof(d->invalid_owner), "%s:%u", role_name[current_role], current_owner);
    atomic_store_explicit(&d->invalid_recorded, true, memory_order_release);
}

void host_diagnostics_rx_stopped(unsigned owner)
{
    atomic_store_explicit(&host.diagnostics.rx[owner].stopped_mono_ns, monotonic_ns(), memory_order_relaxed);
}

/* Same flat key/value names in final statistics and /dpds/diagnostics.
 * Reading an owner counter never reads the owner's non-atomic packet statistics. */
static void number(FILE *out, struct rte_tel_data *tel, const char *name, uint64_t value)
{
    if (out) fprintf(out, "%s=%"PRIu64"\n", name, value);
    else rte_tel_data_add_dict_uint(tel, name, value);
}
static void string(FILE *out, struct rte_tel_data *tel, const char *name, const char *value)
{
    if (out) fprintf(out, "%s=%s\n", name, value);
    else rte_tel_data_add_dict_string(tel, name, value);
}

static void snapshot(FILE *out, struct rte_tel_data *tel)
{
    number(out, tel, "native_observer_epoch_monotonic_ns", host.diagnostics.epoch_monotonic_ns);
    number(out, tel, "native_observer_epoch_read_gap_ns", host.diagnostics.epoch_read_gap_ns);
    number(out, tel, "native_observer_timer_hz", host.cycles_hz);
    string(out, tel, "native_observer_epoch_read_order", "monotonic_before,timer_epoch,monotonic_after");
    for (unsigned i = 0; i < host.port_count; ++i) {
        struct port_close_diagnostics *d = &host.diagnostics.ports[i];
        bool recorded = atomic_load_explicit(&d->recorded, memory_order_acquire);
        char key[64];
        snprintf(key, sizeof(key), "port%u_close_ns", host.ports[i]);
        number(out, tel, key, recorded ? d->native_ns : 0);
        snprintf(key, sizeof(key), "port%u_close_monotonic_ns", host.ports[i]);
        number(out, tel, key, recorded ? d->monotonic_ns : 0);
        snprintf(key, sizeof(key), "port%u_close_read_gap_ns", host.ports[i]);
        number(out, tel, key, recorded ? d->read_gap_ns : 0);
    }
    const char *names[] = {"rx", "scheduler", "tx"};
    struct owner_diagnostics *owners[] = {host.diagnostics.rx, host.diagnostics.scheduler, host.diagnostics.tx};
    for (unsigned role = 0; role < 3; ++role)
        for (unsigned k = 0; k < 2; ++k) {
            char key[64];
            snprintf(key, sizeof(key), "%s%u_loop_gap_max_ns", names[role], k);
            number(out, tel, key, atomic_load_explicit(&owners[role][k].loop_gap_max_ns, memory_order_relaxed));
        }
    for (unsigned k = 0; k < 2; ++k) {
        char key[64];
        snprintf(key, sizeof(key), "tx_owner%u_ring_high_water", k);
        number(out, tel, key, atomic_load_explicit(&host.diagnostics.scheduler[k].ring_high_water, memory_order_relaxed));
        snprintf(key, sizeof(key), "rx%u_stopped_mono_ns", k);
        number(out, tel, key, atomic_load_explicit(&host.diagnostics.rx[k].stopped_mono_ns, memory_order_relaxed));
    }
    host_storage_report(out, tel);
    struct host_diagnostics *d = &host.diagnostics;
    bool recorded = atomic_load_explicit(&d->invalid_recorded, memory_order_acquire);
    number(out, tel, "first_invalid_recorded", recorded);
    number(out, tel, "first_invalid_mono_ns", recorded ? d->invalid_mono_ns : 0);
    number(out, tel, "first_invalid_at_ns", recorded ? d->invalid_at_ns : 0);
    number(out, tel, "first_invalid_lcore", recorded ? d->invalid_lcore : LCORE_ID_ANY);
    string(out, tel, "first_invalid_owner", recorded ? d->invalid_owner : "none");
    string(out, tel, "first_invalid_reason", recorded ? d->invalid_reason : "none");
}

void host_diagnostics_report(FILE *out) { snapshot(out, NULL); }

static int telemetry(const char *cmd, const char *params, struct rte_tel_data *data)
{
    (void)cmd;
    if (params && *params) return -EINVAL;
    rte_tel_data_start_dict(data);
    snapshot(NULL, data);
    return 0;
}

int host_diagnostics_register(void)
{
    return rte_telemetry_register_cmd("/dpds/diagnostics", telemetry,
        "Atomic owner loop gaps, TX handoff peaks, RX stop and first INVALID; no parameters");
}
