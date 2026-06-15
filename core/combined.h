#ifndef COMBINED_H
#define COMBINED_H

#include "../common/common.h"

// Worker + TX combined (3-core mode).
// Reads from receive_worker_ring, processes, transmits directly.
int lcore_worker_transmit(struct lcore_parameters *parameters);

// RX + Worker + TX combined (2-core mode).
// No rings needed - receives, processes, transmits all in one loop.
int lcore_receive_worker_transmit(struct lcore_parameters *parameters);

#endif // COMBINED_H
