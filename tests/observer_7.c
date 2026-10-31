/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../core/receive.h"
#include "../core/worker.h"
#include "../core/transmit.h"

#define PACKETS 8
static struct rte_mempool *pool;
static struct pipeline_stats stats;
static pthread_barrier_t start, ready, paused, resume;
static unsigned calls[2];
struct direction { unsigned owner; struct rte_mbuf *batch[PACKETS]; struct tx_state tx; };

static void *paused_rx1(void *argument)
{
    bool reserve = *(bool *)argument;
    if (reserve) assert(observer_reserve(OBS_RX1));
    pthread_barrier_wait(&paused);
    pthread_barrier_wait(&resume);
    if (!reserve) observer_input_end(OBS_RX1, host.ports[1], 0);
    return NULL;
}

static uint16_t partial(uint16_t port, uint16_t queue, struct rte_mbuf **batch, uint16_t count)
{
    (void)queue;
    unsigned owner = port == host.ports[1] ? 0 : 1;
    for (unsigned i = 0; i < count; ++i) {
        struct observer_packet x = observer_packet(batch[i]);
        assert(x.ingress_port == host.ports[owner] && x.handoff_queue == owner);
    }
    if (!calls[owner]++) return 0;
    for (unsigned i = 0; i < count; ++i) rte_pktmbuf_free(batch[i]);
    return count;
}

static void *receive_and_schedule(void *argument)
{
    struct direction *d = argument;
    unsigned k = d->owner;
    uint16_t port = host.ports[k];
    struct rx_state rx; rx_state_init(&rx, &stats);
    rx.producer = k ? OBS_RX1 : OBS_RX;
    rx.stats = &stats.rx_owners[k]; rx.split = true;
    rx.last_port = port; rx.last_index = k; rx.last_watermark = 8000;
    struct worker_state worker; worker_state_init_owner(&worker, &stats.owners[k], port);
    struct rte_mbuf *input[PACKETS];
    pthread_barrier_wait(&start);
    for (unsigned i = 0; i < PACKETS; ++i) {
        input[i] = rte_pktmbuf_alloc(pool); assert(input[i]);
        uint8_t *data = (uint8_t *)rte_pktmbuf_append(input[i], 76); assert(data); memset(data, 0, 76);
        data[12] = 0x81; data[15] = 100 + i % 2; data[16] = 0x88; data[17] = 0xb5; data[18] = k * PACKETS + i;
        *packet_meta(input[i]) = (struct packet_meta){.ingress_port = port, .ingress_seq = i, .arrival_ns = i * 1000};
        observer_input(rx.producer, input[i]);
    }
    rx.sequence[port] = PACKETS; rx.last_arrival[port] = 7000;
    rx_publish(&rx, PACKETS);
    observer_input_end(rx.producer, port, PACKETS);
    assert(worker_step_at(&worker, input, PACKETS, d->batch, 10000) == PACKETS);
    worker_end_input(&worker);
    struct rte_mbuf *extra[MAX_BURST_SIZE];
    assert(worker_step_at(&worker, NULL, 0, extra, 10000) == 0);
    struct observer_record *reservations[MAX_BURST_SIZE];
    worker_dispatch_prepare(&worker, d->batch, PACKETS, reservations, 10000);
    worker_dispatch_done(&worker, reservations, PACKETS, PACKETS);
    worker_state_cleanup(&worker);
    /* Both final snapshots are published before either TX reads them. */
    pthread_barrier_wait(&ready);
    return NULL;
}

