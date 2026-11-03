#define main recorder_main
#include "../../scripts/mac_live_recording/process_tap.m"
#undef main

#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>
#include <sys/resource.h>

static bool region_at(const void *pointer, mach_vm_address_t *address, vm_region_submap_info_data_64_t *info) {
    *address = (mach_vm_address_t)pointer;
    mach_vm_size_t size = 0;
    natural_t depth = 0;
    mach_msg_type_number_t count = VM_REGION_SUBMAP_INFO_COUNT_64;
    return mach_vm_region_recurse(mach_task_self(), address, &size, &depth,
        (vm_region_recurse_info_t)info, &count) == KERN_SUCCESS;
}

static bool wired(const void *pointer) {
    mach_vm_address_t address;
    vm_region_submap_info_data_64_t info = {0};
    return region_at(pointer, &address, &info) && address <= (mach_vm_address_t)pointer && info.user_wired_count > 0;
}

static bool unmapped(const void *pointer) {
    mach_vm_address_t address;
    vm_region_submap_info_data_64_t info = {0};
    return !region_at(pointer, &address, &info) || address > (mach_vm_address_t)pointer;
}

static int denied_errno(void) {
    size_t page = (size_t)getpagesize();
    void *memory = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    require(memory != MAP_FAILED, @"Test mmap failed");
    int result = mlock(memory, page);
    int saved = errno;
    if (!result) munlock(memory, page);
    munmap(memory, page);
    require(result == -1, @"RLIMIT_MEMLOCK did not deny mlock");
    return saved;
}

static void deny_locking(void) {
    struct rlimit limit;
    require(getrlimit(RLIMIT_MEMLOCK, &limit) == 0, @"getrlimit failed");
    limit.rlim_cur = 0;
    require(setrlimit(RLIMIT_MEMLOCK, &limit) == 0, @"setrlimit failed");
}

static void expect_failure(bool arena) {
    Queue *q = arena ? resident_alloc(sizeof(*q)) : NULL;
    deny_locking();
    int expected = denied_errno();
    size_t bytes = arena ? queue_bytes(512) : sizeof(Queue);
    bool failed = false;
    @try {
        if (arena) prepare_queue(q, 512);
        else (void)resident_alloc(bytes);
    } @catch (NSException *error) {
        NSString *reason = error.reason;
        require([error.name isEqualToString:@"CaptureError"] &&
            [reason containsString:@"mlock"] &&
            [reason containsString:[NSString stringWithFormat:@"%zu", bytes]] &&
            [reason containsString:@(strerror(expected))] &&
            [reason containsString:[NSString stringWithFormat:@"errno %d", expected]], @"Missing mlock failure details");
        failed = true;
    }
    require(failed, @"Allocation succeeded despite denied mlock");
    if (arena) {
        require(q->blocks == NULL && q->pcm == NULL && wired(q), @"Failed arena left usable storage");
        const void *context_address = q;
        require(destroy_queue(q) == 0 && unmapped(context_address), @"Cannot release context after failed arena");
    }
}

static void check_residency(void) {
    const unsigned frames = 512;
    Queue *q = resident_alloc(sizeof(*q));
    require((uintptr_t)q % (size_t)getpagesize() == 0 && wired(q), @"Queue context is not page-aligned and wired");
    const unsigned char *context = (const unsigned char *)q;
    for (size_t i = 0; i < sizeof(*q); ++i) require(context[i] == 0, @"Context is not zeroed");
    require(prepare_queue(q, frames), @"Queue allocation failed");
    require(q->capacity == frames && q->pcm == (float *)(q->blocks + frames) &&
        (uintptr_t)q->blocks % (size_t)getpagesize() == 0, @"Unexpected queue layout");
    size_t bytes = queue_bytes(frames);
    const unsigned char *arena = (const unsigned char *)q->blocks;
    for (size_t i = 0; i < bytes; ++i) require(arena[i] == 0, @"Queue arena is not zeroed");
    for (size_t i = 0; i < bytes; i += (size_t)getpagesize()) require(wired(arena + i), @"Queue page is not wired");
    require(wired(q->blocks) && wired(q->pcm) && wired(arena + bytes - 1), @"Queue storage is not wired");
    const void *context_address = q;
    const void *arena_address = q->blocks;
    require(destroy_queue(q) == 0 && unmapped(context_address) && unmapped(arena_address), @"Queue mappings remain after release");
    require(destroy_queue(NULL) == 0, @"NULL release failed");
}

int main(int argc, const char **argv) {
    @autoreleasepool { @try {
        if (argc == 2 && !strcmp(argv[1], "--memory-resident")) check_residency();
        else if (argc == 2 && !strcmp(argv[1], "--memory-deny-context")) expect_failure(false);
        else if (argc == 2 && !strcmp(argv[1], "--memory-deny-arena")) expect_failure(true);
        else return recorder_main(argc, argv);
        return 0;
    } @catch (NSException *error) {
        fprintf(stderr, "%s\n", error.reason.UTF8String);
        return 1;
    } }
}
