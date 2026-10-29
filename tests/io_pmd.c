/* SPDX-License-Identifier: Apache-2.0 */
/* Synthetic PMD integrity test. Fixture clocks never use pcap timestamps. */
#define _GNU_SOURCE
#include <assert.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_eth_ring.h>
#include <rte_launch.h>
#include <rte_malloc.h>
#include "../core/receive.h"
#include "../core/worker.h"
#include "../core/transmit.h"
#include "io_oracle.h"

static pthread_barrier_t barrier;
static struct rte_ring *rx_handoff[2],*tx_handoff[2];
static struct rte_mempool *pool;
static struct pipeline_stats stats;
static _Atomic unsigned tx_done;
static _Atomic unsigned workers_done;
static unsigned seed,epoch,epochs,owners=1;
/* split: one production scheduler owner per ingress port, each with its own RX handoff.
 * io_split: also one RX owner and one TX owner (own TX handoff and TX state) per direction. */
static bool ring_mode,stepped,split,io_split;
static unsigned stages(void) { return 3+(unsigned)split+2*(unsigned)io_split; }
/* Stepped mode: shared fixture clock and backpressure evidence. */
static _Atomic uint64_t clock_ns;
static _Atomic uint64_t rx_full[2];
static uint64_t source_full,interleaved[2],partial[2],tx_hold;
static uint64_t base;
static size_t expected_count[2];
static unsigned seed_epoch(void) { return ring_mode?epoch%153:0; }
static uint64_t fixture_now(void) { return (uint64_t)(seed_epoch()+1)*1000000; }
static uint64_t decode(const uint8_t *p) { uint64_t n=0; for(unsigned i=0;i<8;++i) n|=(uint64_t)p[i]<<(i*8); return n; }
static void enqueue(struct rte_ring *r,struct rte_mbuf **m,unsigned n)
{
    unsigned sent=0;
    while(sent<n) {
        sent+=rte_ring_enqueue_burst(r,(void **)(m+sent),n-sent,NULL);
        for(unsigned k=0;k<owners;++k) if(sent<n && r==rx_handoff[k]) atomic_fetch_add(&rx_full[k],1);
    }
}
static void clock_advance(uint64_t t) { uint64_t c=atomic_load(&clock_ns); while(c<t && !atomic_compare_exchange_weak(&clock_ns,&c,t)) {} }
static int receive_owner(void *arg)
{
    unsigned k=(unsigned)(uintptr_t)arg; /* io_split: this owner polls only port k */
    struct rx_stats *rx=io_split?&stats.rx_owners[k]:&stats.rx;
    for(unsigned run=0;run<epochs;++run) {
        pthread_barrier_wait(&barrier); unsigned received[2]={0};
        while(received[0]+received[1]<(io_split?1:2)*IO_BATCH) for(unsigned p=0;p<2;++p) {
            if(io_split && p!=k) continue;
            struct rte_mbuf *batch[MAX_BURST_SIZE];
            unsigned n=rte_eth_rx_burst(p,0,batch,MAX_BURST_SIZE);
            for(unsigned i=0;i<n;++i) {
                struct rte_mbuf *m=batch[i]; const uint8_t *d=rte_pktmbuf_mtod(m,const uint8_t *);
                assert(m->pkt_len>=76 && received[p]<IO_BATCH);
                assert(decode(d+34)==(uint64_t)seed_epoch()*IO_BATCH+received[p] && decode(d+18)==base+p*IO_BATCH+received[p]);
                *packet_meta(m)=(struct packet_meta){.ingress_port=p,.ingress_seq=decode(d+34),.arrival_ns=decode(d+26)};
                rx->received++; rx->received_bytes+=m->pkt_len; received[p]++;
            }
            enqueue(rx_handoff[split?p:0],batch,n); rx->enqueued+=n;
        }
        pthread_barrier_wait(&barrier);
    }
    return 0;
}
static int scheduler_owner(void *arg)
{
    unsigned k=(unsigned)(uintptr_t)arg, batch=split?IO_BATCH:2*IO_BATCH;
    struct worker_state worker;
    for(unsigned run=0;run<epochs;++run) {
        pthread_barrier_wait(&barrier);
        if(seed_epoch()==0) {
            if(split) worker_state_init_owner(&worker,&stats.owners[k],(int)k);
            else worker_state_init(&worker,&stats,2);
        }
        unsigned received=0;
        static uint64_t now_k[2],last_k[2],steps_k[2];
        uint64_t *now=now_k+k,*last_arrival=last_k+k,*steps=steps_k+k;
        if(seed_epoch()==0) *now=*last_arrival=0;
        /* Stepped: hold the RX handoff until it is full and its producer has blocked. */
        if(stepped) { uint64_t blocked=atomic_load(&rx_full[k]); while(atomic_load(&rx_full[k])==blocked) rte_pause(); }
        while(received<batch) {
            struct rte_mbuf *in[MAX_BURST_SIZE],*out[MAX_BURST_SIZE];
            unsigned n=rte_ring_dequeue_burst(rx_handoff[k],(void **)in,MAX_BURST_SIZE,NULL);
            received+=n;
            if(!stepped) {
                unsigned emitted=worker_step_at(&worker,in,n,out,0);
                assert(emitted==0); /* All arrivals are admitted before advancing the fixture clock. */
                continue;
            }
            /* The clock trails the newest arrival by 0-4 us, so heads become due mid-input. */
            for(unsigned i=0;i<n;++i) if(packet_meta(in[i])->arrival_ns>*last_arrival) *last_arrival=packet_meta(in[i])->arrival_ns;
            if(n) { uint64_t t=*last_arrival+((*steps)++%5)*1000; if(t>*now) *now=t; }
            clock_advance(*now);
            unsigned emitted=worker_step_at(&worker,in,n,out,*now);
            if(emitted && received<batch) {
                interleaved[k]++;
                for(unsigned line=0;line<host.line_count;++line) {
                    struct HngStats held;
                    if(host.lines[line].egress_port!=out[0]->port || !worker.owned[line]) continue;
                    assert(hng_line_stats(worker.line[line],&held));
                    if(held.storage_packets) { partial[k]++; break; } /* some due, some not yet */
                }
            }
            enqueue(tx_handoff[io_split?k:0],out,emitted);
        }
        bool final=!ring_mode || seed_epoch()==152;
        if(final) worker_end_input(&worker);
        else for(unsigned p=0;p<2;++p) if(!split || p==k) host_publish(p,worker.consumed,fixture_now()-1);
        clock_advance(fixture_now()); *now=fixture_now();
        while(final?worker_has_backlog(&worker):worker.stats->sent<expected_count[k]) {
            struct rte_mbuf *out[MAX_BURST_SIZE];
            unsigned n=worker_step_at(&worker,NULL,0,out,fixture_now());
            enqueue(tx_handoff[io_split?k:0],out,n);
        }
        if(final) worker_state_cleanup(&worker);
        else for(unsigned line=0;line<host.line_count;++line) if(worker.owned[line]) assert(hng_line_stats(worker.line[line],&worker.stats->lines[line]));
        atomic_fetch_add(&workers_done,1);
        pthread_barrier_wait(&barrier);
    }
    return 0;
}
static int transmit_owner(void *arg)
{
    unsigned k=(unsigned)(uintptr_t)arg; struct rte_ring *ring=tx_handoff[io_split?k:0];
    for(unsigned run=0;run<epochs;++run) {
        pthread_barrier_wait(&barrier);
        struct tx_state tx; tx_state_init(&tx,&stats);
        if(io_split) tx.stats=&stats.tx_owners[k];
        do {
            struct rte_mbuf *in[MAX_BURST_SIZE];
            bool room=true; /* Stepped: stop dequeuing before the bounded TX backlog fills. */
            for(unsigned line=0;stepped && line<HNG_MAX_LINES;++line) if(tx.pending[line].len>TX_PENDING_PER_LINE-MAX_BURST_SIZE) room=false;
            if(!room) tx_hold++;
            unsigned n=room?rte_ring_dequeue_burst(ring,(void **)in,MAX_BURST_SIZE,NULL):0;
            tx_step_at(&tx,in,n,stepped?atomic_load(&clock_ns):fixture_now());
        } while(atomic_load(&workers_done)<owners || !rte_ring_empty(ring) || tx_has_backlog(&tx));
        tx_state_cleanup(&tx); atomic_fetch_add(&tx_done,1);
        pthread_barrier_wait(&barrier);
    }
    return 0;
}
/* Offer one burst to companion port p+2; false when the source ring is full. */
static bool send_burst(unsigned p,const struct io_record *input,unsigned *sent)
{
    struct rte_mbuf *packets[MAX_BURST_SIZE]; unsigned n=IO_BATCH-*sent<MAX_BURST_SIZE?IO_BATCH-*sent:MAX_BURST_SIZE;
    for(unsigned j=0;j<n;++j) { const struct io_record *r=input+p*IO_BATCH+*sent+j; packets[j]=rte_pktmbuf_alloc(pool); assert(packets[j]);
        uint8_t *d=(uint8_t *)rte_pktmbuf_append(packets[j],r->length); assert(d); io_bytes(r,d); }
    unsigned k=rte_eth_tx_burst(p+2,0,packets,n); *sent+=k;
    for(unsigned j=k;j<n;++j) rte_pktmbuf_free(packets[j]);
    if(k<n) source_full++;
    return k==n;
}
static void check_frame(const uint8_t *d,size_t length,unsigned output,
    struct io_record *expected,size_t count,size_t next[2])
{
    unsigned ingress=1-output; size_t index=next[ingress];
    while(index<count && expected[index].port!=ingress) index++;
    assert(index<count); const struct io_record *want=expected+index;
    struct io_record got={.id=decode(d+18),.arrival=decode(d+26),.seq=decode(d+34),
        .port=ingress,.vlan=((unsigned)d[14]<<8|d[15])&4095,.length=length};
    assert(io_equal(want,&got) && io_frame_equal(want,d,length)); next[ingress]=index+1;
}
static void check_pcap(const char *dir,struct io_record *expected,size_t count)
{
    size_t next[2]={0}; unsigned observed=0;
    for(unsigned port=0;port<2;++port) {
        char path[1024]; snprintf(path,sizeof(path),"%s/out%u.pcap",dir,port);
        FILE *f=fopen(path,"rb"); assert(f); uint32_t header[6]; assert(fread(header,sizeof(header),1,f)==1);
        assert(header[0]==0xa1b2c3d4 || header[0]==0xa1b23c4d);
        uint32_t record[4]; while(fread(record,sizeof(record),1,f)==1) {
            uint8_t bytes[IO_MAX_FRAME]; assert(record[2]>=76 && record[2]<=sizeof(bytes) && record[2]==record[3]);
            assert(fread(bytes,record[2],1,f)==1); check_frame(bytes,record[2],port,expected,count,next); observed++;
        }
        assert(feof(f) && !ferror(f)); fclose(f);
    }
    assert(observed==count);
}
/* -lcores mode: the scheduler stand-in between the production RX and TX lcores. It restores the
 * fixture arrival time (production RX stamps the real clock), admits at time 0, drains at the
 * fixture time, then ends its input only after the production RX release chain, as lcore_worker does. */
