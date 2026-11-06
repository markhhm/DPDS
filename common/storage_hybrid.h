/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HNG_STORAGE_HYBRID_H
#define HNG_STORAGE_HYBRID_H
/* Hybrid custody primitive shared by the standalone experiment and M7 backend. */
#include "storage.h"
#define HYBRID_DEFAULT_HOLD_NS UINT64_C(10000000)
enum hybrid_kind { HYBRID_EMPTY, HYBRID_RECORD, HYBRID_MBUF };
struct hybrid_item {
    union { uint64_t generation; struct rte_mbuf *mbuf; } value;
    uint32_t index;
    enum hybrid_kind kind;
};
_Static_assert(sizeof(struct hybrid_item) == 16, "account the real tagged item size");
struct hybrid {
    struct storage *compact;
    uint64_t hold_ns, full_since_ns, retained_live, retained_peak, retained_total, returned_total, discarded_total;
    uint16_t threshold_l2;
    bool m7, full, invalid;
    const char *reason;
};
bool hybrid_init(struct hybrid *s, struct storage *compact, bool m7, uint16_t threshold_l2, uint64_t hold_ns);
/* A successful put takes source ownership; failure leaves source and item unchanged.
 * Each item is linear: it must not be copied while live. Reuse only once EMPTY. */
bool hybrid_put(struct hybrid *s, struct rte_mbuf *source, struct hybrid_item *item);
/* Success consumes item. Large packets return the original pointer without allocation. */
bool hybrid_get(struct hybrid *s, struct hybrid_item *item, struct rte_mempool *pool, struct rte_mbuf **out);
bool hybrid_discard(struct hybrid *s, struct hybrid_item *item);
bool hybrid_drained(const struct hybrid *s);
/* Explicit clock: enforce the registered full-ring failure threshold. */
bool hybrid_handoff(struct hybrid *s, bool full, bool model_backlog, uint64_t now_ns);
#endif
