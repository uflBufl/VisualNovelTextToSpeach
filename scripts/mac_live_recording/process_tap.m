// Explicit-process diagnostic recorder. No screen, microphone, parent or global fallback.
#import <Foundation/Foundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/CATapDescription.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <AudioToolbox/AudioToolbox.h>
#import <libproc.h>
#import <mach/mach_time.h>
#include <stdatomic.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/mount.h>
#import <dispatch/dispatch.h>
#include <math.h>
#include <unistd.h>

#define SLOT_COUNT 64u
#define MAX_FRAMES 8192u
#define CHANNELS 2u

typedef enum { PhaseIdle, PhaseConsume, PhaseCAFWrite, PhaseJSONSerialize, PhaseJSONWrite,
    PhaseIdentity, PhaseStop, PhaseCAFFinalize, PhaseWait, PhaseCount } ConsumerPhase;
static NSString *const phaseNames[] = {@"idle", @"consume", @"caf_write", @"json_serialize", @"json_write",
    @"hal_identity", @"stop", @"caf_finalize", @"wait"};
typedef struct { uint64_t count, total, maximum; } Timing;
typedef struct {
    UInt32 frames;
    uint64_t received;
    AudioTimeStamp timestamp;
    float pcm[MAX_FRAMES * CHANNELS];
} Block;
typedef struct {
    _Atomic unsigned produced, consumed;
    _Atomic int failure;
    _Atomic uint64_t highBlocks, highFrames;
    _Atomic uint64_t phaseVersion, phaseStarted, phase;
    uint64_t overflowTicks, overflowBlocks, overflowFrames, overflowPhase, overflowPhaseStarted;
    bool overflowPhaseConsistent;
    Timing timings[PhaseCount], lag;
    uint64_t lagReceived, lagStarted, lagBlocks, lagFrames;
    bool planar;
    Block blocks[SLOT_COUNT];
} Queue;
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "Stop flag must remain signal-safe");
static _Atomic int interrupted;
static void stop_signal(int value) { (void)value; atomic_store_explicit(&interrupted, 1, memory_order_relaxed); alarm(10); }
static void watchdog(int value) { (void)value; _exit(2); }

static bool queue_lock_free(Queue *q) {
    return q && atomic_is_lock_free(&q->produced) && atomic_is_lock_free(&q->consumed) &&
        atomic_is_lock_free(&q->failure) && atomic_is_lock_free(&q->highBlocks) &&
        atomic_is_lock_free(&q->highFrames) && atomic_is_lock_free(&q->phaseVersion) &&
        atomic_is_lock_free(&q->phaseStarted) && atomic_is_lock_free(&q->phase);
}
static uint64_t queued_frames(Queue *q, unsigned r, unsigned w) {
    uint64_t frames = 0;
    for (; r != w; ++r) frames += q->blocks[r % SLOT_COUNT].frames;
    return frames;
}
static void phase_begin(Queue *q, ConsumerPhase phase, uint64_t tick) {
    // A bounded callback snapshot can detect a transition without spinning on the consumer.
    atomic_fetch_add(&q->phaseVersion, 1);
    atomic_store(&q->phaseStarted, tick);
    atomic_store(&q->phase, phase);
    atomic_fetch_add(&q->phaseVersion, 1);
}
static void timing_add(Timing *timing, uint64_t elapsed) {
    ++timing->count; timing->total += elapsed;
    if (elapsed > timing->maximum) timing->maximum = elapsed;
}
static void phase_end(Queue *q, ConsumerPhase phase, uint64_t begin, uint64_t end) {
    timing_add(&q->timings[phase], end - begin);
    phase_begin(q, PhaseIdle, end);
}
static void observe_overflow(Queue *q, unsigned r, unsigned w, uint64_t tick) {
    q->overflowTicks = tick; q->overflowBlocks = w - r; q->overflowFrames = queued_frames(q, r, w);
    uint64_t before = atomic_load(&q->phaseVersion);
    q->overflowPhase = atomic_load(&q->phase);
    q->overflowPhaseStarted = atomic_load(&q->phaseStarted);
    uint64_t after = atomic_load(&q->phaseVersion);
    q->overflowPhaseConsistent = !(before & 1) && before == after && q->overflowPhaseStarted <= tick;
    // Publish the immutable overflow context only after all its fields are ready.
    atomic_store_explicit(&q->failure, 1, memory_order_release);
}
static void observe_lag(Queue *q, Block *b, unsigned r, unsigned w, uint64_t tick) {
    uint64_t lag = tick - b->received;
    if (!q->lag.count || lag > q->lag.maximum) {
        q->lagReceived = b->received; q->lagStarted = tick;
        q->lagBlocks = w - r; q->lagFrames = queued_frames(q, r, w);
    }
    timing_add(&q->lag, lag);
}

