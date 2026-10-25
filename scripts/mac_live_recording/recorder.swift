import Foundation
import AppKit
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreGraphics
import Darwin

struct CaptureError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}
func monotonicSeconds(_ ticks: UInt64 = mach_absolute_time()) -> Double {
    CMTimeGetSeconds(CMClockMakeHostTimeFromSystemUnits(ticks))
}
func clockAnchor() -> [String: Any] {
    var timebase = mach_timebase_info_data_t()
    mach_timebase_info(&timebase)
    let ticks = mach_absolute_time()
    return ["mach_absolute_ticks": ticks, "timebase_numer": timebase.numer,
        "timebase_denom": timebase.denom, "monotonic_s": monotonicSeconds(ticks),
        "host_clock_s": CMClockGetTime(CMClockGetHostTimeClock()).seconds]
}
func activity(_ buffer: AVAudioPCMBuffer, threshold: Float = 0.001) throws -> (Float, Int?) {
    guard let channels = buffer.floatChannelData else { throw CaptureError("Expected float32 PCM") }
    var peak: Float = 0
    var first: Int?
    for frame in 0..<Int(buffer.frameLength) {
        for channel in 0..<Int(buffer.format.channelCount) {
            let value = abs(channels[buffer.format.isInterleaved ? 0 : channel][frame * buffer.stride + (buffer.format.isInterleaved ? channel : 0)])
            peak = max(peak, value)
            if first == nil && value >= threshold { first = frame }
        }
    }
    return (peak, first)
}


func captureSafetyFailure(parentAlive: Bool, freeBytes: UInt64?) -> String? {
    if !parentAlive { return "controlling_parent_exited" }
    guard let freeBytes else { return "free_space_unavailable" }
    return freeBytes < 512 * 1024 * 1024 ? "low_free_space" : nil
}

final class CaptureSafetyGuard {
    let parentPID: pid_t
    private let lock = NSLock()
    private var failure: String?
    private var timer: DispatchSourceTimer?
    var reason: String? { lock.lock(); defer { lock.unlock() }; return failure }
    init(directory: URL, parentPID: pid_t) {
        self.parentPID = parentPID
        let source = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        source.schedule(deadline: .now(), repeating: .milliseconds(250))
        source.setEventHandler { [weak self] in
            guard let self else { return }
            var info = statfs()
            let free = statfs(directory.path, &info) == 0 ? UInt64(info.f_bavail) * UInt64(info.f_bsize) : nil
            guard let reason = captureSafetyFailure(parentAlive: self.parentPID > 1 && getppid() == self.parentPID, freeBytes: free) else { return }
            self.lock.lock()
            guard self.failure == nil else { self.lock.unlock(); return }
            self.failure = reason
            self.lock.unlock()
            // This queue also bounds a stalled main actor or native finalization after controller loss.
            DispatchQueue.global().asyncAfter(deadline: .now() + 10) { _exit(2) }
            kill(getpid(), SIGTERM)
        }
        timer = source
        source.resume()
    }
    deinit { timer?.cancel() }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let label: String
    let root: URL
    let log: FileHandle
    let queue: DispatchQueue
    var audio: AVAudioFile?
    var audioFrames: Int64 = 0
    var videoFrames = 0
    var failure: String?
    var writer: AVAssetWriter?
    var videoInput: AVAssetWriterInput?
    var videoStarted = false
    var videoDrops = 0
    var firstVideoPTS: CMTime?
    var lastVideoPTS: CMTime?
    let requiresAudio: Bool

