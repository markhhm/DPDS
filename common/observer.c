/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "common.h"
#include "observer.h"

struct queue {
    struct observer_record *records;
    uint64_t reserved;
    _Atomic uint64_t head, tail, attempted, emitted, dropped, capture_drops, high_water, identity_errors, malformed_records;
};
struct published_snapshot {
    struct observer_snapshot slots[3];
    _Atomic unsigned middle;
    unsigned front, back; /* One TX reader owns front; this scheduler owns back. */
};
#define SNAPSHOT_DIRTY 4u
#define SNAPSHOT_INDEX 3u
static struct {
    struct published_snapshot snapshots[OBS_QUEUES];
    struct queue queues[OBS_PRODUCERS];
    _Atomic uint64_t order;
    _Atomic bool stop;
    _Atomic unsigned abort_reasons;
    uint16_t ports[OBS_PORTS], port_count;
    bool input_ended[OBS_PORTS];
    pthread_t thread;
    int fd;
    unsigned capacity, probe_port;
    enum observer_identity_policy policy;
    bool enabled, started, complete, write_failed;
    uint64_t intended[HNG_MAX_LINES];
} capture;
static struct observer_health last_health[OBS_PRODUCERS];

bool observer_enabled(void) { return capture.enabled; }
struct observer_health observer_health(enum observer_producer producer)
{
    if (!capture.enabled) return last_health[producer];
    struct queue *q = &capture.queues[producer];
    return (struct observer_health){atomic_load(&q->attempted), atomic_load(&q->emitted),
        atomic_load(&q->dropped), atomic_load(&q->capture_drops), atomic_load(&q->high_water),
        capture.capacity, atomic_load(&q->identity_errors), atomic_load(&q->malformed_records)};
}

bool observer_open(int fd, enum observer_identity_policy policy, unsigned probe_port, unsigned capacity)
{
    struct stat st;
    if (capture.enabled || !capacity || capacity > 65536 || policy > OBS_ID_SOFTWARE_L2 ||
        !probe_port || probe_port > 65535 || fstat(fd, &st) || (!S_ISFIFO(st.st_mode) && !S_ISSOCK(st.st_mode))) return false;
    memset(&capture, 0, sizeof(capture));
    capture.fd = -1;
    capture.capacity = capacity; capture.policy = policy; capture.probe_port = probe_port;
    for (unsigned owner = 0; owner < OBS_QUEUES; ++owner) {
        capture.snapshots[owner].back = 2;
        atomic_init(&capture.snapshots[owner].middle, 1);
    }
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
        capture.queues[p].records = calloc(capacity, sizeof(struct observer_record));
        if (!capture.queues[p].records) goto fail;
    }
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || (flags & O_ACCMODE) == O_RDONLY) goto fail;
    capture.fd = dup(fd);
    if (capture.fd < 0 || fcntl(capture.fd, F_SETFL, fcntl(capture.fd, F_GETFL) | O_NONBLOCK) < 0) goto fail;
    if (host.port_count > OBS_PORTS) goto fail;
    capture.port_count = host.port_count;
    for (unsigned i = 0; i < host.port_count; ++i) capture.ports[i] = host.ports[i];
    capture.enabled = true;
    return true;
fail:
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) free(capture.queues[p].records);
    if (capture.fd >= 0) close(capture.fd);
    return false;
}

