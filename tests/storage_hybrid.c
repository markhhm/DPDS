/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <rte_eal.h>
#include "../common/storage_hybrid.h"
static struct rte_mbuf *packet(struct rte_mempool *p, unsigned bytes)
{
    struct rte_mbuf *m=rte_pktmbuf_alloc(p);assert(m);
    unsigned char *d=(unsigned char *)rte_pktmbuf_append(m,bytes);assert(d);
    for(unsigned i=0;i<bytes;++i)d[i]=(unsigned char)(i*17+bytes);
    memset(packet_meta(m),0,sizeof(struct packet_meta));
    packet_meta(m)->ingress_seq=101;packet_meta(m)->deadline_ns=123456;
    packet_meta(m)->observation.identity=0xabcdef;
    m->port=1;return m;
}
int main(int argc,char **argv)
{
    assert(rte_eal_init(argc,argv)>=0);
    struct rte_mempool *p=rte_pktmbuf_pool_create("hybrid",255,0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());assert(p);
    struct storage *compact=storage_create(4,96);assert(compact);
    for(unsigned t=128;t<=1024;t*=2) {
        struct hybrid h;assert(hybrid_init(&h,compact,true,t,HYBRID_DEFAULT_HOLD_NS));
        for(unsigned above=0;above<2;++above) {
            unsigned bytes=t-4+above;
            struct rte_mbuf *m=packet(p,bytes),*out=NULL;
            struct hybrid_item item={0};
            assert(hybrid_put(&h,m,&item));
            assert(item.kind==(above?HYBRID_MBUF:HYBRID_RECORD));
            assert(hybrid_get(&h,&item,p,&out));
            if(above)assert(out==m); /* Exact original mbuf, no copy or refcount transfer. */
            assert(out->pkt_len==bytes && packet_meta(out)->deadline_ns==123456);
            assert(packet_meta(out)->ingress_seq==101 && packet_meta(out)->observation.identity==0xabcdef);
            const unsigned char *d=rte_pktmbuf_mtod(out,const unsigned char *);
            for(unsigned i=0;i<bytes;++i)assert(d[i]==(unsigned char)(i*17+bytes));
            rte_pktmbuf_free(out);assert(item.kind==HYBRID_EMPTY && hybrid_drained(&h));
        }
        assert(h.retained_total==1 && h.returned_total==1);
        assert(hybrid_handoff(&h,true,true,0));
        assert(hybrid_handoff(&h,true,true,HYBRID_DEFAULT_HOLD_NS));
        assert(!hybrid_handoff(&h,true,true,HYBRID_DEFAULT_HOLD_NS+1));
        assert(h.invalid && !strcmp(h.reason,"TX handoff hold exceeded"));
    }
    struct hybrid h;assert(hybrid_init(&h,compact,false,128,HYBRID_DEFAULT_HOLD_NS));
    assert(hybrid_handoff(&h,true,true,0));
    assert(hybrid_handoff(&h,true,true,HYBRID_DEFAULT_HOLD_NS+1));
    assert(hybrid_handoff(&h,true,true,TERMINAL_TIMEOUT_NS));
    assert(!hybrid_handoff(&h,true,true,TERMINAL_TIMEOUT_NS+1));
    assert(!strcmp(h.reason,"TX handoff terminal timeout"));
    assert(hybrid_init(&h,compact,true,128,123));
    assert(hybrid_handoff(&h,true,false,0));
    assert(hybrid_handoff(&h,true,false,123));
    assert(!hybrid_handoff(&h,true,false,124));
    assert(hybrid_init(&h,compact,true,128,123));
    assert(hybrid_handoff(&h,true,true,0));
    assert(hybrid_handoff(&h,false,true,100));
    assert(hybrid_handoff(&h,true,true,200));
    assert(hybrid_handoff(&h,true,true,323));
    assert(!hybrid_handoff(&h,true,true,324));
    assert(hybrid_init(&h,compact,true,128,HYBRID_DEFAULT_HOLD_NS));
    struct hybrid_item item={0};struct rte_mbuf *m=packet(p,1518),*out=NULL;
    assert(hybrid_put(&h,m,&item)&&hybrid_discard(&h,&item));
    assert(h.discarded_total==1 && hybrid_drained(&h));
    assert(!hybrid_discard(&h,&item));
    assert(hybrid_init(&h,compact,true,128,HYBRID_DEFAULT_HOLD_NS));
    m=packet(p,60);assert(hybrid_put(&h,m,&item));
    assert(!hybrid_get(&h,&item,NULL,&out)&&item.kind==HYBRID_RECORD);
    assert(hybrid_get(&h,&item,p,&out));rte_pktmbuf_free(out);assert(hybrid_drained(&h));
    assert(hybrid_init(&h,compact,false,128,HYBRID_DEFAULT_HOLD_NS));
    uint64_t copied=storage_stats(compact).copied_in;
    m=packet(p,60);assert(hybrid_put(&h,m,&item)&&item.kind==HYBRID_MBUF);
    assert(hybrid_get(&h,&item,p,&out)&&out==m);rte_pktmbuf_free(out);
    assert(storage_stats(compact).copied_in==copied&&hybrid_drained(&h));
    struct storage *tiny=storage_create(1,1);assert(tiny);
    assert(hybrid_init(&h,tiny,true,128,HYBRID_DEFAULT_HOLD_NS));
    m=packet(p,124);assert(!hybrid_put(&h,m,&item));
    assert(item.kind==HYBRID_EMPTY&&rte_mbuf_refcnt_read(m)==1&&hybrid_drained(&h));
    rte_pktmbuf_free(m);assert(h.invalid&&storage_destroy(tiny));
    assert(storage_destroy(compact)&&rte_mempool_avail_count(p)==p->size);
    rte_mempool_free(p);assert(rte_eal_cleanup()==0);
    puts("hybrid T/T+1, retained pointer, metadata, registered H and legacy terminal budget: PASS");
}