// The IO thread only validates shape and copies into bounded preallocated storage.
static OSStatus receive(AudioObjectID device, const AudioTimeStamp *now,
    const AudioBufferList *input, const AudioTimeStamp *time,
    AudioBufferList *output, const AudioTimeStamp *outputTime, void *context) {
    (void)device; (void)now; (void)output; (void)outputTime;
    uint64_t received = mach_absolute_time();
    Queue *q = context;
    if (atomic_load_explicit(&q->failure, memory_order_relaxed)) return noErr;
    unsigned w = atomic_load_explicit(&q->produced, memory_order_relaxed);
    unsigned r = atomic_load_explicit(&q->consumed, memory_order_acquire);
    if (w - r == SLOT_COUNT) { observe_overflow(q, r, w, mach_absolute_time()); return noErr; }
    unsigned expected = q->planar ? CHANNELS : 1;
    unsigned bytesPerFrame = sizeof(float) * (q->planar ? 1 : CHANNELS);
    if (!input || input->mNumberBuffers != expected || !time ||
        !(time->mFlags & kAudioTimeStampHostTimeValid) ||
        !(time->mFlags & kAudioTimeStampSampleTimeValid) || !isfinite(time->mSampleTime)) {
        atomic_store(&q->failure, 2); return noErr;
    }
    UInt32 bytes = input->mBuffers[0].mDataByteSize;
    if (!bytes) return noErr;
    if (bytes % bytesPerFrame || bytes / bytesPerFrame > MAX_FRAMES) {
        atomic_store(&q->failure, 2); return noErr;
    }
    for (unsigned c = 0; c < expected; ++c) {
        if (!input->mBuffers[c].mData || input->mBuffers[c].mDataByteSize != bytes ||
            input->mBuffers[c].mNumberChannels != (q->planar ? 1 : CHANNELS)) {
            atomic_store(&q->failure, 2); return noErr;
        }
    }
    Block *b = &q->blocks[w % SLOT_COUNT];
    b->frames = bytes / bytesPerFrame; b->timestamp = *time; b->received = received;
    if (q->planar) {
        for (UInt32 i = 0; i < b->frames; ++i)
            for (unsigned c = 0; c < CHANNELS; ++c)
                b->pcm[i * CHANNELS + c] = ((float *)input->mBuffers[c].mData)[i];
    } else memcpy(b->pcm, input->mBuffers[0].mData, bytes);
    atomic_store_explicit(&q->produced, w + 1, memory_order_release);
    r = atomic_load_explicit(&q->consumed, memory_order_acquire);
    uint64_t blocks = w + 1 - r, frames = queued_frames(q, r, w + 1);
    if (blocks > atomic_load_explicit(&q->highBlocks, memory_order_relaxed))
        atomic_store_explicit(&q->highBlocks, blocks, memory_order_relaxed);
    if (frames > atomic_load_explicit(&q->highFrames, memory_order_relaxed))
        atomic_store_explicit(&q->highFrames, frames, memory_order_relaxed);
    return noErr;
}

