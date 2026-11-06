/* SPDX-License-Identifier: Apache-2.0 */
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <rte_memcpy.h>
#include "storage.h"
#define NONE UINT32_MAX
#define FREE_GUARD UINT16_C(0x59a3)
#define LIVE_GUARD UINT16_C(0xc46d)
struct descriptor {
    struct packet_meta meta;
    uint64_t generation, flags;
    uint32_t packet_type, hash;
    union { uint32_t first_cell, next_free; } link;
    uint16_t length, port, vlan, outer_vlan, guard, reserved;
} __attribute__((aligned(64)));
_Static_assert(sizeof(struct descriptor) == 192, "capacity accounting requires 192-byte descriptors");
struct storage {
    struct descriptor *records;
    unsigned char (*payload)[STORAGE_CELL_BYTES];
    uint32_t *next;
    uint32_t record_count, cell_count, free_record, free_cell;
    struct storage_stats stats;
};
static uint64_t round64(uint64_t bytes) { return (bytes + 63) & ~UINT64_C(63); }
bool storage_layout(uint32_t records, uint32_t cells, struct storage_layout *out)
{
    if (!out || !records || !cells || records == NONE || cells == NONE) return false;
    *out = (struct storage_layout){.records=records, .cells=cells,
        .descriptor_bytes=sizeof(struct descriptor), .cell_bytes=STORAGE_CELL_BYTES, .link_bytes=sizeof(uint32_t),
        .control_bytes=sizeof(struct storage), .control_allocation=round64(sizeof(struct storage)),
        .descriptor_allocation=(uint64_t)records*sizeof(struct descriptor),
        .payload_allocation=(uint64_t)cells*STORAGE_CELL_BYTES, .links_allocation=round64((uint64_t)cells*sizeof(uint32_t))};
    out->total_bytes = out->control_allocation + out->descriptor_allocation + out->payload_allocation + out->links_allocation;
    return true;
}
static void *allocate(size_t bytes)
{
    void *p = NULL;
    return posix_memalign(&p, 64, bytes) ? NULL : p;
}
struct storage *storage_create(uint32_t records, uint32_t cells)
{
    struct storage_layout layout;
    if (!storage_layout(records, cells, &layout)) return NULL;
    struct storage *s = allocate(layout.total_bytes);
    if (!s) return NULL;
    memset(s, 0, layout.control_allocation);
    s->records = (struct descriptor *)((unsigned char *)s + layout.control_allocation);
    s->payload = (void *)((unsigned char *)s->records + layout.descriptor_allocation);
    s->next = (void *)((unsigned char *)s->payload + layout.payload_allocation);
    s->record_count = records; s->cell_count = cells;
    memset(s->records, 0, layout.descriptor_allocation);
    /* Commit the whole bounded working set at startup, before any timing. */
    memset(s->payload, 0, layout.payload_allocation);
    for (uint32_t i = 0; i < records; ++i) {
        s->records[i].guard = FREE_GUARD;
        s->records[i].link.next_free = i+1 < records ? i+1 : NONE;
    }
    for (uint32_t i = 0; i < cells; ++i) s->next[i] = i+1 < cells ? i+1 : NONE;
    return s;
}
bool storage_destroy(struct storage *s)
{
    if (!s || s->stats.records_live || s->stats.cells_live) return false;
    free(s); return true;
}
static struct descriptor *lookup(struct storage *s, struct storage_handle h)
{
    if (h.index < s->record_count) {
        struct descriptor *d = &s->records[h.index];
        if (d->guard == LIVE_GUARD && d->generation == h.generation) return d;
    }
    s->stats.invalid_handles++; s->stats.invalid = true;
    return NULL;
}
bool storage_copy_in(struct storage *s, const struct rte_mbuf *m, struct storage_handle *out)
{
    if (!m || !out || rte_pktmbuf_priv_size(m->pool) < sizeof(struct packet_meta) ||
        !m->pkt_len || m->pkt_len > STORAGE_MAX_BYTES || m->nb_segs != 1 ||
        m->data_len != m->pkt_len) {
        s->stats.invalid_inputs++; s->stats.invalid = true; return false;
    }
    unsigned cells = (m->pkt_len + STORAGE_CELL_BYTES - 1) / STORAGE_CELL_BYTES;
    if (s->free_record == NONE) { s->stats.record_exhaustions++; s->stats.invalid = true; return false; }
    if (cells > s->cell_count - s->stats.cells_live) { s->stats.cell_exhaustions++; s->stats.invalid = true; return false; }
    uint32_t index = s->free_record;
    struct descriptor *d = &s->records[index];
    if (d->guard != FREE_GUARD || d->generation == UINT64_MAX) {
        s->stats.invalid_handles++; s->stats.invalid = true; return false;
    }
    s->free_record = d->link.next_free;
    rte_memcpy(&d->meta, packet_meta((struct rte_mbuf *)m), sizeof(d->meta));
    d->generation++; d->guard = LIVE_GUARD;
    d->flags = m->ol_flags & ~(RTE_MBUF_F_EXTERNAL | RTE_MBUF_F_INDIRECT);
    d->packet_type = m->packet_type; d->hash = m->hash.rss;
    d->length = m->pkt_len; d->port = m->port; d->vlan = m->vlan_tci; d->outer_vlan = m->vlan_tci_outer;
    d->link.first_cell = s->free_cell;
    const unsigned char *bytes = rte_pktmbuf_mtod(m, const unsigned char *);
    uint32_t cell = s->free_cell, last = cell;
    for (unsigned offset = 0; offset < m->pkt_len; offset += STORAGE_CELL_BYTES) {
        unsigned n = m->pkt_len-offset < STORAGE_CELL_BYTES ? m->pkt_len-offset : STORAGE_CELL_BYTES;
        rte_memcpy(s->payload[cell], bytes+offset, n);
        last = cell; cell = s->next[cell];
    }
    s->free_cell = cell; s->next[last] = NONE;
    s->stats.copied_in++; s->stats.records_live++; s->stats.cells_live += cells;
    if (s->stats.records_live > s->stats.records_peak) s->stats.records_peak = s->stats.records_live;
    if (s->stats.cells_live > s->stats.cells_peak) s->stats.cells_peak = s->stats.cells_live;
    *out = (struct storage_handle){index, d->generation};
    return true;
}
static void release(struct storage *s, struct descriptor *d, uint32_t index, uint32_t last)
{
    unsigned cells = (d->length+STORAGE_CELL_BYTES-1)/STORAGE_CELL_BYTES;
    s->next[last] = s->free_cell; s->free_cell = d->link.first_cell;
    d->guard = FREE_GUARD; d->link.next_free = s->free_record; s->free_record = index;
    s->stats.cells_live -= cells; s->stats.records_live--;
}
bool storage_materialize(struct storage *s, struct storage_handle h, struct rte_mempool *pool, struct rte_mbuf **out)
{
    if (!out || !pool) { s->stats.invalid_inputs++; s->stats.invalid = true; return false; }
    *out = NULL;
    if (rte_pktmbuf_priv_size(pool) < sizeof(struct packet_meta)) {
        s->stats.invalid_inputs++; s->stats.invalid = true; return false;
    }
    struct descriptor *d = lookup(s, h);
    if (!d) return false;
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
    unsigned char *bytes = m ? (unsigned char *)rte_pktmbuf_append(m, d->length) : NULL;
    if (!bytes) {
        if (m) rte_pktmbuf_free(m);
        s->stats.tx_exhaustions++; s->stats.invalid = true; return false;
    }
    rte_memcpy(packet_meta(m), &d->meta, sizeof(d->meta));
    m->port = d->port; m->vlan_tci = d->vlan; m->vlan_tci_outer = d->outer_vlan;
    m->packet_type = d->packet_type; m->hash.rss = d->hash;
    m->ol_flags |= d->flags;
    uint32_t cell = d->link.first_cell, last = cell;
    for (unsigned offset = 0; offset < d->length; offset += STORAGE_CELL_BYTES) {
        unsigned n = d->length-offset < STORAGE_CELL_BYTES ? d->length-offset : STORAGE_CELL_BYTES;
        rte_memcpy(bytes+offset, s->payload[cell], n);
        last = cell; cell = s->next[cell];
    }
    release(s, d, h.index, last); s->stats.materialized++; *out = m;
    return true;
}
bool storage_discard(struct storage *s, struct storage_handle h)
{
    struct descriptor *d = lookup(s, h);
    if (!d) return false;
    uint32_t last = d->link.first_cell;
    unsigned cells = (d->length+STORAGE_CELL_BYTES-1)/STORAGE_CELL_BYTES;
    for (unsigned i = 1; i < cells; ++i) last = s->next[last];
    release(s, d, h.index, last); s->stats.discarded++;
    return true;
}
struct storage_stats storage_stats(const struct storage *s) { return s->stats; }
