/* SPDX-License-Identifier: Apache-2.0 */
#ifndef IO_ORACLE_H
#define IO_ORACLE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define IO_BATCH 1024
#define IO_MAX_FRAME 1518
struct io_record { uint64_t id, arrival, seq, release; uint16_t port, vlan, length; };
struct io_model { uint64_t rate, q, delay, delta, seed, draw_base; };
size_t io_reference(const struct io_record *in, size_t count, struct io_model model,
    struct io_record *out, uint64_t drops[4]);
bool io_equal(const struct io_record *expected, const struct io_record *actual);
bool io_frame_equal(const struct io_record *expected, const uint8_t *bytes, size_t length);
uint64_t io_select(uint64_t seed, uint64_t seq);
void io_fixture(struct io_record *r, unsigned seed, unsigned port, unsigned seq);
void io_bytes(const struct io_record *r, uint8_t *bytes);
#endif
