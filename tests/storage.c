/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <rte_malloc.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include "../common/storage.h"
static const unsigned lengths[] = {60,64,128,129,508,512,513,1518};
static void fill(struct rte_mbuf *m, unsigned len)
{
    unsigned char *p = (unsigned char *)rte_pktmbuf_append(m, len);
    assert(p);
    for (unsigned i = 0; i < len; ++i) p[i] = (unsigned char)(i*17+len);
    memset(packet_meta(m), 0xa5, sizeof(struct packet_meta));
    packet_meta(m)->retry_active = true;
    packet_meta(m)->observation.classified = true;
    packet_meta(m)->observation.stripped = false;
    m->port = 13; m->vlan_tci = 710; m->vlan_tci_outer = 711;
    m->packet_type = RTE_PTYPE_L3_IPV4; m->hash.rss = 0x12345678;
    m->ol_flags = RTE_MBUF_F_RX_RSS_HASH;
}
static void exhaustion(struct rte_mempool *pool)
{
    struct rte_mbuf *source = rte_pktmbuf_alloc(pool), *out = NULL;
    assert(source); fill(source, 129);
    struct storage *s = storage_create(2, 4); assert(s);
    struct storage_handle a, b, failed;
    assert(storage_copy_in(s, source, &a));
    /* Three cells used, only one left: no partial allocation or source consumption. */
    assert(!storage_copy_in(s, source, &failed));
    struct storage_stats t = storage_stats(s);
    assert(t.invalid && t.cell_exhaustions == 1 && t.records_live == 1 && t.cells_live == 3);
    assert(storage_materialize(s, a, pool, &out)); rte_pktmbuf_free(out);
    assert(storage_copy_in(s, source, &b));
    assert(!storage_discard(s, a)); /* Reused index, stale generation. */
    assert(storage_discard(s, b) && !storage_discard(s, b));
    t = storage_stats(s);
    assert(t.invalid_handles == 2 && t.cells_live == 0 && t.records_live == 0);
    assert(storage_destroy(s));
    s = storage_create(1, 24); assert(s);
    assert(storage_copy_in(s, source, &a));
    assert(!storage_copy_in(s, source, &b));
    assert(storage_stats(s).record_exhaustions == 1);
    assert(!storage_destroy(s)); /* Refuse to hide ownership still held by a live record. */
    assert(storage_discard(s, a) && storage_destroy(s));
    rte_pktmbuf_free(source);
    assert(rte_mempool_avail_count(pool) == pool->size);
}
static void materialize_failure(struct rte_mempool *pool)
{
    struct rte_mempool *bare = rte_pktmbuf_pool_create("storage_bare", 31, 0, 0,
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(bare);
    struct storage *s = storage_create(1, 24); assert(s);
    struct rte_mbuf *source = rte_pktmbuf_alloc(pool), *out = NULL;
    assert(source); fill(source, 1518);
    struct storage_handle h;
    assert(storage_copy_in(s, source, &h));
    assert(!storage_materialize(s, h, bare, &out) && out == NULL);
    assert(storage_stats(s).records_live == 1 && storage_stats(s).cells_live == 24);
    assert(storage_materialize(s, h, pool, &out)); rte_pktmbuf_free(out);
    assert(storage_destroy(s));
    struct rte_mempool *tiny = rte_pktmbuf_pool_create("storage_tiny", 1, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());
    assert(tiny);
    struct rte_mbuf *held = rte_pktmbuf_alloc(tiny); assert(held);
    s = storage_create(1,24); assert(s && storage_copy_in(s,source,&h));
    assert(!storage_materialize(s,h,tiny,&out) && !out);
    assert(storage_stats(s).tx_exhaustions == 1 && storage_stats(s).records_live == 1);
    rte_pktmbuf_free(held);
    assert(storage_materialize(s,h,tiny,&out));rte_pktmbuf_free(out);
    assert(storage_stats(s).invalid && storage_stats(s).cells_live == 0);
    assert(storage_destroy(s) && rte_mempool_avail_count(tiny) == 1);
    rte_mempool_free(tiny);
    rte_pktmbuf_free(source); rte_mempool_free(bare);
}
static void shuffled_reclaim(struct rte_mempool *pool)
{
    struct storage *s = storage_create(257,257*24); assert(s);
    struct storage_handle h[257];
    for (unsigned round = 0; round < 4; ++round) {
        for (unsigned i = 0; i < 257; ++i) {
            struct rte_mbuf *m = rte_pktmbuf_alloc(pool); assert(m);
            fill(m,lengths[i%8]); packet_meta(m)->ingress_seq = 1000*round+i;
            uint64_t identity = 1000*round+i;
            memcpy(rte_pktmbuf_mtod(m,void *),&identity,sizeof(identity));
            assert(storage_copy_in(s,m,h+i)); rte_pktmbuf_free(m);
        }
        for (unsigned n = 0; n < 257; ++n) {
            unsigned i = n*37%257;
            if (i%3) {
                struct rte_mbuf *out = NULL; assert(storage_materialize(s,h[i],pool,&out));
                assert(packet_meta(out)->ingress_seq == 1000*round+i && out->pkt_len == lengths[i%8]);
                const unsigned char *b = rte_pktmbuf_mtod(out,const unsigned char *);
                uint64_t identity; memcpy(&identity,b,sizeof(identity));
                assert(identity == 1000*round+i);
                for (unsigned k=sizeof(identity);k<out->pkt_len;++k) assert(b[k] == (unsigned char)(k*17+out->pkt_len));
                rte_pktmbuf_free(out);
            } else assert(storage_discard(s,h[i]));
        }
        struct storage_stats t = storage_stats(s);
        assert(!t.invalid && !t.records_live && !t.cells_live);
        assert(t.copied_in == t.materialized+t.discarded);
    }
    assert(storage_destroy(s) && rte_mempool_avail_count(pool) == pool->size);
}
static void indirect_source(struct rte_mempool *pool)
{
    struct rte_mbuf *direct = rte_pktmbuf_alloc(pool); assert(direct); fill(direct,60);
    struct rte_mbuf *clone = rte_pktmbuf_clone(direct,pool); assert(clone);
    memcpy(packet_meta(clone),packet_meta(direct),sizeof(struct packet_meta));
    struct storage *s = storage_create(1,1); assert(s);
    struct storage_handle h;
    assert(rte_mbuf_refcnt_read(direct) == 2 && storage_copy_in(s,clone,&h));
    rte_pktmbuf_free(clone); assert(rte_mbuf_refcnt_read(direct) == 1);
    struct rte_mbuf *out = NULL; assert(storage_materialize(s,h,pool,&out));
    assert(!(out->ol_flags & (RTE_MBUF_F_INDIRECT|RTE_MBUF_F_EXTERNAL)));
    assert(rte_mbuf_refcnt_read(out) == 1 && rte_mbuf_refcnt_read(direct) == 1);
    assert(!memcmp(rte_pktmbuf_mtod(out,void *),rte_pktmbuf_mtod(direct,void *),60));
    rte_pktmbuf_free(out);rte_pktmbuf_free(direct);
    assert(storage_destroy(s) && rte_mempool_avail_count(pool) == pool->size);
}
static void socket_storage(struct rte_mempool *pool)
{
    struct storage *s=storage_create_socket(1,24,rte_socket_id()); assert(s);
    size_t bytes=0;
    assert(rte_malloc_validate(s,&bytes)==0 && bytes>=2048);
    struct rte_mbuf *m=rte_pktmbuf_alloc(pool),*out=NULL; assert(m);fill(m,1518);
    struct storage_handle h; assert(storage_copy_in(s,m,&h));rte_pktmbuf_free(m);
    assert(!storage_destroy(s)); /* Live records cannot be freed with their heap. */
    assert(storage_materialize(s,h,pool,&out));
    assert(out->pkt_len==1518);rte_pktmbuf_free(out);
    assert(storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
static void retained_terminal_observation(struct rte_mempool *pool)
{
    struct storage *s=storage_create(1,1);assert(s);
    struct rte_mbuf *m=rte_pktmbuf_alloc(pool);assert(m);fill(m,60);
    struct observer_packet expected,actual;
    memcpy(&expected,&packet_meta(m)->observation,sizeof(expected));
    struct storage_handle h;assert(storage_copy_in(s,m,&h));rte_pktmbuf_free(m);
    /* Abort must attest custody without allocating a materialized mbuf. */
    assert(storage_observation(s,h,&actual));
    assert(!memcmp(&expected,&actual,sizeof(actual)));
    assert(storage_discard(s,h));
    assert(!storage_observation(s,h,&actual));
    assert(storage_destroy(s) && rte_mempool_avail_count(pool)==pool->size);
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("storage_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct storage_layout layout;
    assert(storage_layout(1,24,&layout));
    assert(layout.descriptor_bytes == 192 && layout.cell_bytes == 64 && layout.link_bytes == 4);
    assert(layout.total_bytes == 2048); /* One aligned arena: padded control + record +24 cells +links. */
    struct storage *s = storage_create(8,192); assert(s);
    for (unsigned k = 0; k < sizeof(lengths)/sizeof(lengths[0]); ++k) {
        struct rte_mbuf *source = rte_pktmbuf_alloc(pool), *out = NULL;
        assert(source); fill(source, lengths[k]);
        struct packet_meta expected;
        memcpy(&expected, packet_meta(source), sizeof(expected));
        unsigned char bytes[STORAGE_MAX_BYTES];
        memcpy(bytes, rte_pktmbuf_mtod(source, void *), lengths[k]);
        struct storage_handle h;
        assert(storage_copy_in(s, source, &h));
        rte_pktmbuf_free(source); /* Prove the record owns its own bytes and metadata. */
        assert(storage_materialize(s, h, pool, &out));
        assert(out && out->pkt_len == lengths[k] && out->nb_segs == 1);
        assert(!memcmp(packet_meta(out), &expected, sizeof(expected)));
        assert(!memcmp(rte_pktmbuf_mtod(out, void *), bytes, lengths[k]));
        assert(out->port == 13 && out->vlan_tci == 710 && out->vlan_tci_outer == 711);
        assert(out->hash.rss == 0x12345678 && out->packet_type == RTE_PTYPE_L3_IPV4);
        assert(out->ol_flags == RTE_MBUF_F_RX_RSS_HASH && rte_mbuf_refcnt_read(out) == 1);
        rte_pktmbuf_free(out);
    }
    struct storage_stats stats = storage_stats(s);
    assert(!stats.invalid && stats.copied_in == 8 && stats.materialized == 8);
    assert(stats.records_live == 0 && stats.cells_live == 0);
    assert(storage_destroy(s) && rte_mempool_avail_count(pool) == 255);
    exhaustion(pool); materialize_failure(pool); shuffled_reclaim(pool); indirect_source(pool); socket_storage(pool); retained_terminal_observation(pool);
    rte_mempool_free(pool); assert(rte_eal_cleanup() == 0);
    puts("compact round-trip preserves every payload byte and all private metadata: PASS");
}
