/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdlib.h>
#include <sched.h>
#include <string.h>
#include <unistd.h>
#include <rte_net_crc.h>
#include <rte_byteorder.h>
#include "observer_binary.h"

#define FRAME_HEADER 16u
#define FRAME_TRAILER 4u

_Static_assert(MAX_BURST_SIZE <= 16 && OBS_PORTS <= 2 && OBS_PRODUCERS == 7,
    "binary v1 count and producer bounds changed");

static void little(unsigned char *dst, uint64_t value, unsigned bytes)
{
    for (unsigned i = 0; i < bytes; ++i) { dst[i] = (unsigned char)value; value >>= 8; }
}

static unsigned char *integer(unsigned char *dst, uint64_t value)
{
    if (value < 128) { *dst++ = (unsigned char)value; return dst; }
    unsigned bytes = (64u - (unsigned)__builtin_clzll(value) + 7) / 8;
    *dst++ = (unsigned char)(128 | (bytes - 1));
    uint64_t encoded = rte_cpu_to_le_64(value);
    /* The scratch entry has room for the full store, including its unused tail. */
    memcpy(dst, &encoded, sizeof(encoded)); dst += bytes;
    return dst;
}

#define FIELD(bit, expression) do { \
    uint64_t value = (expression); \
    if (value) { mask |= UINT32_C(1) << (bit); p = integer(p, value); } \
} while (0)

static unsigned char *packet(unsigned char *p, const struct observer_packet *x)
{
    unsigned char *bits = p; p += 4; uint32_t mask = 0;
    FIELD(0, x->identity); FIELD(1, x->ingress_seq); FIELD(2, x->selection_seq);
    FIELD(3, x->arrival_ns); FIELD(4, x->handoff_seq); FIELD(5, x->gate_ns);
    FIELD(6, x->source); FIELD(7, x->destination); FIELD(8, x->length);
    FIELD(9, x->source_port); FIELD(10, x->destination_port); FIELD(11, x->ingress_port);
    FIELD(12, x->line); FIELD(13, x->vlan_id); FIELD(14, x->segments); FIELD(15, x->handoff_queue);
    FIELD(16, x->identity_kind); FIELD(17, x->selected_late); FIELD(18, x->input_error);
    FIELD(19, x->classified); FIELD(20, x->stripped); FIELD(21, x->rx_flags);
    little(bits, mask, 4); return p;
}

struct observer_io_slot {
    size_t length;
    bool ok;
    uint64_t counts[OBS_PRODUCERS];
    _Alignas(RTE_CACHE_LINE_SIZE) unsigned char data[65536];
};

static void wait_sem(sem_t *semaphore)
{
    int result;
    do { result = sem_wait(semaphore); } while (result && errno == EINTR);
    if (result) abort();
}

static bool write_all(int fd, const void *data, size_t length)
{
    const unsigned char *bytes = data;
    while (length) {
        ssize_t n = write(fd, bytes, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        bytes += n; length -= (size_t)n;
    }
    return true;
}

static void *io_worker(void *argument)
{
    struct observer_binary_writer *w = argument;
    const int fd = w->fd;
    struct observer_io_slot *slots = w->slots;
    uint64_t next = 0; bool failed = false;
    w->io_cpu = sched_getcpu();
    for (;;) {
        wait_sem(&w->ready);
        if (atomic_load_explicit(&w->io_stop, memory_order_acquire)) break;
        struct observer_io_slot *slot = &slots[next++ % OBS_IO_BLOCKS];
        slot->ok = !failed && write_all(fd, slot->data, slot->length);
        if (!slot->ok) failed = true;
        if (sem_post(&w->done)) abort();
    }
    return NULL;
}

static void reap(struct observer_binary_writer *w)
{
    struct observer_io_slot *slot = &w->slots[w->reaped++ % OBS_IO_BLOCKS];
    if (!slot->ok) {
        w->failed = true;
        for (unsigned p = 0; p < OBS_PRODUCERS; ++p) w->pending[p] += slot->counts[p];
    }
}

static bool drain(struct observer_binary_writer *w)
{
    while (w->reaped != w->submitted) { wait_sem(&w->done); reap(w); }
    return !w->failed;
}

static bool output(struct observer_binary_writer *w, const void *data, size_t length, const uint64_t *counts)
{
    if (w->failed) return false;
    while (w->reaped != w->submitted) {
        if (sem_trywait(&w->done)) {
            if (errno == EINTR) continue;
            assert(errno == EAGAIN); break;
        }
        reap(w);
    }
    if (w->failed) { drain(w); return false; }
    if (w->submitted - w->reaped == OBS_IO_BLOCKS) {
        wait_sem(&w->done); reap(w);
        if (w->failed) { drain(w); return false; }
    }
    struct observer_io_slot *slot = &w->slots[w->submitted % OBS_IO_BLOCKS];
    assert(length <= sizeof(slot->data));
    memcpy(slot->data, data, length); slot->length = length;
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) slot->counts[p] = counts ? counts[p] : 0;
    uint64_t pending = ++w->submitted - w->reaped;
    if (pending > w->io_peak) w->io_peak = pending;
    if (sem_post(&w->ready)) abort();
    return true;
}

static bool frame(struct observer_binary_writer *w, unsigned type)
{
    unsigned char *b = w->buffer;
    b[0] = (unsigned char)type; b[1] = b[2] = b[3] = 0;
    little(b + 4, w->used - FRAME_HEADER, 4); little(b + 8, w->frame++, 8);
    uint32_t crc = rte_net_crc_calc(w->crc, b, (uint32_t)w->used);
    little(b + w->used, crc, FRAME_TRAILER);
    return output(w, b, w->used + FRAME_TRAILER, type == 2 ? w->pending : NULL);
}

