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
#include <math.h>
#include <unistd.h>

#define SLOT_COUNT 64u
#define MAX_FRAMES 8192u
#define CHANNELS 2u

typedef struct {
    UInt32 frames;
    AudioTimeStamp timestamp;
    float pcm[MAX_FRAMES * CHANNELS];
} Block;
typedef struct {
    _Atomic unsigned produced, consumed;
    _Atomic int failure;
    bool planar;
    Block blocks[SLOT_COUNT];
} Queue;
static volatile sig_atomic_t interrupted;
static void stop_signal(int value) { (void)value; interrupted = 1; }
static void watchdog(int value) { (void)value; _exit(2); }

// The IO thread only validates shape and copies into bounded preallocated storage.
static OSStatus receive(AudioObjectID device, const AudioTimeStamp *now,
    const AudioBufferList *input, const AudioTimeStamp *time,
    AudioBufferList *output, const AudioTimeStamp *outputTime, void *context) {
    (void)device; (void)now; (void)output; (void)outputTime;
    Queue *q = context;
    if (atomic_load_explicit(&q->failure, memory_order_relaxed)) return noErr;
    unsigned w = atomic_load_explicit(&q->produced, memory_order_relaxed);
    unsigned r = atomic_load_explicit(&q->consumed, memory_order_acquire);
    if (w - r == SLOT_COUNT) { atomic_store(&q->failure, 1); return noErr; }
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
    b->frames = bytes / bytesPerFrame; b->timestamp = *time;
    if (q->planar) {
        for (UInt32 i = 0; i < b->frames; ++i)
            for (unsigned c = 0; c < CHANNELS; ++c)
                b->pcm[i * CHANNELS + c] = ((float *)input->mBuffers[c].mData)[i];
    } else memcpy(b->pcm, input->mBuffers[0].mData, bytes);
    atomic_store_explicit(&q->produced, w + 1, memory_order_release);
    return noErr;
}

static void check(OSStatus status, NSString *operation) {
    if (status != noErr) @throw [NSException exceptionWithName:@"CaptureError"
        reason:[NSString stringWithFormat:@"%@: OSStatus %d", operation, (int)status] userInfo:nil];
}
static void require(bool condition, NSString *reason) {
    if (!condition) @throw [NSException exceptionWithName:@"CaptureError" reason:reason userInfo:nil];
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
static void json(NSDictionary *value, NSString *path) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
    require(data && [data writeToFile:path options:NSDataWritingAtomic error:&error], error.localizedDescription ?: @"Cannot write metadata");
}
static void event(FILE *file, NSDictionary *value) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:&error];
    require(data && fwrite(data.bytes, 1, data.length, file) == data.length && fputc('\n', file) != EOF,
        @"Cannot write audio timeline");
}
static double seconds(uint64_t ticks) {
    mach_timebase_info_data_t base;
    mach_timebase_info(&base);
    return (double)ticks * base.numer / base.denom / 1e9;
}

static void record_stop(NSMutableDictionary *metadata, bool requested, double begin, double end, double duration) {
    metadata[@"stop_requested"] = @(requested);
    metadata[@"stop_reason"] = requested ? @"requested_stop" : @"duration";
    metadata[@"capture_stopped_monotonic_s"] = @(end);
    metadata[@"captured_seconds"] = @(end - begin);
    metadata[@"duration_completed"] = @(end - begin >= duration);
}

static void selfcheck(void) {
    NSMutableDictionary *stopped = [NSMutableDictionary dictionary];
    record_stop(stopped, true, 10, 11, 2);
    require([stopped[@"stop_reason"] isEqual:@"requested_stop"] && ![stopped[@"duration_completed"] boolValue] &&
        [stopped[@"captured_seconds"] doubleValue] == 1, @"Requested Stop must retain partial duration");
    record_stop(stopped, false, 10, 12, 2);
    require([stopped[@"stop_reason"] isEqual:@"duration"] && [stopped[@"duration_completed"] boolValue],
        @"Duration completion must be explicit");
    Queue *q = calloc(1, sizeof(*q));
    require(q != NULL, @"Allocation failed");
    require(atomic_is_lock_free(&q->produced) && atomic_is_lock_free(&q->failure), @"IO atomics must be lock free");
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
    memset(q, 0, sizeof(*q)); q->planar = true;
    struct { UInt32 count; AudioBuffer buffers[2]; } planar = {2, {{1, 8, samples}, {1, 8, samples + 2}}};
    receive(0, NULL, (AudioBufferList *)&planar, &stamp, NULL, NULL, q);
    require(q->blocks[0].pcm[0] == 0 && q->blocks[0].pcm[1] == -0.25 && q->blocks[0].pcm[2] == 0.5,
        @"Planar conversion failed");
    planar.buffers[1].mDataByteSize = 4;
    receive(0, NULL, (AudioBufferList *)&planar, &stamp, NULL, NULL, q);
    require(atomic_load(&q->failure) == 2, @"Mismatched buffers must fail closed");
    free(q);
    puts("selfcheck passed: bounded FIFO, wrap, overflow, interleaved/planar PCM, malformed buffers, controlled Stop metadata; no capture or playback");
}

