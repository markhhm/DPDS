/* SPDX-License-Identifier: Apache-2.0 */
/* Same model, different custody backend: every output byte and counter agrees. */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "m7_oracle.h"
static const unsigned sizes[]={60,64,128,129,508,512,513,1518};
static uint64_t now_ns=1000000000;
uint64_t __wrap_host_now_ns(void){return now_ns;}
#define N 64
struct result {struct packet_meta meta[2][N];struct HngStats stats[2];unsigned seq[2][N],count[2];};
/* Compare every declared field, not C/Rust ABI padding. Per-packet opaque
 * private-byte round trips are separately checked by the backend tests. */
#define META_FIELDS(X) \
    X(arrival_ns) X(ingress_seq) X(release_ns) X(deadline_ns) X(pending_since_ns) X(retry_since_ns) X(retry_active) \
    X(observation.identity) X(observation.ingress_seq) X(observation.selection_seq) X(observation.arrival_ns) \
    X(observation.handoff_seq) X(observation.gate_ns) X(observation.rx_flags) \
    X(observation.source) X(observation.destination) X(observation.length) \
    X(observation.source_port) X(observation.destination_port) X(observation.ingress_port) X(observation.line) \
    X(observation.vlan_id) X(observation.segments) X(observation.handoff_queue) \
    X(observation.identity_kind) X(observation.selected_late) X(observation.input_error) \
    X(observation.classified) X(observation.stripped) X(ingress_port) X(vlan_id) X(line)
#define MODEL_FIELDS(X) \
    X(accepted) X(accepted_bytes) X(accepted_l2_bytes) X(accepted_wire_bytes) \
    X(dequeued) X(dequeued_bytes) X(dequeued_l2_bytes) X(dequeued_wire_bytes) \
    X(q_drops) X(q_drop_bytes) X(q_drop_payload_bytes) X(storage_drops) X(storage_drop_bytes) \
    X(invalid_drops) X(invalid_drop_bytes) X(selected_late) X(q_bytes) X(q_high_water_bytes) \
    X(storage_bytes) X(storage_high_water_bytes) X(storage_packets) X(storage_high_water_packets) \
    X(admission_watermark_ns) X(ledger_watermark_ns) X(input_complete) \
    X(trace_loss_drops) X(trace_loss_bytes) X(trace_outage_drops) X(trace_outage_bytes) \
    X(clamp_packets) X(clamp_extra_ns) X(clamp_max_ns)
static bool compare_diagnostics;
#define VALUE_EQ(name) if(a->name!=b->name) { \
    if(compare_diagnostics)fprintf(stderr,"different field %s: old=%"PRIu64" m7=%"PRIu64"\n",#name,(uint64_t)a->name,(uint64_t)b->name); \
    return false; }
