/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <signal.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "../core/transmit.h"

static uint64_t clock_ns;
static unsigned tx_calls;
uint64_t __wrap_host_now_ns(void) { return clock_ns; }

static uint16_t transmit(uint16_t port, uint16_t queue, struct rte_mbuf **items, uint16_t n)
{
    (void)port; (void)queue;
    if (!tx_calls++) return 0;
    for (unsigned i = 0; i < n; ++i) rte_pktmbuf_free(items[i]);
    return n;
}

/* Replay the same deterministic capture; consumers start after publication so
 * queue peaks and every legacy JSON byte, including health, are reproducible. */
static void capture(const char *directory, bool binary, struct rte_mempool *pool)
{
    assert(host_load("config.json"));
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    char path[4096]; snprintf(path, sizeof(path), "%s/%s", directory, binary ? "native.hngb" : "native.jsonl");
    int target = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600); assert(target >= 0);
    int null_fd = open("/dev/null", O_WRONLY); assert(null_fd >= 0);
    assert(!observer_open_binary(null_fd, OBS_ID_SOFTWARE_L2, 9000, 128, false));
    assert(!observer_open_binary_discard(target, OBS_ID_SOFTWARE_L2, 9000, 128, false));
    close(null_fd);
    int sockets[2] = {-1, -1};
    if (binary) assert(observer_open_binary(target, OBS_ID_SOFTWARE_L2, 9000, 128, false));
    else {
        assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
        assert(observer_open(sockets[0], OBS_ID_SOFTWARE_L2, 9000, 128, false));
    }
    const uint64_t epoch = UINT64_C(1) << 53;
    clock_ns = epoch; tx_calls = 0;
    struct rte_eth_stats nic = {0};
    for (unsigned p = 0; p < 2; ++p)
        observer_resources(host.ports[p], false, pool->size, pool->size, &nic);
    struct pipeline_stats stats = {0};
    struct tx_state tx; tx_state_init(&tx, &stats); tx.burst = transmit;
    for (unsigned p = 0; p < 2; ++p) {
        struct worker_state wk; worker_state_init_owner(&wk, &stats.owners[p], host.ports[p]);
        struct rte_mbuf *input[2], *out[MAX_BURST_SIZE];
        for (unsigned i = 0; i < 2; ++i) {
            input[i] = rte_pktmbuf_alloc(pool); assert(input[i]);
            uint8_t *data = (uint8_t *)rte_pktmbuf_append(input[i], 76); assert(data); memset(data, 0, 76);
            data[12] = 0x81; data[15] = 100; data[16] = 0x88; data[17] = 0xb5;
            uint64_t identity = UINT64_MAX - i; memcpy(data + 18, &identity, sizeof(identity));
            *packet_meta(input[i]) = (struct packet_meta){.ingress_port=host.ports[p], .ingress_seq=i, .arrival_ns=epoch+100};
            observer_input(OBS_RX, input[i]);
        }
        assert(worker_step_at(&wk, input, 2, out, epoch+100) == 0);
        host_publish(host.ports[p], 2, epoch+200);
        struct observer_record frontier; observer_record_init(&frontier, OBS_FRONTIER, 0);
        frontier.port = host.ports[p]; frontier.at_ns = epoch+199; frontier.actual_selection_seq = 2;
        assert(observer_emit(OBS_RX, &frontier));
        clock_ns = epoch+100;
        worker_end_input(&wk);
        unsigned n = worker_step_at(&wk, NULL, 0, out, epoch+1000); assert(n == 1);
        struct observer_record *reservations[MAX_BURST_SIZE];
        worker_dispatch_prepare(&wk, out, n, reservations, epoch+1000);
        worker_dispatch_done(&wk, reservations, n, n);
        tx_step_at(&tx, out, n, epoch+1000);
        if (tx_has_backlog(&tx)) tx_step_at(&tx, NULL, 0, epoch+1100);
        worker_state_cleanup(&wk);
        observer_input_end(OBS_RX, host.ports[p], 2);
    }
    assert(stats.tx.transmitted == 2);
    clock_ns = epoch+2000; nic.ipackets = 2; nic.opackets = 1;
    for (unsigned p = 0; p < 2; ++p)
        observer_resources(host.ports[p], true, pool->size, pool->size, &nic);
    assert(observer_start()); assert(observer_close(true));
    if (!binary) {
        close(sockets[0]);
        char buffer[16384]; ssize_t n;
        while ((n = read(sockets[1], buffer, sizeof(buffer))) > 0) assert(write(target, buffer, (size_t)n) == n);
        assert(n == 0); close(sockets[1]);
    }
    close(target); host_cleanup();
    assert(rte_mempool_avail_count(pool) == pool->size);
}

static void failed_capture(const char *directory, bool io_failure)
{
    assert(host_load("config.json")); host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    char path[4096]; snprintf(path, sizeof(path), "%s/%s", directory, io_failure ? "write-failure.hngb" : "queue-loss.hngb");
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600); assert(fd >= 0);
    assert(observer_open_binary(fd, OBS_ID_SOFTWARE_L2, 9000, io_failure ? 128 : 2, false));
    struct observer_record r; observer_record_init(&r, OBS_FRONTIER, 0); r.port = 7;
    if (io_failure) for (unsigned i = 0; i < 100; ++i) { r.at_ns = i; assert(observer_emit(OBS_RX, &r)); }
    observer_input_end(OBS_RX, 7, 0); observer_input_end(OBS_RX, 13, 0);
    struct rlimit original, limited;
    void (*handler)(int) = SIG_DFL;
    if (io_failure) {
        assert(getrlimit(RLIMIT_FSIZE, &original) == 0); limited = original; limited.rlim_cur = 512;
        handler = signal(SIGXFSZ, SIG_IGN); assert(handler != SIG_ERR);
        assert(setrlimit(RLIMIT_FSIZE, &limited) == 0);
    } else assert(!observer_emit(OBS_RX, &r));
    assert(observer_start()); assert(!observer_close(true));
    struct observer_health h = observer_health(OBS_RX);
    if (io_failure) {
        assert(h.records_attempted == 102 && h.capture_drops == 102 && h.records_emitted == 0 && h.records_dropped == 0);
        assert(setrlimit(RLIMIT_FSIZE, &original) == 0); assert(signal(SIGXFSZ, handler) != SIG_ERR);
    } else assert(h.records_attempted == 3 && h.records_emitted == 2 && h.records_dropped == 1);
    close(fd); host_cleanup();
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv); assert(eal >= 0);
    argc -= eal; argv += eal; assert(argc == 2);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("binary_fixture", 63, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    capture(argv[1], false, pool); capture(argv[1], true, pool);
    failed_capture(argv[1], false); failed_capture(argv[1], true);
    rte_mempool_free(pool); assert(rte_eal_cleanup() == 0);
}