    init(label: String, root: URL, queue: DispatchQueue, width: Int = 0, height: Int = 0, requiresAudio: Bool = true) throws {
        self.requiresAudio = requiresAudio
        self.label = label; self.root = root; self.queue = queue
        let logURL = root.appendingPathComponent("\(label).jsonl")
        guard FileManager.default.createFile(atPath: logURL.path, contents: nil) else { throw CaptureError("Cannot create log") }
        log = try FileHandle(forWritingTo: logURL)
        super.init()
        if width > 0 {
            let asset = try AVAssetWriter(outputURL: root.appendingPathComponent("window.mov"), fileType: .mov)
            asset.movieFragmentInterval = CMTime(seconds: 10, preferredTimescale: 600)
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
                // Reordering retains capture surfaces and stalls the three-frame SCStream pool.
                AVVideoCompressionPropertiesKey: [AVVideoAllowFrameReorderingKey: false]
            ])
            input.expectsMediaDataInRealTime = true
            guard asset.canAdd(input) else { throw CaptureError("Cannot add H264 video writer") }
            asset.add(input)
            writer = asset; videoInput = input

        }
    }
    func event(_ values: [String: Any]) throws {
        var record = values
        record["source"] = label
        record["callback_monotonic_s"] = monotonicSeconds()
        var data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        data.append(10)
        try log.write(contentsOf: data)
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.failure = self.failure ?? String(describing: error) }
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        autoreleasepool { process(buffer, type: type) }
    }
    func process(_ buffer: CMSampleBuffer, type: SCStreamOutputType) {
        guard failure == nil else { return }
        do {
            guard buffer.isValid else {
                try event(["kind": "invalid_sample", "output_type": type.rawValue])
                return
            }
            let pts = buffer.presentationTimeStamp
            guard pts.isValid, !pts.isIndefinite else { throw CaptureError("Invalid sample timestamp") }
            var values: [String: Any] = ["kind": type == .audio ? "audio" : "video", "pts_s": pts.seconds,
                "pts_value": pts.value, "pts_timescale": pts.timescale,
                "host_clock_s": CMClockGetTime(CMClockGetHostTimeClock()).seconds]
            if type == .audio {
                try buffer.withAudioBufferList { list, retainedBlock in
                    guard let description = buffer.formatDescription else { throw CaptureError("Missing audio format") }
                    let format = AVAudioFormat(cmAudioFormatDescription: description)
                    guard format.commonFormat == .pcmFormatFloat32,
                          let pcm = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: list.unsafePointer)
                    else { throw CaptureError("Unsupported captured PCM format") }
                    if audio == nil {
                        audio = try AVAudioFile(forWriting: root.appendingPathComponent("\(label).caf"),
                            settings: format.settings, commonFormat: .pcmFormatFloat32, interleaved: format.isInterleaved)
                    }
                    let (peak, first) = try activity(pcm)
                    values["file_frame_start"] = audioFrames
                    values["frame_count"] = pcm.frameLength
                    values["sample_rate"] = format.sampleRate
                    values["channels"] = format.channelCount
                    values["peak"] = peak
                    values["first_active_frame"] = first.map { $0 as Any } ?? NSNull()
                    values["activity_threshold"] = 0.001
                    try audio!.write(from: pcm)
                    audioFrames += Int64(pcm.frameLength)
                    withExtendedLifetime(retainedBlock) {}
                }
                try event(values)
            } else if type == .screen {
                let attachments = (CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false)
                    as? [[SCStreamFrameInfo: Any]])?.first
                let status = (attachments?[.status] as? NSNumber)?.intValue ?? -1
                values["frame_status"] = status
                if let ticks = attachments?[.displayTime] as? NSNumber {
                    values["display_mach_ticks"] = ticks.uint64Value
                    values["display_monotonic_s"] = monotonicSeconds(ticks.uint64Value)
                }
                guard status == SCFrameStatus.complete.rawValue, let writer, let input = videoInput else {
                    values["written"] = false
                    try event(values)
                    return
                }
                if !videoStarted {
                    guard writer.startWriting() else { throw writer.error ?? CaptureError("Cannot start video") }
                    writer.startSession(atSourceTime: pts)
                    videoStarted = true
                    values["movie_pts_origin_s"] = pts.seconds
                }
                if input.isReadyForMoreMediaData {
                    guard input.append(buffer) else { throw writer.error ?? CaptureError("Cannot append video") }
                    values["written"] = true
                    videoFrames += 1
                    firstVideoPTS = firstVideoPTS ?? pts
                    lastVideoPTS = pts
                } else {
                    values["written"] = false
                    values["drop_reason"] = "writer_backpressure"
                    videoDrops += 1
                }
                try event(values)
            }
        } catch {
            failure = String(describing: error)
            try? event(["kind": "error", "error": failure!])
        }
    }
    func finish() async throws {
        queue.sync { audio = nil; if videoStarted { videoInput?.markAsFinished() } }
        if let writer, videoStarted {
            await writer.finishWriting()
            if writer.status != .completed { failure = failure ?? String(describing: writer.error ?? CaptureError("Video did not finish")) }
        }
        try queue.sync {
            if requiresAudio && audioFrames == 0 { failure = failure ?? "No application audio buffers" }
            if writer != nil && videoFrames == 0 { failure = failure ?? "No window video frames" }
            try event(["kind": failure == nil ? "finished" : "failed", "audio_frames": audioFrames,
                "video_frames": videoFrames, "video_drops": videoDrops, "error": failure as Any? ?? NSNull()])
            try log.close()
            if let failure { throw CaptureError(failure) }
        }
    }
}

@main @MainActor struct Main {
    static func main() async {
        do { try await run() }
        catch { FileHandle.standardError.write(Data("capture_helper: \(error)\n".utf8)); exit(1) }
    }
    static func run() async throws {
        let parentPID = getppid()
        let args = Array(CommandLine.arguments.dropFirst())
        if args == ["--preflight"] {
            guard CGPreflightScreenCaptureAccess() else { throw CaptureError("Screen Recording permission missing; no prompt") }
            print("Screen Recording preflight passed; no capture")
            return
        }
        if args == ["--clock"] {
            print(String(data: try JSONSerialization.data(withJSONObject: clockAnchor(), options: [.sortedKeys]), encoding: .utf8)!)
            return
        }
        if args == ["--selfcheck"] {
            try targetGuardSelfcheck()
            let before = monotonicSeconds()
            let host = CMClockGetTime(CMClockGetHostTimeClock()).seconds
            let after = monotonicSeconds()
            guard before <= host && host <= after else { throw CaptureError("Host clock mismatch") }
            for interleaved in [false, true] {
                let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 2, interleaved: interleaved)!
                let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16)!
                pcm.frameLength = 16
                for channel in 0..<2 { for frame in 0..<16 { pcm.floatChannelData![interleaved ? 0 : channel][frame * pcm.stride + (interleaved ? channel : 0)] = 0 } }
                pcm.floatChannelData![interleaved ? 0 : 1][7 * pcm.stride + (interleaved ? 1 : 0)] = 0.5
                let result = try activity(pcm)
                guard result.0 == 0.5 && result.1 == 7 else { throw CaptureError("PCM activity check failed") }
            }
            print("selfcheck passed: host clock, planar/interleaved PCM onset; no capture or playback")
            return
        }
        if args.count == 2 && args[0] == "--writer-selfcheck" {
            try await writerSelfcheck(root: URL(fileURLWithPath: args[1], isDirectory: true))
            return
        }
        if args.count == 2 && args[0] == "--writer-crash-selfcheck" {
            try await writerSelfcheck(root: URL(fileURLWithPath: args[1], isDirectory: true), crash: true)
            return
        }
        try await capture(args, parentPID: parentPID)
    }
}