static void check(OSStatus status, NSString *operation) {
    if (status != noErr) @throw [NSException exceptionWithName:@"CaptureError"
        reason:[NSString stringWithFormat:@"%@: OSStatus %d", operation, (int)status] userInfo:nil];
}
static void require(bool condition, NSString *reason) {
    if (!condition) @throw [NSException exceptionWithName:@"CaptureError" reason:reason userInfo:nil];
}
static void require_queue_healthy(Queue *q, uint64_t tick) {
    int failure = atomic_load_explicit(&q->failure, memory_order_acquire);
    if (!failure) return;
    unsigned r = atomic_load_explicit(&q->consumed, memory_order_relaxed);
    unsigned w = atomic_load_explicit(&q->produced, memory_order_acquire);
    if (r != w) observe_lag(q, &q->blocks[r % SLOT_COUNT], r, w, tick ? tick : mach_absolute_time());
    require(false, failure == 1 ? @"Capture queue overflow" : @"Invalid callback shape/timestamp");
}
static AudioObjectPropertyAddress address(AudioObjectPropertySelector selector) {
    return (AudioObjectPropertyAddress){selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
}
static void property(AudioObjectID object, AudioObjectPropertySelector selector, UInt32 size, void *data) {
    AudioObjectPropertyAddress a = address(selector);
    UInt32 actual = size;
    check(AudioObjectGetPropertyData(object, &a, 0, NULL, &actual, data), @"Read HAL property");
    require(actual == size, @"Unexpected HAL property size");
}
static NSDictionary *identity(pid_t pid, NSString *expectedPath) {
    struct proc_bsdinfo info = {0};
    char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
    require(proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) == sizeof(info), @"Target process unavailable");
    require(proc_pidpath(pid, path, sizeof(path)) > 0, @"Target executable unavailable");
    NSString *actualPath = @(path);
    require([actualPath isEqualToString:expectedPath], @"Target executable does not match explicit path");
    AudioObjectID object = kAudioObjectUnknown;
    UInt32 size = sizeof(object);
    AudioObjectPropertyAddress a = address(kAudioHardwarePropertyTranslatePIDToProcessObject);
    check(AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, sizeof(pid), &pid, &size, &object), @"Translate selected PID");
    require(size == sizeof(object) && object != kAudioObjectUnknown, @"Selected PID has no HAL process object");
    pid_t roundtrip = 0;
    property(object, kAudioProcessPropertyPID, sizeof(roundtrip), &roundtrip);
    require(roundtrip == pid, @"HAL PID roundtrip mismatch");
    return @{@"pid": @(pid), @"hal_process_object": @(object), @"executable": actualPath,
        @"start_seconds": @(info.pbi_start_tvsec), @"start_microseconds": @(info.pbi_start_tvusec)};
}
static NSDictionary *measured_identity(Queue *q, pid_t pid, NSString *expectedPath) {
    uint64_t begin = mach_absolute_time(); phase_begin(q, PhaseIdentity, begin);
    @try { return identity(pid, expectedPath); }
    @finally { phase_end(q, PhaseIdentity, begin, mach_absolute_time()); }
}
static void write_caf(Queue *q, ExtAudioFileRef audio, UInt32 frames, AudioBufferList *list) {
    uint64_t begin = mach_absolute_time(); phase_begin(q, PhaseCAFWrite, begin);
    @try { check(ExtAudioFileWrite(audio, frames, list), @"Write captured PCM"); }
    @finally { phase_end(q, PhaseCAFWrite, begin, mach_absolute_time()); }
}
static OSStatus finalize_caf(Queue *q, ExtAudioFileRef audio) {
    uint64_t begin = mach_absolute_time(); phase_begin(q, PhaseCAFFinalize, begin);
    OSStatus status = ExtAudioFileDispose(audio);
    phase_end(q, PhaseCAFFinalize, begin, mach_absolute_time());
    return status;
}
static NSData *serialize(Queue *q, NSDictionary *value, NSJSONWritingOptions options, NSError **error) {
    uint64_t begin = mach_absolute_time();
    if (q) phase_begin(q, PhaseJSONSerialize, begin);
    @try { return [NSJSONSerialization dataWithJSONObject:value options:options error:error]; }
    @finally { if (q) phase_end(q, PhaseJSONSerialize, begin, mach_absolute_time()); }
}
static void json(Queue *q, NSDictionary *value, NSString *path) {
    NSError *error = nil;
    NSData *data = serialize(q, value, NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys, &error);
    require(data != nil, error.localizedDescription ?: @"Cannot serialize metadata");
    uint64_t begin = mach_absolute_time();
    if (q) phase_begin(q, PhaseJSONWrite, begin);
    @try { require([data writeToFile:path options:NSDataWritingAtomic error:&error], error.localizedDescription ?: @"Cannot write metadata"); }
    @finally { if (q) phase_end(q, PhaseJSONWrite, begin, mach_absolute_time()); }
}
static void event(Queue *q, FILE *file, NSDictionary *value) {
    NSError *error = nil;
    NSData *data = serialize(q, value, NSJSONWritingSortedKeys, &error);
    require(data != nil, error.localizedDescription ?: @"Cannot serialize audio timeline");
    uint64_t begin = mach_absolute_time(); phase_begin(q, PhaseJSONWrite, begin);
    @try {
        require(fwrite(data.bytes, 1, data.length, file) == data.length && fputc('\n', file) != EOF,
            @"Cannot write audio timeline");
    } @finally { phase_end(q, PhaseJSONWrite, begin, mach_absolute_time()); }
}
static double seconds(uint64_t ticks) {
    mach_timebase_info_data_t base;
    mach_timebase_info(&base);
    return (double)ticks * base.numer / base.denom / 1e9;
}
static NSDictionary *timing_summary(Timing timing) {
    return @{@"count": @(timing.count), @"total_wall_s": @(seconds(timing.total)),
        @"max_wall_s": @(seconds(timing.maximum))};
}
static NSDictionary *diagnostics(Queue *q) {
    if (!q) return @{};
    mach_timebase_info_data_t base;
    mach_timebase_info(&base);
    NSMutableDictionary *phases = [NSMutableDictionary dictionary];
    for (ConsumerPhase p = PhaseCAFWrite; p <= PhaseCAFFinalize; ++p)
        phases[phaseNames[p]] = timing_summary(q->timings[p]);
    NSMutableDictionary *lag = [timing_summary(q->lag) mutableCopy];
    lag[@"max_context"] = q->lag.count ? @{@"callback_received_ticks": @(q->lagReceived),
        @"consumer_started_ticks": @(q->lagStarted), @"queued_blocks": @(q->lagBlocks),
        @"queued_frames": @(q->lagFrames), @"queued_pcm_bytes": @(q->lagFrames * CHANNELS * sizeof(float))} : (id)NSNull.null;
    id overflow = NSNull.null;
    if (atomic_load_explicit(&q->failure, memory_order_acquire) == 1) {
        overflow = @{@"observed_ticks": @(q->overflowTicks), @"observed_monotonic_s": @(seconds(q->overflowTicks)),
            @"queued_blocks": @(q->overflowBlocks),
            @"queued_frames": @(q->overflowFrames), @"queued_pcm_bytes": @(q->overflowFrames * CHANNELS * sizeof(float)),
            @"consumer_phase_snapshot_consistent": @(q->overflowPhaseConsistent),
            @"consumer_phase": q->overflowPhaseConsistent ? phaseNames[q->overflowPhase] : @"transition",
            @"consumer_phase_started_ticks": q->overflowPhaseConsistent ? @(q->overflowPhaseStarted) : (id)NSNull.null,
            @"consumer_phase_elapsed_wall_s": q->overflowPhaseConsistent && q->overflowPhaseStarted <= q->overflowTicks ?
                @(seconds(q->overflowTicks - q->overflowPhaseStarted)) : (id)NSNull.null};
    }
    uint64_t highFrames = atomic_load_explicit(&q->highFrames, memory_order_relaxed);
    return @{@"clock": @"mach_absolute_time; same clock as callback receipt, independent of source timestamp",
        @"timebase_numer": @(base.numer), @"timebase_denom": @(base.denom),
        @"timing_scope": @"wall durations include scheduling; queue maxima are observed snapshots; JSON excludes final session summary write",
        @"consumer_lag_scope": @"count is wait observations, including oldest queued block when consumer observes failure",
        @"queue_high_water": @{@"blocks": @(atomic_load_explicit(&q->highBlocks, memory_order_relaxed)),
            @"frames": @(highFrames), @"pcm_bytes": @(highFrames * CHANNELS * sizeof(float))},
        @"consumer_lag": lag, @"phases": phases, @"overflow": overflow};
}

