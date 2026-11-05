/* SPDX-License-Identifier: Apache-2.0 */
/* Emulate a PMD retaining accepted TX mbufs until the egress port closes.
 * The pools and mbufs are real. Ingress pool k is retained by egress 1-k. */
#include <assert.h>
#include <stdlib.h>
#include "../common/common.h"
#define HELD 986
static struct rte_mempool *pools[2];
static struct rte_mbuf *held[2][HELD];
int __real_initialize_port(uint16_t port, struct rte_mempool *pool);
int __real_close_port(uint16_t port);
void __real_rte_mempool_free(struct rte_mempool *pool);
int __wrap_initialize_port(uint16_t port, struct rte_mempool *pool)
{
    int result = __real_initialize_port(port, pool);
    if (!result && port < 2) {
        pools[port] = pool;
        assert(rte_pktmbuf_alloc_bulk(pool, held[port], HELD) == 0);
    }
    return result;
}
int __wrap_close_port(uint16_t port)
{
    if (port < 2) {
        rte_pktmbuf_free_bulk(held[1-port], HELD);
        if (port == 0 && getenv("HNG_TEST_CLOSE_INVALID")) host_invalidate("fixture port cleanup failure");
    }
    return __real_close_port(port);
}
void __wrap_rte_mempool_free(struct rte_mempool *pool)
{
    if (pool == pools[0] || pool == pools[1])
        assert(rte_mempool_avail_count(pool) == pool->size);
    __real_rte_mempool_free(pool);
}

void __real_host_mark_launched(void);
void __wrap_host_mark_launched(void)
{
    __real_host_mark_launched();
    if (getenv("HNG_TEST_START_INVALID")) host_invalidate("fixture runtime invalid");
}