static bool same_meta(const struct packet_meta *a,const struct packet_meta *b) {META_FIELDS(VALUE_EQ) return true;}
static bool same_model(const struct HngStats *a,const struct HngStats *b) {MODEL_FIELDS(VALUE_EQ) return true;}
#undef VALUE_EQ
static bool same_result(const struct result *a,const struct result *b)
{
    if(memcmp(a->seq,b->seq,sizeof(a->seq)) || memcmp(a->count,b->count,sizeof(a->count)))return false;
    for(unsigned k=0;k<2;++k) {
        if(!same_model(&a->stats[k],&b->stats[k]))return false;
        for(unsigned i=0;i<N;++i)if(!same_meta(&a->meta[k][i],&b->meta[k][i]))return false;
    }
    return true;
}
static void padding_is_not_a_counter(void)
{
    struct result a={0},b={0};
    size_t from=offsetof(struct HngStats,input_complete)+sizeof(b.stats[0].input_complete);
    size_t to=offsetof(struct HngStats,trace_loss_drops);assert(to>from);
    memset((unsigned char *)&b.stats[0]+from,0xa5,to-from);
    /* Every declared value is still zero. A different legal ABI padding
     * representation cannot be a model or packet difference. */
    assert(same_result(&a,&b));
    from=offsetof(struct packet_meta,retry_active)+sizeof(b.meta[0][0].retry_active);
    to=offsetof(struct packet_meta,observation);assert(to>from);
    memset((unsigned char *)&b.meta[0][0]+from,0x5a,to-from);
    assert(same_result(&a,&b));
    /* Every declared packet/model field must still make the oracle fail. */
#define MUTATE_MODEL(name) b.stats[0].name=1;assert(!same_result(&a,&b));b.stats[0].name=0;
    MODEL_FIELDS(MUTATE_MODEL)
#undef MUTATE_MODEL
#define MUTATE_META(name) b.meta[0][0].name=1;assert(!same_result(&a,&b));b.meta[0][0].name=0;
    META_FIELDS(MUTATE_META)
#undef MUTATE_META
}
static struct rte_mempool *pools[2];
static void configuration(bool m7,unsigned q,const char *p,char *path)
{
    int fd=mkstemp(path);assert(fd>=0);FILE *f=fdopen(fd,"w");assert(f);
    fputs("{\"schema\":\"hng-v1\",\"lines\":[",f);
    for(unsigned k=0;k<2;++k) {
        fprintf(f,"%s{\"name\":\"l%u\",\"ingress_port\":%u,\"egress_port\":%u,\"vlan_id\":100,"
          "\"model\":{\"c_bps\":8000000000,\"q_bytes\":%u,\"t_ns\":3000,\"delta_ns\":5000,\"p\":\"%s\","
          "\"seed\":123,\"storage_capacity_packets\":4096,\"storage_capacity_bytes\":6217728},"
          "\"envelope\":{\"min_q_bytes\":64,\"max_q_bytes\":1522,\"burst_bound_packets\":64},\"trace\":null",
          k?",":"",k,k,1-k,q,p);
        if(m7)fputs(",\"m7_storage\":{\"mode\":\"m7-hybrid-v1\",\"hold_ns\":10,\"composition\":["
          "{\"l2_bytes\":64,\"weight\":1},{\"l2_bytes\":68,\"weight\":1},{\"l2_bytes\":132,\"weight\":1},"
          "{\"l2_bytes\":133,\"weight\":1},{\"l2_bytes\":512,\"weight\":1},{\"l2_bytes\":516,\"weight\":1},"
          "{\"l2_bytes\":517,\"weight\":1},{\"l2_bytes\":1522,\"weight\":1}]}",f);
        fputs("}",f);
    }
    fputs("]}\n",f);assert(fclose(f)==0);
}
static struct rte_mbuf *packet(unsigned port,unsigned seq)
{
    struct rte_mbuf *m=rte_pktmbuf_alloc(pools[port]);assert(m);
    unsigned size=sizes[seq%8];unsigned char *b=(unsigned char *)rte_pktmbuf_append(m,size);assert(b);
    for(unsigned i=0;i<size;++i)b[i]=(unsigned char)(seq+i*13);
    b[12]=0x81;b[13]=0;b[14]=0;b[15]=100;b[16]=0x88;b[17]=0xb5;
    *packet_meta(m)=(struct packet_meta){.ingress_port=port,.ingress_seq=seq,.arrival_ns=(seq/8)*10000};
    packet_meta(m)->observation=(struct observer_packet){.identity=seq+1,.ingress_port=port,.vlan_id=100,
        .identity_kind=2,.segments=1,.classified=true,.length=size,.arrival_ns=(seq/8)*10000,.ingress_seq=seq};
    m->hash.rss=seq+99;m->packet_type=0x123;m->vlan_tci=100;m->vlan_tci_outer=101;m->ol_flags=RTE_MBUF_F_RX_RSS_HASH;
    return m;
}
static void run(bool m7,unsigned q,const char *p,struct result *r,bool aborting)
{
    if(m7_oracle)fprintf(m7_oracle,"run,m7=%u,q=%u,p=%s,abort=%u\n",m7,q,p,aborting);
    char path[]="/tmp/hng-m7-worker-XXXXXX";configuration(m7,q,p,path);assert(host_load(path));unlink(path);
    host.port_count=2;host.ports[0]=0;host.ports[1]=1;
    for(unsigned k=0;k<2;++k)assert(host_storage_bind_pool(k,pools[k],rte_socket_id()));
    struct worker_stats stats[2]={0};struct worker_state workers[2];
    for(unsigned k=0;k<2;++k) {
        worker_state_init_owner(&workers[k],&stats[k],k);workers[k].output_limit=0;
        for(unsigned seq=0;seq<N;++seq) {
            struct rte_mbuf *in=packet(k,seq),*out[MAX_BURST_SIZE];
            assert(worker_step_at(&workers[k],&in,1,out,0)==0);
        }
        assert(!host.invalid);worker_end_input(&workers[k]);
    }
    if(aborting)host_invalidate("test bounded abort");
    for(unsigned k=0;k<2;++k) {
        struct worker_state *w=&workers[k];w->output_limit=MAX_BURST_SIZE;
        struct rte_mbuf *out[MAX_BURST_SIZE];unsigned n;
        while((n=worker_step_at(w,NULL,0,out,now_ns)))for(unsigned i=0;i<n;++i) {
            struct rte_mbuf *m=out[i];unsigned seq=packet_meta(m)->ingress_seq,idx=r->count[k]++;
            assert(seq<N && idx<N && m->pkt_len==sizes[seq%8] && rte_mbuf_refcnt_read(m)==1 && m->port==1-k);
            assert(m->hash.rss==seq+99 && m->packet_type==0x123 && m->vlan_tci==100 && m->vlan_tci_outer==101);
            assert(m->ol_flags==RTE_MBUF_F_RX_RSS_HASH);
            unsigned char *b=rte_pktmbuf_mtod(m,unsigned char *);
            const unsigned char header[]={0x81,0,0,100,0x88,0xb5};
            for(unsigned j=0;j<m->pkt_len;++j)
                assert(b[j]==(j>=12 && j<18 ? header[j-12] : (unsigned char)(seq+j*13)));
            if(m7_oracle){
                fprintf(m7_oracle,"packet,owner=%u,index=%u,",k,idx);
                const struct packet_meta *value=packet_meta(m);
#define VALUE(name) fprintf(m7_oracle,#name "=%"PRIu64",",(uint64_t)value->name);
                META_FIELDS(VALUE)
#undef VALUE
                fprintf(m7_oracle,"bytes=");for(unsigned j=0;j<m->pkt_len;++j)fprintf(m7_oracle,"%02x",b[j]);
                fprintf(m7_oracle,"\n");
            }
            r->meta[k][idx]=*packet_meta(m);r->seq[k][idx]=seq;rte_pktmbuf_free(m);
        }
        /* FFI output storage may have arbitrary previous bytes. Only the
         * declared fields are values; repr(C) padding has no defined value. */
        memset(&stats[k].lines[k],m7?0xa5:0x5a,sizeof(stats[k].lines[k]));
        worker_state_cleanup(w);assert(!worker_has_backlog(w));r->stats[k]=stats[k].lines[k];
        if(m7_oracle){
            fprintf(m7_oracle,"model,owner=%u,",k);
#define VALUE(name) fprintf(m7_oracle,#name "=%"PRIu64",",(uint64_t)r->stats[k].name);
            MODEL_FIELDS(VALUE)
#undef VALUE
            fprintf(m7_oracle,"\n");m7_oracle_worker(&stats[k]);if(m7)m7_oracle_storage(k);
            fprintf(m7_oracle,"pool,owner=%u,free=%u,total=%u\n",k,rte_mempool_avail_count(pools[k]),pools[k]->size);
        }
        assert(!stats[k].storage_drops && !stats[k].invalid_drops && !stats[k].tx_ring_drops);
        assert(stats[k].sent+stats[k].emulator_drops+stats[k].shutdown_drops==N);
        if(aborting)assert(stats[k].sent==0 && stats[k].shutdown_drops>0);
        if(m7)assert(m7_storage_drained(host.m7[k]));
        assert(rte_mempool_avail_count(pools[k])==pools[k]->size);
    }
    if(m7_oracle)fprintf(m7_oracle,"invalid=%u,reason=%s\n",(unsigned)host.invalid,aborting?host.diagnostics.invalid_reason:"none");
    assert(host.invalid==aborting);host_cleanup();
}
int main(int argc,char **argv)
{
    assert(rte_eal_init(argc,argv)>=0);padding_is_not_a_counter();compare_diagnostics=true;m7_oracle_open("worker");
    for(unsigned k=0;k<2;++k){char name[16];snprintf(name,sizeof(name),"m7_worker%u",k);
        pools[k]=rte_pktmbuf_pool_create(name,1023,0,RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),
            RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());assert(pools[k]);}
    const char *p[]={"0","0.5","1"};
    for(unsigned q=0;q<2;++q)for(unsigned choice=0;choice<3;++choice){
        struct result a={0},b={0};run(false,q?32768:80,p[choice],&a,false);run(true,q?32768:80,p[choice],&b,false);
        if (!same_result(&a,&b)) {
            fprintf(stderr,"differential q=%u p=%s meta_bytes=%zu stats_bytes=%zu\n",q?32768:80,p[choice],sizeof(struct packet_meta),sizeof(struct HngStats));
            for (size_t at=0,found=0;at<sizeof(a) && found<32;++at)
                if (((unsigned char *)&a)[at]!=((unsigned char *)&b)[at]) {
                    fprintf(stderr,"difference offset=%zu old=%02x m7=%02x\n",at,((unsigned char *)&a)[at],((unsigned char *)&b)[at]);found++;
                }
            fprintf(stderr,"observation offset=%zu ingress_port offset=%zu line offset=%zu\n",offsetof(struct packet_meta,observation),offsetof(struct packet_meta,ingress_port),offsetof(struct packet_meta,line));
        }
        assert(same_result(&a,&b));
    }
    struct result aborted={0};run(true,32768,"0.5",&aborted,true);
    for(unsigned k=0;k<2;++k)rte_mempool_free(pools[k]);
    assert(rte_eal_cleanup()==0);m7_oracle_close();
    puts("M7/legacy Q/selection/late/seq/deadline/full metadata/bytes differential, eight sizes, bidirectional END and abort: PASS");
}