bool observer_binary_init(struct observer_binary_writer *w, int fd)
{
    memset(w, 0, sizeof(*w)); w->fd = fd; w->used = FRAME_HEADER;
    w->crc = rte_net_crc_set_alg(RTE_NET_CRC_SSE42, RTE_NET_CRC32_ETH);
    if (!w->crc) w->crc = rte_net_crc_set_alg(RTE_NET_CRC_SCALAR, RTE_NET_CRC32_ETH);
    if (!w->crc) { w->failed = true; return false; }
    void *slots = NULL;
    size_t bytes = OBS_IO_BLOCKS * sizeof(struct observer_io_slot);
    if (posix_memalign(&slots, RTE_CACHE_LINE_SIZE, bytes)) { w->failed = true; return false; }
    w->slots = slots; memset(slots, 0, bytes);
    if (sem_init(&w->ready, 0, 0)) { w->failed = true; return false; }
    w->ready_initialized = true;
    if (sem_init(&w->done, 0, 0)) { w->failed = true; return false; }
    w->done_initialized = true; atomic_init(&w->io_stop, false);
    if (pthread_create(&w->io_thread, NULL, io_worker, w)) { w->failed = true; return false; }
    w->io_started = true;
    return output(w, "HNGBIN1\n", 8, NULL) && drain(w);
}

static bool flush_frame(struct observer_binary_writer *w)
{
    if (w->failed) return false;
    if (w->used > FRAME_HEADER && !frame(w, 2)) return false;
    w->used = FRAME_HEADER;
    memset(w->pending, 0, sizeof(w->pending));
    return true;
}

bool observer_binary_flush(struct observer_binary_writer *w)
{
    if (!flush_frame(w)) { drain(w); return false; }
    return drain(w);
}

bool observer_binary_text(struct observer_binary_writer *w, unsigned type, const void *text, size_t length)
{
    if (!observer_binary_flush(w) || !length || length > sizeof(w->buffer) - FRAME_HEADER - FRAME_TRAILER)
        return false;
    memcpy(w->buffer + FRAME_HEADER, text, length); w->used += length;
    bool ok = frame(w, type); w->used = FRAME_HEADER;
    return ok && drain(w);
}

bool observer_binary_record(struct observer_binary_writer *w, unsigned producer, const struct observer_record *r)
{
    if (w->failed) return false;
    if (producer >= OBS_PRODUCERS || r->count > MAX_BURST_SIZE || r->snapshot.count > OBS_PORTS ||
        r->kind > OBS_RESOURCES || r->reason > UINT8_MAX) {
        w->failed = true; return false;
    }
    /* The maximum original integer widths bound an entry below 4096 bytes. */
    unsigned char entry[4096], *p = entry + 2;
    *p++ = (unsigned char)producer; *p++ = (unsigned char)r->kind;
    *p++ = (unsigned char)r->count; *p++ = (unsigned char)r->reason;
    p = integer(p, r->producer_seq); p = integer(p, r->order);
    unsigned char *bits = p; p += 4; uint32_t mask = 0;
    FIELD(0, r->at_ns); FIELD(1, r->gate_ns); FIELD(2, r->submit_before_ns); FIELD(3, r->submit_after_ns);
    FIELD(4, r->resources[0]); FIELD(5, r->resources[1]); FIELD(6, r->resources[2]); FIELD(7, r->resources[3]);
    FIELD(8, r->resources[4]); FIELD(9, r->resources[5]); FIELD(10, r->resources[6]); FIELD(11, r->resources[7]);
    FIELD(12, r->resources[8]); FIELD(13, r->resources[9]);
    FIELD(14, r->actual_selection_seq); FIELD(15, r->attempt_id);
    FIELD(16, r->consumed_dispatch[0]); FIELD(17, r->consumed_dispatch[1]);
    FIELD(18, r->first_handoff_seq); FIELD(19, r->handoff_queue); FIELD(20, r->port); FIELD(21, r->accepted_count);
    little(bits, mask, 4); *p++ = (unsigned char)r->snapshot.count;
    for (unsigned i = 0; i < r->snapshot.count; ++i) {
        p = integer(p, r->snapshot.ports[i]); p = integer(p, r->snapshot.seen[i]);
        p = integer(p, r->snapshot.lower_bound[i]); *p++ = r->snapshot.ended[i];
    }
    for (unsigned i = 0; i < r->count; ++i) p = packet(p, &r->items[i]);
    size_t length = (size_t)(p - entry); little(entry, length - 2, 2);
    if (w->used + length + FRAME_TRAILER > sizeof(w->buffer) && !flush_frame(w)) return false;
    memcpy(w->buffer + w->used, entry, length); w->used += length; w->pending[producer]++;
    return true;
}

void observer_binary_destroy(struct observer_binary_writer *w)
{
    if (w->io_started) {
        drain(w);
        atomic_store_explicit(&w->io_stop, true, memory_order_release);
        if (sem_post(&w->ready)) abort();
        if (pthread_join(w->io_thread, NULL)) abort();
        w->io_started = false;
    }
    if (w->ready_initialized) sem_destroy(&w->ready);
    if (w->done_initialized) sem_destroy(&w->done);
    free(w->slots); w->slots = NULL;
    rte_net_crc_free(w->crc); w->crc = NULL;
}