static NSString *safety_failure(bool parentAlive, bool spaceAvailable, uint64_t freeBytes) {
    if (!parentAlive) return @"controlling_parent_exited";
    if (!spaceAvailable) return @"free_space_unavailable";
    return freeBytes < 512ull * 1024 * 1024 ? @"low_free_space" : nil;
}

static void record_stop(NSMutableDictionary *metadata, bool requested, double begin, double end, double duration) {
    metadata[@"stop_requested"] = @(requested);
    metadata[@"stop_reason"] = requested ? @"requested_stop" : @"duration";
    metadata[@"capture_stopped_monotonic_s"] = @(end);
    metadata[@"captured_seconds"] = @(end - begin);
    metadata[@"duration_completed"] = @(end - begin >= duration);
}

static void selfcheck_caf(Queue *q) {
    NSString *root = [NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString];
    require([NSFileManager.defaultManager createDirectoryAtPath:root withIntermediateDirectories:NO attributes:nil error:nil], @"Cannot create CAF selfcheck directory");
    ExtAudioFileRef audio = NULL;
    @try {
        AudioStreamBasicDescription format = {.mSampleRate = 48000, .mFormatID = kAudioFormatLinearPCM,
            .mFormatFlags = kAudioFormatFlagsNativeFloatPacked, .mBytesPerPacket = CHANNELS * sizeof(float),
            .mFramesPerPacket = 1, .mBytesPerFrame = CHANNELS * sizeof(float), .mChannelsPerFrame = CHANNELS, .mBitsPerChannel = 32};
        float samples[] = {0, 0.5, -0.25, 0};
        for (unsigned frameCount = 0; frameCount <= 2; frameCount += 2) {
            NSURL *url = [NSURL fileURLWithPath:[root stringByAppendingPathComponent:[NSString stringWithFormat:@"%u.caf", frameCount]]];
            check(ExtAudioFileCreateWithURL((__bridge CFURLRef)url, kAudioFileCAFType, &format, NULL, 0, &audio), @"Create selfcheck CAF");
            check(ExtAudioFileSetProperty(audio, kExtAudioFileProperty_ClientDataFormat, sizeof(format), &format), @"Set selfcheck CAF format");
            if (frameCount) {
                AudioBufferList list = {.mNumberBuffers = 1, .mBuffers = {{CHANNELS, sizeof(samples), samples}}};
                write_caf(q, audio, frameCount, &list);
            }
            OSStatus status = finalize_caf(q, audio); audio = NULL;
            check(status, @"Finalize partial selfcheck CAF");
            check(ExtAudioFileOpenURL((__bridge CFURLRef)url, &audio), @"Reopen partial selfcheck CAF");
            SInt64 length = -1; UInt32 size = sizeof(length);
            check(ExtAudioFileGetProperty(audio, kExtAudioFileProperty_FileLengthFrames, &size, &length), @"Read partial selfcheck CAF length");
            require(length == frameCount, @"Partial CAF frame count changed during finalization");
            if (frameCount) {
                float decoded[4] = {0}; UInt32 count = frameCount;
                AudioBufferList list = {.mNumberBuffers = 1, .mBuffers = {{CHANNELS, sizeof(decoded), decoded}}};
                check(ExtAudioFileSetProperty(audio, kExtAudioFileProperty_ClientDataFormat, sizeof(format), &format), @"Set selfcheck read format");
                check(ExtAudioFileRead(audio, &count, &list), @"Read partial selfcheck CAF");
                require(count == frameCount && !memcmp(samples, decoded, sizeof(samples)), @"Partial CAF PCM changed");
            }
            status = ExtAudioFileDispose(audio); audio = NULL; check(status, @"Close selfcheck CAF reader");
        }
    } @finally {
        if (audio) ExtAudioFileDispose(audio);
        [NSFileManager.defaultManager removeItemAtPath:root error:nil];
    }
}

