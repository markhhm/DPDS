/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <rte_eal.h>
#include "../common/common.h"
#include "../common/storage_engine.h"

static struct rte_mbuf *packet(struct rte_mempool *pool,unsigned bytes)
{
    struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);
    unsigned char *data=(unsigned char *)rte_pktmbuf_append(m,bytes);assert(data);
    for(unsigned i=0;i<bytes;++i)data[i]=(unsigned char)(i*13+bytes);
    memset(packet_meta(m),0,sizeof(struct packet_meta));
    packet_meta(m)->arrival_ns=17;packet_meta(m)->ingress_seq=42;
    packet_meta(m)->observation.identity=UINT64_MAX-3;
    m->port=7;m->hash.rss=0x12345678;m->vlan_tci=710;
    return m;
}
static void record_limit(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={64},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.records=3,.cells=16,
        .slot_limit=4,.record_limit=1,.cell_limit=16};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *a=packet(pool,60),*b=packet(pool,60);
    struct HngPacket record={.packet_ptr=a,.packet_bytes=60};
    void *first=m7_storage_commit(s,&record);assert(first);
    record.packet_ptr=b;
    assert(!m7_storage_commit(s,&record));
    assert(rte_mbuf_refcnt_read(b)==1);rte_pktmbuf_free(b);
    assert(m7_storage_stats(s).record_exhaustions==1);
    assert(m7_storage_stats(s).records_live==1);
    assert(m7_storage_discard(s,first));
    assert(m7_storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
static void cell_limit(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={512},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.records=3,.cells=16,
        .slot_limit=4,.record_limit=3,.cell_limit=8};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *a=packet(pool,508),*b=packet(pool,508);
    struct HngPacket record={.packet_ptr=a,.packet_bytes=508};
    void *first=m7_storage_commit(s,&record);assert(first);
    record.packet_ptr=b;
    assert(!m7_storage_commit(s,&record));
    assert(rte_mbuf_refcnt_read(b)==1);rte_pktmbuf_free(b);
    struct m7_storage_stats stats=m7_storage_stats(s);
    assert(stats.cell_exhaustions==1 && stats.record_exhaustions==0);
    assert(stats.records_live==1 && stats.cells_live==8);
    assert(m7_storage_discard(s,first));
    assert(m7_storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
static void normal_limit(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={1522},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.normal_mbufs=4,
        .slot_limit=4,.normal_limit=1};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *a=packet(pool,1518),*b=packet(pool,1518);
    struct HngPacket record={.packet_ptr=a,.packet_bytes=1518};
    void *first=m7_storage_commit(s,&record);assert(first);
    record.packet_ptr=b;
    assert(!m7_storage_commit(s,&record));
    assert(rte_mbuf_refcnt_read(b)==1);rte_pktmbuf_free(b);
    struct m7_storage_stats stats=m7_storage_stats(s);
    assert(stats.normal_exhaustions==1 && stats.normal_live==1);
    assert(m7_storage_discard(s,first));
    assert(m7_storage_stats(s).normal_discarded==1);
    assert(m7_storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
static void repeated_tag_release(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={64},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.records=3,.cells=16,
        .slot_limit=4,.record_limit=3,.cell_limit=16};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *m=packet(pool,60),*out=NULL;
    struct HngPacket record={.packet_ptr=m,.packet_bytes=60};
    void *token=m7_storage_commit(s,&record);assert(token);
    assert(m7_storage_materialize(s,token,&out));rte_pktmbuf_free(out);
    assert(m7_storage_release(s,token));
    assert(!m7_storage_release(s,token));
    assert(m7_storage_stats(s).invalid_inputs==1);
    assert(m7_storage_drained(s) && m7_storage_destroy(s));
    assert(rte_mempool_avail_count(pool)==pool->size);
}
static void materialize_pool_failure(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={64},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.records=3,.cells=16,
        .slot_limit=4,.record_limit=3,.cell_limit=16};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *m=packet(pool,60),*out=NULL,*held[255];
    struct HngPacket record={.packet_ptr=m,.packet_bytes=60};
    void *token=m7_storage_commit(s,&record);assert(token);
    for(unsigned i=0;i<255;++i){held[i]=rte_pktmbuf_alloc(pool);assert(held[i]);}
    assert(!m7_storage_materialize(s,token,&out) && !out);
    struct m7_storage_stats stats=m7_storage_stats(s);
    assert(stats.materialize_exhaustions==1 && stats.records_live==1 && stats.cells_live==1);
    struct observer_packet observation;assert(m7_storage_observation(s,token,&observation));
    assert(observation.identity==UINT64_MAX-3);
    for(unsigned i=0;i<255;++i)rte_pktmbuf_free(held[i]);
    assert(m7_storage_discard(s,token));
    stats=m7_storage_stats(s);assert(stats.materialized==0 && stats.compact_discarded==1);
    assert(m7_storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
static void item_limit(struct rte_mempool *pool)
{
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=1,.lengths={1522},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.normal_mbufs=4,
        .slot_limit=1,.normal_limit=4};
    struct m7_storage *s=m7_storage_create(&plan,pool,13,rte_socket_id());assert(s);
    struct rte_mbuf *a=packet(pool,1518),*b=packet(pool,1518);
    struct HngPacket record={.packet_ptr=a,.packet_bytes=1518};
    void *first=m7_storage_commit(s,&record);assert(first);record.packet_ptr=b;
    assert(!m7_storage_commit(s,&record));
    assert(rte_mbuf_refcnt_read(b)==1);rte_pktmbuf_free(b);
    struct m7_storage_stats stats=m7_storage_stats(s);
    assert(stats.item_exhaustions==1 && stats.items_live==1);
    assert(m7_storage_discard(s,first));
    assert(m7_storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
int main(int argc,char **argv)
{
    assert(rte_eal_init(argc,argv)>=0);
    struct rte_mempool *pool=rte_pktmbuf_pool_create("m7_backend",255,0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());assert(pool);
    struct M7StorageInfo plan={.threshold_l2=1024,.class_count=3,.lengths={64,512,1522},
        .hold_ns=10000000,.window_ns=10000000,.slots=4,.records=3,.cells=16,.normal_mbufs=2,
        .slot_limit=4,.record_limit=3,.cell_limit=16,.normal_limit=2};
    struct m7_storage *store=m7_storage_create(&plan,pool,13,rte_socket_id());assert(store);
    for(unsigned test=0;test<2;++test){
        unsigned bytes=test?1518:60;
        struct rte_mbuf *m=packet(pool,bytes),*out=NULL;
        struct packet_meta expected;memcpy(&expected,packet_meta(m),sizeof(expected));
        expected.release_ns=77;expected.deadline_ns=66;
        struct HngPacket admitted={.packet_ptr=m,.packet_bytes=bytes,.release_ns=77,.deadline_ns=66};
        void *item=m7_storage_commit(store,&admitted);assert(item);
        assert(rte_mempool_avail_count(pool)==(test?254u:255u));
        assert(m7_storage_stats(store).items_live==1);
        assert(m7_storage_materialize(store,item,&out));assert(out);
        if(test)assert(out==m && rte_mbuf_refcnt_read(out)==1);
        assert(!memcmp(packet_meta(out),&expected,sizeof(expected)) && out->port==13);
        const unsigned char *data=rte_pktmbuf_mtod(out,const unsigned char *);
        for(unsigned i=0;i<bytes;++i)assert(data[i]==(unsigned char)(i*13+bytes));
        assert(out->hash.rss==0x12345678 && out->vlan_tci==710);
        assert(!m7_storage_destroy(store)); /* Model must pop before the tag is recycled. */
        assert(m7_storage_release(store,item));
        rte_pktmbuf_free(out);
        assert(m7_storage_drained(store) && rte_mempool_avail_count(pool)==255);
    }
    assert(m7_storage_destroy(store));
    record_limit(pool);cell_limit(pool);normal_limit(pool);repeated_tag_release(pool);materialize_pool_failure(pool);item_limit(pool);
    rte_mempool_free(pool);assert(rte_eal_cleanup()==0);
    puts("M7 accepted custody, compact/retained bytes and metadata, post-pop tag release: PASS");
}
