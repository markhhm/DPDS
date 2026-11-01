// SPDX-License-Identifier: Apache-2.0
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <openssl/evp.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CHUNK (1024u * 1024u)
#define PAGE_BITS 12u
#define PAGE_SIZE (1u << PAGE_BITS)
#define WORDS (PAGE_SIZE / 64u)
enum { INGRESS, RECEIVER, REORDER, MISSING, DUP_IN, DUP_OUT, UNEXPECTED, FLAG, COUNTERS };
static const char *names[] = {"ingress", "receiver", "ordinary_reorder", "missing",
    "duplicate_ingress", "duplicate_receiver", "unexpected", "flag_mismatch"};
typedef struct { uint32_t src, dst; uint16_t sport, dport, vlan; } Key;
typedef struct Page {
    uint64_t number, rank[PAGE_SIZE], present[WORDS], late[WORDS], received[WORDS];
} Page;
typedef struct Flow {
    Key key;
    uint64_t count[COUNTERS], high;
    size_t capacity, used;
    Page **pages, *last;
} Flow;
typedef struct { Flow *flow; uint64_t seq, rank, receiver; } Detail;
typedef struct {
    Flow **table, **flows, *last;
    size_t capacity, used, limit, detail_count[COUNTERS];
    Detail *details[COUNTERS];
    uint64_t count[COUNTERS];
    const char *error;
} Check;
typedef struct {
    uint64_t packets, first, last, stats_at, start, end, delivered, epb_drops, drops;
    uint32_t snaplen;
    bool stats, seen, drop_known;
} Interface;
typedef struct {
    const char *path, *error;
    int fd;
    struct stat initial;
    uint8_t *buffer;
    size_t position, length;
    uint64_t bytes, packets, drops;
    pthread_t hash_thread;
    const char *hash_error;
    uint64_t hash_bytes;
    bool hash_started;
    char sha256[65];
    bool eof, complete, drops_known;
    Interface *interfaces;
    size_t interface_count;
} Capture;