struct observer_record *observer_reserve(enum observer_producer producer)
{
    if (!capture.enabled) return NULL;
    struct queue *q = &capture.queues[producer];
    uint64_t seq = atomic_fetch_add_explicit(&q->attempted, 1, memory_order_relaxed) + 1;
    uint64_t head = atomic_load_explicit(&q->head, memory_order_acquire);
    if (q->reserved - head == capture.capacity || seq == UINT64_MAX) {
        atomic_fetch_add_explicit(&q->dropped, 1, memory_order_relaxed);
        return NULL;
    }
    struct observer_record *dst = &q->records[q->reserved % capture.capacity];
    memset(dst, 0, sizeof(*dst)); dst->producer_seq = seq;
    dst->order = atomic_fetch_add_explicit(&capture.order, 1, memory_order_relaxed);
    uint64_t size = ++q->reserved - head;
    if (size > atomic_load_explicit(&q->high_water, memory_order_relaxed))
        atomic_store_explicit(&q->high_water, size, memory_order_relaxed);
    return dst;
}
void observer_commit(enum observer_producer producer)
{
    struct queue *q = &capture.queues[producer];
    atomic_fetch_add_explicit(&q->tail, 1, memory_order_release);
}
bool observer_emit(enum observer_producer producer, const struct observer_record *record)
{
    if (!capture.enabled) return true;
    struct observer_record *dst = observer_reserve(producer);
    if (!dst) return false;
    uint64_t seq = dst->producer_seq, order = dst->order;
    *dst = *record; dst->producer_seq = seq; dst->order = order;
    observer_commit(producer);
    return true;
}

