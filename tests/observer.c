/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../common/common.h"
#include "../common/observer.h"
#include "../core/worker.h"
#include "../core/transmit.h"

static unsigned calls;
static uint16_t partial(uint16_t port, uint16_t queue, struct rte_mbuf **m, uint16_t n)
{
    (void)port; (void)queue;
    uint16_t sent = calls++ == 0 ? 0 : 1;
    if (sent > n) sent = n;
    for (unsigned i = 0; i < sent; ++i) rte_pktmbuf_free(m[i]);
    return sent;
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    host.port_count = 1; host.ports[0] = 7;
    int fd[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 2, false));
    struct observer_record r = {.kind = OBS_END, .port = 0};
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
        assert(observer_emit(p, &r));
        assert(observer_emit(p, &r));
        assert(!observer_emit(p, &r));
        struct observer_health h = observer_health(p);
        assert(h.records_attempted == 3 && h.records_dropped == 1 && h.queue_high_water == 2);
    }
    struct observer_health h = observer_health(OBS_RX);
    assert(h.records_attempted == 3 && h.records_dropped == 1 && h.queue_high_water == 2);
    struct observer_snapshot snapshot = {.count = 1, .ports = {0}, .seen = {5}, .lower_bound = {99}};
    observer_publish_snapshot(0, &snapshot);
    struct observer_snapshot loaded = {0};
    assert(observer_load_snapshot(OBS_TX, &loaded));
    assert(loaded.count == 1 && loaded.seen[0] == 5 && loaded.lower_bound[0] == 99);
    assert(observer_start());
    assert(!observer_close(true));
    h = observer_health(OBS_RX);
    assert(h.records_attempted == 3 && h.records_dropped == 1 && h.records_emitted == 2);
    close(fd[0]);
    char data[32768]; ssize_t n = read(fd[1], data, sizeof(data)-1); assert(n > 0); data[n] = 0;
    assert(strstr(data, "\"producer_seq\":1") && strstr(data, "\"producer_seq\":2"));
    assert(strstr(data, "\"complete\":false") && strstr(data, "\"dropped\":1")); close(fd[1]);

    /* No reader: a full pipe must never turn shutdown into a consumer wait. */
    assert(pipe(fd) == 0); assert(fcntl(fd[1], F_SETFL, O_NONBLOCK) == 0);
    memset(data, 'x', sizeof(data)); while(write(fd[1], data, sizeof(data)) > 0) {}
    assert(observer_open(fd[1], OBS_ID_STAGE1, 9000, 2, false));
    assert(observer_emit(OBS_RX, &r)); assert(observer_start()); assert(!observer_close(true));
    assert(observer_health(OBS_RX).capture_drops > 0); close(fd[0]); close(fd[1]);
    assert(!observer_enabled());
    assert(host_load("config.json"));
    host.port_count = 1; host.ports[0] = 7;
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 64, false));
    assert(observer_start());
    struct rte_mempool *pool = rte_pktmbuf_pool_create("observer_pool", 63, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct rte_mbuf *in[2], *out[MAX_BURST_SIZE];
    for (unsigned i = 0; i < 2; ++i) {
        in[i] = rte_pktmbuf_alloc(pool); assert(in[i]);
        uint8_t *d = (uint8_t *)rte_pktmbuf_append(in[i], 76); assert(d); memset(d, 0, 76);
        d[12] = 0x81; d[15] = 100; d[16] = 0x88; d[17] = 0xb5; d[18] = i;
        *packet_meta(in[i]) = (struct packet_meta){.ingress_port = 7, .ingress_seq = i, .arrival_ns = i * 1000};
        observer_input(OBS_RX, in[i]);
        assert(packet_meta(in[i])->observation.identity_kind == 2);
        assert(packet_meta(in[i])->observation.selection_seq == i);
    }
    struct pipeline_stats stats = {0}; struct worker_state wk; struct tx_state tx;
    worker_state_init(&wk, &stats, 2); tx_state_init(&tx, &stats); tx.burst = partial;
    assert(worker_step_at(&wk, in, 2, out, 0) == 0);
    worker_end_input(&wk);
    unsigned count = worker_step_at(&wk, NULL, 0, out, 10000); assert(count == 2);
    struct observer_record *reservations[MAX_BURST_SIZE];
    worker_dispatch_prepare(&wk, out, count, reservations, 10000);
    worker_dispatch_done(&wk, reservations, count, count);
    tx_step_at(&tx, out, count, 10000); assert(stats.tx.transmitted == 0);
    tx_step_at(&tx, NULL, 0, 11000); assert(stats.tx.transmitted == 1);
    tx_step_at(&tx, NULL, 0, 12000); assert(stats.tx.transmitted == 2);
    worker_state_cleanup(&wk); host_cleanup();
    observer_input_end(OBS_RX, 7, 2);
    assert(observer_close(true)); close(fd[0]);
    n = read(fd[1], data, sizeof(data)-1); assert(n > 0); data[n] = 0; close(fd[1]);
    assert(strstr(data, "\"kind\":1") && strstr(data, "\"kind\":5") && strstr(data, "\"kind\":6"));
    assert(strstr(data, "\"accepted_count\":0") && strstr(data, "\"accepted_count\":1"));
    assert(strstr(data, "\"consumed_dispatch\":[2,0]"));
    assert(rte_mempool_avail_count(pool) == pool->size);
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_STAGE1, 9000, 16, false)); assert(observer_start());
    struct rte_mbuf *probe = rte_pktmbuf_alloc(pool); assert(probe);
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(probe, 146); assert(d); memset(d, 0, 146);
    d[12] = 0x81; d[15] = 100; d[16] = 8; d[18] = 0x45; d[21] = 128; d[27] = 17;
    d[30] = 192; d[32] = 2; d[33] = 1; d[34] = 198; d[35] = 51; d[36] = 100; d[37] = 2;
    d[38] = 0x2e; d[39] = 0xe0; d[40] = 0x23; d[41] = 0x28; d[43] = 108;
    d[52] = 0x12; d[53] = 0x34; d[54] = 0x4c; d[55] = 0x30; d[56] = 0x50; d[57] = 0x4b; d[58] = 1;
    uint8_t original[146]; memcpy(original, d, sizeof(original));
    *packet_meta(probe) = (struct packet_meta){.ingress_port = 7}; observer_input(OBS_RX, probe);
    struct observer_packet decoded = observer_packet(probe);
    assert(decoded.identity_kind == 1 && decoded.identity == 0x1234 && decoded.selected_late == 1);
    assert(decoded.source == UINT32_C(0xc0000201) && decoded.destination == UINT32_C(0xc6336402));
    assert(!memcmp(d, original, sizeof(original)));
    d[54] = 0; packet_meta(probe)->ingress_seq = 1; observer_input(OBS_RX, probe);
    assert(observer_packet(probe).identity_kind == 0);
    d[54] = 0x4c; d[15] = 102; packet_meta(probe)->ingress_seq = 2; observer_input(OBS_RX, probe);
    assert(!observer_packet(probe).classified && observer_packet(probe).identity_kind == 1);
    probe->ol_flags = RTE_MBUF_F_RX_VLAN_STRIPPED; packet_meta(probe)->ingress_seq = 3; observer_input(OBS_RX, probe);
    assert(observer_packet(probe).stripped && !observer_packet(probe).classified);
    observer_input_end(OBS_RX, 7, 4); rte_pktmbuf_free(probe);
    assert(!observer_close(true)); assert(observer_health(OBS_RX).identity_errors == 1);
    close(fd[0]); close(fd[1]);
    rte_mempool_free(pool);

    assert(pipe(fd) == 0); close(fd[0]);
    assert(observer_open(fd[1], OBS_ID_STAGE1, 9000, 2, false));
    assert(observer_emit(OBS_RX, &r)); assert(observer_start());
    assert(!observer_close(true)); close(fd[1]);
    assert(observer_health(OBS_RX).capture_drops == 1);
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 2, false));
    assert(observer_reserve(OBS_WORKER0)); /* Producer abort before publish. */
    assert(observer_emit(OBS_TX, &r)); /* A later producer must not prevent shutdown. */
    assert(observer_start()); assert(!observer_close(false));
    assert(observer_health(OBS_WORKER0).capture_drops == 1);
    assert(observer_health(OBS_TX).capture_drops == 1);
    close(fd[0]); close(fd[1]);
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 2, false));
    observer_abort(OBS_ABORT_DRAIN);
    assert(observer_start()); assert(!observer_close(true));
    close(fd[0]); n = read(fd[1], data, sizeof(data)-1); assert(n > 0); data[n] = 0;
    assert(strstr(data, "\"abort_reasons\":1")); close(fd[1]);
    assert(rte_eal_cleanup() == 0);
    puts("observer bounded queue, exact loss and stalled-consumer shutdown: PASS");
}
