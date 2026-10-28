import AppKit
import CoreGraphics
import CoreMedia
import CoreVideo
import Darwin
import Foundation
import ScreenCaptureKit

private let maximumPixels = 32 * 1024 * 1024
private let maximumRequestBytes = 65_536

struct CaptureFailure: Error {
    let code: String
    let message: String
    var nativeDomain: String? = nil
    var nativeCode: Int? = nil
}

func fail(_ code: String, _ message: String) -> CaptureFailure {
    CaptureFailure(code: code, message: message)
}

func nativeFailure(_ error: Error) -> CaptureFailure {
    if let failure = error as? CaptureFailure { return failure }
    let native = error as NSError
    let interrupted = native.domain == SCStreamErrorDomain && native.code == SCStreamError.Code.failedApplicationConnectionInterrupted.rawValue
    return CaptureFailure(code: interrupted ? "connection_interrupted" : "native_error",
                          message: "\(native.localizedDescription) (\(native.domain), code \(native.code))",
                          nativeDomain: native.domain, nativeCode: native.code)
}

func clockNow() -> Double {
    CMTimeGetSeconds(CMClockMakeHostTimeFromSystemUnits(mach_absolute_time()))
}

func remainingTime(until deadline: Double) throws -> Double {
    let remaining = deadline - clockNow()
    guard remaining > 0 else { throw fail("frame_timeout", "Native capture deadline expired") }
    return remaining
}

func retryLayoutChanges<T>(deadline: Double, acquire: () async throws -> T) async throws -> T {
    while true {
        _ = try remainingTime(until: deadline)
        do {
            let result = try await acquire()
            _ = try remainingTime(until: deadline)
            return result
        } catch let error as CaptureFailure where error.code == "layout_changed" {
            let remaining = try remainingTime(until: deadline)
            try await Task.sleep(nanoseconds: UInt64(min(0.025, remaining) * 1_000_000_000))
        }
    }
}

func retryCaptureChanges<T>(deadline: Double, recover: () async throws -> Void,
                            acquire: () async throws -> T) async throws -> T {
    var recoveredConnection = false
    return try await retryLayoutChanges(deadline: deadline) {
        while true {
            do { return try await acquire() }
            catch {
                let failure = nativeFailure(error)
                if failure.code == "layout_changed" {
                    try await recover()
                    throw failure
                }
                guard failure.code == "connection_interrupted", !recoveredConnection else { throw failure }
                recoveredConnection = true
                _ = try remainingTime(until: deadline)
                try await recover()
                _ = try remainingTime(until: deadline)
            }
        }
    }
}

func integer(_ value: Any?, _ name: String, minimum: Int = 0, maximum: Int = Int.max) throws -> Int {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite,
          number.doubleValue >= Double(minimum), number.doubleValue < Double(Int.max),
          number.doubleValue.rounded(.towardZero) == number.doubleValue,
          number.intValue <= maximum else {
        throw fail("invalid_request", "Invalid integer: \(name)")
    }
    return number.intValue
}

func checkedSize(_ width: Int, _ height: Int) throws {
    guard width > 0, height > 0, width <= maximumPixels / height else {
        throw fail("invalid_size", "Capture dimensions exceed the pixel limit")
    }
}

func packedPixels(_ base: UnsafeRawPointer, width: Int, height: Int, stride: Int) throws -> Data {
    try checkedSize(width, height)
    let rowBytes = width * 4
    guard stride >= rowBytes, stride <= maximumPixels * 4 / height else {
        throw fail("invalid_stride", "Invalid native pixel stride")
    }
    var result = Data(count: rowBytes * height)
    result.withUnsafeMutableBytes { destination in
        for row in 0..<height {
            destination.baseAddress!.advanced(by: row * rowBytes)
                .copyMemory(from: base.advanced(by: row * stride), byteCount: rowBytes)
        }
    }
    return result
}

struct Request {
    let id: Int
    let operation: String
    let sessionID: String
    let values: [String: Any]

