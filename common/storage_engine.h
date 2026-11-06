/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HNG_STORAGE_ENGINE_H
#define HNG_STORAGE_ENGINE_H
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
struct rte_mempool;
struct rte_mbuf;
struct HngPacket;
struct M7StorageInfo;
struct observer_packet;
struct m7_storage;
#define M7_STORAGE_FIELDS(X) \
    X(items_live) X(items_peak) X(records_live) X(records_peak) X(cells_live) X(cells_peak) \
    X(normal_live) X(normal_peak) X(copied_in) X(materialized) X(compact_discarded) \
    X(normal_retained) X(normal_returned) X(normal_discarded) \
    X(item_exhaustions) X(record_exhaustions) X(cell_exhaustions) X(normal_exhaustions) \
    X(materialize_exhaustions) X(core_exhaustions) X(invalid_inputs)
#define M7_VALUE(name) uint64_t name;
struct m7_storage_stats { M7_STORAGE_FIELDS(M7_VALUE) };
#undef M7_VALUE
#define M7_ATOMIC(name) _Atomic uint64_t name;
struct m7_storage_snapshot { M7_STORAGE_FIELDS(M7_ATOMIC) } __attribute__((aligned(64)));
#undef M7_ATOMIC
/* One scheduler owns this backend. The pool and plan stay live until destroy. */
struct m7_storage *m7_storage_create(const struct M7StorageInfo *plan,
    struct rte_mempool *pool, uint16_t egress_port, int socket);
/* Non-NULL takes source custody; failure leaves the source caller-owned. */
void *m7_storage_commit(struct m7_storage *s, const struct HngPacket *admitted);
/* Does not recycle the tag: the caller must pop that exact model head first. */
bool m7_storage_materialize(struct m7_storage *s, void *token, struct rte_mbuf **out);
bool m7_storage_release(struct m7_storage *s, void *token);
bool m7_storage_discard(struct m7_storage *s, void *token);
bool m7_storage_observation(struct m7_storage *s, void *token, struct observer_packet *out);
void m7_storage_core_exhausted(struct m7_storage *s);
bool m7_storage_handoff(struct m7_storage *s, bool full, bool backlog, uint64_t now_ns);
bool m7_storage_drained(const struct m7_storage *s);
bool m7_storage_destroy(struct m7_storage *s);
const char *m7_storage_reason(const struct m7_storage *s);
struct m7_storage_stats m7_storage_stats(const struct m7_storage *s);
#endif