static void *transmit(void *argument)
{
    struct direction *d = argument;
    unsigned k = d->owner;
    tx_state_init(&d->tx, &stats);
    d->tx.stats = &stats.tx_owners[k]; d->tx.producer = k ? OBS_TX1 : OBS_TX;
    d->tx.handoff_mask = 1U << k; d->tx.burst = partial;
    pthread_barrier_wait(&start);
    pthread_barrier_wait(&ready);
    tx_step_at(&d->tx, d->batch, PACKETS, 11000);
    tx_step_at(&d->tx, NULL, 0, 12000);
    assert(!tx_has_backlog(&d->tx));
    assert(d->tx.consumed_dispatch[k] == PACKETS && d->tx.consumed_dispatch[1-k] == 0);
    assert(stats.tx_owners[k].transmitted == PACKETS && stats.tx_owners[k].retries == 1);
    return NULL;
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("config.json"));
    assert(OBS_PRODUCERS == 7 && OBS_RX1 == 5 && OBS_TX1 == 6);
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    int fd[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 128, true) && observer_start());
    pool = rte_pktmbuf_pool_create("observer_7", 63, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    assert(!pthread_barrier_init(&start, NULL, 4) && !pthread_barrier_init(&ready, NULL, 4));
    struct direction directions[2] = {{.owner = 0}, {.owner = 1}};
    pthread_t threads[4];
    for (unsigned k = 0; k < 2; ++k) {
        assert(!pthread_create(&threads[k], NULL, receive_and_schedule, &directions[k]));
        assert(!pthread_create(&threads[2+k], NULL, transmit, &directions[k]));
    }
    for (unsigned i = 0; i < 4; ++i) assert(!pthread_join(threads[i], NULL));
    assert(!pthread_barrier_destroy(&start) && !pthread_barrier_destroy(&ready));
    assert(!host.invalid && observer_close(true)); close(fd[0]);
    char raw[131072]; size_t used = 0; ssize_t n;
    while ((n = read(fd[1], raw + used, sizeof(raw)-1-used)) > 0) used += (size_t)n;
    raw[used] = 0; close(fd[1]);
    assert(strstr(raw, "\"schema\":\"hng-native-v2\"") && strstr(raw, "\"layout\":\"per-direction\""));
    assert(strstr(raw, "\"ports\":[7,13]") && strstr(raw, "\"complete\":true"));
    assert(strstr(raw, "\"producer\":5,\"producer_seq\":1") && strstr(raw, "\"producer\":6,\"producer_seq\":1"));
    assert(strstr(raw, "\"consumed_dispatch\":[8,0]") && strstr(raw, "\"consumed_dispatch\":[0,8]"));
    for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
        struct observer_health h = observer_health(p);
        assert(h.records_attempted == h.records_emitted && !h.records_dropped && !h.capture_drops && !h.identity_errors && !h.malformed_records);
    }
    assert(observer_health(OBS_RX).records_emitted == PACKETS + 2 && observer_health(OBS_RX1).records_emitted == PACKETS + 2);
    assert(observer_health(OBS_TX).records_emitted == 2 && observer_health(OBS_TX1).records_emitted == 2);
    assert(rte_mempool_avail_count(pool) == pool->size); rte_mempool_free(pool);
    for (unsigned mode = 0; mode < 3; ++mode) {
        assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
        if (mode == 2) {
            /* A stalled reader must produce visible loss without a blocking drain. */
            assert(fcntl(fd[0], F_SETFL, O_NONBLOCK) == 0);
            char padding[4096] = {0};
            while (write(fd[0], padding, sizeof(padding)) > 0) {}
            assert(errno == EAGAIN || errno == EWOULDBLOCK);
        }
        assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 16, true) && observer_start());
        assert(!pthread_barrier_init(&paused, NULL, 2) && !pthread_barrier_init(&resume, NULL, 2));
        bool reserve = mode == 0;
        pthread_t delayed; assert(!pthread_create(&delayed, NULL, paused_rx1, &reserve));
        pthread_barrier_wait(&paused);
        if (reserve) {
            struct observer_record later = {.kind = OBS_ATTEMPT, .port = 7};
            assert(observer_emit(OBS_TX1, &later));
            assert(observer_health(OBS_TX1).records_emitted == 0);
        } else {
            observer_input_end(OBS_RX, 7, 0);
            assert(observer_health(OBS_RX).records_attempted == 1);
            assert(observer_health(OBS_RX1).records_attempted == 0);
        }
        pthread_barrier_wait(&resume);
        assert(!pthread_join(delayed, NULL));
        assert(observer_close(!reserve) == (mode == 1));
        if (reserve) {
            assert(observer_health(OBS_RX1).capture_drops == 1 && observer_health(OBS_TX1).capture_drops == 1);
        } else if (mode == 2) {
            assert(observer_health(OBS_RX).capture_drops == 1 && observer_health(OBS_RX1).capture_drops == 1);
        }
        close(fd[0]); close(fd[1]);
        assert(!pthread_barrier_destroy(&paused) && !pthread_barrier_destroy(&resume));
    }
    /* Supplying every valid END must not hide an unknown port or a foreign writer. */
    for (unsigned mode = 0; mode < 2; ++mode) {
        host.port_count = mode + 1;
        assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
        assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 16, mode != 0) && observer_start());
        observer_input_end(OBS_RX, mode ? 13 : 99, 0);
        observer_input_end(OBS_RX, 7, 0);
        if (mode) observer_input_end(OBS_RX1, 13, 0);
        assert(!observer_close(true)); close(fd[0]);
        n = read(fd[1], raw, sizeof(raw)-1); assert(n > 0); raw[n] = 0; close(fd[1]);
        assert(strstr(raw, mode ? "\"abort_reasons\":2" : "\"abort_reasons\":4"));
    }
    host_cleanup(); assert(rte_eal_cleanup() == 0);
    puts("concurrent RX/TX owners, native v2 and local retry prefixes: PASS");
}
