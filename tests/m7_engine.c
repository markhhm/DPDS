/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "../core/transmit.h"
#include "m7_oracle.h"
static uint64_t storage_reads;
struct m7_storage_stats __real_m7_storage_stats(const struct m7_storage*);
struct m7_storage_stats __wrap_m7_storage_stats(const struct m7_storage *s){storage_reads++;return __real_m7_storage_stats(s);}
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
static unsigned mixed_calls, mixed_delivered;
static const unsigned mixed_sizes[]={60,1518,508,1518};
static struct packet_meta mixed_meta[4];
static uint16_t mixed_prefix(uint16_t port,uint16_t queue,struct rte_mbuf **batch,uint16_t count)
{
    assert(port==1 && queue==0 && count==4-mixed_delivered);
    for(unsigned i=0;i<count;++i) {
        unsigned seq=mixed_delivered+i;struct rte_mbuf *m=batch[i];
        assert(packet_meta(m)->ingress_seq==seq && m->pkt_len==mixed_sizes[seq]);
        assert(packet_meta(m)->deadline_ns==mixed_meta[seq].deadline_ns);
        assert(packet_meta(m)->release_ns==mixed_meta[seq].release_ns && rte_mbuf_refcnt_read(m)==1);
        unsigned char *b=rte_pktmbuf_mtod(m,unsigned char *);
        for(unsigned j=18;j<m->pkt_len;++j)assert(b[j]==(unsigned char)(seq+j));
    }
    /* Transfer one prefix frame, then refuse both compact and retained suffixes. */
    if(mixed_calls++==0){rte_pktmbuf_free(batch[0]);mixed_delivered++;return 1;}
    return 0;
}
static void mixed_materialize_refused_terminal(struct rte_mempool *pool)
{
    assert(host_load("m7-mixed.json"));host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_storage_bind_pool(0,pool,rte_socket_id()));
    struct pipeline_stats stats={0};struct worker_state w;worker_state_init_owner(&w,&stats.worker,0);w.output_limit=0;
    struct rte_mbuf *out[MAX_BURST_SIZE],*original[4];
    for(unsigned seq=0;seq<4;++seq) {
        struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);original[seq]=m;
        unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,mixed_sizes[seq]);assert(b);
        for(unsigned j=0;j<m->pkt_len;++j)b[j]=(unsigned char)(seq+j);
        b[12]=0x81;b[13]=0;b[14]=0;b[15]=100;b[16]=0x88;b[17]=0xb5;
        *packet_meta(m)=(struct packet_meta){.ingress_seq=seq,.arrival_ns=seq*20000000};
        assert(worker_step_at(&w,&m,1,out,0)==0 && !host.invalid);
    }
    worker_end_input(&w);clock_ns=100000000;w.output_limit=MAX_BURST_SIZE;
    assert(worker_step_at(&w,NULL,0,out,clock_ns)==4 && !host.invalid);
    for(unsigned seq=0;seq<4;++seq) {
        assert(packet_meta(out[seq])->ingress_seq==seq);mixed_meta[seq]=*packet_meta(out[seq]);
        if(mixed_sizes[seq]>1020)assert(out[seq]==original[seq]);
    }
    struct m7_storage_stats used=m7_storage_stats(host.m7[0]);
    assert(used.copied_in==2 && used.normal_retained==2 && used.materialized==2 && used.normal_returned==2);
    assert(m7_storage_drained(host.m7[0]));
    struct tx_state tx;tx_state_init(&tx,&stats);tx.burst=mixed_prefix;mixed_calls=mixed_delivered=0;
    tx_step_at(&tx,out,4,clock_ns);
    assert(mixed_calls==1 && mixed_delivered==1 && tx.pending[0].len==3 && !host.invalid);
    assert(rte_mempool_avail_count(pool)==pool->size-3);
    /* The exact timeout boundary keeps the refused suffix; one ns later it must
     * terminate without offering expired materialized frames to the PMD again. */
    tx_step_at(&tx,NULL,0,clock_ns+TERMINAL_TIMEOUT_NS);
    assert(mixed_calls==2 && tx.pending[0].len==3 && !host.invalid);
    tx_step_at(&tx,NULL,0,clock_ns+TERMINAL_TIMEOUT_NS+1);
    assert(host.invalid && !strcmp(host.diagnostics.invalid_reason,"TX retry timeout"));
    assert(mixed_calls==2 && mixed_delivered==1 && !tx_has_backlog(&tx));
    assert(stats.worker.sent==4 && !stats.worker.emulator_drops && !stats.worker.shutdown_drops);
    assert(stats.tx.transmitted==1 && stats.tx.tx_drops==3 && stats.tx.line_invalid_drops[0]==3);
    assert(m7_storage_drained(host.m7[0]) && rte_mempool_avail_count(pool)==pool->size);
    tx_state_cleanup(&tx);worker_state_cleanup(&w);host_cleanup();clock_ns=1000000;
}
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
    assert(atomic_load(&host.m7_snapshot[0].materialize_exhaustions)==1);
    assert(!atomic_load(&host.m7_snapshot[0].items_live) && !atomic_load(&host.m7_snapshot[0].records_live));
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
static void publication_boundaries(struct rte_mempool *pool)
{
    assert(host_load("m7-engine.json"));host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    assert(host_storage_bind_pool(0,pool,rte_socket_id()));
    struct worker_stats stats={0};struct worker_state w;worker_state_init_owner(&w,&stats,0);
    struct rte_mbuf *out[MAX_BURST_SIZE];storage_reads=0;
    for(unsigned i=0;i<100;++i)assert(worker_step_at(&w,NULL,0,out,clock_ns)==0);
    assert(storage_reads==0); /* Unchanged idle steps must not aggregate or publish. */
    for(unsigned i=0;i<65;++i) {
        struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);
        unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,60);assert(b);memset(b,0,60);b[12]=0x81;b[15]=100;
        *packet_meta(m)=(struct packet_meta){.ingress_seq=i,.arrival_ns=i*1000000};clock_ns=(i+1)*1000000;
        host_publish(0,i+1,clock_ns);assert(worker_step_at(&w,&m,1,out,clock_ns)==1);rte_pktmbuf_free(out[0]);
        if(i<63)assert(storage_reads==0);
        if(i==63)assert(storage_reads==1 && atomic_load(&host.m7_snapshot[0].copied_in)==64);
    }
    assert(atomic_load(&host.m7_snapshot[0].copied_in)==64);
    assert(worker_step_at(&w,NULL,0,out,clock_ns)==0);
    assert(atomic_load(&host.m7_snapshot[0].copied_in)==65); /* idle flush */
    struct rte_mbuf *last=rte_pktmbuf_alloc(pool);assert(last);
    unsigned char *b=(unsigned char *)rte_pktmbuf_append(last,60);assert(b);memset(b,0,60);b[12]=0x81;b[15]=100;
    *packet_meta(last)=(struct packet_meta){.ingress_seq=65,.arrival_ns=65000000};clock_ns=66000000;
    host_publish(0,66,clock_ns);assert(worker_step_at(&w,&last,1,out,clock_ns)==1);rte_pktmbuf_free(out[0]);
    assert(atomic_load(&host.m7_snapshot[0].copied_in)==65);
    /* Final cleanup must flush pending values even without another worker step. */
    worker_end_input(&w);worker_state_cleanup(&w);
    assert(atomic_load(&host.m7_snapshot[0].copied_in)==66 && !host.invalid);
    host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);clock_ns=1000000;
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
    assert(atomic_load(&host.m7_snapshot[0].copied_in)==1); /* END forces pending state. */
    worker_state_cleanup(&worker);assert(!worker_has_backlog(&worker) && !host.invalid);
    m7_oracle_worker(&stats.worker);m7_oracle_storage(0);
    host_cleanup();assert(rte_mempool_avail_count(pool)==pool->size);
    char *report=NULL;size_t report_size=0;FILE *stream=open_memstream(&report,&report_size);assert(stream);
    host_diagnostics_report(stream);fclose(stream);
    assert(strstr(report,"m7_line0_items_live=0\n"));
    assert(strstr(report,"m7_line0_copied_in=1\n"));
    assert(strstr(report,"m7_line0_materialized=1\n"));
    assert(strstr(report,"m7_line0_normal_reserve=17\n"));free(report);
    exhausted_output_pool(pool);core_limit(pool);m7_oracle_close();rejected_head_after_materialize(pool);publication_boundaries(pool);
    mixed_materialize_refused_terminal(pool);
    rte_mempool_free(pool);assert(rte_eal_cleanup()==0);m7_oracle_close();
    puts("M7 production pool quota, accepted copy custody and original deadlines: PASS");
}
