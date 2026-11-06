/* SPDX-License-Identifier: Apache-2.0 */
#include <stdlib.h>
#include <string.h>
#include "storage_hybrid.h"
#include "storage_engine.h"
#define NONE UINT32_MAX
/* Active EMPTY means materialized but still model-owned; FREE is distinct. */
#define M7_FREE ((enum hybrid_kind)3)
struct m7_storage {
    struct M7StorageInfo plan;
    struct hybrid hybrid;
    struct rte_mempool *pool;
    struct hybrid_item *items;
    struct m7_storage_stats failures;
    uint32_t free_item;
    uint16_t egress;
    uint64_t live, peak;
    bool failed;
    const char *reason;
};
static bool fail(struct m7_storage *s,const char *reason)
{
    if(!s->failed)s->reason=reason;
    s->failed=true;return false;
}
static struct hybrid_item *item(struct m7_storage *s,void *token)
{
    uintptr_t base=(uintptr_t)s->items,p=(uintptr_t)token;
    if(p<base || p-base>=s->plan.slots*sizeof(*s->items) || (p-base)%sizeof(*s->items)) {
        s->failures.invalid_inputs++;fail(s,"invalid M7 storage token");return NULL;
    }
    struct hybrid_item *value=token;
    if(value->kind!=HYBRID_EMPTY && value->kind!=HYBRID_RECORD && value->kind!=HYBRID_MBUF) {
        s->failures.invalid_inputs++;fail(s,"M7 storage tag already free or invalid");return NULL;
    }
    return value;
}
struct m7_storage *m7_storage_create(const struct M7StorageInfo *plan,
    struct rte_mempool *pool,uint16_t egress,int socket)
{
    if(!plan || !pool || !plan->slots || plan->slots>=UINT32_MAX ||
       plan->records>=UINT32_MAX || plan->cells>=UINT32_MAX ||
       rte_pktmbuf_priv_size(pool)<sizeof(struct packet_meta))return NULL;
    struct m7_storage *s=NULL;
    if(posix_memalign((void **)&s,64,(sizeof(*s)+63)&~(size_t)63))return NULL;
    memset(s,0,sizeof(*s));s->plan=*plan;s->pool=pool;s->egress=egress;
    if(posix_memalign((void **)&s->items,64,(plan->slots*sizeof(*s->items)+63)&~(size_t)63)){free(s);return NULL;}
    for(uint32_t i=0;i<plan->slots;++i)s->items[i]=(struct hybrid_item){.index=i+1<plan->slots?i+1:NONE,.kind=M7_FREE};
    /* All-large registrations use only a tiny unused control arena, covered by
     * EAL margin. Their logical compact capacity remains zero. */
    struct storage *compact=storage_create_socket(plan->records?plan->records:1,plan->cells?plan->cells:1,socket);
    if(!compact || !hybrid_init(&s->hybrid,compact,true,plan->threshold_l2,plan->hold_ns)) {
        if(compact)storage_destroy(compact);
        free(s->items);free(s);return NULL;
    }
    return s;
}
bool m7_storage_release(struct m7_storage *s,void *token)
{
    struct hybrid_item *p=item(s,token);
    if(!p || p->kind!=HYBRID_EMPTY || !s->live)return fail(s,"M7 storage tag is not materialized or already free");
    p->kind=M7_FREE;p->index=s->free_item;s->free_item=(uint32_t)(p-s->items);s->live--;return true;
}
void *m7_storage_commit(struct m7_storage *s,const struct HngPacket *admitted)
{
    if(s->failed)return NULL;
    struct rte_mbuf *m=admitted?admitted->packet_ptr:NULL;
    if(!m || m->pool!=s->pool || m->pkt_len!=admitted->packet_bytes) {
        s->failures.invalid_inputs++;fail(s,"invalid M7 source packet");return NULL;
    }
    if(m->pkt_len+4<=s->plan.threshold_l2) {
        struct storage_stats used=storage_stats(s->hybrid.compact);
        if(used.records_live>=s->plan.record_limit) {
            s->failures.record_exhaustions++;fail(s,"M7 compact record envelope exhausted");return NULL;
        }
        if(used.cells_live+(m->pkt_len+63)/64>s->plan.cell_limit) {
            s->failures.cell_exhaustions++;fail(s,"M7 compact cell envelope exhausted");return NULL;
        }
    } else if(s->hybrid.retained_live>=s->plan.normal_limit) {
        s->failures.normal_exhaustions++;fail(s,"M7 retained mbuf envelope exhausted");return NULL;
    }
    if(s->free_item==NONE || s->live>=s->plan.slot_limit){s->failures.item_exhaustions++;fail(s,"M7 item storage exhausted");return NULL;}
    struct hybrid_item *p=s->items+s->free_item;s->free_item=p->index;p->kind=HYBRID_EMPTY;
    s->live++;if(s->live>s->peak)s->peak=s->live;
    packet_meta(m)->release_ns=admitted->release_ns;packet_meta(m)->deadline_ns=admitted->deadline_ns;m->port=s->egress;
    if(!hybrid_put(&s->hybrid,m,p)) {
        m7_storage_release(s,p);fail(s,"M7 storage conversion failed");return NULL;
    }
    return p;
}
bool m7_storage_materialize(struct m7_storage *s,void *token,struct rte_mbuf **out)
{
    if(out)*out=NULL;
    struct hybrid_item *p=item(s,token);
    if(!p || s->failed || !out)return false;
    if(!hybrid_get(&s->hybrid,p,s->pool,out)) {
        s->failures.materialize_exhaustions++;return fail(s,"M7 materialization pool exhausted");
    }
    return true;
}
bool m7_storage_observation(struct m7_storage *s,void *token,struct observer_packet *out)
{
    struct hybrid_item *p=item(s,token);if(!p || !out)return false;
    if(p->kind==HYBRID_RECORD)return storage_observation(s->hybrid.compact,(struct storage_handle){p->index,p->value.generation},out);
    if(p->kind==HYBRID_MBUF){memcpy(out,&packet_meta(p->value.mbuf)->observation,sizeof(*out));return true;}
    return false;
}
bool m7_storage_discard(struct m7_storage *s,void *token)
{
    struct hybrid_item *p=item(s,token);if(!p)return false;
    if(!hybrid_discard(&s->hybrid,p))return fail(s,"M7 stored item already consumed");
    return m7_storage_release(s,p);
}
void m7_storage_core_exhausted(struct m7_storage *s)
{
    s->failures.core_exhaustions++;
    fail(s,"M7 Core slot or service envelope exhausted");
}
bool m7_storage_handoff(struct m7_storage *s,bool full,bool backlog,uint64_t now)
{
    if(!hybrid_handoff(&s->hybrid,full,backlog,now))return fail(s,s->hybrid.reason?s->hybrid.reason:"invalid M7 handoff");
    return true;
}
bool m7_storage_drained(const struct m7_storage *s){return !s->live && hybrid_drained(&s->hybrid);}
bool m7_storage_destroy(struct m7_storage *s)
{
    if(!s || !m7_storage_drained(s))return false;
    if(!storage_destroy(s->hybrid.compact))return false;
    free(s->items);free(s);return true;
}
const char *m7_storage_reason(const struct m7_storage *s){return s->reason;}
struct m7_storage_stats m7_storage_stats(const struct m7_storage *s)
{
    struct storage_stats a=storage_stats(s->hybrid.compact);struct m7_storage_stats r=s->failures;
    r.items_live=s->live;r.items_peak=s->peak;r.records_live=a.records_live;r.records_peak=a.records_peak;
    r.cells_live=a.cells_live;r.cells_peak=a.cells_peak;r.normal_live=s->hybrid.retained_live;r.normal_peak=s->hybrid.retained_peak;
    r.copied_in=a.copied_in;r.materialized=a.materialized;r.compact_discarded=a.discarded;
    r.normal_retained=s->hybrid.retained_total;r.normal_returned=s->hybrid.returned_total;r.normal_discarded=s->hybrid.discarded_total;
    return r;
}