static void selfcheck(void) {
    require(safety_failure(true, true, 512ull * 1024 * 1024) == nil &&
        [safety_failure(false, true, UINT64_MAX) isEqual:@"controlling_parent_exited"] &&
        [safety_failure(true, true, 512ull * 1024 * 1024 - 1) isEqual:@"low_free_space"] &&
        [safety_failure(true, false, 0) isEqual:@"free_space_unavailable"], @"Native safety guard failed");
    NSMutableDictionary *stopped = [NSMutableDictionary dictionary];
    record_stop(stopped, true, 10, 11, 2);
    require([stopped[@"stop_reason"] isEqual:@"requested_stop"] && ![stopped[@"duration_completed"] boolValue] &&
        [stopped[@"captured_seconds"] doubleValue] == 1, @"Requested Stop must retain partial duration");
    record_stop(stopped, false, 10, 12, 2);
    require([stopped[@"stop_reason"] isEqual:@"duration"] && [stopped[@"duration_completed"] boolValue],
        @"Duration completion must be explicit");
    Queue *q = calloc(1, sizeof(*q));
    require(q != NULL, @"Allocation failed");
    require(queue_lock_free(q), @"IO atomics must be lock free");
    NSDictionary *empty = diagnostics(q);
    require([empty[@"queue_high_water"][@"frames"] unsignedLongLongValue] == 0 &&
        [empty[@"consumer_lag"][@"count"] unsignedLongLongValue] == 0 &&
        empty[@"overflow"] == NSNull.null, @"Startup cleanup diagnostics must allow zero samples");
    require_queue_healthy(q, 1000);
    require(q->lag.count == 0, @"Healthy queue checks must not count wait observations");
    float samples[] = {0, 0.5, -0.25, 0};
    AudioBufferList list = {.mNumberBuffers = 1, .mBuffers = {{2, sizeof(samples), samples}}};
    AudioTimeStamp stamp = {.mHostTime = 100, .mSampleTime = 0,
        .mFlags = kAudioTimeStampHostTimeValid | kAudioTimeStampSampleTimeValid};
    for (unsigned i = 0; i < SLOT_COUNT; ++i) receive(0, NULL, &list, &stamp, NULL, NULL, q);
    require(atomic_load(&q->produced) == SLOT_COUNT && !atomic_load(&q->failure), @"Queue capacity failed");
    require(q->blocks[0].frames == 2 && q->blocks[0].pcm[1] == 0.5, @"PCM copy failed");
    atomic_store(&q->consumed, 1);
    receive(0, NULL, &list, &stamp, NULL, NULL, q);
    require(atomic_load(&q->produced) == SLOT_COUNT + 1, @"Queue wrap failed");
    receive(0, NULL, &list, &stamp, NULL, NULL, q);
    require(atomic_load(&q->failure) == 1, @"Overflow must fail closed");
    require(atomic_load(&q->highBlocks) == SLOT_COUNT && atomic_load(&q->highFrames) == SLOT_COUNT * 2,
        @"Queue high water must count blocks and PCM frames");
    q->blocks[1].received = 1000;
    observe_lag(q, &q->blocks[1], 1, SLOT_COUNT + 1, 1070);
    observe_lag(q, &q->blocks[1], 1, SLOT_COUNT + 1, 1030);
    require(q->lag.count == 2 && q->lag.total == 100 && q->lag.maximum == 70 && q->lagStarted == 1070,
        @"Consumer lag must use callback receipt, independent of source timestamp");
    phase_begin(q, PhaseWait, 1100);
    observe_overflow(q, 1, SLOT_COUNT + 1, 1150);
    @try { require_queue_healthy(q, 2000); require(false, @"Overflow unexpectedly accepted"); }
    @catch (NSException *error) { require([error.reason isEqual:@"Capture queue overflow"], @"Unexpected queue failure"); }
    require(q->lag.count == 3 && q->lag.total == 1100 && q->lag.maximum == 1000 && q->lagStarted == 2000 &&
        atomic_load(&q->consumed) == 1 && atomic_load(&q->produced) == SLOT_COUNT + 1 && atomic_load(&q->failure) == 1 &&
        [diagnostics(q)[@"overflow"][@"consumer_phase"] isEqual:@"wait"],
        @"Resumed consumer must observe oldest pending wait before failing without draining");
    phase_begin(q, PhaseCAFWrite, 1100);
    observe_overflow(q, 1, SLOT_COUNT + 1, 1150);
    phase_end(q, PhaseCAFWrite, 1100, 1200);
    phase_begin(q, PhaseCAFWrite, 1300);
    phase_end(q, PhaseCAFWrite, 1300, 1320);
    require(q->timings[PhaseCAFWrite].count == 2 && q->timings[PhaseCAFWrite].total == 120 &&
        q->timings[PhaseCAFWrite].maximum == 100, @"Phase wall timing aggregation failed");
    NSDictionary *summary = diagnostics(q);
    require([summary[@"overflow"][@"observed_ticks"] unsignedLongLongValue] == 1150 &&
        [summary[@"overflow"][@"queued_frames"] unsignedLongLongValue] == SLOT_COUNT * 2 &&
        [summary[@"overflow"][@"queued_pcm_bytes"] unsignedLongLongValue] == SLOT_COUNT * 2 * CHANNELS * sizeof(float) &&
        [summary[@"overflow"][@"consumer_phase"] isEqual:@"caf_write"] &&
        [summary[@"overflow"][@"consumer_phase_started_ticks"] unsignedLongLongValue] == 1100 &&
        [summary[@"overflow"][@"consumer_phase_elapsed_wall_s"] doubleValue] == seconds(50), @"Overflow context must survive phase completion");
    atomic_fetch_add(&q->phaseVersion, 1);
    observe_overflow(q, 1, SLOT_COUNT + 1, 1400);
    require([diagnostics(q)[@"overflow"][@"consumer_phase"] isEqual:@"transition"], @"Callback phase snapshot must not retry a concurrent transition");
    atomic_fetch_add(&q->phaseVersion, 1);
    @try { measured_identity(q, -1, @"invalid"); require(false, @"Invalid PID unexpectedly resolved"); }
    @catch (NSException *error) { require([error.reason isEqual:@"Target process unavailable"], @"Unexpected selfcheck failure"); }
    require(q->timings[PhaseIdentity].count == 1, @"Failed phase attempts must remain measured");
    observe_overflow(q, 1, SLOT_COUNT + 1, 1500);
    summary = diagnostics(q);
    NSMutableDictionary *failed = [@{@"status": @"failed", @"capture_diagnostics": summary} mutableCopy];
    record_stop(failed, true, 10, 11, 2);
    NSData *encoded = [NSJSONSerialization dataWithJSONObject:failed options:0 error:nil];
    NSDictionary *decoded = encoded ? [NSJSONSerialization JSONObjectWithData:encoded options:0 error:nil] : nil;
    require([decoded[@"capture_diagnostics"] isEqual:summary] && [decoded[@"stop_reason"] isEqual:@"requested_stop"],
        @"Failed session diagnostics and partial Stop must serialize together");
    require([decoded[@"capture_diagnostics"][@"timebase_numer"] unsignedLongLongValue] > 0 &&
        [decoded[@"capture_diagnostics"][@"timebase_denom"] unsignedLongLongValue] > 0 &&
        [decoded[@"capture_diagnostics"][@"overflow"][@"observed_monotonic_s"] doubleValue] == seconds(1500) &&
        encoded.length < 8192, @"Serialized diagnostics must preserve clock conversion and stay bounded");
    memset(q, 0, sizeof(*q)); q->planar = true;
    struct { UInt32 count; AudioBuffer buffers[2]; } planar = {2, {{1, 8, samples}, {1, 8, samples + 2}}};
    receive(0, NULL, (AudioBufferList *)&planar, &stamp, NULL, NULL, q);
    require(q->blocks[0].pcm[0] == 0 && q->blocks[0].pcm[1] == -0.25 && q->blocks[0].pcm[2] == 0.5,
        @"Planar conversion failed");
    planar.buffers[1].mDataByteSize = 4;
    receive(0, NULL, (AudioBufferList *)&planar, &stamp, NULL, NULL, q);
    require(atomic_load(&q->failure) == 2, @"Mismatched buffers must fail closed");
    selfcheck_caf(q);
    free(q);
    puts("selfcheck passed: bounded FIFO, wrap, overflow, interleaved/planar PCM, malformed buffers, controlled Stop metadata, bounded capture diagnostics, zero/partial CAF finalization; no capture or playback");
}