static uint16_t be16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3]; }
static uint64_t be64(const uint8_t *p) { return (uint64_t)be32(p)<<32 | be32(p+4); }
static uint32_t u32(const uint8_t *p, bool big) {
    return big ? be32(p) : (uint32_t)p[3]<<24 | (uint32_t)p[2]<<16 | (uint32_t)p[1]<<8 | p[0];
}
static uint16_t u16(const uint8_t *p, bool big) { return big ? be16(p) : (uint16_t)(p[0] | p[1]<<8); }
static uint64_t u64(const uint8_t *p, bool big) {
    return big ? be64(p) : (uint64_t)u32(p+4, false)<<32 | u32(p, false);
}
static uint64_t mix(uint64_t n) {
    n ^= n >> 30; n *= UINT64_C(0xbf58476d1ce4e5b9);
    n ^= n >> 27; n *= UINT64_C(0x94d049bb133111eb); return n ^ (n >> 31);
}
static uint64_t key_hash(Key k) {
    return mix((uint64_t)k.src<<32 | k.dst) ^ mix((uint64_t)k.sport<<32 | (uint64_t)k.dport<<16 | k.vlan);
}
static bool equal(Key a, Key b) {
    return a.src==b.src && a.dst==b.dst && a.sport==b.sport && a.dport==b.dport && a.vlan==b.vlan;
}
static void *allocate(Check *c, size_t n, size_t size) {
    if (size && n > SIZE_MAX / size) { c->error="allocation_overflow"; return NULL; }
    void *p=calloc(n, size);
    if (!p) c->error="memory_limit";
    return p;
}
static Flow *flow(Check *c, Key key) {
    if (c->last && equal(c->last->key, key)) return c->last;
    if (!c->capacity || c->used >= c->capacity/2) {
        size_t cap=c->capacity ? c->capacity*2 : 64;
        if (cap < c->capacity) { c->error="allocation_overflow"; return NULL; }
        Flow **table=allocate(c, cap, sizeof(*table));
        if (!table) return NULL;
        for (size_t i=0;i<c->used;i++) {
            Flow *f=c->flows[i]; size_t at=key_hash(f->key)&(cap-1);
            while (table[at]) at=(at+1)&(cap-1);
            table[at]=f;
        }
        Flow **list=realloc(c->flows, cap/2*sizeof(*list));
        if (!list) { free(table); c->error="memory_limit"; return NULL; }
        free(c->table); c->table=table; c->flows=list; c->capacity=cap;
    }
    size_t at=key_hash(key)&(c->capacity-1);
    while (c->table[at] && !equal(c->table[at]->key,key)) at=(at+1)&(c->capacity-1);
    if (!c->table[at]) {
        Flow *f=allocate(c,1,sizeof(*f)); if (!f) return NULL;
        f->key=key; c->table[at]=f; c->flows[c->used++]=f;
    }
    return c->last=c->table[at];
}
static Page *page(Check *c, Flow *f, uint64_t number) {
    if (f->last && f->last->number==number) return f->last;
    if (!f->capacity || f->used>=f->capacity/2) {
        size_t cap=f->capacity ? f->capacity*2 : 16;
        if (cap<f->capacity) { c->error="allocation_overflow"; return NULL; }
        Page **table=allocate(c,cap,sizeof(*table)); if (!table) return NULL;
        for (size_t i=0;i<f->capacity;i++) if (f->pages[i]) {
            Page *p=f->pages[i]; size_t at=mix(p->number)&(cap-1);
            while (table[at]) at=(at+1)&(cap-1);
            table[at]=p;
        }
        free(f->pages); f->pages=table; f->capacity=cap;
    }
    size_t at=mix(number)&(f->capacity-1);
    while (f->pages[at] && f->pages[at]->number!=number) at=(at+1)&(f->capacity-1);
    if (!f->pages[at]) {
        Page *p=allocate(c,1,sizeof(*p)); if (!p) return NULL;
        p->number=number; f->pages[at]=p; f->used++;
    }
    return f->last=f->pages[at];
}
static void count(Check *c, Flow *f, unsigned type) { f->count[type]++; c->count[type]++; }
static void detail(Check *c, Flow *f, unsigned type, uint64_t seq, uint64_t rank, uint64_t receiver) {
    count(c,f,type);
    if (c->detail_count[type]>=c->limit) return;
    if (!c->details[type]) {
        c->details[type]=allocate(c,c->limit,sizeof(Detail)); if (!c->details[type]) return;
    }
    c->details[type][c->detail_count[type]++]=(Detail){f,seq,rank,receiver};
}
static bool packet(Check *c, Capture *cap, bool ingress, const uint8_t *b, uint32_t length, uint32_t wire) {
    if (length<38 || be16(b+12)!=0x8100 || be16(b+16)!=0x0800) { cap->error="unsupported_identity"; return false; }
    unsigned ihl=(b[18]&15)*4u, udp=18+ihl;
    if (b[18]>>4!=4 || ihl<20 || length<udp+8+13 || b[27]!=17 || (be16(b+24)&0x3fff)) {
        cap->error="truncated_or_unsupported_probe"; return false;
    }
    unsigned ip_length=be16(b+20), udp_length=be16(b+udp+4);
    if (udp_length<21 || ip_length!=ihl+udp_length || 18+ip_length>wire || be32(b+udp+16)!=0x4c30504b || b[udp+20]>1) {
        cap->error="invalid_probe"; return false;
    }
    Key key={be32(b+30),be32(b+34),be16(b+udp),be16(b+udp+2),(uint16_t)(be16(b+14)&4095)};
    uint64_t seq=be64(b+udp+8); bool late=b[udp+20]!=0;
    Flow *f=flow(c,key); if (!f) return false;
    Page *p=page(c,f,seq>>PAGE_BITS); if (!p) return false;
    unsigned slot=seq&(PAGE_SIZE-1), word=slot/64; uint64_t bit=UINT64_C(1)<<(slot%64);
    count(c,f,ingress?INGRESS:RECEIVER);
    if (ingress) {
        if (p->present[word]&bit) { detail(c,f,DUP_IN,seq,p->rank[slot]-1,UINT64_MAX); return !c->error; }
        p->present[word]|=bit; if (late) p->late[word]|=bit;
        /* Capture ordinal preserves per-flow arrival order without assuming numeric sequence order. */
        p->rank[slot]=cap->packets;
    } else {
        bool known=(p->present[word]&bit)!=0, duplicate=(p->received[word]&bit)!=0;
        uint64_t rank=known?p->rank[slot]-1:UINT64_MAX;
        if (duplicate) detail(c,f,DUP_OUT,seq,rank,cap->packets-1);
        p->received[word]|=bit;
        if (!known) detail(c,f,UNEXPECTED,seq,UINT64_MAX,cap->packets-1);
        else {
            bool selected=(p->late[word]&bit)!=0;
            if (late!=selected) detail(c,f,FLAG,seq,rank,cap->packets-1);
            if (!duplicate && !selected) {
                if (p->rank[slot]<f->high) detail(c,f,REORDER,seq,rank,cap->packets-1);
                else f->high=p->rank[slot];
            }
        }
    }
    return !c->error;
}

