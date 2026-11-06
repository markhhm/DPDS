/* SPDX-License-Identifier: Apache-2.0 */
#ifndef M7_TEST_ORACLE_H
#define M7_TEST_ORACLE_H
/* Optional deterministic ledger for comparison against the merged M7 engine.
 * Never serialize struct padding, addresses or wall-clock diagnostic anchors. */
#include <stdlib.h>
#include <inttypes.h>
static FILE *m7_oracle;
static inline void m7_oracle_open(const char *fixture)
{
    const char *path=getenv("HNG_M7_ORACLE");
    if(path){m7_oracle=fopen(path,"a");assert(m7_oracle);fprintf(m7_oracle,"fixture=%s\n",fixture);}
}
static inline void m7_oracle_close(void){if(m7_oracle){assert(fclose(m7_oracle)==0);m7_oracle=NULL;}}
static inline void m7_oracle_worker(const struct worker_stats *s)
{
    if(!m7_oracle)return;
    fprintf(m7_oracle,"worker,");
#define VALUE(name) fprintf(m7_oracle,#name "=%"PRIu64",",s->name);
    VALUE(received) VALUE(received_bytes) VALUE(sent) VALUE(sent_bytes) VALUE(emulator_drops)
    VALUE(tx_ring_drops) VALUE(invalid_drops) VALUE(invalid_drop_bytes) VALUE(storage_drops)
    VALUE(storage_drop_bytes) VALUE(shutdown_drops) VALUE(shutdown_drop_bytes) VALUE(tx_ring_drop_bytes)
#undef VALUE
    fprintf(m7_oracle,"\n");
}
static inline void m7_oracle_storage(unsigned line)
{
    if(!m7_oracle)return;
    struct m7_storage_stats s=m7_storage_stats(host.m7[line]);
    fprintf(m7_oracle,"storage,line=%u,",line);
#define VALUE(name) fprintf(m7_oracle,#name "=%"PRIu64",",s.name);
    M7_STORAGE_FIELDS(VALUE)
#undef VALUE
    fprintf(m7_oracle,"\n");
}
#endif