static dispatch_source_t monitor_safety(NSString *root, pid_t controllingParent, NSMutableDictionary *state) {
    dispatch_source_t safety = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    dispatch_source_set_timer(safety, DISPATCH_TIME_NOW, 250 * NSEC_PER_MSEC, 10 * NSEC_PER_MSEC);
    dispatch_source_set_event_handler(safety, ^{
        @autoreleasepool {
            struct statfs info = {0};
            bool available = statfs(root.stringByDeletingLastPathComponent.fileSystemRepresentation, &info) == 0;
            NSString *reason = safety_failure(controllingParent > 1 && getppid() == controllingParent, available, (uint64_t)info.f_bavail * info.f_bsize);
            @synchronized (state) {
                if (reason && !state[@"reason"]) { state[@"reason"] = reason; atomic_store_explicit(&interrupted, 1, memory_order_relaxed); alarm(10); }
            }
        }
    });
    dispatch_resume(safety);
    return safety;
}

static int capture(pid_t pid, NSString *expectedPath, double duration, NSString *root, pid_t controllingParent) {
    signal(SIGALRM, watchdog); alarm((unsigned)ceil(duration) + 15);
    signal(SIGINT, stop_signal); signal(SIGTERM, stop_signal);
    NSMutableDictionary *safetyState = [NSMutableDictionary dictionary];
    dispatch_source_t safety = monitor_safety(root, controllingParent, safetyState);
    uint64_t identityBegin = mach_absolute_time();
    NSDictionary *selected = identity(pid, expectedPath);
    uint64_t identityEnd = mach_absolute_time();
    char recorderPath[PROC_PIDPATHINFO_MAXSIZE] = {0};
    require(proc_pidpath(getpid(), recorderPath, sizeof(recorderPath)) > 0, @"Recorder executable unavailable");
    require(mkdir(root.fileSystemRepresentation, 0700) == 0, @"Output directory must be new, with an existing parent");
    NSMutableDictionary *metadata = [@{@"status": @"in_progress", @"target": selected,
        @"controlling_parent_pid": @(controllingParent), @"recorder_pid": @(getpid()), @"recorder_executable": @(recorderPath),
        @"mechanism": @"CoreAudio explicit process tap", @"scope": @"one HAL process; all its output streams",
        @"requested_seconds": @(duration), @"microphone": @NO, @"global_fallback": @NO,
        @"capture_consent": @"unqualified; nonzero PCM required", @"started_monotonic_s": @(seconds(mach_absolute_time()))} mutableCopy];
    NSString *session = [root stringByAppendingPathComponent:@"session.json"];
    json(NULL, metadata, session);
    AudioObjectID tap = 0, aggregate = 0;
    AudioDeviceIOProcID io = NULL;
    ExtAudioFileRef audio = NULL;
    FILE *timeline = NULL;
    Queue *q = calloc(1, sizeof(*q));
    bool started = false;
    uint64_t frames = 0;
    double peak = 0, previousEnd = -1, previousHost = -1;
    NSMutableArray *errors = [NSMutableArray array];
    @try {
        require(queue_lock_free(q), @"Cannot allocate lock-free capture queue");
        timing_add(&q->timings[PhaseIdentity], identityEnd - identityBegin);
        phase_begin(q, PhaseIdle, mach_absolute_time());
        CATapDescription *description = [[CATapDescription alloc] initStereoMixdownOfProcesses:@[selected[@"hal_process_object"]]];
        description.name = @"VNTTS explicit process evidence";
        description.privateTap = YES; description.exclusive = NO; description.muteBehavior = CATapUnmuted;
        check(AudioHardwareCreateProcessTap(description, &tap), @"Create private process tap");
        require(tap != kAudioObjectUnknown, @"No tap object returned");
        AudioStreamBasicDescription format = {0};
        property(tap, kAudioTapPropertyFormat, sizeof(format), &format);
        q->planar = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0;
        require(format.mFormatID == kAudioFormatLinearPCM && (format.mFormatFlags & kAudioFormatFlagIsFloat) &&
            (format.mFormatFlags & kAudioFormatFlagIsPacked) && !(format.mFormatFlags & kAudioFormatFlagIsBigEndian) &&
            format.mBitsPerChannel == 32 && format.mChannelsPerFrame == CHANNELS &&
            format.mBytesPerFrame == sizeof(float) * (q->planar ? 1 : CHANNELS) &&
            format.mFramesPerPacket == 1 && format.mBytesPerPacket == format.mBytesPerFrame &&
            isfinite(format.mSampleRate) && format.mSampleRate >= 8000 && format.mSampleRate <= 192000,
            @"Unsupported tap format (expected packed float32 stereo)");
        CFStringRef uid = NULL;
        property(tap, kAudioTapPropertyUID, sizeof(uid), &uid);
        require(uid != NULL, @"Missing tap UID");
        NSString *tapUID = CFBridgingRelease(uid);
        NSDictionary *composition = @{@kAudioAggregateDeviceNameKey: @"VNTTS private capture",
            @kAudioAggregateDeviceUIDKey: NSUUID.UUID.UUIDString, @kAudioAggregateDeviceIsPrivateKey: @YES,
            @kAudioAggregateDeviceTapAutoStartKey: @NO,
            @kAudioAggregateDeviceTapListKey: @[@{@kAudioSubTapUIDKey: tapUID, @kAudioSubTapDriftCompensationKey: @YES}]};
        check(AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)composition, &aggregate), @"Create private aggregate");
        require(aggregate != kAudioObjectUnknown, @"No aggregate object returned");
        metadata[@"tap_uid"] = tapUID; metadata[@"aggregate_uid"] = composition[@kAudioAggregateDeviceUIDKey];
        metadata[@"sample_rate"] = @(format.mSampleRate); metadata[@"channels"] = @(CHANNELS);
        metadata[@"source_planar"] = @(q->planar); metadata[@"queue_slots"] = @(SLOT_COUNT);
        metadata[@"max_callback_frames"] = @(MAX_FRAMES);
        json(q, metadata, session);
        AudioStreamBasicDescription fileFormat = format;
        fileFormat.mFormatFlags = kAudioFormatFlagsNativeFloatPacked;
        fileFormat.mBytesPerFrame = fileFormat.mBytesPerPacket = CHANNELS * sizeof(float);
        NSURL *url = [NSURL fileURLWithPath:[root stringByAppendingPathComponent:@"process.caf"]];
        check(ExtAudioFileCreateWithURL((__bridge CFURLRef)url, kAudioFileCAFType, &fileFormat, NULL, 0, &audio), @"Create CAF");
        check(ExtAudioFileSetProperty(audio, kExtAudioFileProperty_ClientDataFormat, sizeof(fileFormat), &fileFormat), @"Set CAF client format");
        timeline = fopen([root stringByAppendingPathComponent:@"audio.jsonl"].fileSystemRepresentation, "wx");
        require(timeline != NULL, @"Cannot create timeline");
        check(AudioDeviceCreateIOProcID(aggregate, receive, q, &io), @"Create IOProc");
        require([measured_identity(q, pid, expectedPath) isEqual:selected], @"Target changed before capture");
        require(!interrupted, @"Capture stopped before acquisition");
        require(controllingParent > 1 && getppid() == controllingParent, @"Native safety stop: controlling_parent_exited");
        check(AudioDeviceStart(aggregate, io), @"Start tap (Audio Capture consent may be required)"); started = true;
        uint64_t begin = mach_absolute_time(), lastIdentity = begin;
        metadata[@"capture_started_monotonic_s"] = @(seconds(begin)); json(q, metadata, session);
        for (;;) {
            require_queue_healthy(q, 0);
            unsigned r = atomic_load_explicit(&q->consumed, memory_order_relaxed);
            unsigned w = atomic_load_explicit(&q->produced, memory_order_acquire);
            while (r != w) { @autoreleasepool {
                Block *b = &q->blocks[r % SLOT_COUNT];
                uint64_t consumeStarted = mach_absolute_time();
                observe_lag(q, b, r, atomic_load_explicit(&q->produced, memory_order_acquire), consumeStarted);
                phase_begin(q, PhaseConsume, consumeStarted);
                double host = seconds(b->timestamp.mHostTime);
                require(previousEnd < 0 || fabs(b->timestamp.mSampleTime - previousEnd) < 0.5, @"Sample-time discontinuity");
                require(previousHost < 0 || host > previousHost, @"Host clock discontinuity");
                double blockPeak = 0;
                for (unsigned i = 0; i < b->frames * CHANNELS; ++i) {
                    require(isfinite(b->pcm[i]), @"Non-finite captured PCM"); blockPeak = fmax(blockPeak, fabs(b->pcm[i]));
                }
                AudioBufferList list = {.mNumberBuffers = 1, .mBuffers = {{CHANNELS, b->frames * CHANNELS * sizeof(float), b->pcm}}};
                write_caf(q, audio, b->frames, &list);
                event(q, timeline, @{@"kind": @"audio", @"file_frame_start": @(frames), @"frame_count": @(b->frames),
                    @"host_ticks": @(b->timestamp.mHostTime), @"monotonic_s": @(host),
                    @"sample_time": @(b->timestamp.mSampleTime), @"peak": @(blockPeak)});
                phase_begin(q, PhaseConsume, mach_absolute_time());
                frames += b->frames; peak = fmax(peak, blockPeak);
                previousEnd = b->timestamp.mSampleTime + b->frames; previousHost = host;
                atomic_store_explicit(&q->consumed, ++r, memory_order_release);
            } }
            phase_begin(q, PhaseIdle, mach_absolute_time());
            uint64_t now = mach_absolute_time();
            if (!started) break;
            if (seconds(now - lastIdentity) >= 0.25) {
                require([measured_identity(q, pid, expectedPath) isEqual:selected], @"Target process identity changed"); lastIdentity = now;
            }
            if (interrupted || seconds(now - begin) >= duration) {
                uint64_t stopBegin = mach_absolute_time(); phase_begin(q, PhaseStop, stopBegin);
                @try { check(AudioDeviceStop(aggregate, io), @"Stop capture"); started = false; }
                @finally { phase_end(q, PhaseStop, stopBegin, mach_absolute_time()); }
                record_stop(metadata, interrupted != 0, seconds(begin), seconds(mach_absolute_time()), duration);
                require([measured_identity(q, pid, expectedPath) isEqual:selected], @"Target changed at completion");
            } else { phase_begin(q, PhaseWait, mach_absolute_time()); usleep(5000); }
        }
        @synchronized (safetyState) {
            NSString *safetyReason = safetyState[@"reason"];
            if (safetyReason) { metadata[@"stop_reason"] = safetyReason; require(false, [@"Native safety stop: " stringByAppendingString:safetyReason]); }
        }
        require(frames > 0 && peak > 0, @"No nonzero PCM; silence/consent is unqualified");
        metadata[@"capture_consent"] = @"nonzero process PCM observed; source correlation still required";
    } @catch (NSException *error) { [errors addObject:error.reason ?: error.name]; }
    // If HAL cannot stop/destroy its callback, keep context alive until process exit.
    if (started) {
        uint64_t begin = mach_absolute_time(); phase_begin(q, PhaseStop, begin);
        OSStatus s = AudioDeviceStop(aggregate, io);
        phase_end(q, PhaseStop, begin, mach_absolute_time());
        if (s) [errors addObject:[NSString stringWithFormat:@"Stop cleanup: %d", (int)s]];
    }
    bool callbackDestroyed = io == NULL;
    if (io) { OSStatus s = AudioDeviceDestroyIOProcID(aggregate, io); callbackDestroyed = s == noErr; if (s) [errors addObject:[NSString stringWithFormat:@"Destroy IOProc: %d", (int)s]]; }
    if (audio) {
        OSStatus s = finalize_caf(q, audio);
        if (s) [errors addObject:[NSString stringWithFormat:@"Finalize CAF: %d", (int)s]];
    }
    if (timeline && fclose(timeline)) [errors addObject:@"Finalize timeline failed"];
    if (aggregate) { OSStatus s = AudioHardwareDestroyAggregateDevice(aggregate); if (s) [errors addObject:[NSString stringWithFormat:@"Destroy aggregate: %d", (int)s]]; }
    if (tap) { OSStatus s = AudioHardwareDestroyProcessTap(tap); if (s) [errors addObject:[NSString stringWithFormat:@"Destroy tap: %d", (int)s]]; }
    metadata[@"capture_diagnostics"] = diagnostics(q);
    if (callbackDestroyed) free(q);
    metadata[@"frames"] = @(frames); metadata[@"peak"] = @(peak); metadata[@"errors"] = errors;
    metadata[@"status"] = errors.count ? @"failed" : @"captured_unqualified";
    metadata[@"finished_monotonic_s"] = @(seconds(mach_absolute_time())); json(NULL, metadata, session);
    dispatch_source_cancel(safety);
    alarm(0);
    if (errors.count) fprintf(stderr, "%s\n", [errors componentsJoinedByString:@"; "].UTF8String);
    return errors.count ? 1 : 0;
}

