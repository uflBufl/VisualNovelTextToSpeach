import AppKit
import Foundation
final class FixtureWindow: NSWindow {
    var allowsFocus = false
    override var canBecomeKey: Bool { allowsFocus }
    override var canBecomeMain: Bool { allowsFocus }
}
final class Scene: NSView {
    let start = ProcessInfo.processInfo.systemUptime
    var resizing = false
    override func draw(_ dirtyRect: NSRect) {
        let elapsed = Int(ProcessInfo.processInfo.systemUptime - start)
        let phase = elapsed < 25 ? 0 : elapsed < 50 ? 1 : 2
        (resizing ? NSColor.green : [NSColor.systemRed, .systemGreen, .systemBlue][phase]).setFill()
        bounds.fill()
        if resizing {
            NSColor.yellow.setStroke()
            let border = NSBezierPath(rect: bounds.insetBy(dx: 6, dy: 6))
            border.lineWidth = 10; border.stroke()
        }
        let text = resizing ? "RESIZE\nsecond \(elapsed)" : "VNTTS recording fixture\n\(["BEGIN", "MIDDLE", "END"][phase])  second \(elapsed)"
        let style = NSMutableParagraphStyle(); style.alignment = .center
        (text as NSString).draw(in: NSRect(x: 10, y: bounds.midY - 50, width: bounds.width - 20, height: 100), withAttributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 34, weight: .bold), .foregroundColor: NSColor.white, .paragraphStyle: style])
    }
}
@main @MainActor struct Main {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        NSWindow.allowsAutomaticWindowTabbing = false
        let resizing = CommandLine.arguments.contains("--resize")
        let window = FixtureWindow(contentRect: NSRect(x: 60, y: 60, width: 640, height: 400), styleMask: resizing ? [.titled, .resizable, .closable] : [.titled], backing: .buffered, defer: false)
        window.allowsFocus = resizing
        window.isRestorable = false
        window.tabbingMode = .disallowed
        window.title = "VNTTS generated recorder fixture"
        let scene = Scene(frame: window.contentView!.bounds)
        scene.resizing = resizing
        window.contentView = scene
        var extraWindows: [NSWindow] = []
        if resizing {
            for frame in [NSScreen.main!.frame, NSRect(x: 100, y: 100, width: 280, height: 200)] {
                let extra = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
                extra.backgroundColor = .magenta; extra.isOpaque = true
                extraWindows.append(extra)
            }
            extraWindows[0].orderFrontRegardless()
            window.collectionBehavior = [.fullScreenPrimary, .fullScreenDisallowsTiling]
        }
        window.orderFrontRegardless()
        print("{\"pid\":\(ProcessInfo.processInfo.processIdentifier),\"window_id\":\(window.windowNumber)}")
        fflush(stdout)
        let timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            let elapsed = ProcessInfo.processInfo.systemUptime - scene.start
            if resizing {
                let second = Int(elapsed)
                let sizes = [4: NSSize(width: 320, height: 200), 8: NSSize(width: 960, height: 600),
                             12: NSSize(width: 350, height: 650), 16: NSSize(width: 900, height: 300),
                             29: NSSize(width: 640, height: 400)]
                if let size = sizes[second] { window.setContentSize(size) }
                if second == 6 { window.setFrameOrigin(NSPoint(x: 200, y: 130)) }
                if second == 17 { extraWindows[1].setFrameOrigin(window.frame.origin); extraWindows[1].orderFrontRegardless() }
                if second == 20 { extraWindows[1].orderOut(nil); window.makeKeyAndOrderFront(nil); app.activate(ignoringOtherApps: true); window.toggleFullScreen(nil) }
                if second == 25 { window.toggleFullScreen(nil) }
            } else {
                let origin = elapsed >= 15 && elapsed < 35 ? NSPoint(x: 140, y: 100) : NSPoint(x: 60, y: 60)
                if window.frame.origin != origin { window.setFrameOrigin(origin) }
            }
            if resizing {
                print("{\"second\":\(Int(elapsed)),\"window_id\":\(window.windowNumber),\"width\":\(window.frame.width),\"height\":\(window.frame.height),\"fullscreen\":\(window.styleMask.contains(.fullScreen))}")
                fflush(stdout)
            }
            scene.needsDisplay = true; scene.display()
        }
        let stop = Timer.scheduledTimer(withTimeInterval: 90, repeats: false) { _ in app.terminate(nil) }
        withExtendedLifetime((window, extraWindows, timer, stop)) { app.run() }
    }
}