/* A partial failed row ends the raw stream: never append another row to it. */
static bool output(const char *data, size_t length)
{
    if (capture.write_failed) return false;
    size_t done = 0;
    while (done < length) {
        ssize_t n = write(capture.fd, data + done, length - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { capture.write_failed = true; return false; }
        done += (size_t)n;
    }
    return true;
}

static void snapshot(FILE *f, const struct observer_snapshot *s)
{
    fputs("[", f);
    for (unsigned i = 0; i < s->count; ++i)
        fprintf(f, "%s[%u,%"PRIu64",%"PRIu64",%s]", i ? "," : "", s->ports[i], s->seen[i], s->lower_bound[i], s->ended[i] ? "true" : "false");
    fputs("]", f);
}
static void serialize(FILE *f, unsigned producer, const struct observer_record *r)
{
    fprintf(f, "{\"producer\":%u,\"producer_seq\":%"PRIu64",\"order\":%"PRIu64",\"kind\":%u,\"at_ns\":%"PRIu64
        ",\"gate_ns\":%"PRIu64",\"submit_before_ns\":%"PRIu64",\"submit_after_ns\":%"PRIu64
        ",\"actual_selection_seq\":%"PRIu64",\"attempt_id\":%"PRIu64",\"consumed_dispatch\":[%"PRIu64",%"PRIu64
        "],\"first_handoff_seq\":%"PRIu64",\"handoff_queue\":%u,\"port\":%u,\"accepted_count\":%u,\"reason\":%u,\"snapshot\":",
        producer, r->producer_seq, r->order, r->kind, r->at_ns, r->gate_ns, r->submit_before_ns, r->submit_after_ns,
        r->actual_selection_seq, r->attempt_id, r->consumed_dispatch[0], r->consumed_dispatch[1], r->first_handoff_seq,
        r->handoff_queue, r->port, r->accepted_count, r->reason);
    snapshot(f, &r->snapshot); fputs(",\"resources\":[", f);
    for (unsigned i = 0; i < 10; ++i) fprintf(f, "%s%"PRIu64, i ? "," : "", r->resources[i]);
    fputs("],\"items\":[", f);
    for (unsigned i = 0; i < r->count; ++i) {
        const struct observer_packet *x = &r->items[i];
        fprintf(f, "%s[%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64
            ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%s,%s,%"PRIu64"]", i ? "," : "", x->identity, x->ingress_seq,
            x->selection_seq, x->arrival_ns, x->handoff_seq, x->gate_ns, x->source, x->destination, x->length,
            x->source_port, x->destination_port, x->ingress_port, x->line, x->vlan_id, x->segments, x->handoff_queue,
            x->identity_kind, x->selected_late, x->input_error, x->classified ? "true" : "false", x->stripped ? "true" : "false", x->rx_flags);
    }
    fputs("]}\n", f);
}
static void *consumer(void *unused)
{
    (void)unused;
    sigset_t blocked; sigemptyset(&blocked); sigaddset(&blocked, SIGPIPE); pthread_sigmask(SIG_BLOCK, &blocked, NULL);
    char header[256];
    int n = snprintf(header, sizeof(header), "{\"schema\":\"hng-native-v1\",\"identity_policy\":%u,\"probe_port\":%u,\"capacity\":%u,\"record_bytes\":%zu}\n",
        capture.policy, capture.probe_port, capture.capacity, sizeof(struct observer_record));
    output(header, (size_t)n);
    uint64_t next_order = 0;
    for (;;) {
        bool pending = false, emitted = false;
        for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
            struct queue *q = &capture.queues[p];
            uint64_t head = atomic_load_explicit(&q->head, memory_order_relaxed);
            if (head == atomic_load_explicit(&q->tail, memory_order_acquire)) continue;
            pending = true;
            struct observer_record *r = &q->records[head % capture.capacity];
            if (r->order != next_order) continue;
            char *data = NULL; size_t length = 0;
            FILE *f = open_memstream(&data, &length);
            bool ok = f != NULL;
            if (ok) { serialize(f, p, r); ok = fclose(f) == 0; }
            if (ok) ok = output(data, length);
            free(data);
            atomic_fetch_add(ok ? &q->emitted : &q->capture_drops, 1);
            atomic_store_explicit(&q->head, head + 1, memory_order_release);
            next_order++; emitted = true;
        }
        if (atomic_load_explicit(&capture.stop, memory_order_acquire) && !pending) break;
        if (!emitted && pending && atomic_load_explicit(&capture.stop, memory_order_acquire)) {
            /* An aborted reservation can leave a global-order hole; discard its blocked suffix. */
            for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
                struct queue *q = &capture.queues[p];
                uint64_t head = atomic_load(&q->head), tail = atomic_load(&q->tail);
                atomic_fetch_add(&q->capture_drops, tail - head);
                atomic_store(&q->head, tail);
            }
        }
        if (!emitted) { struct timespec delay = {0, 1000000}; nanosleep(&delay, NULL); }
    }
    char *data = NULL; size_t length = 0; FILE *f = open_memstream(&data, &length);
    if (f) {
        bool complete = capture.complete && !capture.write_failed && !atomic_load(&capture.abort_reasons);
        for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
            struct observer_health h = observer_health(p);
            if (h.records_dropped || h.capture_drops || h.identity_errors || h.malformed_records) complete = false;
        }
        fprintf(f, "{\"kind\":\"health\",\"complete\":%s,\"abort_reasons\":%u,\"producers\":[", complete ? "true" : "false", atomic_load(&capture.abort_reasons));
        for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
            struct observer_health h = observer_health(p);
            fprintf(f, "%s{\"attempted\":%"PRIu64",\"emitted\":%"PRIu64",\"dropped\":%"PRIu64",\"capture_drops\":%"PRIu64
                ",\"high_water\":%"PRIu64",\"identity_errors\":%"PRIu64",\"malformed_records\":%"PRIu64"}", p ? "," : "", h.records_attempted,
                h.records_emitted, h.records_dropped, h.capture_drops, h.queue_high_water, h.identity_errors, h.malformed_records);
        }
        fputs("]}\n", f); if (fclose(f) == 0) output(data, length); free(data);
    } else capture.write_failed = true;
    return NULL;
}
bool observer_start(void)
{
    if (!capture.enabled || capture.started) return false;
    capture.started = pthread_create(&capture.thread, NULL, consumer, NULL) == 0;
    return capture.started;
}
bool observer_close(bool complete)
{
    if (!capture.enabled) return true;
    if (!complete) observer_abort(OBS_ABORT_REQUESTED);
    for (unsigned i = 0; i < capture.port_count; ++i)
        if (!capture.input_ended[i]) observer_abort(OBS_ABORT_DRAIN);
    capture.complete = complete;
    /* Producers have stopped. Unpublished reservations are known losses, not drain work. */
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
        struct queue *q = &capture.queues[p];
        uint64_t unused = q->reserved - atomic_load(capture.started ? &q->tail : &q->head);
        atomic_fetch_add(&q->capture_drops, unused);
    }
    atomic_store_explicit(&capture.stop, true, memory_order_release);
    if (capture.started) pthread_join(capture.thread, NULL);
    bool ok = complete && capture.started && !capture.write_failed && !atomic_load(&capture.abort_reasons);
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
        last_health[p] = observer_health(p);
        if (last_health[p].records_dropped || last_health[p].capture_drops || last_health[p].identity_errors || last_health[p].malformed_records) ok = false;
        free(capture.queues[p].records);
    }
    close(capture.fd); capture.enabled = false;
    return ok;
}

