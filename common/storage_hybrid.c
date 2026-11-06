/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "storage_hybrid.h"
static bool invalid(struct hybrid *s,const char *reason)
{
    if(!s->invalid)s->reason=reason;
    s->invalid=true;return false;
}
bool hybrid_init(struct hybrid *s,struct storage *compact,bool m7,uint16_t threshold,uint64_t hold)
{
    if(!s||!compact||threshold<64||threshold>1522||!hold)return false;
    *s=(struct hybrid){.compact=compact,.hold_ns=hold,.threshold_l2=threshold,.m7=m7};return true;
}
bool hybrid_put(struct hybrid *s,struct rte_mbuf *m,struct hybrid_item *item)
{
    if(!item||item->kind!=HYBRID_EMPTY||!m||!m->pkt_len||m->pkt_len>STORAGE_MAX_BYTES||
        m->nb_segs!=1||m->data_len!=m->pkt_len||rte_pktmbuf_priv_size(m->pool)<sizeof(struct packet_meta))
        return invalid(s,"invalid hybrid input or nonempty item");
    if(s->m7&&m->pkt_len+4<=s->threshold_l2) {
        struct storage_handle h;
        if(!storage_copy_in(s->compact,m,&h))return invalid(s,"compact storage exhausted or invalid");
        *item=(struct hybrid_item){.value.generation=h.generation,.index=h.index,.kind=HYBRID_RECORD};
        rte_pktmbuf_free(m);
    } else {
        *item=(struct hybrid_item){.value.mbuf=m,.kind=HYBRID_MBUF};
        s->retained_live++;s->retained_total++;
        if(s->retained_live>s->retained_peak)s->retained_peak=s->retained_live;
    }
    return true;
}
bool hybrid_get(struct hybrid *s,struct hybrid_item *item,struct rte_mempool *pool,struct rte_mbuf **out)
{
    if(!out||!item)return invalid(s,"invalid hybrid output");
    *out=NULL;
    if(item->kind==HYBRID_RECORD) {
        struct storage_handle h={item->index,item->value.generation};
        if(!storage_materialize(s->compact,h,pool,out))return invalid(s,"compact materialize failed");
    } else if(item->kind==HYBRID_MBUF) {
        *out=item->value.mbuf;s->retained_live--;s->returned_total++;
    } else return invalid(s,"hybrid item already consumed");
    item->kind=HYBRID_EMPTY;return true;
}
bool hybrid_discard(struct hybrid *s,struct hybrid_item *item)
{
    if(!item)return invalid(s,"invalid hybrid item");
    if(item->kind==HYBRID_RECORD) {
        struct storage_handle h={item->index,item->value.generation};
        if(!storage_discard(s->compact,h))return invalid(s,"compact discard failed");
    } else if(item->kind==HYBRID_MBUF) {
        rte_pktmbuf_free(item->value.mbuf);s->retained_live--;s->discarded_total++;
    } else return invalid(s,"hybrid item already consumed");
    item->kind=HYBRID_EMPTY;return true;
}
bool hybrid_drained(const struct hybrid *s)
{
    struct storage_stats stats=storage_stats(s->compact);
    return !s->retained_live&&!stats.records_live&&!stats.cells_live;
}
bool hybrid_handoff(struct hybrid *s,bool full,bool backlog,uint64_t now)
{
    if(full&&(s->m7||backlog)) {
        if(!s->full){s->full=true;s->full_since_ns=now;}
        else if(now<s->full_since_ns)return invalid(s,"nonmonotonic hybrid hold clock");
        else if(now-s->full_since_ns>(s->m7?s->hold_ns:TERMINAL_TIMEOUT_NS))
            return invalid(s,s->m7?"TX handoff hold exceeded":"TX handoff terminal timeout");
    } else s->full=false;
    return !s->invalid;
}