    init(_ data: Data) throws {
        guard data.count <= maximumRequestBytes,
              let values = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw fail("invalid_request", "Expected a bounded JSON object")
        }
        guard try integer(values["version"], "version") == 1 else {
            throw fail("protocol_version", "Unsupported capture protocol version")
        }
        id = try integer(values["id"], "id", minimum: 1, maximum: 9_007_199_254_740_991)
        guard let operation = values["op"] as? String,
              ["open", "capture", "close"].contains(operation),
              let sessionID = values["session_id"] as? String,
              sessionID.utf8.count == 36,
              UUID(uuidString: sessionID)?.uuidString.lowercased() == sessionID.lowercased() else {
            throw fail("invalid_request", "Invalid capture operation or session identity")
        }
        self.operation = operation
        self.sessionID = sessionID
        self.values = values
    }
}

// Read one bounded line without buffering a second request behind raw pixels.
func readRequest() throws -> Data? {
    var result = Data()
    var byte: UInt8 = 0
    while true {
        let count = Darwin.read(STDIN_FILENO, &byte, 1)
        if count == 0 {
            if result.isEmpty { return nil }
            throw fail("invalid_request", "Truncated capture request")
        }
        if count < 0 {
            if errno == EINTR { continue }
            throw fail("transport_error", "Unable to read capture request")
        }
        if byte == 10 { return result }
        guard result.count < maximumRequestBytes - 1 else {
            throw fail("invalid_request", "Capture request is too large")
        }
        result.append(byte)
    }
}

func reply(_ id: Int, result: [String: Any] = [:], pixels: Data = Data(), error: CaptureFailure? = nil) throws {
    var value: [String: Any] = ["version": 1, "id": id, "ok": error == nil, "payload_bytes": pixels.count]
    if let error {
        var details: [String: Any] = ["code": error.code, "message": error.message]
        if let domain = error.nativeDomain { details["native_domain"] = domain }
        if let code = error.nativeCode { details["native_code"] = code }
        value["error"] = details
    } else {
        value["result"] = result
    }
    var header = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    guard header.count < maximumRequestBytes else { throw fail("transport_error", "Response metadata is too large") }
    header.append(10)
    try FileHandle.standardOutput.write(contentsOf: header)
    if !pixels.isEmpty { try FileHandle.standardOutput.write(contentsOf: pixels) }
}

struct Target {
    let windowID: CGWindowID?
    let pid: pid_t?
    let displayID: CGDirectDisplayID?

    init(_ value: Any?) throws {
        guard let value = value as? [String: Any], let kind = value["kind"] as? String else {
            throw fail("invalid_request", "Expected a capture target")
        }
        switch kind {
        case "window":
            windowID = CGWindowID(try integer(value["window_id"], "window_id", minimum: 1, maximum: Int(UInt32.max)))
            pid = pid_t(try integer(value["pid"], "pid", minimum: 1, maximum: Int(Int32.max)))
            displayID = nil
        case "display":
            let identifier = try integer(value["display_id"] ?? 0, "display_id", maximum: Int(UInt32.max))
            displayID = identifier == 0 ? CGMainDisplayID() : CGDirectDisplayID(identifier)
            windowID = nil
            pid = nil
        default:
            throw fail("invalid_request", "Unknown capture target kind")
        }
    }
}

struct ProcessIdentity: Equatable {
    let seconds: Int
    let microseconds: Int
}

func processIdentity(_ pid: pid_t) throws -> ProcessIdentity {
    var info = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var query = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard sysctl(&query, u_int(query.count), &info, &size, nil, 0) == 0,
          size == MemoryLayout<kinfo_proc>.stride, info.kp_proc.p_starttime.tv_sec > 0 else {
        throw fail("identity_changed", "Selected process lifetime is unavailable")
    }
    return ProcessIdentity(seconds: info.kp_proc.p_starttime.tv_sec,
                           microseconds: Int(info.kp_proc.p_starttime.tv_usec))
}

struct Geometry: Equatable {
    let bounds: CGRect
    let displayID: CGDirectDisplayID

    func sameLayout(as other: Geometry) -> Bool {
        bounds.size == other.bounds.size && displayID == other.displayID
    }
}

