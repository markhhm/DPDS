/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HNG_COMPACT_STORAGE_H
#define HNG_COMPACT_STORAGE_H
/* Independent single-owner prototype. It is NOT linked into the engine. */
#include "common.h"
#define STORAGE_MAX_BYTES 1518u
#define STORAGE_CELL_BYTES 64u
struct storage;
struct storage_handle { uint32_t index; uint64_t generation; };
struct storage_stats {
    uint64_t records_live, cells_live, records_peak, cells_peak;
    uint64_t copied_in, materialized, discarded;
    uint64_t record_exhaustions, cell_exhaustions, tx_exhaustions;
    uint64_t invalid_inputs, invalid_handles;
    bool invalid;
};
struct storage_layout {
    uint64_t records, cells, descriptor_bytes, cell_bytes, link_bytes;
    uint64_t control_bytes, control_allocation, descriptor_allocation, payload_allocation, links_allocation, total_bytes;
};
bool storage_layout(uint32_t records, uint32_t cells, struct storage_layout *out);
struct storage *storage_create(uint32_t records, uint32_t cells);
/* Never drops live objects; caller drains/discards them first. */
bool storage_destroy(struct storage *s);
/* Source remains caller-owned. False never consumes source or partial capacity. */
bool storage_copy_in(struct storage *s, const struct rte_mbuf *source, struct storage_handle *out);
/* Success consumes the record; failure keeps it available for drain/discard. */
bool storage_materialize(struct storage *s, struct storage_handle handle,
    struct rte_mempool *pool, struct rte_mbuf **out);
bool storage_discard(struct storage *s, struct storage_handle handle);
struct storage_stats storage_stats(const struct storage *s);
#endif
