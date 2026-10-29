/* SPDX-License-Identifier: Apache-2.0 */
/* Independent finite-Q service ledger + global stable sort; no core/DPDK calls. */
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "io_oracle.h"
uint64_t io_select(uint64_t seed, uint64_t seq)
{
    uint64_t x=seed+(seq+1)*UINT64_C(0x9e3779b97f4a7c15);
    x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);
    x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);
    return x^(x>>31);
}
static int ordered(const void *a, const void *b)
{
    const struct io_record *x=a,*y=b;
    if(x->port!=y->port) return x->port<y->port?-1:1;
    if(x->release!=y->release) return x->release<y->release?-1:1;
    return (x->seq>y->seq)-(x->seq<y->seq);
}
size_t io_reference(const struct io_record *in,size_t count,struct io_model m,
    struct io_record *out,uint64_t drops[4])
{
    struct ledger { __uint128_t finish[IO_BATCH*2],tail; uint64_t bytes[IO_BATCH*2],used,draw; size_t head,end; } q[4]={0};
    assert(count<=IO_BATCH*2 && m.rate); memset(drops,0,4*sizeof(*drops)); size_t n=0;
    for(size_t i=0;i<count;++i) {
        struct io_record r=in[i]; unsigned line=r.port*2+(r.vlan==101);
        assert(line<4); struct ledger *l=q+line; __uint128_t now=(__uint128_t)r.arrival*m.rate;
        while(l->head<l->end && l->finish[l->head]<=now) l->used-=l->bytes[l->head++];
        uint64_t bytes=r.length+4<64?64:r.length+4, draw=m.draw_base+l->draw++;
        if(bytes>m.q || l->used>m.q-bytes) { drops[line]++; continue; }
        __uint128_t finish=(l->tail>now?l->tail:now)+(__uint128_t)(bytes+20)*8000000000ULL;
        l->tail=finish; l->finish[l->end]=finish; l->bytes[l->end++]=bytes; l->used+=bytes;
        r.release=(uint64_t)(finish/m.rate+(finish%m.rate!=0))+m.delay+
            (r.vlan==101?3000:0)+(io_select(m.seed,draw)<(1ULL<<63)?m.delta:0);
        out[n++]=r;
    }
    qsort(out,n,sizeof(*out),ordered); return n;
}
bool io_equal(const struct io_record *a,const struct io_record *b)
{
    return a->id==b->id && a->port==b->port && a->seq==b->seq && a->vlan==b->vlan && a->length==b->length;
}
void io_fixture(struct io_record *r,unsigned seed,unsigned port,unsigned seq)
{
    *r=(struct io_record){.id=(uint64_t)port*IO_BATCH+seq,.arrival=(seq/8)*700,
        .seq=seq,.port=port,.vlan=100+((seq+seed+port)%2),.length=76+(seq*17+seed*3+port*5)%128};
}
void io_bytes(const struct io_record *r,uint8_t *d)
{
    for(unsigned i=0;i<r->length;++i) d[i]=(uint8_t)(r->id*13+i*7);
    memset(d,0xff,6); d[6]=2; d[7]=(uint8_t)r->port;
    d[12]=0x81; d[13]=0; d[14]=(uint8_t)(r->vlan>>8); d[15]=(uint8_t)r->vlan;
    d[16]=0x88; d[17]=0xb5;
    for(unsigned i=0;i<8;++i) { d[18+i]=(uint8_t)(r->id>>(8*i)); d[26+i]=(uint8_t)(r->arrival>>(8*i)); d[34+i]=(uint8_t)(r->seq>>(8*i)); }
}

bool io_frame_equal(const struct io_record *expected,const uint8_t *bytes,size_t length)
{
    if(length!=expected->length || length>IO_MAX_FRAME) return false;
    uint8_t want[IO_MAX_FRAME]; io_bytes(expected,want); return memcmp(want,bytes,length)==0;
}