func geometry(_ target: Target) throws -> Geometry {
    if let displayID = target.displayID {
        guard CGDisplayIsActive(displayID) != 0 else { throw fail("target_unavailable", "Selected display is unavailable") }
        return Geometry(bounds: CGDisplayBounds(displayID), displayID: displayID)
    }
    guard let windowID = target.windowID, let pid = target.pid,
          let entries = CGWindowListCopyWindowInfo(.optionIncludingWindow, windowID) as? [[String: Any]],
          let entry = entries.first(where: { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == windowID }),
          (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid else {
        throw fail("target_lost", "Selected window or owning process is no longer available")
    }
    guard entry[kCGWindowIsOnscreen as String] as? Bool == true,
          let rawBounds = entry[kCGWindowBounds as String] as? [String: Any],
          let bounds = CGRect(dictionaryRepresentation: rawBounds as CFDictionary),
          bounds.width > 0, bounds.height > 0 else {
        throw fail("target_unavailable", "Selected window is minimized or not on an active display")
    }
    var displays = [CGDirectDisplayID](repeating: 0, count: 32)
    var count: UInt32 = 0
    guard CGGetDisplaysWithRect(bounds, UInt32(displays.count), &displays, &count) == .success, count > 0 else {
        throw fail("target_unavailable", "Selected window has no active display")
    }
    let best = displays.prefix(Int(count)).max { first, second in
        let left = CGDisplayBounds(first).intersection(bounds)
        let right = CGDisplayBounds(second).intersection(bounds)
        return left.width * left.height < right.width * right.height
    }!
    return Geometry(bounds: bounds, displayID: best)
}

struct Frame {
    let pixels: Data
    let width: Int
    let height: Int
    let sequence: Int
    let sourceTime: Double?
    let acquiredAt: Double
    let epoch: Int
}

final class FrameStore {
    let condition = NSCondition()
    private var latest: Frame?
    private var failure: CaptureFailure?
    private var terminal = false
    private var epoch = 0
    private var sequence = 0
    private var expectedSize = CGSize.zero

    func reset(width: Int, height: Int) -> Int {
        condition.lock()
        defer { condition.unlock() }
        epoch += 1
        latest = nil
        failure = nil
        terminal = false
        expectedSize = CGSize(width: width, height: height)
        condition.broadcast()
        return epoch
    }

    func invalidate(_ error: CaptureFailure, epoch expectedEpoch: Int? = nil, recoverable: Bool = false) {
        condition.lock()
        defer { condition.unlock() }
        guard expectedEpoch == nil || expectedEpoch == epoch else { return }
        latest = nil
        failure = error
        terminal = terminal || !recoverable
        condition.broadcast()
    }

    func accept(_ pixels: Data, width: Int, height: Int, sourceTime: Double?, epoch expectedEpoch: Int) {
        condition.lock()
        defer { condition.unlock() }
        guard expectedEpoch == epoch, !terminal else { return }
        guard CGSize(width: width, height: height) == expectedSize else {
            latest = nil
            failure = fail("layout_changed", "Native frame dimensions changed")
            terminal = true
            condition.broadcast()
            return
        }
        sequence += 1
        latest = Frame(pixels: pixels, width: width, height: height, sequence: sequence,
                       sourceTime: sourceTime, acquiredAt: clockNow(), epoch: epoch)
        failure = nil
        condition.broadcast()
    }

    func capture(deadline: Double) throws -> Frame {
        condition.lock()
        defer { condition.unlock() }
        while latest == nil && failure == nil {
            let remaining = try remainingTime(until: deadline)
            _ = condition.wait(until: Date(timeIntervalSinceNow: min(0.025, remaining)))
        }
        if let failure { throw failure }
        _ = try remainingTime(until: deadline)
        return latest!
    }
}

func canonicalRect(content: CGRect, contentScale: Double, scaleFactor: Double,
                   width: Int, height: Int, expected: CGSize) throws -> CGRect {
    guard contentScale.isFinite, scaleFactor.isFinite, contentScale > 0, scaleFactor > 0,
          [content.minX, content.minY, content.width, content.height].allSatisfy({ $0.isFinite }) else {
        throw fail("invalid_frame", "Invalid native content geometry")
    }
    let pixels = CGRect(x: content.minX * scaleFactor, y: content.minY * scaleFactor,
                        width: content.width * scaleFactor, height: content.height * scaleFactor)
    guard abs(contentScale - 1) < 0.001,
          abs(pixels.width - expected.width) < 1, abs(pixels.height - expected.height) < 1 else {
        throw fail("layout_changed", "Native content scale or extent changed")
    }
    let rounded = CGRect(x: pixels.minX.rounded(), y: pixels.minY.rounded(),
                         width: expected.width, height: expected.height)
    guard rounded.minX >= 0, rounded.minY >= 0,
          rounded.maxX <= CGFloat(width), rounded.maxY <= CGFloat(height) else {
        throw fail("invalid_frame", "Native content lies outside the pixel buffer")
    }
    return rounded
}

@available(macOS 14.0, *)
final class Output: NSObject, SCStreamOutput, SCStreamDelegate {
    let store: FrameStore
    let epoch: Int
    let expected: CGSize

    init(store: FrameStore, epoch: Int, width: Int, height: Int) {
        self.store = store; self.epoch = epoch; self.expected = CGSize(width: width, height: height)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        store.invalidate(nativeFailure(error), epoch: epoch)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        autoreleasepool {
            guard type == .screen else { return }
            guard sample.isValid,
                  let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
                  let rawStatus = attachments.first?[.status] as? Int,
                  let status = SCFrameStatus(rawValue: rawStatus) else {
                store.invalidate(fail("invalid_frame", "Native frame is missing capture status"), epoch: epoch)
                return
            }
            if status == .idle { return }
            guard status == .complete else {
                store.invalidate(fail("frame_unavailable", "Native capture is blank, suspended or stopped"),
                                 epoch: epoch, recoverable: status != .stopped)
                return
            }
            guard let buffer = CMSampleBufferGetImageBuffer(sample),
                  CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_32BGRA else {
                store.invalidate(fail("unsupported_format", "Expected native BGRA pixels"), epoch: epoch)
                return
            }
            guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
                store.invalidate(fail("invalid_frame", "Unable to lock captured pixels"), epoch: epoch)
                return
            }
            defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
            do {
                guard let base = CVPixelBufferGetBaseAddress(buffer) else { throw fail("invalid_frame", "Native frame has no pixels") }
                let width = CVPixelBufferGetWidth(buffer)
                let height = CVPixelBufferGetHeight(buffer)
                guard let info = attachments.first,
                      let rawRect = info[.contentRect] as? [String: Any],
                      let content = CGRect(dictionaryRepresentation: rawRect as CFDictionary),
                      let contentScale = info[.contentScale] as? Double,
                      let scaleFactor = info[.scaleFactor] as? Double else {
                    throw fail("invalid_frame", "Native frame is missing content geometry")
                }
                let crop = try canonicalRect(content: content, contentScale: contentScale, scaleFactor: scaleFactor,
                                             width: width, height: height, expected: expected)
                let stride = CVPixelBufferGetBytesPerRow(buffer)
                try checkedSize(width, height)
                guard stride >= width * 4, stride <= maximumPixels * 4 / height else {
                    throw fail("invalid_stride", "Invalid native pixel stride")
                }
                let pixels = try packedPixels(base.advanced(by: Int(crop.minY) * stride + Int(crop.minX) * 4),
                                              width: Int(crop.width), height: Int(crop.height), stride: stride)
                let presentation = sample.presentationTimeStamp.seconds
                store.accept(pixels, width: Int(crop.width), height: Int(crop.height),
                             sourceTime: presentation.isFinite ? presentation : nil, epoch: epoch)
            } catch {
                store.invalidate(error as? CaptureFailure ?? fail("invalid_frame", "Unable to copy native pixels"), epoch: epoch)
            }
        }
    }
}

