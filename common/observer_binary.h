/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HNG_OBSERVER_BINARY_H
#define HNG_OBSERVER_BINARY_H
#include <stddef.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include "observer.h"

struct rte_net_crc;
struct observer_io_slot;
#define OBS_IO_BLOCKS 2048u
struct observer_binary_writer {
    int fd;
    bool failed;
    size_t used;
    uint64_t frame, pending[OBS_PRODUCERS];
    struct rte_net_crc *crc;
    unsigned char buffer[65536];
    struct observer_io_slot *slots;
    pthread_t io_thread;
    sem_t ready, done;
    bool ready_initialized, done_initialized, io_started;
    _Atomic bool io_stop;
    uint64_t submitted, reaped, io_peak;
    int io_cpu;
};

bool observer_binary_init(struct observer_binary_writer *writer, int fd);
bool observer_binary_text(struct observer_binary_writer *writer, unsigned type, const void *text, size_t length);
bool observer_binary_record(struct observer_binary_writer *writer, unsigned producer, const struct observer_record *record);
bool observer_binary_flush(struct observer_binary_writer *writer);
void observer_binary_destroy(struct observer_binary_writer *writer);
#endif
