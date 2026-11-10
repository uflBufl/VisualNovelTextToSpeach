#include "shared_memory.h"
#include <sys/mman.h>

int vntts_shm_open(const char *name, int flags, mode_t mode) {
    return shm_open(name, flags, mode);
}
