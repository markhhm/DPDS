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

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    int fd[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 2));
    struct observer_record r = {.kind = OBS_END, .port = 0};
    assert(observer_emit(OBS_RX, &r));
    assert(observer_emit(OBS_RX, &r));
    assert(!observer_emit(OBS_RX, &r));
    struct observer_health h = observer_health(OBS_RX);
    assert(h.records_attempted == 3 && h.records_dropped == 1 && h.queue_high_water == 2);
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
    assert(observer_open(fd[1], OBS_ID_STAGE1, 9000, 2));
    assert(observer_emit(OBS_RX, &r)); assert(observer_start()); assert(!observer_close(true));
    assert(observer_health(OBS_RX).capture_drops > 0); close(fd[0]); close(fd[1]);
    assert(!observer_enabled());
    assert(rte_eal_cleanup() == 0);
    puts("observer bounded queue, exact loss and stalled-consumer shutdown: PASS");
}