static uint64_t decode(const uint8_t *d, unsigned n, bool big)
{
    uint64_t result = 0;
    for (unsigned i = 0; i < n; ++i) result |= (uint64_t)d[i] << (8 * (big ? n - i - 1 : i));
    return result;
}
struct observer_packet observer_packet(const struct rte_mbuf *packet) { return packet_meta((struct rte_mbuf *)packet)->observation; }
void observer_input(struct rte_mbuf *m)
{
    if (!capture.enabled) return;
    struct packet_meta *meta = packet_meta(m);
    struct observer_packet *x = &meta->observation;
    *x = (struct observer_packet){.ingress_port = meta->ingress_port, .ingress_seq = meta->ingress_seq,
        .arrival_ns = meta->arrival_ns, .length = m->pkt_len, .segments = m->nb_segs,
        .rx_flags = m->ol_flags, .selected_late = 2, .stripped = (m->ol_flags & (RTE_MBUF_F_RX_VLAN_STRIPPED | RTE_MBUF_F_RX_QINQ_STRIPPED)) != 0};
    uint8_t buffer[256]; unsigned size = m->pkt_len < sizeof(buffer) ? m->pkt_len : sizeof(buffer);
    const uint8_t *d = rte_pktmbuf_read(m, 0, size, buffer);
    if (d && size >= 18 && d[12] == 0x81 && d[13] == 0) {
        x->vlan_id = decode(d + 14, 2, true) & 4095;
        bool qinq = (d[16] == 0x81 && d[17] == 0) || (d[16] == 0x88 && d[17] == 0xa8);
        uint64_t q = m->pkt_len + 4 < 64 ? 64 : m->pkt_len + 4;
        for (unsigned i = 0; i < host.line_count; ++i) {
            struct LineInfo *l = &host.lines[i];
            if (!qinq && !x->stripped && m->nb_segs == 1 && m->data_len == m->pkt_len && m->pkt_len <= 1518 &&
                l->ingress_port == meta->ingress_port && l->vlan_id == x->vlan_id && q >= l->min_q_bytes && q <= l->max_q_bytes) {
                x->classified = true; x->line = i; x->selection_seq = capture.intended[i]++; break;
            }
        }
        if (capture.policy == OBS_ID_SOFTWARE_L2 && size >= 42 && d[16] == 0x88 && d[17] == 0xb5) {
            x->identity = decode(d + 18, 8, false); x->identity_kind = 2;
        } else if (capture.policy == OBS_ID_STAGE1 && size >= 38 && d[16] == 8 && d[17] == 0 && (d[18] >> 4) == 4 && d[27] == 17) {
            unsigned ihl = (d[18] & 15) * 4, udp = 18 + ihl;
            if (ihl >= 20 && size >= udp + 108 && decode(d + 20, 2, true) == ihl + 108 && !(decode(d + 24, 2, true) & 0x3fff) &&
                decode(d + udp + 2, 2, true) == capture.probe_port && decode(d + udp + 4, 2, true) == 108 &&
                decode(d + udp + 16, 4, true) == UINT32_C(0x4c30504b) && d[udp + 20] <= 1) {
                bool padding = true; for (unsigned i = udp + 21; i < udp + 108; ++i) padding &= d[i] == 0;
                if (padding) {
                    x->identity = decode(d + udp + 8, 8, true); x->source = decode(d + 30, 4, true);
                    x->destination = decode(d + 34, 4, true); x->source_port = decode(d + udp, 2, true);
                    x->destination_port = capture.probe_port; x->selected_late = d[udp + 20]; x->identity_kind = 1;
                }
            }
        }
    }
    if (!x->classified) x->input_error = 1;
    if (!x->identity_kind) { x->input_error = 2; atomic_fetch_add(&capture.queues[OBS_RX].identity_errors, 1); }
    struct observer_record r = {.kind = OBS_PACKET, .count = 1}; r.items[0] = *x; observer_emit(OBS_RX, &r);
}
void observer_terminal(enum observer_producer producer, const struct rte_mbuf *m, enum observer_reason reason, uint64_t at_ns)
{
    if (!capture.enabled) return;
    struct observer_record r = {.kind = OBS_TERMINAL, .reason = reason, .at_ns = at_ns, .count = 1};
    r.items[0] = observer_packet(m); observer_emit(producer, &r);
}
void observer_input_end(uint16_t port, uint64_t next)
{
    if (!capture.enabled) return;
    unsigned i = 0; while (i < capture.port_count && capture.ports[i] != port) ++i;
    if (i == capture.port_count) {
        if (i == OBS_PORTS) { observer_abort(OBS_ABORT_RESOURCE); return; }
        capture.ports[capture.port_count++] = port;
    }
    capture.input_ended[i] = true;
    struct observer_record r = {.kind = OBS_END, .port = port, .actual_selection_seq = next}; observer_emit(OBS_RX, &r);
}

