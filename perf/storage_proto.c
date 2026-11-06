/* SPDX-License-Identifier: Apache-2.0 */
/* Independent storage-only experiment; no engine, RX loop or TX loop is linked. */
#define _GNU_SOURCE
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <rte_cycles.h>
#include "../common/storage.h"
static const unsigned lengths[] = {60,64,128,129,508,512,513,1518};
static unsigned length_at(int kind, uint64_t n) { return kind < 0 ? (unsigned[]){60,508,1518}[n%3] : lengths[kind]; }
static uint64_t checksum;
static void fill(struct rte_mbuf *m, unsigned bytes, uint64_t seq)
{
    void *p = rte_pktmbuf_append(m, bytes); assert(p);
    memset(p, (unsigned char)seq, bytes);
    memset(packet_meta(m), 0, sizeof(struct packet_meta));
    packet_meta(m)->ingress_seq = seq; packet_meta(m)->arrival_ns = seq*1000;
    m->port = 1;
}
int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--describe")) {
        struct storage_layout l; assert(storage_layout(1, 1, &l));
        printf("{\"descriptor_bytes\":%"PRIu64",\"cell_bytes\":%"PRIu64",\"link_bytes\":%"PRIu64
            ",\"control_bytes\":%"PRIu64",\"control_allocation\":%"PRIu64",\"handle_bytes\":%zu,\"metadata_bytes\":%zu,\"core_slot_bytes\":%zu,\"cache_bytes\":0,\"descriptor_guard_included\":true}\n",
            l.descriptor_bytes, l.cell_bytes, l.link_bytes, l.control_bytes, l.control_allocation, sizeof(struct storage_handle),sizeof(struct packet_meta),hng_slot_metadata_bytes());
        return 0;
    }
    int n = rte_eal_init(argc, argv); assert(n >= 0);
    argc -= n; argv += n;
    /* EAL -- mode(baseline/hot/resident) length-index(-1=mix) trials */
    assert(argc == 4);
    const char *mode = argv[1]; int kind = atoi(argv[2]); unsigned trials = strtoul(argv[3],NULL,10);
    bool baseline = !strcmp(mode, "baseline"), resident = !strcmp(mode, "resident");
    assert((baseline || resident || !strcmp(mode,"hot")) && kind>=-1 && kind<8 && trials>=1 && trials<=10);
    uint32_t window = 96;
    unsigned average_cells = kind < 0 ? 11 : (length_at(kind,0)+63)/64;
    if (resident) {
        uint64_t per = 192+68*average_cells;
        window = (uint32_t)((UINT64_C(2147483648)+per-1)/per);
        window = (window+47)/48*48; /* Exact equal-packet mix and burst alignment. */
    }
    uint32_t cells = window*average_cells;
    struct storage_layout l; assert(storage_layout(window,cells,&l));
    struct storage *s = baseline ? NULL : storage_create(window,cells); assert(baseline || s);
    struct storage_handle *handles = baseline ? NULL : malloc((size_t)window*sizeof(*handles)); assert(baseline || handles);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("compact_proto",255,MBUF_CACHE_SIZE,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());assert(pool);
    if (!baseline) for (uint32_t i=0;i<window;++i) {
        struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);fill(m,length_at(kind,i),i);
        assert(storage_copy_in(s,m,handles+i));rte_pktmbuf_free(m);
    }
    unsigned operations = resident ? window : 1048576;
    operations = (operations+47)/48*48;
    for (unsigned trial=0;trial<trials;++trial) {
        uint64_t start=rte_get_timer_cycles();rte_rmb();
        for(unsigned first=0;first<operations;first+=16) {
            struct rte_mbuf *in[16];assert(!rte_pktmbuf_alloc_bulk(pool,in,16));
            for(unsigned j=0;j<16;++j) {
                uint32_t index=(first+j)%window;
                unsigned len=length_at(kind,index);
                fill(in[j],len,first+j+(uint64_t)trial*operations);
                if (!baseline) {
                    struct rte_mbuf *out=NULL;
                    assert(storage_materialize(s,handles[index],pool,&out));
                    checksum+=packet_meta(out)->ingress_seq+rte_pktmbuf_mtod(out,const unsigned char *)[0];
                    rte_pktmbuf_free(out);
                    assert(storage_copy_in(s,in[j],handles+index));
                } else checksum+=packet_meta(in[j])->ingress_seq+rte_pktmbuf_mtod(in[j],const unsigned char *)[0];
            }
            rte_pktmbuf_free_bulk(in,16);
        }
        rte_rmb();uint64_t ticks=rte_get_timer_cycles()-start;
        printf("{\"mode\":\"%s\",\"length_index\":%d,\"trial\":%u,\"operations\":%u,\"window\":%u,\"allocator_bytes\":%"PRIu64
            ",\"handle_bytes\":%zu,\"ns_per_packet\":%.6f,\"tsc_ticks_per_packet\":%.6f,\"timer_hz\":%"PRIu64"}\n",
            mode,kind,trial,operations,window,baseline?0:l.total_bytes,baseline?0:(size_t)window*sizeof(*handles),
            (double)ticks/rte_get_timer_hz()*1e9/operations,(double)ticks/operations,rte_get_timer_hz());fflush(stdout);
    }
    if (!baseline) {
        struct storage_stats stats=storage_stats(s);assert(!stats.invalid && stats.records_live==window && stats.cells_live==cells);
        for(unsigned i=0;i<window;++i)assert(storage_discard(s,handles[i]));
        stats=storage_stats(s);assert(!stats.records_live&&!stats.cells_live);
        assert(stats.copied_in==stats.materialized+stats.discarded);
        assert(storage_destroy(s));free(handles);
    }
    assert(rte_mempool_avail_count(pool)==pool->size);rte_mempool_free(pool);
    fprintf(stderr,"checksum=%"PRIu64"\n",checksum);assert(rte_eal_cleanup()==0);
}
