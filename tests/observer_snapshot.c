/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../common/common.h"
#include "../common/observer.h"

#define STEPS 100000
static pthread_barrier_t start;
static _Atomic unsigned finished;
static int cpus[CPU_SETSIZE], cpu_count;
static bool per_direction;

static struct observer_snapshot value(unsigned owner, uint64_t step)
{
    struct observer_snapshot snapshot = {.count = 2, .ports = {7, 13}};
    snapshot.seen[owner] = step;
    snapshot.lower_bound[owner] = step == STEPS ? UINT64_MAX : 17*step + owner;
    snapshot.ended[owner] = step == STEPS;
    return snapshot;
}

static void *publish(void *argument)
{
    unsigned owner = *(unsigned *)argument;
    cpu_set_t affinity; CPU_ZERO(&affinity); CPU_SET(cpus[(owner+1)%cpu_count], &affinity);
    assert(pthread_setaffinity_np(pthread_self(), sizeof(affinity), &affinity) == 0);
    pthread_barrier_wait(&start);
    for (uint64_t step = 2; step <= STEPS; ++step) {
        struct observer_snapshot snapshot = value(owner, step);
        observer_publish_snapshot(owner, &snapshot);
    }
    atomic_fetch_add(&finished, 1);
    return NULL;
}

static void *read_snapshots(void *argument)
{
    unsigned reader = *(unsigned *)argument;
    struct observer_snapshot loaded = {0};
    uint64_t last[2] = {0};
    pthread_barrier_wait(&start);
    for (unsigned reads = 0; reads < STEPS || atomic_load(&finished) != 2; ++reads) {
        assert(observer_load_snapshot(reader ? OBS_TX1 : OBS_TX, &loaded));
        assert(loaded.count == 2);
        for (unsigned i = 0; i < loaded.count; ++i) {
            unsigned owner = loaded.ports[i] == 7 ? 0 : 1;
            assert(loaded.ports[i] == (owner ? 13 : 7));
            uint64_t seen = loaded.seen[i];
            if (per_direction && owner != reader) {
                assert(seen == 0 && loaded.lower_bound[i] == 0 && !loaded.ended[i]);
                continue;
            }
            assert(seen >= last[owner] && seen <= STEPS);
            assert(loaded.lower_bound[i] == (seen == STEPS ? UINT64_MAX : 17*seen+owner));
            assert(loaded.ended[i] == (seen == STEPS));
            last[owner] = seen;
        }
    }
    return NULL;
}

static void stress(bool split)
{
    per_direction = split;
    atomic_store(&finished, 0);
    int fd[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 4, split) && observer_start());
    struct observer_snapshot loaded = {0};
    assert(observer_load_snapshot(OBS_TX, &loaded) && loaded.count == 0);
    for (unsigned owner = 0; owner < 2; ++owner) {
        struct observer_snapshot snapshot = value(owner, 1);
        observer_publish_snapshot(owner, &snapshot);
    }
    pthread_t writers[2], second_reader; unsigned owners[] = {0, 1};
    assert(pthread_barrier_init(&start, NULL, split ? 4 : 3) == 0);
    for (unsigned owner = 0; owner < 2; ++owner)
        assert(pthread_create(&writers[owner], NULL, publish, &owners[owner]) == 0);
    if (split) assert(pthread_create(&second_reader, NULL, read_snapshots, &owners[1]) == 0);
    read_snapshots(&owners[0]);
    for (unsigned owner = 0; owner < 2; ++owner) assert(pthread_join(writers[owner], NULL) == 0);
    if (split) assert(pthread_join(second_reader, NULL) == 0);
    for (unsigned reader = 0; reader < (split ? 2u : 1u); ++reader) {
        assert(observer_load_snapshot(reader ? OBS_TX1 : OBS_TX, &loaded));
        for (unsigned i = 0; i < loaded.count; ++i) {
            bool owned = !split || loaded.ports[i] == host.ports[reader];
            assert(loaded.seen[i] == (owned ? STEPS : 0) && loaded.ended[i] == owned);
        }
    }
    assert(observer_health(OBS_TX).malformed_records == 0);
    assert(observer_health(OBS_TX1).malformed_records == 0);
    observer_input_end(OBS_RX, 7, STEPS);
    observer_input_end(split ? OBS_RX1 : OBS_RX, 13, STEPS);
    assert(observer_close(true)); close(fd[0]); close(fd[1]);
    assert(pthread_barrier_destroy(&start) == 0);
}

int main(int argc, char **argv)
{
    cpu_set_t allowed; assert(sched_getaffinity(0, sizeof(allowed), &allowed) == 0);
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) if (CPU_ISSET(cpu, &allowed)) cpus[cpu_count++] = cpu;
    assert(cpu_count && rte_eal_init(argc, argv) >= 0);
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    stress(false);
    stress(true);
    int fd[2];
    struct observer_snapshot loaded = {0};
    /* A new capture starts empty; a truly invalid merged port set still fails. */
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 2, false) && observer_start());
    assert(observer_load_snapshot(OBS_TX, &loaded) && loaded.count == 0);
    struct observer_snapshot first = {.count=2, .ports={7,13}}, second = {.count=1, .ports={19}};
    observer_publish_snapshot(0, &first); observer_publish_snapshot(1, &second);
    assert(!observer_load_snapshot(OBS_TX, &loaded) && loaded.count == 0);
    assert(observer_health(OBS_TX).malformed_records == 1);
    first.count = OBS_PORTS+1;
    observer_publish_snapshot(0, &first);
    assert(!observer_load_snapshot(OBS_TX, &loaded) && loaded.count == 0);
    assert(observer_health(OBS_TX).malformed_records == 2);
    assert(!observer_close(true)); close(fd[0]); close(fd[1]);
    assert(rte_eal_cleanup() == 0);
    puts("observer snapshots: two concurrent publishers, coherent monotone reads, reset and invalid evidence: PASS");
}