@available(macOS 14.0, *)
final class CaptureSession {
    let target: Target
    let sessionID: String
    let maxFPS: Double
    let store = FrameStore()
    private var stream: SCStream?
    private var output: Output?
    private var layout: Geometry?
    private var scale: Double?
    private var processLaunch: ProcessIdentity?
    private let queue = DispatchQueue(label: "vntts.window-capture.frames", qos: .userInitiated)

    init(target: Target, sessionID: String, maxFPS: Double) {
        self.target = target; self.sessionID = sessionID; self.maxFPS = maxFPS
    }

    private func validateProcess() throws {
        guard let pid = target.pid else { return }
        let launch = try processIdentity(pid)
        if let processLaunch, processLaunch != launch {
            throw fail("identity_changed", "Selected process lifetime changed")
        }
        processLaunch = launch
    }

    func open() async throws {
        try validateProcess()
        guard CGPreflightScreenCaptureAccess() else {
            throw fail("permission_denied", "Allow Screen Recording access for the application, then start capture again")
        }
        let snapshot = try geometry(target)
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        let filter: SCContentFilter
        if let windowID = target.windowID {
            guard let window = content.windows.first(where: {
                $0.windowID == windowID && $0.owningApplication?.processID == target.pid
            }) else { throw fail("target_lost", "Selected window is not available to ScreenCaptureKit") }
            filter = SCContentFilter(desktopIndependentWindow: window)
        } else {
            guard let display = content.displays.first(where: { $0.displayID == target.displayID }) else {
                throw fail("target_lost", "Selected display is not available to ScreenCaptureKit")
            }
            filter = SCContentFilter(display: display, excludingWindows: [])
        }
        let pixelScale = Double(filter.pointPixelScale)
        guard pixelScale.isFinite, pixelScale > 0 else { throw fail("invalid_layout", "Native capture has no pixel scale") }
        let width = Int((filter.contentRect.width * pixelScale).rounded())
        let height = Int((filter.contentRect.height * pixelScale).rounded())
        try checkedSize(width, height)
        let config = SCStreamConfiguration()
        config.width = width; config.height = height
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.colorSpaceName = CGColorSpace.sRGB
        config.minimumFrameInterval = CMTime(seconds: 1 / maxFPS, preferredTimescale: 60_000)
        config.queueDepth = 2
        config.showsCursor = false
        config.capturesAudio = false
        config.ignoreShadowsSingleWindow = true
        config.shouldBeOpaque = true
        if #available(macOS 15.0, *) { config.captureDynamicRange = .SDR }
        let nextOutput = Output(store: store, epoch: store.reset(width: width, height: height), width: width, height: height)
        let next = SCStream(filter: filter, configuration: config, delegate: nextOutput)
        try next.addStreamOutput(nextOutput, type: .screen, sampleHandlerQueue: queue)
        stream = next; output = nextOutput; layout = snapshot; scale = displayScale(snapshot.displayID)
        do {
            try await next.startCapture()
            let after = try geometry(target)
            guard snapshot.sameLayout(as: after) else { throw fail("layout_changed", "Window changed while capture started") }
        } catch {
            await close()
            throw error
        }
    }