static int capture(pid_t pid, NSString *expectedPath, double duration, NSString *root) {
    signal(SIGALRM, watchdog); alarm((unsigned)ceil(duration) + 15);
    NSDictionary *selected = identity(pid, expectedPath);
    char recorderPath[PROC_PIDPATHINFO_MAXSIZE] = {0};
    require(proc_pidpath(getpid(), recorderPath, sizeof(recorderPath)) > 0, @"Recorder executable unavailable");
    require(mkdir(root.fileSystemRepresentation, 0700) == 0, @"Output directory must be new, with an existing parent");
    NSMutableDictionary *metadata = [@{@"status": @"in_progress", @"target": selected,
        @"recorder_pid": @(getpid()), @"recorder_executable": @(recorderPath),
        @"mechanism": @"CoreAudio explicit process tap", @"scope": @"one HAL process; all its output streams",
        @"requested_seconds": @(duration), @"microphone": @NO, @"global_fallback": @NO,
        @"capture_consent": @"unqualified; nonzero PCM required", @"started_monotonic_s": @(seconds(mach_absolute_time()))} mutableCopy];
    NSString *session = [root stringByAppendingPathComponent:@"session.json"];
    json(metadata, session);
    signal(SIGINT, stop_signal); signal(SIGTERM, stop_signal);
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
        require(q && atomic_is_lock_free(&q->produced) && atomic_is_lock_free(&q->failure), @"Cannot allocate lock-free capture queue");
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
        json(metadata, session);
        AudioStreamBasicDescription fileFormat = format;
        fileFormat.mFormatFlags = kAudioFormatFlagsNativeFloatPacked;
        fileFormat.mBytesPerFrame = fileFormat.mBytesPerPacket = CHANNELS * sizeof(float);
        NSURL *url = [NSURL fileURLWithPath:[root stringByAppendingPathComponent:@"process.caf"]];
        check(ExtAudioFileCreateWithURL((__bridge CFURLRef)url, kAudioFileCAFType, &fileFormat, NULL, 0, &audio), @"Create CAF");
        check(ExtAudioFileSetProperty(audio, kExtAudioFileProperty_ClientDataFormat, sizeof(fileFormat), &fileFormat), @"Set CAF client format");
        timeline = fopen([root stringByAppendingPathComponent:@"audio.jsonl"].fileSystemRepresentation, "wx");
        require(timeline != NULL, @"Cannot create timeline");
        check(AudioDeviceCreateIOProcID(aggregate, receive, q, &io), @"Create IOProc");
        require([identity(pid, expectedPath) isEqual:selected], @"Target changed before capture");
        check(AudioDeviceStart(aggregate, io), @"Start tap (Audio Capture consent may be required)"); started = true;
        uint64_t begin = mach_absolute_time(), lastIdentity = begin;
        metadata[@"capture_started_monotonic_s"] = @(seconds(begin)); json(metadata, session);
        for (;;) {
            require(!atomic_load(&q->failure), atomic_load(&q->failure) == 1 ? @"Capture queue overflow" : @"Invalid callback shape/timestamp");
            unsigned r = atomic_load_explicit(&q->consumed, memory_order_relaxed);
            unsigned w = atomic_load_explicit(&q->produced, memory_order_acquire);
            while (r != w) { @autoreleasepool {
                Block *b = &q->blocks[r % SLOT_COUNT];
                double host = seconds(b->timestamp.mHostTime);
                require(previousEnd < 0 || fabs(b->timestamp.mSampleTime - previousEnd) < 0.5, @"Sample-time discontinuity");
                require(previousHost < 0 || host > previousHost, @"Host clock discontinuity");
                double blockPeak = 0;
                for (unsigned i = 0; i < b->frames * CHANNELS; ++i) {
                    require(isfinite(b->pcm[i]), @"Non-finite captured PCM"); blockPeak = fmax(blockPeak, fabs(b->pcm[i]));
                }
                AudioBufferList list = {.mNumberBuffers = 1, .mBuffers = {{CHANNELS, b->frames * CHANNELS * sizeof(float), b->pcm}}};
                check(ExtAudioFileWrite(audio, b->frames, &list), @"Write captured PCM");
                event(timeline, @{@"kind": @"audio", @"file_frame_start": @(frames), @"frame_count": @(b->frames),
                    @"host_ticks": @(b->timestamp.mHostTime), @"monotonic_s": @(host),
                    @"sample_time": @(b->timestamp.mSampleTime), @"peak": @(blockPeak)});
                frames += b->frames; peak = fmax(peak, blockPeak);
                previousEnd = b->timestamp.mSampleTime + b->frames; previousHost = host;
                atomic_store_explicit(&q->consumed, ++r, memory_order_release);
            } }
            uint64_t now = mach_absolute_time();
            if (!started) break;
            if (seconds(now - lastIdentity) >= 0.25) {
                require([identity(pid, expectedPath) isEqual:selected], @"Target process identity changed"); lastIdentity = now;
            }
            if (interrupted || seconds(now - begin) >= duration) {
                check(AudioDeviceStop(aggregate, io), @"Stop capture"); started = false;
                record_stop(metadata, interrupted != 0, seconds(begin), seconds(mach_absolute_time()), duration);
                require([identity(pid, expectedPath) isEqual:selected], @"Target changed at completion");
            } else usleep(5000);
        }
        require(frames > 0 && peak > 0, @"No nonzero PCM; silence/consent is unqualified");
        metadata[@"capture_consent"] = @"nonzero process PCM observed; source correlation still required";
    } @catch (NSException *error) { [errors addObject:error.reason ?: error.name]; }
    // If HAL cannot stop/destroy its callback, keep context alive until process exit.
    if (started) { OSStatus s = AudioDeviceStop(aggregate, io); if (s) [errors addObject:[NSString stringWithFormat:@"Stop cleanup: %d", (int)s]]; }
    bool callbackDestroyed = io == NULL;
    if (io) { OSStatus s = AudioDeviceDestroyIOProcID(aggregate, io); callbackDestroyed = s == noErr; if (s) [errors addObject:[NSString stringWithFormat:@"Destroy IOProc: %d", (int)s]]; }
    if (callbackDestroyed) free(q);
    if (audio) { OSStatus s = ExtAudioFileDispose(audio); if (s) [errors addObject:[NSString stringWithFormat:@"Finalize CAF: %d", (int)s]]; }
    if (timeline && fclose(timeline)) [errors addObject:@"Finalize timeline failed"];
    if (aggregate) { OSStatus s = AudioHardwareDestroyAggregateDevice(aggregate); if (s) [errors addObject:[NSString stringWithFormat:@"Destroy aggregate: %d", (int)s]]; }
    if (tap) { OSStatus s = AudioHardwareDestroyProcessTap(tap); if (s) [errors addObject:[NSString stringWithFormat:@"Destroy tap: %d", (int)s]]; }
    metadata[@"frames"] = @(frames); metadata[@"peak"] = @(peak); metadata[@"errors"] = errors;
    metadata[@"status"] = errors.count ? @"failed" : @"captured_unqualified";
    metadata[@"finished_monotonic_s"] = @(seconds(mach_absolute_time())); json(metadata, session);
    alarm(0);
    if (errors.count) fprintf(stderr, "%s\n", [errors componentsJoinedByString:@"; "].UTF8String);
    return errors.count ? 1 : 0;
}

int main(int argc, const char **argv) {
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
        require(*argv[4] && !*end && isfinite(duration) && duration >= 1 && duration <= 600 && argv[5][0] == '/', @"Duration must be 1..600 seconds and output absolute");
        if (@available(macOS 14.2, *)) return capture((pid_t)rawPID, @(argv[3]), duration, @(argv[5]));
        require(false, @"Core Audio process taps require macOS 14.2+");
    } @catch (NSException *error) { fprintf(stderr, "process_tap: %s\n", error.reason.UTF8String); return 1; } }
    return 1;
}
