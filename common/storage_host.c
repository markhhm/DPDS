/* SPDX-License-Identifier: Apache-2.0 */
#include "common.h"
#include <inttypes.h>
#include <rte_telemetry.h>
bool host_storage_bind_pool(uint16_t ingress, struct rte_mempool *pool, int socket)
{
    for (size_t i=0; i<host.line_count; ++i) {
        if (!host.m7_plan[i].slots || host.lines[i].ingress_port!=ingress) continue;
        if (host.m7[i] || atomic_load(&host.launched)) return false;
        host.m7[i]=m7_storage_create(&host.m7_plan[i],pool,host.lines[i].egress_port,socket);
        if (!host.m7[i]) {
            for (size_t j=0; j<i; ++j) {
                if (host.lines[j].ingress_port==ingress && host.m7[j]) {
                    m7_storage_destroy(host.m7[j]); host.m7[j]=NULL;
                }
            }
            return false;
        }
        host_storage_publish(i);
    }
    return true;
}
void host_storage_publish(size_t line)
{
    if (!host.m7[line]) return;
    struct m7_storage_stats stats=m7_storage_stats(host.m7[line]);
#define PUBLISH(name) atomic_store_explicit(&host.m7_snapshot[line].name,stats.name,memory_order_relaxed);
    M7_STORAGE_FIELDS(PUBLISH)
#undef PUBLISH
}

/* Owner atomics survive backend destruction. Telemetry never touches storage. */
static void number(FILE *out, struct rte_tel_data *tel, size_t line, const char *field, uint64_t value)
{
    char key[80];snprintf(key,sizeof(key),"m7_line%zu_%s",line,field);
    if(out)fprintf(out,"%s=%"PRIu64"\n",key,value);
    else rte_tel_data_add_dict_uint(tel,key,value);
}
void host_storage_report(FILE *out, struct rte_tel_data *tel)
{
    for(size_t line=0;line<host.line_count;++line) {
        const struct M7StorageInfo *p=&host.m7_plan[line];if(!p->slots)continue;
#define PRINT(name) number(out,tel,line,#name,atomic_load_explicit(&host.m7_snapshot[line].name,memory_order_relaxed));
        M7_STORAGE_FIELDS(PRINT)
#undef PRINT
        number(out,tel,line,"normal_reserve",p->normal_mbufs);
        number(out,tel,line,"slot_reserve",p->slots);
        number(out,tel,line,"record_reserve",p->records);
        number(out,tel,line,"cell_reserve",p->cells);
        number(out,tel,line,"slot_limit",p->slot_limit);
        number(out,tel,line,"record_limit",p->record_limit);
        number(out,tel,line,"cell_limit",p->cell_limit);
        number(out,tel,line,"normal_limit",p->normal_limit);
        number(out,tel,line,"hold_ns",p->hold_ns);
        number(out,tel,line,"perf_population_bound",p->perf_population_bound);
    }
}