    func capture(timeout: Double) async throws -> (Frame, [String: Any]) {
        let deadline = clockNow() + timeout
        return try await retryCaptureChanges(deadline: deadline, recover: {
            await self.close()
            try self.validateProcess()
            _ = try geometry(self.target)
        }) {
            try await self.captureStable(deadline: deadline)
        }
    }

    private func captureStable(deadline: Double) async throws -> (Frame, [String: Any]) {
        try validateProcess()
        let before: Geometry
        do { before = try geometry(target) }
        catch { store.invalidate(fail("target_unavailable", "Selected capture target is unavailable")); throw error }
        if layout == nil || !before.sameLayout(as: layout!) || displayScale(before.displayID) != scale {
            await close()
            _ = try remainingTime(until: deadline)
            try await open()
        }
        let frame = try store.capture(deadline: deadline)
        try validateProcess()
        let after: Geometry
        do { after = try geometry(target) }
        catch { store.invalidate(fail("target_unavailable", "Selected capture target is unavailable")); throw error }
        guard let layout, layout.sameLayout(as: after), displayScale(after.displayID) == scale else {
            store.invalidate(fail("layout_changed", "Window changed while acquiring its pixels"))
            throw fail("layout_changed", "Window changed while acquiring its pixels")
        }
        return (frame, [
            "backend": "macos_screencapturekit", "scope": target.windowID == nil ? "display" : "selected_window",
            "session_id": sessionID, "window_id": target.windowID.map { $0 as Any } ?? NSNull(),
            "pid": target.pid.map { $0 as Any } ?? NSNull(), "width": frame.width, "height": frame.height,
            "row_stride": frame.width * 4, "pixel_format": "BGRA8",
            "content_rect": [0, 0, frame.width, frame.height],
            "desktop_bounds": [after.bounds.minX, after.bounds.minY, after.bounds.width, after.bounds.height].map { Int($0.rounded()) },
            "frame_sequence": frame.sequence, "source_time": frame.sourceTime.map { $0 as Any } ?? NSNull(),
            "acquired_at": frame.acquiredAt, "layout_epoch": frame.epoch,
        ])
    }

    func close() async {
        store.invalidate(fail("closed", "Capture session is closed"))
        let previous = stream
        stream = nil; output = nil; layout = nil; scale = nil
        if let previous { try? await previous.stopCapture() }
    }
}

func displayScale(_ display: CGDirectDisplayID) -> Double? {
    guard let mode = CGDisplayCopyDisplayMode(display), mode.width > 0 else { return nil }
    return Double(mode.pixelWidth) / Double(mode.width)
}

