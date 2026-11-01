/* SPDX-License-Identifier: Apache-2.0 */
/* Compile with NDEBUG: synchronization must not depend on assertions. */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../common/observer_binary.h"
#define CHECK(condition) do { if (!(condition)) abort(); } while (0)

static uint32_t little(const unsigned char *p, unsigned bytes)
{
    uint32_t value = 0;
    for (unsigned i = 0; i < bytes; ++i) value |= (uint32_t)p[i] << (8 * i);
    return value;
}

static void run(bool fail)
{
    FILE *file = tmpfile(); CHECK(file);
    struct observer_binary_writer *writer = calloc(1, sizeof(*writer)); CHECK(writer);
    CHECK(observer_binary_init(writer, fileno(file)));
    struct rlimit original, limited;
    void (*handler)(int) = SIG_DFL;
    if (fail) {
        CHECK(getrlimit(RLIMIT_FSIZE, &original) == 0);
        limited = original; limited.rlim_cur = 150000;
        handler = signal(SIGXFSZ, SIG_IGN); CHECK(handler != SIG_ERR);
        CHECK(setrlimit(RLIMIT_FSIZE, &limited) == 0);
    }
    uint64_t staged = 0;
    struct observer_record record = {0}; record.kind = OBS_FRONTIER;
    for (unsigned i = 0; i < 50000; ++i) {
        record.producer_seq = i + 1; record.order = i; record.at_ns = UINT64_MAX;
        if (!observer_binary_record(writer, OBS_RX, &record)) { CHECK(fail); break; }
        staged++;
    }
    CHECK(observer_binary_flush(writer) == !fail);
    CHECK(writer->submitted == writer->reaped);
    CHECK(writer->io_peak >= 1 && writer->io_peak <= OBS_IO_BLOCKS);
    if (fail) {
        CHECK(setrlimit(RLIMIT_FSIZE, &original) == 0);
        CHECK(signal(SIGXFSZ, handler) != SIG_ERR);
    }
    /* Count only whole, successfully written frames: the partial suffix is loss. */
    uint64_t prefix = 0; off_t offset = 8;
    unsigned char frame[65536];
    while (pread(fileno(file), frame, 16, offset) == 16) {
        unsigned bytes = little(frame + 4, 4);
        CHECK(bytes <= sizeof(frame) - 20);
        if (pread(fileno(file), frame, bytes + 4, offset + 16) != (ssize_t)(bytes + 4)) break;
        for (unsigned i = 0; i < bytes; ) {
            CHECK(i + 2 <= bytes);
            unsigned size = little(frame + i, 2); CHECK(size && i + size + 2 <= bytes);
            prefix++; i += size + 2;
        }
        offset += 20 + bytes;
    }
    CHECK(prefix + writer->pending[OBS_RX] == staged);
    CHECK(fail ? (prefix > 0 && writer->pending[OBS_RX] > 0) : prefix == 50000);
    observer_binary_destroy(writer); free(writer); CHECK(fclose(file) == 0);
}

int main(int argc, char **argv)
{
    CHECK(rte_eal_init(argc, argv) >= 0);
    run(false); run(true);
    CHECK(rte_eal_cleanup() == 0);
    puts("binary async I/O: release synchronization and partial-frame loss PASS");
}