void observer_resources(uint16_t port, bool end, uint64_t total, uint64_t free_count, const struct rte_eth_stats *nic)
{
    if (!capture.enabled) return;
    struct observer_record r = {.kind = OBS_RESOURCES, .port = port, .at_ns = host_now_ns(),
        .resources = {end, total, free_count, nic != NULL}};
    if (nic) {
        r.resources[4] = nic->ipackets; r.resources[5] = nic->opackets;
        r.resources[6] = nic->imissed; r.resources[7] = nic->ierrors;
        r.resources[8] = nic->oerrors; r.resources[9] = nic->rx_nombuf;
    }
    observer_emit(OBS_CONTROL, &r);
}

void observer_publish_snapshot(unsigned owner, const struct observer_snapshot *snapshot)
{
    if (!capture.enabled) return;
    struct published_snapshot *p = &capture.snapshots[owner];
    p->slots[p->back] = *snapshot;
    /* Publish the completed slot and acquire a slot the reader no longer owns. */
    p->back = atomic_exchange_explicit(&p->middle, p->back | SNAPSHOT_DIRTY,
                                      memory_order_acq_rel) & SNAPSHOT_INDEX;
}
bool observer_load_snapshot(struct observer_snapshot *snapshot)
{
    struct observer_snapshot result = {0};
    for (unsigned owner = 0; owner < OBS_QUEUES; ++owner) {
        struct published_snapshot *p = &capture.snapshots[owner];
        if (atomic_load_explicit(&p->middle, memory_order_acquire) & SNAPSHOT_DIRTY)
            p->front = atomic_exchange_explicit(&p->middle, p->front,
                                               memory_order_acq_rel) & SNAPSHOT_INDEX;
        const struct observer_snapshot *value = &p->slots[p->front];
        if (value->count > OBS_PORTS) goto invalid;
        for (unsigned i = 0; i < value->count; ++i) {
            uint16_t port = value->ports[i];
            unsigned k = 0; while (k < result.count && result.ports[k] != port) ++k;
            if (k == result.count) { if (k == OBS_PORTS) goto invalid; result.ports[result.count++] = port; }
            uint64_t seen = value->seen[i], lower = value->lower_bound[i];
            bool ended = value->ended[i];
            if (seen > result.seen[k] || (seen == result.seen[k] && (lower > result.lower_bound[k] || ended))) {
                result.seen[k] = seen; result.lower_bound[k] = lower; result.ended[k] = ended;
            }
        }
    }
    *snapshot = result;
    return true;
invalid:
    atomic_fetch_add(&capture.queues[OBS_TX].malformed_records, 1);
    return false;
}

void observer_abort(enum observer_abort_reason reason)
{
    if (capture.enabled) atomic_fetch_or(&capture.abort_reasons, reason);
}