static bool fill(Capture *c, size_t need) {
    while (c->length-c->position<need && !c->eof) {
        size_t left=c->length-c->position;
        memmove(c->buffer,c->buffer+c->position,left); c->position=0; c->length=left;
        ssize_t got=read(c->fd,c->buffer+left,CHUNK);
        if (got<0) { if (errno==EINTR) continue; c->error="read_error"; return false; }
        if (!got) { c->eof=true; break; }
        c->length+=(size_t)got; c->bytes+=(uint64_t)got;
    }
    return c->length-c->position>=need;
}
/* Known option pointers are valid only until the reader advances. */
static bool options(Capture *c, const uint8_t *b, size_t length, bool big, const uint8_t **values, size_t n) {
    memset(values,0,n*sizeof(*values));
    while (length) {
        if (length<4) { c->error="truncated_options"; return false; }
        unsigned code=u16(b,big), size=u16(b+2,big); size_t padded=(size+3u)&~3u;
        if (!code) {
            if (size) { c->error="invalid_options_end"; return false; }
            for (size_t i=4;i<length;i++) if (b[i]) { c->error="invalid_options_end"; return false; }
            return true;
        }
        if (padded>length-4) { c->error="truncated_options"; return false; }
        if (code<n) {
            if (values[code]) { c->error="duplicate_option"; return false; }
            values[code]=b;
        }
        b+=4+padded; length-=4+padded;
    }
    return true;
}
static bool sized(Capture *c, const uint8_t *v, unsigned size, bool big) {
    if (v && u16(v+2,big)!=size) { c->error="invalid_option_size"; return false; }
    return true;
}
static uint64_t timestamp(const uint8_t *p, bool big) { return (uint64_t)u32(p,big)<<32 | u32(p+4,big); }
static bool parse(Check *check, Capture *c, bool ingress) {
    bool section=false, big=false; uint64_t section_end=UINT64_MAX, offset=0;
    for (;;) {
        if (!fill(c,12)) {
            if (c->error) return false;
            if (c->length!=c->position || !section || (section_end!=UINT64_MAX && offset!=section_end)) { c->error="truncated_capture"; return false; }
            break;
        }
        const uint8_t *p=c->buffer+c->position;
        bool shb=be32(p)==0x0a0d0d0a;
        if (shb) {
            if (section) { c->error="multiple_sections"; return false; }
            if (be32(p+8)==0x1a2b3c4d) big=true;
            else if (be32(p+8)==0x4d3c2b1a) big=false;
            else { c->error="invalid_byte_order"; return false; }
        } else if (!section) { c->error="missing_section"; return false; }
        uint32_t kind=u32(p,big), size=u32(p+4,big);
        if (size<12 || size%4 || size>CHUNK) { c->error="invalid_block_size"; return false; }
        if (!fill(c,size)) { if (!c->error) c->error="truncated_capture"; return false; }
        p=c->buffer+c->position;
        if (u32(p+size-4,big)!=size) { c->error="block_trailer_mismatch"; return false; }
        const uint8_t *b=p+8, *v[15]; size_t len=size-12;
        offset+=size;
        if (kind==0x0a0d0d0a) {
            if (len<16 || u16(b+4,big)!=1 || u16(b+6,big)!=0) { c->error="unsupported_section"; return false; }
            uint64_t declared=u64(b+8,big);
            if (declared!=UINT64_MAX) {
                if (declared>INT64_MAX || declared>UINT64_MAX-offset) { c->error="invalid_section_length"; return false; }
                section_end=offset+declared;
            }
            if (!options(c,b+16,len-16,big,v,15)) return false;
            section=true;
        } else if (kind==1) {
            if (len<8 || u16(b,big)!=1 || u16(b+2,big)!=0 || c->interface_count>=65536) { c->error="unsupported_interface"; return false; }
            if (!options(c,b+8,len-8,big,v,15) || !sized(c,v[9],1,big) || !sized(c,v[13],1,big) || !sized(c,v[14],8,big)) return false;
            if (!v[9] || v[9][4]!=9 || (v[13] && v[13][4]!=0)) { c->error="unsupported_clock_or_fcs"; return false; }
            Interface *all=realloc(c->interfaces,(c->interface_count+1)*sizeof(*all));
            if (!all) { c->error="memory_limit"; return false; }
            c->interfaces=all; all[c->interface_count++]=(Interface){.snaplen=u32(b+4,big),.drop_known=true};
        } else if (kind==6 || kind==5) {
            size_t minimum=kind==6?20:12;
            if (len<minimum || u32(b,big)>=c->interface_count) { c->error="invalid_packet_interface"; return false; }
            Interface *i=&c->interfaces[u32(b,big)]; uint64_t at=timestamp(b+4,big);
            if (kind==6) {
                uint32_t captured=u32(b+12,big), wire=u32(b+16,big);
                size_t padded=((size_t)captured+3)&~(size_t)3;
                if (captured>wire || padded>len-20 || (i->snaplen && captured>i->snaplen)) { c->error="invalid_caplen"; return false; }
                if (!options(c,b+20+padded,len-20-padded,big,v,15) || !sized(c,v[2],4,big) || !sized(c,v[4],8,big)) return false;
                if (v[2] && ((u32(v[2]+4,big)>>5)&15)) { c->error="retained_fcs"; return false; }
                uint64_t drops=v[4]?u64(v[4]+4,big):0;
                if (drops==UINT64_MAX || drops>UINT64_MAX-i->epb_drops) { c->drops_known=false; c->error="unknown_capture_drops"; return false; }
                i->epb_drops+=drops;
                if (i->seen && at<i->last) { c->error="clock_backward"; return false; }
                if (!i->seen) i->first=at;
                i->seen=true; i->last=at; i->packets++; c->packets++;
                if (!packet(check,c,ingress,b+20,captured,wire)) return false;
            } else {
                if (!options(c,b+12,len-12,big,v,15)) return false;
                const unsigned required[]={2,3,5,7,8};
                for (unsigned j=0;j<5;j++) if (!v[required[j]] || !sized(c,v[required[j]],8,big)) { c->error="unknown_capture_health"; return false; }
                uint64_t start=timestamp(v[2]+4,big), end=timestamp(v[3]+4,big), dropped=u64(v[5]+4,big), os=u64(v[7]+4,big), delivered=u64(v[8]+4,big);
                if (dropped==UINT64_MAX || os==UINT64_MAX || delivered==UINT64_MAX || os>UINT64_MAX-dropped) { c->drops_known=false; c->error="unknown_capture_health"; return false; }
                if (start>end || end>at || delivered!=i->packets || (i->seen && (start>i->first || end<i->last)) ||
                    (i->stats && (start!=i->start || end<i->end || delivered<i->delivered || dropped+os<i->drops || at<i->stats_at))) { c->error="invalid_capture_statistics"; return false; }
                i->start=start; i->end=end; i->delivered=delivered; i->stats_at=at; i->drops=dropped+os; i->stats=true;
            }
        } else if (kind==2 || kind==3) { c->error="unsupported_packet_block"; return false; }
        if (offset>section_end) { c->error="section_length_exceeded"; return false; }
        c->position+=size;
    }
    if (!c->interface_count) { c->error="missing_interface"; return false; }
    for (size_t j=0;j<c->interface_count;j++) {
        Interface *i=&c->interfaces[j];
        if (!i->stats || i->delivered!=i->packets) { c->drops_known=false; c->error="missing_final_statistics"; return false; }
        uint64_t drops=i->drops>i->epb_drops?i->drops:i->epb_drops;
        if (drops>UINT64_MAX-c->drops) { c->drops_known=false; c->error="unknown_capture_drops"; return false; }
        c->drops+=drops;
    }
    if (c->drops) { c->error="capture_drop"; return false; }
    return true;
}
/* Hash immutable inputs concurrently with comparison; one worker per file. */
static void *hash_capture(void *argument) {
    Capture *c=argument;
    uint8_t *buffer=malloc(CHUNK); EVP_MD_CTX *digest=EVP_MD_CTX_new();
    if (!buffer || !digest) c->hash_error="memory_limit";
    else if (!EVP_DigestInit_ex(digest,EVP_sha256(),NULL)) c->hash_error="digest_error";
    else {
        for (;;) {
            ssize_t n=pread(c->fd,buffer,CHUNK,(off_t)c->hash_bytes);
            if (n<0) { if (errno==EINTR) continue; c->hash_error="read_error"; break; }
            if (!n) break;
            if (!EVP_DigestUpdate(digest,buffer,(size_t)n)) { c->hash_error="digest_error"; break; }
            c->hash_bytes+=(uint64_t)n;
        }
        unsigned char bytes[EVP_MAX_MD_SIZE]; unsigned size=0;
        if (!c->hash_error && EVP_DigestFinal_ex(digest,bytes,&size) && size==32) {
            for (unsigned i=0;i<size;i++) snprintf(c->sha256+2*i,3,"%02x",bytes[i]);
        } else if (!c->hash_error) c->hash_error="digest_error";
    }
    EVP_MD_CTX_free(digest); free(buffer); return NULL;
}
static void start_capture(Capture *c) {
    c->fd=open(c->path,O_RDONLY); c->drops_known=true;
    if (c->fd<0) { c->error="open_error"; return; }
    if (fstat(c->fd,&c->initial) || !S_ISREG(c->initial.st_mode)) {
        c->error="not_regular_file"; close(c->fd); c->fd=-1; return;
    }
    if (pthread_create(&c->hash_thread,NULL,hash_capture,c)) {
        c->error="hash_worker_unavailable"; close(c->fd); c->fd=-1; return;
    }
    c->hash_started=true;
}
static void read_capture(Check *check, Capture *c, bool ingress) {
    if (!c->hash_started) return;
    c->buffer=malloc(CHUNK*2);
    if (!c->buffer) c->error="memory_limit";
    else if (!check->error) c->complete=parse(check,c,ingress);
    free(c->buffer); c->buffer=NULL;
}
static void finish_capture(Capture *c) {
    if (!c->hash_started) return;
    if (pthread_join(c->hash_thread,NULL)) { c->error="hash_worker_failed"; c->complete=false; }
    if (c->hash_error) { c->error=c->hash_error; c->complete=false; }
    struct stat after;
    if (fstat(c->fd,&after) || c->hash_bytes!=(uint64_t)c->initial.st_size ||
        (c->complete && c->bytes!=c->hash_bytes) || after.st_size!=c->initial.st_size ||
        after.st_mtim.tv_sec!=c->initial.st_mtim.tv_sec || after.st_mtim.tv_nsec!=c->initial.st_mtim.tv_nsec ||
        after.st_ctim.tv_sec!=c->initial.st_ctim.tv_sec || after.st_ctim.tv_nsec!=c->initial.st_ctim.tv_nsec) {
        c->complete=false; c->error="input_changed";
    }
    c->bytes=c->hash_bytes; close(c->fd); c->fd=-1;
}
static int rank_order(const void *a, const void *b) {
    uint64_t x=((const Detail *)a)->rank, y=((const Detail *)b)->rank; return (x>y)-(x<y);
}
static void missing(Check *c) {
    /* Retain the first N missing capture ordinals using a bounded max heap. */
    for (size_t f=0;f<c->used;f++) for (size_t j=0;j<c->flows[f]->capacity;j++) {
        Flow *flow=c->flows[f]; Page *p=flow->pages[j]; if (!p) continue;
        for (unsigned w=0;w<WORDS;w++) {
            uint64_t bits=p->present[w]&~p->received[w];
            while (bits) {
                unsigned bit=(unsigned)__builtin_ctzll(bits), slot=w*64+bit; bits&=bits-1;
                uint64_t seq=p->number<<PAGE_BITS | slot, rank=p->rank[slot]-1;
                count(c,flow,MISSING); if (!c->limit) continue;
                if (!c->details[MISSING]) { c->details[MISSING]=allocate(c,c->limit,sizeof(Detail)); if (!c->details[MISSING]) return; }
                Detail item={flow,seq,rank,UINT64_MAX}; size_t n=c->detail_count[MISSING]; Detail *heap=c->details[MISSING];
                if (n<c->limit) {
                    size_t at=n; c->detail_count[MISSING]++;
                    while (at && heap[(at-1)/2].rank<rank) { heap[at]=heap[(at-1)/2]; at=(at-1)/2; }
                    heap[at]=item;
                } else if (rank<heap[0].rank) {
                    size_t at=0;
                    while (at*2+1<n) {
                        size_t child=at*2+1;
                        if (child+1<n && heap[child+1].rank>heap[child].rank) child++;
                        if (heap[child].rank<=rank) break;
                        heap[at]=heap[child]; at=child;
                    }
                    heap[at]=item;
                }
            }
        }
    }
    if (c->detail_count[MISSING]) qsort(c->details[MISSING],c->detail_count[MISSING],sizeof(Detail),rank_order);
}
static void quoted(const char *s) {
    putchar('"');
    while (*s) {
        unsigned char ch=(unsigned char)*s;
        if (ch>=128) {
            unsigned n=ch>=0xc2 && ch<=0xdf?2:ch>=0xe0 && ch<=0xef?3:ch>=0xf0 && ch<=0xf4?4:0;
            for (unsigned j=1;j<n;j++) if (((unsigned char)s[j]&0xc0)!=0x80) { n=0; break; }
            if ((n==3 && ((ch==0xe0 && (unsigned char)s[1]<0xa0) || (ch==0xed && (unsigned char)s[1]>=0xa0))) ||
                (n==4 && ((ch==0xf0 && (unsigned char)s[1]<0x90) || (ch==0xf4 && (unsigned char)s[1]>=0x90)))) n=0;
            if (n) { fwrite(s,1,n,stdout); s+=n; continue; }
            /* Non-UTF-8 filename bytes remain diagnostic Unicode byte escapes. */
            printf("\\u%04x",ch);
        } else if (ch=='"' || ch=='\\') printf("\\%c",ch);
        else if (ch<32) printf("\\u%04x",ch);
        else putchar(ch);
        s++;
    }
    putchar('"');
}
static void flow_id(Flow *f) {
    printf("\"%08" PRIx32 ":%08" PRIx32 ":%u:%u\"",f->key.src,f->key.dst,f->key.sport,f->key.dport);
}
static void counts(const uint64_t *values) {
    for (unsigned i=0;i<COUNTERS;i++) printf("%s\"%s\":%" PRIu64,i?",":"",names[i],values[i]);
}
static void output(Check *c, Capture *inputs, bool complete, const char *status) {
    printf("{\"schema\":\"hng-seqcheck-v1\",\"scope\":\"supplied-captures\",\"status\":\"%s\",\"complete\":%s,\"inputs\":{",status,complete?"true":"false");
    for (unsigned i=0;i<2;i++) {
        Capture *in=&inputs[i]; printf("%s\"%s\":{\"path\":",i?",":"",i?"receiver":"ingress"); quoted(in->path);
        printf(",\"sha256\":"); if (*in->sha256) quoted(in->sha256); else printf("null");
        printf(",\"byte_size\":%" PRIu64 ",\"packets\":%" PRIu64 ",\"health\":{\"complete\":%s,\"capture_drops\":",in->bytes,in->packets,in->complete?"true":"false");
        if (in->complete || (in->drops_known && in->error && !strcmp(in->error,"capture_drop"))) printf("%" PRIu64,in->drops); else printf("null");
        printf("},\"error\":"); if (in->error) quoted(in->error); else printf("null"); putchar('}');
    }
    printf("},\"totals\":{"); counts(c->count); printf("},\"flows\":[");
    for (size_t i=0;i<c->used;i++) {
        Flow *f=c->flows[i]; printf("%s{\"flow_id\":",i?",":""); flow_id(f);
        printf(",\"vlan_id\":%u,",f->key.vlan); counts(f->count); putchar('}');
    }
    printf("],\"details_limit\":%zu,\"details\":{",c->limit);
    for (unsigned type=REORDER;type<COUNTERS;type++) {
        printf("%s\"%s\":[",type==REORDER?"":",",names[type]);
        for (size_t j=0;j<c->detail_count[type];j++) {
            Detail *d=&c->details[type][j]; printf("%s{\"flow_id\":",j?",":""); flow_id(d->flow);
            printf(",\"vlan_id\":%u,\"sequence\":%" PRIu64,d->flow->key.vlan,d->seq);
            if (d->rank!=UINT64_MAX) printf(",\"ingress_rank\":%" PRIu64,d->rank);
            if (d->receiver!=UINT64_MAX) printf(",\"receiver_index\":%" PRIu64,d->receiver);
            putchar('}');
        }
        putchar(']');
    }
    printf("},\"details_truncated\":{");
    for (unsigned type=REORDER;type<COUNTERS;type++) printf("%s\"%s\":%s",type==REORDER?"":",",names[type],c->count[type]>c->detail_count[type]?"true":"false");
    printf("},\"error\":"); if (c->error) quoted(c->error); else printf("null"); printf("}\n");
}
static void release(Check *c, Capture *inputs) {
    for (size_t f=0;f<c->used;f++) {
        Flow *flow=c->flows[f];
        for (size_t j=0;j<flow->capacity;j++) free(flow->pages[j]);
        free(flow->pages); free(flow);
    }
    for (unsigned i=0;i<COUNTERS;i++) free(c->details[i]);
    free(c->table); free(c->flows); free(inputs[0].interfaces); free(inputs[1].interfaces);
}
int main(int argc, char **argv) {
    Check check={.limit=20}; Capture inputs[2]={0};
    for (int i=1;i<argc;i++) {
        if ((!strcmp(argv[i],"--ingress") || !strcmp(argv[i],"--receiver")) && i+1<argc) {
            unsigned role=!strcmp(argv[i],"--receiver"); if (inputs[role].path) goto usage;
            inputs[role].path=argv[++i];
        } else if (!strcmp(argv[i],"--details") && i+1<argc) {
            char *end; errno=0; const char *arg=argv[++i]; unsigned long n=strtoul(arg,&end,10);
            if (!*arg || *arg=='-' || *end || errno || n>100000) goto usage;
            check.limit=(size_t)n;
        } else goto usage;
    }
    if (!inputs[0].path || !inputs[1].path) goto usage;
    start_capture(&inputs[0]); start_capture(&inputs[1]);
    read_capture(&check,&inputs[0],true);
    read_capture(&check,&inputs[1],false);
    finish_capture(&inputs[0]); finish_capture(&inputs[1]);
    if (!check.error) missing(&check);
    bool complete=inputs[0].complete && inputs[1].complete && !check.error && !check.count[DUP_IN];
    bool anomaly=false;
    for (unsigned i=REORDER;i<COUNTERS;i++) anomaly |= check.count[i]!=0;
    int result=complete?(anomaly?1:0):2;
    output(&check,inputs,complete,result==0?"PASS":result==1?"FAIL":"INVALID");
    if (fflush(stdout)==EOF || ferror(stdout)) {
        fputs("seqcheck: JSON output write failed\n",stderr); result=2;
    }
    release(&check,inputs); return result;
usage:
    fprintf(stderr,"usage: seqcheck --ingress FILE --receiver FILE [--details N (0..100000)]\n"); return 2;
}