func selfTest() async throws {
    let request = try Request(Data(#"{"version":1,"id":1,"op":"capture","session_id":"12345678-1234-1234-1234-123456789abc"}"#.utf8))
    guard request.id == 1 else { throw fail("self_test", "Request identity failed") }
    do {
        _ = try Request(Data(#"{"version":1,"id":1,"op":"capture","session_id":"12345678123412341234123456789abc"}"#.utf8))
        throw fail("self_test", "Noncanonical session identity accepted")
    } catch let error as CaptureFailure where error.code == "invalid_request" {}
    for value in [true, -1, 1.5] as [Any] {
        do { _ = try integer(value, "test"); throw fail("self_test", "Invalid integer accepted") }
        catch let error as CaptureFailure where error.code == "invalid_request" {}
    }
    do { try checkedSize(Int.max, 2); throw fail("self_test", "Unbounded frame accepted") }
    catch let error as CaptureFailure where error.code == "invalid_size" {}
    let padded = Data([1, 2, 3, 4, 0, 0, 0, 0, 5, 6, 7, 8, 0, 0, 0, 0])
    let pixels = try padded.withUnsafeBytes { try packedPixels($0.baseAddress!, width: 1, height: 2, stride: 8) }
    guard pixels == Data([1, 2, 3, 4, 5, 6, 7, 8]) else { throw fail("self_test", "Row padding entered pixels") }
    let store = FrameStore()
    let oldEpoch = store.reset(width: 1, height: 2)
    store.accept(pixels, width: 1, height: 2, sourceTime: 1, epoch: oldEpoch)
    let first = try store.capture(deadline: clockNow() + 0.01)
    guard try store.capture(deadline: clockNow() + 0.01).sequence == first.sequence else { throw fail("self_test", "Static frame changed identity") }
    let newEpoch = store.reset(width: 1, height: 2)
    store.accept(pixels, width: 1, height: 2, sourceTime: 1, epoch: oldEpoch)
    do { _ = try store.capture(deadline: clockNow()); throw fail("self_test", "Old frame crossed layout epoch") }
    catch let error as CaptureFailure where error.code == "frame_timeout" {}
    store.accept(pixels, width: 1, height: 2, sourceTime: 2, epoch: newEpoch)
    store.invalidate(fail("closed", "test"))
    store.accept(pixels, width: 1, height: 2, sourceTime: 3, epoch: newEpoch)
    do { _ = try store.capture(deadline: clockNow()); throw fail("self_test", "Closed frame remained available") }
    catch let error as CaptureFailure where error.code == "closed" {}
    var attempts = 0
    let recovered = try await retryLayoutChanges(deadline: clockNow() + 1) {
        attempts += 1
        if attempts == 1 { throw fail("layout_changed", "test transition") }
        return attempts
    }
    guard recovered == 2 else { throw fail("self_test", "Layout transition did not recover") }
    for code in ["permission_denied", "target_unavailable", "target_lost", "native_error"] {
        attempts = 0
        do {
            let _: Int = try await retryLayoutChanges(deadline: clockNow() + 1) {
                attempts += 1
                throw fail(code, "test terminal failure")
            }
            throw fail("self_test", "Terminal capture failure recovered")
        } catch let error as CaptureFailure where error.code == code {}
        guard attempts == 1 else { throw fail("self_test", "Terminal capture failure retried") }
    }
    attempts = 0
    let deadline = clockNow() + 0.06
    do {
        let _: Int = try await retryLayoutChanges(deadline: deadline) {
            attempts += 1
            throw fail("layout_changed", "test continuing transition")
        }
        throw fail("self_test", "Layout retries exceeded their deadline")
    } catch let error as CaptureFailure where error.code == "frame_timeout" {}
    guard attempts > 0, attempts <= 4 else { throw fail("self_test", "Layout retries did not back off") }
    let stableEpoch = store.reset(width: 1, height: 2)
    store.accept(pixels, width: 1, height: 2, sourceTime: 4, epoch: stableEpoch)
    do {
        _ = try store.capture(deadline: clockNow() - 1)
        throw fail("self_test", "Expired frame deadline returned pixels")
    } catch let error as CaptureFailure where error.code == "frame_timeout" {}
    guard try processIdentity(getpid()) == processIdentity(getpid()) else {
        throw fail("self_test", "Process identity changed within one lifetime")
    }
    let interrupted = NSError(domain: SCStreamErrorDomain,
                              code: SCStreamError.Code.failedApplicationConnectionInterrupted.rawValue)
    let preserved = nativeFailure(interrupted)
    guard preserved.code == "connection_interrupted", preserved.nativeDomain == interrupted.domain,
          preserved.nativeCode == interrupted.code else { throw fail("self_test", "Native error details lost") }
    var recoveries = 0
    attempts = 0
    do {
        let _: Int = try await retryCaptureChanges(deadline: clockNow() + 1, recover: { recoveries += 1 }) {
            attempts += 1
            throw interrupted
        }
        throw fail("self_test", "Second connection interruption recovered")
    } catch let error as CaptureFailure where error.code == "connection_interrupted" {}
    guard attempts == 2, recoveries == 1 else { throw fail("self_test", "Connection recovery was not bounded") }
    for code in ["permission_denied", "target_lost", "native_error"] {
        recoveries = 0
        do {
            let _: Int = try await retryCaptureChanges(deadline: clockNow() + 1, recover: { recoveries += 1 }) {
                throw fail(code, "terminal")
            }
            throw fail("self_test", "Terminal failure recovered")
        } catch let error as CaptureFailure where error.code == code {}
        guard recoveries == 0 else { throw fail("self_test", "Recovered terminal failure") }
    }
    let crop = try canonicalRect(content: CGRect(x: 2, y: 1, width: 2, height: 1), contentScale: 1,
                                 scaleFactor: 2, width: 10, height: 4, expected: CGSize(width: 4, height: 2))
    guard crop == CGRect(x: 4, y: 2, width: 4, height: 2) else { throw fail("self_test", "Content padding crop failed") }
    for scale in [0.5, 2.0] {
        do {
            _ = try canonicalRect(content: CGRect(x: 0, y: 0, width: 2, height: 1), contentScale: scale,
                                  scaleFactor: 2, width: 4, height: 2, expected: CGSize(width: 4, height: 2))
            throw fail("self_test", "Rescaled frame accepted as canonical")
        } catch let error as CaptureFailure where error.code == "layout_changed" {}
    }
    print("macOS capture protocol, stride, frame ownership and layout recovery self-tests passed")
}

@main
struct Main {
    static func main() async {
        signal(SIGPIPE, SIG_IGN)
        if CommandLine.arguments.dropFirst() == ["--self-test"] {
            do { try await selfTest(); return }
            catch { fputs("Capture self-test failed: \(error)\n", stderr); exit(1) }
        }
        guard CommandLine.arguments.count == 1 else { fputs("Unknown capture worker arguments\n", stderr); exit(2) }
        guard #available(macOS 14.0, *) else { fputs("Native capture requires macOS 14 or later\n", stderr); exit(2) }
        let parent = getppid()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { if getppid() != parent { _exit(2) } }
        timer.resume()
        var session: CaptureSession?
        do {
            while let data = try readRequest() {
                var identifier = 0
                do {
                    let request = try Request(data)
                    identifier = request.id
                    if request.operation == "open" {
                        guard session == nil else { throw fail("already_open", "Capture session is already open") }
                        let target = try Target(request.values["target"])
                        let fps = try integer(request.values["max_fps"] ?? 10, "max_fps", minimum: 1, maximum: 60)
                        let opened = CaptureSession(target: target, sessionID: request.sessionID, maxFPS: Double(fps))
                        try await opened.open()
                        session = opened
                        try reply(identifier, result: ["backend": "macos_screencapturekit", "session_id": opened.sessionID, "clock_now": clockNow()])
                    } else {
                        guard let active = session, request.sessionID == active.sessionID else { throw fail("session_mismatch", "No matching capture session") }
                        if request.operation == "close" {
                            await active.close()
                            session = nil
                            try reply(identifier)
                            break
                        }
                        let timeout = try integer(request.values["timeout_ms"] ?? 2000, "timeout_ms", minimum: 1, maximum: 10_000)
                        let (frame, metadata) = try await active.capture(timeout: Double(timeout) / 1000)
                        try reply(identifier, result: metadata, pixels: frame.pixels)
                    }
                } catch {
                    let failure = nativeFailure(error)
                    try reply(identifier, error: failure)
                }
            }
        } catch {
            fputs("Capture transport stopped: \(error)\n", stderr)
        }
        if let session { await session.close() }
        timer.cancel()
    }
}
