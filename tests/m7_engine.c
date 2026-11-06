/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "m7_oracle.h"
static uint64_t repeated_searches;
static bool refuse_head;
intptr_t __real_hng_lines_pop_merged(struct Line *const*,const uint64_t*,uintptr_t,uint64_t,uint64_t,struct HngPacket*);
intptr_t __wrap_hng_lines_pop_merged(struct Line *const*l,const uint64_t*w,uintptr_t n,uint64_t now,uint64_t limit,struct HngPacket*p)
{
    repeated_searches++;return __real_hng_lines_pop_merged(l,w,n,now,limit,p);
}
bool __real_hng_head_pop(struct HngHead*);
bool __wrap_hng_head_pop(struct HngHead *head)
{
    if(refuse_head){refuse_head=false;return false;}
    return __real_hng_head_pop(head);
}
static uint64_t clock_ns=1000000;
uint64_t __wrap_host_now_ns(void){return clock_ns;}
static void exhausted_output_pool(struct rte_mempool *pool)
{
    assert(host_load("m7-engine.json"));host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_storage_bind_pool(0,pool,rte_socket_id()));
    struct worker_stats stats={0};struct worker_state w;worker_state_init_owner(&w,&stats,0);w.output_limit=0;
    struct rte_mbuf *m=rte_pktmbuf_alloc(pool),*out[MAX_BURST_SIZE];assert(m);
    unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,60);assert(b);memset(b,0,60);b[12]=0x81;b[15]=100;
    *packet_meta(m)=(struct packet_meta){0};host_publish(0,1,clock_ns);
    assert(worker_step_at(&w,&m,1,out,clock_ns)==0 && !host.invalid);
    struct rte_mbuf **held=calloc(pool->size,sizeof(*held));assert(held);
    for(unsigned i=0;i<pool->size;++i){held[i]=rte_pktmbuf_alloc(pool);assert(held[i]);}
    w.output_limit=1;assert(worker_step_at(&w,NULL,0,out,clock_ns)==0);
    assert(host.invalid && w.aborted && stats.shutdown_drops==1 && !stats.emulator_drops);
    assert(!strcmp(host.diagnostics.invalid_reason,"M7 materialization pool exhausted"));
    struct m7_storage_stats used=m7_storage_stats(host.m7[0]);
    assert(used.materialize_exhaustions==1 && used.compact_discarded==1 && !used.items_live && !used.records_live && !used.cells_live);
    m7_oracle_worker(&stats);m7_oracle_storage(0);
    if(m7_oracle)fprintf(m7_oracle,"invalid=%u,reason=%s\n",(unsigned)host.invalid,host.diagnostics.invalid_reason);
    for(unsigned i=0;i<pool->size;++i)rte_pktmbuf_free(held[i]);
    free(held);
    worker_end_input(&w);worker_state_cleanup(&w);host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);
}
static void core_limit(struct rte_mempool *pool)
{
    assert(host_load("m7-core-limit.json"));host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_storage_bind_pool(0,pool,rte_socket_id()));
    struct worker_stats stats={0};struct worker_state w;worker_state_init_owner(&w,&stats,0);w.output_limit=0;
    for(unsigned i=0;i<4;++i) {
        struct rte_mbuf *m=rte_pktmbuf_alloc(pool),*out[MAX_BURST_SIZE];assert(m);
        unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,1518);assert(b);memset(b,0,1518);b[12]=0x81;b[15]=100;
        *packet_meta(m)=(struct packet_meta){.ingress_seq=i,.arrival_ns=i*20000000};
        assert(worker_step_at(&w,&m,1,out,0)==0);
    }
    assert(host.invalid && stats.storage_drops==1 && !stats.emulator_drops && stats.shutdown_drops==3);
    char *report=NULL;size_t size=0;FILE *f=open_memstream(&report,&size);assert(f);host_diagnostics_report(f);fclose(f);
    assert(strstr(report,"m7_line0_core_exhaustions=1\n"));free(report);
    m7_oracle_worker(&stats);m7_oracle_storage(0);
    if(m7_oracle)fprintf(m7_oracle,"invalid=%u,reason=%s\n",(unsigned)host.invalid,host.diagnostics.invalid_reason);
    worker_end_input(&w);worker_state_cleanup(&w);host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);
}
static void rejected_head_after_materialize(struct rte_mempool *pool)
{
    assert(host_load("m7-engine.json"));host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_storage_bind_pool(0,pool,rte_socket_id()));
    struct worker_stats stats={0};struct worker_state w;worker_state_init_owner(&w,&stats,0);w.output_limit=0;
    struct rte_mbuf *m=rte_pktmbuf_alloc(pool),*out[MAX_BURST_SIZE];assert(m);
    unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,60);assert(b);memset(b,0,60);b[12]=0x81;b[15]=100;
    *packet_meta(m)=(struct packet_meta){0};host_publish(0,1,clock_ns);
    assert(worker_step_at(&w,&m,1,out,clock_ns)==0 && !host.invalid);
    refuse_head=true;w.output_limit=1;assert(worker_step_at(&w,NULL,0,out,clock_ns)==0);
    assert(host.invalid && w.aborted && stats.shutdown_drops==1 && !stats.sent);
    assert(!strcmp(host.diagnostics.invalid_reason,"model head changed during M7 materialization"));
    struct m7_storage_stats used=m7_storage_stats(host.m7[0]);
    assert(used.materialized==1 && !used.compact_discarded && m7_storage_drained(host.m7[0]));
    worker_end_input(&w);worker_state_cleanup(&w);host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);
}
int main(int argc,char **argv)
{
    assert(rte_eal_init(argc,argv)>=0);m7_oracle_open("engine");
    assert(host_load("m7-engine.json"));
    host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_prepare_port(0)==0 && host_prepare_port(1)==0);
    int socket;struct rte_mempool *pool=host_create_pool(0,rte_lcore_count(),&socket);assert(pool);
    unsigned transport=host.resources[0].rx_descriptors+SCHED_RX_RING_SZ+SCHED_TX_RING_SZ+
        MBUF_CACHE_SIZE*rte_lcore_count()+HNG_MAX_LINES*TX_PENDING_PER_LINE+4*MAX_BURST_SIZE+
        host.resources[1].tx_descriptors;
    assert(pool->size==transport+17); /* Normal retained reserve, not 19 model slots. */
    struct pipeline_stats stats={0};struct worker_state worker;
    worker_state_init_owner(&worker,&stats.worker,0);worker.output_limit=0;
    struct rte_mbuf *input=rte_pktmbuf_alloc(pool),*output[MAX_BURST_SIZE];assert(input);
    unsigned char *bytes=(unsigned char *)rte_pktmbuf_append(input,60);assert(bytes);memset(bytes,0,60);
    bytes[12]=0x81;bytes[15]=100;bytes[16]=0x88;bytes[17]=0xb5;
    memset(packet_meta(input),0,sizeof(struct packet_meta));
    host_publish(0,1,clock_ns);
    assert(worker_step_at(&worker,&input,1,output,clock_ns)==0);
    assert(rte_mempool_avail_count(pool)==pool->size); /* accepted source already returned */
    worker.output_limit=1;
    assert(worker_step_at(&worker,NULL,0,output,clock_ns)==1);
    assert(repeated_searches==0); /* M7 must consume the selected head without a second group search. */
    assert(output[0]->pkt_len==60 && output[0]->port==1);
    assert(packet_meta(output[0])->deadline_ns==672000 && packet_meta(output[0])->release_ns==672000);
    assert(rte_pktmbuf_mtod(output[0],unsigned char *)[12]==0x81);
    rte_pktmbuf_free(output[0]);
    worker_end_input(&worker);assert(worker_step_at(&worker,NULL,0,output,clock_ns)==0);
    worker_state_cleanup(&worker);assert(!worker_has_backlog(&worker) && !host.invalid);
    m7_oracle_worker(&stats.worker);m7_oracle_storage(0);
    host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);
    char *report=NULL;size_t report_size=0;FILE *stream=open_memstream(&report,&report_size);assert(stream);
    host_diagnostics_report(stream);fclose(stream);
    assert(strstr(report,"m7_line0_items_live=0\n"));
    assert(strstr(report,"m7_line0_copied_in=1\n"));
    assert(strstr(report,"m7_line0_materialized=1\n"));
    assert(strstr(report,"m7_line0_normal_reserve=17\n"));free(report);
    exhausted_output_pool(pool);core_limit(pool);m7_oracle_close();rejected_head_after_materialize(pool);
    rte_mempool_free(pool);assert(rte_eal_cleanup()==0);m7_oracle_close();
    puts("M7 production pool quota, accepted copy custody and original deadlines: PASS");
}
