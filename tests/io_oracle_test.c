/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include "io_oracle.h"
int main(void)
{
    struct io_record in[] = {
        {.id=0,.seq=0,.vlan=100,.length=76},
        {.id=1,.seq=1,.vlan=100,.length=76},
        {.id=2,.seq=2,.vlan=100,.length=76},
        {.id=3,.seq=3,.arrival=100,.vlan=100,.length=76}};
    struct io_record out[4]; uint64_t drops[4];
    struct io_model model={8000000000ULL,160,10,100,10,0};
    size_t n=io_reference(in,4,model,out,drops);
    assert(n==3 && drops[0]==1);
    assert(out[0].id==0 && out[0].release==210);
    assert(out[1].id==1 && out[1].release==210);
    assert(out[2].id==3 && out[2].release==310);
    model.delta=101; n=io_reference(in,4,model,out,drops);
    assert(n==3 && out[0].id==1 && out[1].id==0 && out[2].id==3);
    model=(struct io_model){3000000001ULL,1000,0,0,0,0};
    n=io_reference(in,3,model,out,drops);
    assert(n==3 && out[0].release==267 && out[1].release==534 && out[2].release==800);
    assert(io_select(0,0)==0xe220a8397b1dcdafULL);
    assert(io_select(3,0)<(1ULL<<63) && io_select(3,1)>=(1ULL<<63));
    struct io_record bad=out[0]; assert(io_equal(out,&bad));
    bad.id++; assert(!io_equal(out,&bad)); bad=out[0]; bad.vlan++; assert(!io_equal(out,&bad));
    bad=out[0]; bad.length++; assert(!io_equal(out,&bad));
    assert(!io_equal(out,out+1)); /* Swapped output is rejected. */
    uint8_t frame[IO_MAX_FRAME]; io_bytes(out,frame); assert(io_frame_equal(out,frame,out->length));
    frame[50]^=1; assert(!io_frame_equal(out,frame,out->length));
    assert(!io_frame_equal(out,frame,out->length-1));
    puts("independent oracle Q/release/ties/fractional service/seed/corruption: PASS");
}