static int fixture_scheduler(void *arg)
{
    struct lcore_parameters *par=arg; unsigned k=par->owner_index,received=0;
    struct worker_state w; worker_state_init_owner(&w,&stats.owners[k],(int)k);
    struct rte_mbuf *in[MAX_BURST_SIZE],*out[MAX_BURST_SIZE];
    while(received<IO_BATCH) {
        unsigned n=rte_ring_dequeue_burst(par->receive_worker_ring,(void **)in,MAX_BURST_SIZE,NULL);
        for(unsigned i=0;i<n;++i) {
            struct packet_meta *m=packet_meta(in[i]); const uint8_t *d=rte_pktmbuf_mtod(in[i],const uint8_t *);
            /* Production RX polled only this direction's port and numbered it from 0. */
            assert(m->ingress_port==k && in[i]->port==k && m->ingress_seq==decode(d+34) && decode(d+18)==base+k*IO_BATCH+received+i);
            m->arrival_ns=decode(d+26);
        }
        received+=n;
        assert(worker_step_at(&w,in,(uint16_t)n,out,0)==0);
    }
    /* All input is admitted; RX's real-clock frontier only permits output up to the fixture time. */
    while(w.stats->sent<expected_count[k]) enqueue(par->worker_transmit_ring,out,worker_step_at(&w,NULL,0,out,fixture_now()));
    while(!quit_signal_worker) { assert(rte_ring_empty(par->receive_worker_ring)); rte_pause(); }
    assert(rte_ring_empty(par->receive_worker_ring));
    worker_end_input(&w);
    while(worker_has_backlog(&w)) enqueue(par->worker_transmit_ring,out,worker_step_at(&w,NULL,0,out,fixture_now()));
    worker_state_cleanup(&w);
    if(atomic_fetch_sub(&host.schedulers_running,1)<=1) quit_signal_transmit=1;
    rte_free(par);
    return 0;
}
/* Production lcore_receive (fixed port) and lcore_transmit per direction, one epoch per seed. */
static int run_lcores(const char *dir)
{
    unsigned cores[6],ncores=0,id; RTE_LCORE_FOREACH_WORKER(id) cores[ncores++]=id;
    assert(ncores==6);
    uint64_t total_in=0,total_out=0,total_drop=0;
    for(unsigned s=0;s<32;++s) {
        seed=s; epoch=s*153; base=(uint64_t)epoch*2*IO_BATCH; /* seed_epoch()==0 */
        char config[1024]; snprintf(config,sizeof(config),"%s/seed%u.json",dir,seed);
        assert(host_load(config) && host_split_valid());
        host.port_count=2; host.ports[0]=0; host.ports[1]=1;
        quit_signal_receive=quit_signal_worker=quit_signal_transmit=0;
        host.schedulers_running=host.receivers_running=2;
        memset(&stats,0,sizeof(stats));
        if(s==0) for(unsigned p=0;p<4;++p) assert(initialize_port(p,pool)==0);
        struct rte_eth_stats before[4]; for(unsigned p=0;p<4;++p) assert(rte_eth_stats_get(p,before+p)==0);
        struct io_record input[2*IO_BATCH],expected[2*IO_BATCH]; uint64_t drops[4];
        for(unsigned p=0;p<2;++p) for(unsigned i=0;i<IO_BATCH;++i) { io_fixture(input+p*IO_BATCH+i,seed,p,i); input[p*IO_BATCH+i].id+=base; }
        size_t count=io_reference(input,2*IO_BATCH,(struct io_model){8000000003ULL,600,1000,15000,seed,0},expected,drops);
        expected_count[0]=expected_count[1]=0;
        for(size_t i=0;i<count;++i) expected_count[expected[i].port]++;
        host_mark_launched();
        lcore_function_t *fn[6]={(lcore_function_t *)lcore_transmit,(lcore_function_t *)lcore_transmit,
            fixture_scheduler,fixture_scheduler,(lcore_function_t *)lcore_receive,(lcore_function_t *)lcore_receive};
        for(unsigned i=0;i<6;++i) {
            unsigned k=i%2; struct lcore_parameters *par=rte_zmalloc(NULL,sizeof(*par),0); assert(par);
            *par=(struct lcore_parameters){.stats=&stats,.num_ports=2,.split=true,.io_split=true,.owner_index=(uint16_t)k,
                .owner_rings={rx_handoff[0],rx_handoff[1]},.receive_worker_ring=rx_handoff[k],.worker_transmit_ring=tx_handoff[k]};
            assert(!rte_eal_remote_launch(fn[i],par,cores[i]));
        }
        unsigned sent[2]={0}; size_t next[2]={0}; uint64_t observed=0;
        while(observed<count) {
            for(unsigned p=0;p<2;++p) if(sent[p]<IO_BATCH) send_burst(p,input,sent+p);
            for(unsigned p=0;p<2;++p) {
                struct rte_mbuf *packets[MAX_BURST_SIZE]; unsigned n=rte_eth_rx_burst(p+2,0,packets,MAX_BURST_SIZE);
                for(unsigned i=0;i<n;++i) { check_frame(rte_pktmbuf_mtod(packets[i],const uint8_t *),packets[i]->pkt_len,p,expected,count,next); rte_pktmbuf_free(packets[i]); observed++; }
            }
        }
        /* SIGINT equivalent: the production chain RX -> receivers_running -> schedulers -> TX must drain. */
        quit_signal_receive=1;
        rte_eal_mp_wait_lcore();
        assert(atomic_load(&host.receivers_running)==0 && atomic_load(&host.schedulers_running)==0 && quit_signal_transmit);
        for(unsigned p=0;p<2;++p) { struct rte_mbuf *m; assert(rte_eth_rx_burst(p+2,0,&m,1)==0); }
        for(unsigned k=0;k<2;++k) {
            worker_stats_merge(&stats.worker,&stats.owners[k],(int)k);
            rx_stats_merge(&stats.rx,&stats.rx_owners[k]); tx_stats_merge(&stats.tx,&stats.tx_owners[k]);
            assert(stats.rx_owners[k].received==IO_BATCH && stats.rx_owners[k].enqueued==IO_BATCH);
            assert(stats.tx_owners[k].transmitted==expected_count[k] && stats.tx_owners[k].tx_drops==0);
        }
        assert(!host.invalid && stats.rx.received==2*IO_BATCH && stats.rx.rx_ring_drops==0);
        assert(stats.worker.received==2*IO_BATCH && stats.worker.sent==count && stats.worker.emulator_drops==2*IO_BATCH-count);
        assert(stats.worker.invalid_drops==0 && stats.worker.storage_drops==0 && stats.worker.shutdown_drops==0 && stats.worker.tx_ring_drops==0);
        assert(stats.tx.received==count && stats.tx.transmitted==count && stats.tx.transmitted_bytes==stats.worker.sent_bytes);
        assert(stats.rx.received_bytes==stats.worker.received_bytes);
        for(unsigned line=0;line<4;++line) {
            struct HngStats *l=stats.worker.lines+line;
            assert(l->q_drops==drops[line] && l->accepted==l->dequeued && l->input_complete && stats.tx.line_transmitted[line]==l->dequeued);
        }
        for(unsigned p=0;p<4;++p) {
            struct rte_eth_stats after; assert(rte_eth_stats_get(p,&after)==0);
            uint64_t output_count=expected_count[1-p%2];
            assert(after.ipackets-before[p].ipackets==(p<2?IO_BATCH:output_count));
            assert(after.opackets-before[p].opackets==(p<2?output_count:IO_BATCH));
        }
        host_cleanup(); assert(!host.invalid && rte_mempool_avail_count(pool)==8191);
        total_in+=2*IO_BATCH; total_out+=count; total_drop+=2*IO_BATCH-count;
        printf("ring-io-split-lcores seed=%u production_rx_tx=PASS release_chain=PASS cumulative_input=%"PRIu64" cumulative_output=%"PRIu64" cumulative_q_drop=%"PRIu64" every_ID_bytes_order=PASS conservation=PASS drained=PASS\n",
            seed,total_in,total_out,total_drop);
    }
    for(unsigned p=0;p<4;++p) assert(close_port(p)==0);
    for(unsigned p=0;p<2;++p) {
        char name[32]; snprintf(name,sizeof(name),"source%u",p); rte_ring_free(rte_ring_lookup(name));
        snprintf(name,sizeof(name),"sink%u",p); rte_ring_free(rte_ring_lookup(name));
    }
    rte_mempool_free(pool); for(unsigned k=0;k<2;++k) { rte_ring_free(rx_handoff[k]); rte_ring_free(tx_handoff[k]); }
    assert(rte_eal_cleanup()==0); return 0;
}
int main(int argc,char **argv)
{
    const char *dir=getenv("IO_DIR"),*mode=getenv("IO_MODE"); assert(dir && mode);
    split=strstr(mode,"-split")!=NULL; io_split=strstr(mode,"-io-split")!=NULL; owners=split?2:1;
    stepped=strncmp(mode,"ring-stepped",12)==0; ring_mode=stepped || strncmp(mode,"ring",4)==0;
    unsigned ring_size=stepped?64:4096; /* Small rings make every producer block. */ epochs=ring_mode?32*153:1;
    assert(rte_eal_init(argc,argv)>=0 && rte_lcore_count()==1+stages());
    if(ring_mode) for(unsigned p=0;p<2;++p) {
        char a[32],b[32],port[32]; snprintf(a,sizeof(a),"source%u",p); snprintf(b,sizeof(b),"sink%u",p);
        struct rte_ring *rx=rte_ring_create(a,ring_size,rte_socket_id(),RING_F_SP_ENQ|RING_F_SC_DEQ);
        struct rte_ring *tx=rte_ring_create(b,ring_size,rte_socket_id(),RING_F_SP_ENQ|RING_F_SC_DEQ);
        assert(rx && tx); snprintf(port,sizeof(port),"net_ring%u",p);
        assert(rte_eth_from_rings(port,&rx,1,&tx,1,rte_socket_id())==(int)p);
    }
    /* Companion source/checker ports exercise PMD TX and RX on both sides. */
    if(ring_mode) for(unsigned p=0;p<2;++p) {
        char a[32],b[32],port[32]; snprintf(a,sizeof(a),"source%u",p); snprintf(b,sizeof(b),"sink%u",p);
        struct rte_ring *rx=rte_ring_lookup(b),*tx=rte_ring_lookup(a);
        snprintf(port,sizeof(port),"net_ring%u",p+2);
        assert(rte_eth_from_rings(port,&rx,1,&tx,1,rte_socket_id())==(int)p+2);
    }
    pool=rte_pktmbuf_pool_create("io_pool",8191,0,RTE_ALIGN_CEIL(sizeof(struct packet_meta),RTE_MBUF_PRIV_ALIGN),RTE_MBUF_DEFAULT_BUF_SIZE,rte_socket_id());
    assert(pool);
    for(unsigned k=0;k<owners;++k) { char name[32]; snprintf(name,sizeof(name),"io_rx%u",k);
        rx_handoff[k]=rte_ring_create(name,ring_size,rte_socket_id(),RING_F_SP_ENQ|RING_F_SC_DEQ); assert(rx_handoff[k]); }
    /* Split: both scheduler owners produce into the TX handoff. io_split: one SP/SC ring per direction. */
    unsigned directions=io_split?2:1;
    for(unsigned k=0;k<directions;++k) { char name[32]; snprintf(name,sizeof(name),"io_tx%u",k);
        tx_handoff[k]=rte_ring_create(name,ring_size,rte_socket_id(),(split && !io_split?0:RING_F_SP_ENQ)|RING_F_SC_DEQ); assert(tx_handoff[k]); }
    if(strstr(mode,"-lcores")) return run_lcores(dir);
    pthread_barrier_init(&barrier,NULL,1+stages()); unsigned cores[6],ncores=0,id;
    RTE_LCORE_FOREACH_WORKER(id) cores[ncores++]=id;
    assert(ncores==stages()); unsigned c=0;
    for(unsigned k=0;k<directions;++k) assert(!rte_eal_remote_launch(receive_owner,(void *)(uintptr_t)k,cores[c++]));
    for(unsigned k=0;k<owners;++k) assert(!rte_eal_remote_launch(scheduler_owner,(void *)(uintptr_t)k,cores[c++]));
    for(unsigned k=0;k<directions;++k) assert(!rte_eal_remote_launch(transmit_owner,(void *)(uintptr_t)k,cores[c++]));
    uint64_t total_in=0,total_out=0,total_drop=0;
    for(epoch=0;epoch<epochs;++epoch) {
        seed=ring_mode?epoch/153:(unsigned)strtoul(getenv("IO_SEED"),NULL,10);
        base=(uint64_t)epoch*2*IO_BATCH; char config[1024]; snprintf(config,sizeof(config),"%s/seed%u.json",dir,seed);
        /* Owners wait at the barrier here; they take handles copied by host_load. */
        if(seed_epoch()==0) { assert(host_load(config) && (!split || host_split_valid())); host_mark_launched(); atomic_store(&clock_ns,0); }
        struct HngStats previous[4]; for(unsigned line=0;line<4;++line) assert(hng_engine_stats(host.engine,line,previous+line));
        memset(&stats,0,sizeof(stats)); atomic_store(&workers_done,0); atomic_store(&tx_done,0);
        if(epoch==0) for(unsigned p=0;p<(ring_mode?4:2);++p) assert(initialize_port(p,pool)==0);
        struct rte_eth_stats before[4];
        for(unsigned p=0;p<(ring_mode?4:2);++p) assert(rte_eth_stats_get(p,before+p)==0);
        struct io_record input[2*IO_BATCH],expected[2*IO_BATCH]; uint64_t drops[4];
        for(unsigned p=0;p<2;++p) for(unsigned i=0;i<IO_BATCH;++i) { io_fixture(input+p*IO_BATCH+i,seed,p,i); input[p*IO_BATCH+i].id+=base; input[p*IO_BATCH+i].seq+=(uint64_t)seed_epoch()*IO_BATCH; input[p*IO_BATCH+i].arrival+=(uint64_t)seed_epoch()*1000000; }
        size_t count=io_reference(input,2*IO_BATCH,(struct io_model){8000000003ULL,600,1000,15000,seed,(uint64_t)seed_epoch()*IO_BATCH/2},expected,drops);
        expected_count[0]=expected_count[1]=0;
        for(size_t i=0;i<count;++i) expected_count[split?expected[i].port:0]++;
        unsigned sent[2]={0};
        /* Batched: queue the whole epoch. Stepped: fill the source rings until they are full. */
        uint64_t full_before=source_full;
        if(ring_mode) for(unsigned p=0;p<2;++p) while(sent[p]<IO_BATCH && (send_burst(p,input,sent+p) || !stepped)) {}
        assert(!stepped || source_full==full_before+2);
        pthread_barrier_wait(&barrier);
        size_t next[2]={0}; uint64_t observed=0;
        if(ring_mode) do {
            for(unsigned p=0;p<2;++p) if(sent[p]<IO_BATCH) send_burst(p,input,sent+p);
            for(unsigned p=0;p<2;++p) {
                struct rte_mbuf *packets[MAX_BURST_SIZE]; unsigned n=rte_eth_rx_burst(p+2,0,packets,MAX_BURST_SIZE);
                for(unsigned i=0;i<n;++i) { check_frame(rte_pktmbuf_mtod(packets[i],const uint8_t *),packets[i]->pkt_len,p,expected,count,next); rte_pktmbuf_free(packets[i]); observed++; }
            }
        } while(atomic_load(&tx_done)<directions || observed<count);
        pthread_barrier_wait(&barrier);
        for(unsigned k=0;split && k<2;++k) worker_stats_merge(&stats.worker,&stats.owners[k],(int)k);
        for(unsigned k=0;io_split && k<2;++k) { rx_stats_merge(&stats.rx,&stats.rx_owners[k]); tx_stats_merge(&stats.tx,&stats.tx_owners[k]);
            /* Each direction's RX and TX owners saw only their own port's packets. */
            assert(stats.rx_owners[k].received==IO_BATCH && stats.tx_owners[k].transmitted==expected_count[k]); }
        assert(!host.invalid && stats.rx.received==2*IO_BATCH && stats.rx.enqueued==stats.rx.received);
        assert(stats.worker.received==stats.rx.enqueued && stats.worker.sent==count && stats.worker.emulator_drops==2*IO_BATCH-count);
        assert(stats.worker.invalid_drops==0 && stats.worker.storage_drops==0 && stats.worker.shutdown_drops==0 && stats.worker.tx_ring_drops==0);
        assert(stats.tx.received==count && stats.tx.transmitted==count && stats.tx.tx_drops==0 && stats.tx.trace_outage_drops==0);
        assert(stats.worker.received_bytes==stats.rx.received_bytes && stats.worker.sent_bytes==stats.tx.received_bytes && stats.tx.received_bytes==stats.tx.transmitted_bytes);
        for(unsigned line=0;line<4;++line) {
            struct HngStats *s=stats.worker.lines+line;
            assert(s->q_drops-previous[line].q_drops==drops[line] && s->accepted+s->q_drops==(uint64_t)(seed_epoch()+1)*IO_BATCH/2 && s->accepted==s->dequeued);
            assert(s->storage_packets==0 && s->storage_bytes==0);
            if(!ring_mode || seed_epoch()==152) assert(s->q_bytes==0 && s->input_complete);
            assert(stats.tx.line_transmitted[line]==s->dequeued-previous[line].dequeued && stats.tx.line_transmitted_bytes[line]==s->dequeued_bytes-previous[line].dequeued_bytes);
            uint64_t offered_bytes=0;
            for(unsigned i=0;i<2*IO_BATCH;++i) if(input[i].port==line/2 && input[i].vlan==100+line%2) offered_bytes+=input[i].length;
            assert(s->accepted_bytes-previous[line].accepted_bytes+s->q_drop_payload_bytes-previous[line].q_drop_payload_bytes==offered_bytes);
        }
        for(unsigned p=0;p<(ring_mode?4:2);++p) {
            struct rte_eth_stats after; assert(rte_eth_stats_get(p,&after)==0);
            uint64_t output_count=0;
            for(size_t i=0;i<count;++i) if(expected[i].port==1-p%2) output_count++;
            assert(after.ipackets-before[p].ipackets==(p<2?IO_BATCH:output_count));
            assert(after.opackets-before[p].opackets==(p<2?output_count:IO_BATCH));
            assert(after.ierrors==before[p].ierrors && after.oerrors==before[p].oerrors && after.imissed==before[p].imissed && after.rx_nombuf==before[p].rx_nombuf);
        }
        total_in+=2*IO_BATCH; total_out+=count; total_drop+=2*IO_BATCH-count;
        if(!ring_mode || seed_epoch()==152) { host_cleanup(); assert(!host.invalid); }
        if(ring_mode) assert(rte_mempool_avail_count(pool)==8191);
        if(!ring_mode) { assert(close_port(0)==0 && close_port(1)==0); check_pcap(dir,expected,count); }
        if(stepped && epoch%153==152) {
            /* per seed and owner: interleaving, partial due, full rings */
            for(unsigned k=0;k<owners;++k) assert(interleaved[k] && partial[k] && atomic_load(&rx_full[k]));
            printf("%s seed=%u interleaved_steps=%"PRIu64" partial_due_steps=%"PRIu64" source_full=%"PRIu64" rx_handoff_full=%"PRIu64" tx_hold=%"PRIu64"\n",
                mode,seed,interleaved[0]+interleaved[1],partial[0]+partial[1],source_full,
                (uint64_t)(atomic_load(&rx_full[0])+atomic_load(&rx_full[1])),tx_hold);
            interleaved[0]=interleaved[1]=partial[0]=partial[1]=source_full=tx_hold=0; atomic_store(&rx_full[0],0); atomic_store(&rx_full[1],0);
        }
        if(!ring_mode || epoch%153==152) printf("%s seed=%u epochs=%u cumulative_input=%"PRIu64" cumulative_output=%"PRIu64" cumulative_q_drop=%"PRIu64" every_ID_bytes_order=PASS conservation=PASS drained=PASS\n",mode,seed,ring_mode?153:1,total_in,total_out,total_drop);
    }
    rte_eal_mp_wait_lcore(); pthread_barrier_destroy(&barrier);
    if(ring_mode) {
        for(unsigned p=0;p<4;++p) assert(close_port(p)==0);
        for(unsigned p=0;p<2;++p) {
            char name[32]; snprintf(name,sizeof(name),"source%u",p); rte_ring_free(rte_ring_lookup(name));
            snprintf(name,sizeof(name),"sink%u",p); rte_ring_free(rte_ring_lookup(name));
        }
    }
    assert(rte_mempool_avail_count(pool)==8191); rte_mempool_free(pool); for(unsigned k=0;k<owners;++k) rte_ring_free(rx_handoff[k]); for(unsigned k=0;k<directions;++k) rte_ring_free(tx_handoff[k]);
    assert(rte_eal_cleanup()==0); return 0;
}
