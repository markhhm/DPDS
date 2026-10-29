/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include "../core/worker.h"
#include "../core/transmit.h"

static struct lcore_parameters *parameters(struct pipeline_stats *stats, struct rte_ring *rx, struct rte_ring *tx)
{
    struct lcore_parameters *p = rte_zmalloc(NULL, sizeof(*p), 0);
    assert(p); p->stats=stats; p->receive_worker_ring=rx; p->worker_transmit_ring=tx; p->num_ports=2;
    return p;
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("rings.json"));
    assert(host_pool_objects(0, 2) == 0);
    assert(host_prepare_port(0) == 0 && host_prepare_port(1) == 0);
    struct port_resources saved0 = host.resources[0], saved1 = host.resources[1];
    /* Synthetic adjusted PMD limits: RX512 + egressTX768 + reserves6272 + storage64. */
    host.resources[0].rx_descriptors = 512;
    host.resources[0].tx_descriptors = 1024;
    host.resources[1].tx_descriptors = 768;
    assert(host_pool_objects(0, 2) == 7616);
    host.lines[2].egress_port = 0;
    assert(host_pool_objects(0, 2) == 8640);
    size_t saved_capacity = host.lines[0].storage_capacity_packets;
    host.lines[0].storage_capacity_packets = UINT_MAX - 7553u;
    assert(host_pool_objects(0, 2) == 0);
    host.lines[0].storage_capacity_packets = saved_capacity;
    host.lines[2].egress_port = 1;
    host.resources[0] = saved0; host.resources[1] = saved1;
    struct rte_mempool *pool = rte_pktmbuf_pool_create("rings_test", 1023, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool && initialize_port(0,pool)==0 && initialize_port(1,pool)==0);
    struct rte_ring *rx=rte_ring_create("test_rx",64,rte_socket_id(),RING_F_SP_ENQ|RING_F_SC_DEQ);
    struct rte_ring *tx=rte_ring_create("test_tx",64,rte_socket_id(),RING_F_SP_ENQ|RING_F_SC_DEQ);
    assert(rx && tx);
    struct pipeline_stats stats={0};
    const uint16_t ports[]={0,1,0,0,1}, vlans[]={100,100,100,101,101};
    const uint64_t seq[]={0,0,1,2,1};
    for(unsigned i=0;i<5;++i) {
        struct rte_mbuf *m=rte_pktmbuf_alloc(pool); assert(m);
        uint8_t *d=(uint8_t *)rte_pktmbuf_append(m,76); assert(d);
        memset(d,0,76); d[12]=0x81; d[15]=vlans[i]; d[16]=0x08;
        *packet_meta(m)=(struct packet_meta){.ingress_port=ports[i],.ingress_seq=seq[i]};
        assert(rte_ring_enqueue(rx,m)==0);
    }
    /* Completion flags can be true while the final partial bursts remain in rings. */
    quit_signal_worker=1;
    assert(lcore_worker(parameters(&stats,rx,tx))==0);
    assert(rte_ring_empty(rx) && quit_signal_transmit && rte_ring_count(tx)==4);
    assert(lcore_transmit(parameters(&stats,rx,tx))==0);
    assert(rte_ring_empty(tx) && stats.worker.received==5 && stats.worker.emulator_drops==1);
    assert(stats.tx.transmitted==4 && stats.tx.transmitted_bytes==304 && stats.tx.tx_drops==0);
    assert(close_port(0)==0 && close_port(1)==0);
    host_cleanup(); assert(!host.invalid && rte_mempool_avail_count(pool)==1023);
    rte_ring_free(rx); rte_ring_free(tx); rte_mempool_free(pool);
    assert(rte_eal_cleanup()==0);
    puts("nonempty shutdown rings and final partial bursts drain exactly once to null TX: PASS");
}