int main(int argc, const char **argv) {
    pid_t controllingParent = getppid();
    @autoreleasepool { @try {
        if (argc == 2 && !strcmp(argv[1], "--selfcheck")) { selfcheck(); return 0; }
        bool inspect = argc == 4 && !strcmp(argv[1], "--inspect");
        require(inspect || (argc == 7 && !strcmp(argv[1], "--capture") && !strcmp(argv[6], "--allow-audio-capture")),
            @"Usage: --selfcheck | --inspect PID /absolute/executable | --capture PID /absolute/executable SECONDS NEW_ABSOLUTE_DIR --allow-audio-capture");
        char *end = NULL; long rawPID = strtol(argv[2], &end, 10);
        require(*argv[2] && !*end && rawPID > 0 && rawPID <= INT32_MAX && argv[3][0] == '/', @"Explicit positive PID and absolute executable path required");
        if (@available(macOS 14.2, *)) {} else { require(false, @"Core Audio process taps require macOS 14.2+"); }
        if (inspect) {
            signal(SIGALRM, watchdog); alarm(5);
            NSData *data = [NSJSONSerialization dataWithJSONObject:identity((pid_t)rawPID, @(argv[3])) options:NSJSONWritingSortedKeys error:nil];
            puts([[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String); alarm(0); return 0;
        }
        double duration = strtod(argv[4], &end);
        require(*argv[4] && !*end && isfinite(duration) && duration >= 1 && duration <= 3600 && argv[5][0] == '/', @"Duration must be 1..3600 seconds and output absolute");
        if (@available(macOS 14.2, *)) return capture((pid_t)rawPID, @(argv[3]), duration, @(argv[5]), controllingParent);
        require(false, @"Core Audio process taps require macOS 14.2+");
    } @catch (NSException *error) { fprintf(stderr, "process_tap: %s\n", error.reason.UTF8String); return 1; } }
    return 1;
}
