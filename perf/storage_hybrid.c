/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <rte_cycles.h>
#include "../common/storage_hybrid.h"
struct sizing { uint32_t window,records,cells,mbufs,retained; uint64_t live_bytes,arena_bytes; unsigned eal_mib; };
static uint64_t checksum;
static unsigned length_at(int length,unsigned i){return length<0?(unsigned[]){60,508,1518}[i%3]:(unsigned)length;}
static uint32_t stride(void)
{
    struct rte_mempool_objsz out;
    return rte_mempool_calc_obj_size(sizeof(struct rte_mbuf)+RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN)+RTE_MBUF_DEFAULT_BUF_SIZE,0,&out);
}
static struct sizing sizing(unsigned t,int length,bool resident)
{
    unsigned count=length<0?3:1,small=0,cells=0,large=0;
    uint64_t sum=0;
    for(unsigned i=0;i<count;++i){unsigned n=length_at(length,i);if(n+4<=t){small++;cells+=(n+63)/64;sum+=192+68*((n+63)/64);}else{large++;sum+=stride();}}
    uint64_t n=resident?(UINT64_C(2147483648)*count+sum-1)/sum:96;
    n=(n+47)/48*48;assert(n<UINT32_MAX);
    struct sizing p={.window=n,.records=n*small/count,.cells=n*cells/count,.mbufs=n*large/count+64,.retained=n*large/count};
    struct storage_layout l;assert(storage_layout(p.records?p.records:1,p.cells?p.cells:1,&l));
    p.arena_bytes=l.total_bytes;p.live_bytes=p.arena_bytes+(uint64_t)p.retained*stride();
    uint64_t pool=(uint64_t)p.mbufs*4096+rte_ring_get_memsize((unsigned)rte_align32pow2(p.mbufs+1))+RTE_MEMPOOL_HEADER_SIZE((struct rte_mempool *)NULL,MBUF_CACHE_SIZE);
    p.eal_mib=(unsigned)((pool+(64u<<20)-1)/(64u<<20))*64+64;
    return p;
}
static void fill(struct rte_mbuf *m,unsigned len,uint64_t seq)
{
    void *d=rte_pktmbuf_append(m,len);assert(d);memset(d,(unsigned char)seq,len);
    memset(packet_meta(m),0,sizeof(struct packet_meta));packet_meta(m)->ingress_seq=seq;m->port=1;
}
int main(int argc,char **argv)
{
    if(argc==2&&!strcmp(argv[1],"--describe")){
        struct storage_layout l;assert(storage_layout(1,1,&l));
        printf("{\"descriptor_bytes\":%"PRIu64",\"cell_bytes\":64,\"link_bytes\":4,\"item_bytes\":%zu,\"hybrid_control_bytes\":%zu,\"control_allocation\":%"PRIu64",\"core_slot_bytes\":%zu,\"mbuf_stride\":%u,\"mempool_header_bytes\":%zu}\n",l.descriptor_bytes,sizeof(struct hybrid_item),sizeof(struct hybrid),l.control_allocation,hng_slot_metadata_bytes(),stride(),RTE_MEMPOOL_HEADER_SIZE((struct rte_mempool *)NULL,MBUF_CACHE_SIZE));return 0;
    }
    if(argc==5&&!strcmp(argv[1],"--sizing")){
        struct sizing p=sizing(strtoul(argv[2],NULL,10),atoi(argv[3]),!strcmp(argv[4],"resident"));
        printf("{\"window\":%u,\"eal_mib\":%u,\"records\":%u,\"cells\":%u,\"mbufs\":%u,\"retained\":%u,\"arena_bytes\":%"PRIu64",\"live_bytes\":%"PRIu64"}\n",p.window,p.eal_mib,p.records,p.cells,p.mbufs,p.retained,p.arena_bytes,p.live_bytes);return 0;
    }
    int n=rte_eal_init(argc,argv);assert(n>=0);argc-=n;argv+=n;assert(argc==6);
    const char *mode=argv[1];bool cold_baseline=!strcmp(mode,"baseline-resident");bool baseline=!strcmp(mode,"baseline")||cold_baseline,resident=!strcmp(mode,"resident")||cold_baseline;
    int length=atoi(argv[2]);unsigned trials=strtoul(argv[3],NULL,10),t=strtoul(argv[4],NULL,10);uint64_t hold=strtoull(argv[5],NULL,10);
    assert((baseline||resident||!strcmp(mode,"hot"))&&trials&&trials<=10&&(length==-1||(length>=60&&length<=1518))&&t>=64&&t<=1522&&hold);
    struct sizing p=sizing(cold_baseline?0:t,length,resident);
    struct storage *arena=baseline?NULL:storage_create(p.records?p.records:1,p.cells?p.cells:1);assert(baseline||arena);
    struct hybrid state;assert(baseline||hybrid_init(&state,arena,true,t,hold));
    struct hybrid_item *items=baseline?NULL:calloc(p.window,sizeof(*items));assert(baseline||items);
    struct rte_mbuf **old=cold_baseline?calloc(p.window,sizeof(*old)):NULL;assert(!cold_baseline||old);
    struct rte_mempool *pool=rte_pktmbuf_pool_create("hybrid_proto",baseline&&!cold_baseline?255:p.mbufs,MBUF_CACHE_SIZE,RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());assert(pool);
    if(cold_baseline)for(unsigned i=0;i<p.window;++i){old[i]=rte_pktmbuf_alloc(pool);assert(old[i]);fill(old[i],length_at(length,i),i);}
    if(!baseline)for(unsigned i=0;i<p.window;++i){struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);fill(m,length_at(length,i),i);assert(hybrid_put(&state,m,items+i));}
    unsigned ops=resident?p.window:1048608;
    for(unsigned trial=0;trial<trials;++trial){
        uint64_t start=rte_get_timer_cycles();rte_rmb();
        for(unsigned first=0;first<ops;first+=16){
            struct rte_mbuf *in[16];assert(!rte_pktmbuf_alloc_bulk(pool,in,16));
            for(unsigned j=0;j<16;++j){unsigned index=(first+j)%p.window;fill(in[j],length_at(length,index),first+j+(uint64_t)trial*ops);
                if(cold_baseline){checksum+=packet_meta(old[index])->ingress_seq;rte_pktmbuf_free(old[index]);old[index]=in[j];}
                else if(baseline){checksum+=packet_meta(in[j])->ingress_seq;rte_pktmbuf_free(in[j]);}
                else{struct rte_mbuf *out=NULL;assert(hybrid_get(&state,items+index,pool,&out));checksum+=packet_meta(out)->ingress_seq;rte_pktmbuf_free(out);assert(hybrid_put(&state,in[j],items+index));}
            }
        }
        rte_rmb();uint64_t ticks=rte_get_timer_cycles()-start;
        printf("{\"mode\":\"%s\",\"length\":%d,\"threshold_l2\":%u,\"hold_ns\":%"PRIu64",\"trial\":%u,\"operations\":%u,\"window\":%u,\"live_storage_bytes\":%"PRIu64",\"retained_mbufs\":%u,\"ns_per_packet\":%.6f}\n",mode,length,t,hold,trial,ops,p.window,baseline&&!cold_baseline?0:p.live_bytes,baseline&&!cold_baseline?0:p.retained,(double)ticks/rte_get_timer_hz()*1e9/ops);fflush(stdout);
    }
    if(cold_baseline){for(unsigned i=0;i<p.window;++i)rte_pktmbuf_free(old[i]);free(old);}
    if(!baseline){assert(!state.invalid&&state.retained_live==p.retained);struct storage_stats z=storage_stats(arena);assert(!z.invalid&&z.records_live==p.records&&z.cells_live==p.cells);for(unsigned i=0;i<p.window;++i)assert(hybrid_discard(&state,items+i));assert(hybrid_drained(&state)&&storage_destroy(arena));free(items);}
    assert(rte_mempool_avail_count(pool)==pool->size);rte_mempool_free(pool);fprintf(stderr,"checksum=%"PRIu64"\n",checksum);assert(rte_eal_cleanup()==0);
}
