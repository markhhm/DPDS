#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include "../core/worker.h"

static struct rte_mbuf *packet(struct rte_mempool *pool, uint16_t port, uint16_t vlan, uint64_t seq, uint64_t arrival)
{
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
    assert(m);
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, 76);
    assert(d);
    memset(d, 0, 76);
    d[12] = 0x81; d[13] = 0x00; d[14] = vlan >> 8; d[15] = vlan; d[16] = 0x08;
    memset(packet_meta(m), 0, sizeof(struct packet_meta));
    packet_meta(m)->ingress_port = port;
    packet_meta(m)->ingress_seq = seq;
    packet_meta(m)->arrival_ns = arrival;
    m->port = port;
    return m;
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("worker_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool && host_load("config.json"));
    struct pipeline_stats stats = {0};
    struct worker_state worker;
    worker_state_init(&worker, &stats, 2);
    struct rte_mbuf *batch[8], *out[MAX_BURST_SIZE];
    batch[0] = packet(pool, 7, 100, 0, 0);
    batch[1] = packet(pool, 13, 100, 0, 0);
    batch[2] = packet(pool, 7, 100, 1, 0);
    batch[3] = packet(pool, 7, 101, 2, 0);
    batch[4] = packet(pool, 13, 101, 1, 0);
    assert(worker_step_at(&worker, batch, 5, out, 0) == 0);
    assert(stats.worker.emulator_drops == 1);
    /* A clock advance cannot certify unseen RX/handoff input. */
    assert(worker_step_at(&worker, NULL, 0, out, 1000) == 0);
    host_publish(7, 6, 1000);
    host_publish(13, 6, 1000);
    assert(worker_step_at(&worker, NULL, 0, out, 1000) == 0);
    batch[0] = packet(pool, 7, 100, 3, 0);
    assert(worker_step_at(&worker, batch, 1, out, 1000) == 4);
    assert(stats.worker.emulator_drops == 2);
    for (unsigned i = 0; i < 4; ++i) {
        assert(out[i]->port == (packet_meta(out[i])->ingress_port == 7 ? 13 : 7));
        const uint8_t *d = rte_pktmbuf_mtod(out[i], const uint8_t *);
        assert(d[12] == 0x81 && d[13] == 0 && d[15] == packet_meta(out[i])->vlan_id);
        rte_pktmbuf_free(out[i]);
    }
    /* Untagged, stacked tags, unsupported chain and unknown VLAN reject. */
    batch[0] = packet(pool, 7, 100, 4, 1000);
    rte_pktmbuf_mtod(batch[0], uint8_t *)[12] = 0x08;
    batch[1] = packet(pool, 7, 100, 5, 1000);
    rte_pktmbuf_mtod(batch[1], uint8_t *)[16] = 0x81;
    rte_pktmbuf_mtod(batch[1], uint8_t *)[17] = 0;
    batch[2] = packet(pool, 7, 100, 6, 1000);
    batch[2]->ol_flags = RTE_MBUF_F_RX_VLAN_STRIPPED;
    batch[3] = packet(pool, 7, 999, 7, 1000);
    assert(worker_step_at(&worker, batch, 4, out, 1000) == 0);
    assert(stats.worker.invalid_drops == 4);
    worker_end_input(&worker);
    assert(worker_step_at(&worker, NULL, 0, out, 1000) == 0);
    assert(!worker_has_backlog(&worker));
    worker_state_cleanup(&worker);
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("four C/Q states, VLAN, sparse ports, certified frontier, mbuf drain: PASS");
}
