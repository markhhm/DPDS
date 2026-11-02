/* SPDX-License-Identifier: Apache-2.0 */
/* Test-only bus substitution: software packets and pools remain real PMD fixtures. */
#include <stdlib.h>
#include <string.h>
#include <rte_bus.h>
#include <rte_dev.h>
#include <rte_ethdev.h>
static const struct rte_device *devices[2];
static const int physical_bus;
int __real_rte_eth_dev_info_get(uint16_t port, struct rte_eth_dev_info *info);
const struct rte_bus *__real_rte_dev_bus(const struct rte_device *device);
const char *__real_rte_bus_name(const struct rte_bus *bus);
int __wrap_rte_eth_dev_info_get(uint16_t port, struct rte_eth_dev_info *info)
{
    int result = __real_rte_eth_dev_info_get(port, info);
    if (!result && port < 2 && !(port == 1 && getenv("HNG_TEST_MIXED"))) {
        devices[port] = info->device;
        const char *driver = getenv("HNG_TEST_DRIVER"), *bus = getenv("HNG_TEST_BUS");
        info->driver_name = driver ? driver : bus && !strcmp(bus, "auxiliary") ? "mlx5_auxiliary" : "mlx5_pci";
    }
    return result;
}
const struct rte_bus *__wrap_rte_dev_bus(const struct rte_device *device)
{
    if (device && (device == devices[0] || device == devices[1]))
        return (const struct rte_bus *)&physical_bus;
    return __real_rte_dev_bus(device);
}
const char *__wrap_rte_bus_name(const struct rte_bus *bus)
{
    if (bus == (const struct rte_bus *)&physical_bus) {
        const char *name = getenv("HNG_TEST_BUS");
        return name ? name : "pci";
    }
    return __real_rte_bus_name(bus);
}
