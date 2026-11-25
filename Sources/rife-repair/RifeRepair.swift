// rife-repair: find frames a recorder duplicated because the game dropped a frame,
// and replace each one with a RIFE-interpolated frame. Frame count, timestamps and
// audio are kept as-is, so the repaired file stays in sync with the original.

import ArgumentParser
import AVFoundation
import CoreVideo
import Foundation
import RifeMetal

// MARK: - Frame comparison

/// Splits both frames into a grid of cells and returns the largest per-cell mean
/// absolute difference (0...255). A true duplicate is ~0 in every cell; a real
/// frame where only a small HUD element changed still scores high in that cell,
/// which a whole-frame average would miss.
func frameDifference(_ a: CVPixelBuffer, _ b: CVPixelBuffer) -> Double {
    CVPixelBufferLockBaseAddress(a, .readOnly)
    CVPixelBufferLockBaseAddress(b, .readOnly)
    defer {
        CVPixelBufferUnlockBaseAddress(a, .readOnly)
        CVPixelBufferUnlockBaseAddress(b, .readOnly)
    }
    let w = CVPixelBufferGetWidth(a), h = CVPixelBufferGetHeight(a)
    guard w == CVPixelBufferGetWidth(b), h == CVPixelBufferGetHeight(b),
          let pa = CVPixelBufferGetBaseAddress(a)?.assumingMemoryBound(to: UInt8.self),
          let pb = CVPixelBufferGetBaseAddress(b)?.assumingMemoryBound(to: UInt8.self)
    else { return .infinity }
    let rowA = CVPixelBufferGetBytesPerRow(a), rowB = CVPixelBufferGetBytesPerRow(b)

    let cols = 32, rows = 18
    let step = max(1, min(w, h) / 360)           // sample ~360 lines on the short side
    var sums = [Int](repeating: 0, count: cols * rows)
    var counts = [Int](repeating: 0, count: cols * rows)
    var y = 0
    while y < h {
        let cy = min(rows - 1, y * rows / h)
        let ra = pa + y * rowA, rb = pb + y * rowB
        var x = 0
        while x < w {
            let o = x * 4
            let d = abs(Int(ra[o]) - Int(rb[o]))
                  + abs(Int(ra[o + 1]) - Int(rb[o + 1]))
                  + abs(Int(ra[o + 2]) - Int(rb[o + 2]))
            let c = cy * cols + min(cols - 1, x * cols / w)
            sums[c] += d
            counts[c] += 3
            x += step
        }
        y += step
    }
    var worst = 0.0
    for i in 0..<sums.count where counts[i] > 0 {
        worst = max(worst, Double(sums[i]) / Double(counts[i]))
    }
    return worst
}

// MARK: - Helpers

func fmtTime(_ t: CMTime) -> String {
    let s = max(0, t.seconds)
    return String(format: "%d:%02d.%03d", Int(s) / 60, Int(s) % 60, Int((s - floor(s)) * 1000))
}

func fourCC(_ v: FourCharCode) -> String {
    let bytes = [24, 16, 8, 0].map { UInt8((v >> $0) & 0xFF) }
    return String(bytes: bytes, encoding: .ascii)?.trimmingCharacters(in: .whitespaces) ?? "?"
}

final class Progress {
    /// Set by the hidden --machine-progress flag (used by the Frame Repair app):
    /// progress goes to stdout as "@progress <phase> <done> <total>" lines.
    nonisolated(unsafe) static var machine = false
    private let phase: String
    private var last = Date.distantPast
    init(_ phase: String) { self.phase = phase }
    func tick(_ done: Int, _ total: Int, _ extra: String) {
        let now = Date()
        guard now.timeIntervalSince(last) > (Progress.machine ? 0.25 : 1) else { return }
        last = now
        if Progress.machine { print("@progress \(phase) \(done) \(total)"); return }
        let pct = total > 0 ? min(100, done * 100 / total) : 0
        FileHandle.standardError.write("\r  \(pct)%  frame \(done)/\(total)  \(extra)    ".data(using: .utf8)!)
    }
    func done() {
        if Progress.machine { print("@progress \(phase) 1 1"); return }
        FileHandle.standardError.write("\n".data(using: .utf8)!)
    }
}

/// RIFE_REPAIR_TIMING=1: wall-clock totals per stage, printed at the end.
enum Clock {
    nonisolated(unsafe) static let on = ProcessInfo.processInfo.environment["RIFE_REPAIR_TIMING"] == "1"
    nonisolated(unsafe) static var totals: [String: Double] = [:]
    static func time<T>(_ key: String, _ body: () throws -> T) rethrows -> T {
        guard on else { return try body() }
        let t0 = DispatchTime.now().uptimeNanoseconds
        defer { totals[key, default: 0] += Double(DispatchTime.now().uptimeNanoseconds - t0) / 1e9 }
        return try body()
    }
    static func report() {
        guard on else { return }
        print("Timing:")
        for (k, v) in totals.sorted(by: { $0.value > $1.value }) { print(String(format: "  %-16@ %7.2f s", k as NSString, v)) }
    }
}

enum Codec: String, ExpressibleByArgument, CaseIterable {
    case auto, hevc, h264, prores
}

struct RepairError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

// MARK: - Command

@main
struct RifeRepair: AsyncParsableCommand {
    static let configuration = CommandConfiguration(
        commandName: "rife-repair",
        abstract: "Replace duplicated (dropped) frames in a recording with RIFE-interpolated frames.",
        discussion: """
        A game that drops a frame leaves the recorder showing the previous frame again.
        This finds those repeated frames and replaces them with frames generated between
        the last real frame and the next one. Timestamps, frame count and audio are
        unchanged. Run with --dry-run first to see what it finds.
        """
    )

    @Argument(help: "Input video (.mov / .mp4).")
    var input: String

    @Option(name: .shortAndLong, help: "Output file. Default: <input>-repaired.<ext>.")
    var output: String?

    @Flag(name: .long, help: "Only analyse: list duplicate runs and the difference histogram. Writes nothing.")
    var dryRun = false

    @Option(name: .long, help: "A frame counts as a duplicate when its worst-cell difference from the last real frame is below this (0-255 scale).")
    var threshold: Double = 1.5

    @Option(name: .long, help: "Longest run of duplicates to repair. Longer runs are real freezes (pause, loading) and are left alone.")
    var maxGap: Int = 4

    @Option(name: .long, help: "RIFE quality: hq | balanced | fast. Default: hq, or balanced with --shutter (blur hides the difference and it is ~4x faster).")
    var tier: String?

    @Option(name: .long, help: """
        Add camera motion blur: shutter angle in degrees (180 = classic film look, 1/48 s at 24 fps). \
        Each output frame averages several RIFE-generated moments across the exposure. \
        Use with --fps, e.g. --fps 24 --shutter 180.
        """)
    var shutter: Double?

    @Option(name: .long, help: "Moments averaged per output frame with --shutter. Default: one per 1/240 s of exposure (5 for 180 degrees at 24 fps).")
    var samples: Int?

    @Option(name: .long, help: "Output codec: auto (ProRes if the source is ProRes, else HEVC) | hevc | h264 | prores.")
    var codec: Codec = .auto

    @Option(name: .long, help: "Video bitrate in Mbps for hevc/h264. Default: 1.25x the source.")
    var bitrate: Double?

    @Option(name: .shortAndLong, help: "Path to a .rmw weight file. Default: the bundled rife-v4.25 weights.")
    var model: String?

    @Flag(name: [.customShort("y"), .long], help: "Overwrite the output file if it exists.")
    var overwrite = false

    @Flag(name: .long, help: """
        Retime mode for games running below / unsteady vs the recording rate. Estimates when each \
        real game frame was actually rendered and rebuilds EVERY output frame on an even grid, so \
        motion advances the same amount each frame. Slower (most frames go through RIFE).
        """)
    var retime = false

    @Flag(name: .long, help: .hidden)
    var machineProgress = false

    @Option(name: .long, help: "Retime smoothing: real frames on each side used to estimate render timing.")
    var smooth: Int = 3

    @Option(name: .customLong("fps"), help: """
        Output frame rate, e.g. 60 or 59.94. Turns a 30 fps file into 60 fps in one step. \
        Uses the retime engine: repeats are removed, timing is evened out, and every output \
        frame is placed on an even grid at this rate. Length and audio are unchanged.
        """)
    var targetFps: Double?

    /// Output ticks on an even grid at `rate` covering [start, start + duration).
    /// Exact rational timestamps for whole and NTSC (x/1.001) rates.
    static func grid(start: CMTime, duration: Double, rate: Double) -> [CMTime] {
        let count = Int(duration * rate + 1e-6)
        let whole = (rate - rate.rounded()).magnitude < 0.001
        let ntsc = (rate * 1.001 - (rate * 1.001).rounded()).magnitude < 0.01
        return (0..<count).map { i in
            let off: CMTime
            if whole {
                off = CMTime(value: CMTimeValue(i), timescale: CMTimeScale(rate.rounded()))
            } else if ntsc {
                off = CMTime(value: CMTimeValue(i) * 1001, timescale: CMTimeScale((rate * 1.001).rounded()) * 1000)
            } else {
                off = CMTime(seconds: Double(i) / rate, preferredTimescale: 600_000)
            }
            return CMTimeAdd(start, off)
        }
    }

    func run() async throws {
        setvbuf(stdout, nil, _IOLBF, 0)          // line-buffered, so a parent app sees lines live
        Progress.machine = machineProgress
        let inURL = URL(fileURLWithPath: (input as NSString).expandingTildeInPath)
        guard FileManager.default.fileExists(atPath: inURL.path) else { throw RepairError("No such file: \(inURL.path)") }

        let asset = AVURLAsset(url: inURL)
        guard let vTrack = try await asset.loadTracks(withMediaType: .video).first else {
            throw RepairError("No video track in \(inURL.lastPathComponent)")
        }
        let aTracks = try await asset.loadTracks(withMediaType: .audio)
        let (size, transform, fps, dataRate, timeRange, formats) = try await vTrack.load(
            .naturalSize, .preferredTransform, .nominalFrameRate, .estimatedDataRate, .timeRange, .formatDescriptions)
        let duration = timeRange.duration.seconds
        let estFrames = Int((duration * Double(fps)).rounded())
        let srcCodec = formats.first.map { CMFormatDescriptionGetMediaSubType($0) } ?? 0
        let srcIsProRes = fourCC(srcCodec).hasPrefix("ap") || fourCC(srcCodec).hasPrefix("aprh")
        var tenBit = false
        if let f = formats.first, let ext = CMFormatDescriptionGetExtensions(f) as? [String: Any],
           let depth = ext["BitsPerComponent"] as? Int, depth > 8 { tenBit = true }

        print("Input:  \(inURL.lastPathComponent)")
        print(String(format: "        %dx%d  %.3f fps  %@  %.1f Mbps  %@  ~%d frames  %d audio track(s)",
                     Int(size.width), Int(size.height), fps, fourCC(srcCodec), Double(dataRate) / 1e6,
                     fmtTime(timeRange.duration), estFrames, aTracks.count))
        if tenBit {
            print("Note:   source is >8-bit. RIFE works in 8-bit, so the output is 8-bit (HDR, if any, becomes SDR).")
        }

        // Keep the source's colour standard: older OBS defaults to BT.601, newer to
        // BT.709. Tagging a 601 source as 709 shifts colours. HDR / wide-gamut
        // values fall back to BT.709 because the pipeline is 8-bit SDR.
        func ext(_ key: CFString) -> String? {
            formats.first.flatMap { CMFormatDescriptionGetExtension($0, extensionKey: key) as? String }
        }
        let srcMatrix = ext(kCMFormatDescriptionExtension_YCbCrMatrix)
        let matrix = [AVVideoYCbCrMatrix_ITU_R_601_4, AVVideoYCbCrMatrix_ITU_R_709_2].contains(srcMatrix ?? "")
            ? srcMatrix! : AVVideoYCbCrMatrix_ITU_R_709_2
        let srcPrim = ext(kCMFormatDescriptionExtension_ColorPrimaries)
        let primaries = [AVVideoColorPrimaries_ITU_R_709_2, AVVideoColorPrimaries_SMPTE_C, "EBU_3213"].contains(srcPrim ?? "")
            ? srcPrim! : (matrix == AVVideoYCbCrMatrix_ITU_R_601_4 ? AVVideoColorPrimaries_SMPTE_C : AVVideoColorPrimaries_ITU_R_709_2)
        let colorProps: [String: String] = [
            AVVideoColorPrimariesKey: primaries,
            AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
            AVVideoYCbCrMatrixKey: matrix,
        ]
        let colorNote = matrix == AVVideoYCbCrMatrix_ITU_R_601_4 ? "BT.601" : "BT.709"

        if dryRun {
            try analyse(asset: asset, track: vTrack, estFrames: estFrames, fps: fps)
            return
        }

        // Output settings
        let outURL: URL = {
            if let o = output { return URL(fileURLWithPath: (o as NSString).expandingTildeInPath) }
            let ext = inURL.pathExtension.isEmpty ? "mov" : inURL.pathExtension
            return inURL.deletingLastPathComponent()
                .appendingPathComponent(inURL.deletingPathExtension().lastPathComponent + "-repaired.\(ext)")
        }()
        if FileManager.default.fileExists(atPath: outURL.path) {
            guard overwrite else { throw RepairError("\(outURL.path) exists. Pass -y to overwrite.") }
            try FileManager.default.removeItem(at: outURL)
        }
        let fileType: AVFileType = outURL.pathExtension.lowercased() == "mp4" ? .mp4 : .mov

        if let s = shutter, !(s > 0 && s <= 360) { throw RepairError("--shutter must be between 1 and 360 degrees") }
        if let n = samples, !(n >= 1 && n <= 64) { throw RepairError("--samples must be between 1 and 64") }
        guard let qTier = RifeQualityTier(rawValue: tier ?? (shutter != nil ? "balanced" : "hq")) else { throw RepairError("--tier must be hq, balanced or fast") }
        let config: RifeConfiguration
        if let m = model {
            config = RifeConfiguration(modelURL: URL(fileURLWithPath: (m as NSString).expandingTildeInPath), qualityTier: qTier)
        } else {
            config = .bundled(qualityTier: qTier)
        }
        let rife = try RifeInterpolator(configuration: config)

        if let r = targetFps, !(r >= 1 && r <= 240) { throw RepairError("--fps must be between 1 and 240") }
        let useRetime = retime || targetFps != nil || shutter != nil
        let useCodec: Codec = codec == .auto ? (srcIsProRes ? .prores : .hevc) : codec
        let w = Int(size.width), h = Int(size.height)
        var videoSettings: [String: Any] = [
            AVVideoWidthKey: w,
            AVVideoHeightKey: h,
            AVVideoColorPropertiesKey: colorProps,
        ]
        switch useCodec {
        case .prores:
            videoSettings[AVVideoCodecKey] = AVVideoCodecType.proRes422HQ
        case .hevc, .h264, .auto:
            videoSettings[AVVideoCodecKey] = useCodec == .h264 ? AVVideoCodecType.h264 : AVVideoCodecType.hevc
            let bps = bitrate.map { $0 * 1e6 } ?? max(Double(dataRate) * 1.25, 8e6)
            videoSettings[AVVideoCompressionPropertiesKey] = [
                AVVideoAverageBitRateKey: Int(bps),
                AVVideoExpectedSourceFrameRateKey: Int((targetFps ?? Double(fps)).rounded()),
            ]
        }

        // Reader
        let reader = try AVAssetReader(asset: asset)
        let vOut = AVAssetReaderTrackOutput(track: vTrack, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferMetalCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
        ])
        vOut.alwaysCopiesSampleData = false
        reader.add(vOut)
        var aOuts: [AVAssetReaderTrackOutput] = []
        for t in aTracks {
            let o = AVAssetReaderTrackOutput(track: t, outputSettings: nil)   // passthrough
            reader.add(o)
            aOuts.append(o)
        }

        // Writer
        let writer = try AVAssetWriter(outputURL: outURL, fileType: fileType)
        let vIn = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        vIn.transform = transform
        vIn.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: vIn, sourcePixelBufferAttributes: nil)
        writer.add(vIn)
        var aIns: [AVAssetWriterInput] = []
        for t in aTracks {
            let hint = try await t.load(.formatDescriptions).first
            let i = AVAssetWriterInput(mediaType: .audio, outputSettings: nil, sourceFormatHint: hint)
            i.expectsMediaDataInRealTime = false
            guard writer.canAdd(i) else { throw RepairError("Can't pass this audio track through into .\(outURL.pathExtension); try a .mov output.") }
            writer.add(i)
            aIns.append(i)
        }

        var retimePlan: RetimePlan?
        if useRetime {
            print("Pass 1/2: finding real game frames ...")
            let (pts, unique) = try Clock.time("pass 1 scan") { try scan(asset: asset, track: vTrack, estFrames: estFrames) }
            let plan = buildRetimePlan(pts: pts, unique: unique,
                                       outFps: targetFps ?? (shutter != nil ? Double(fps) : nil))
            print(plan.summary)
            retimePlan = plan
            print("Pass 2/2: rebuilding frames ...")
        }

        guard reader.startReading() else { throw RepairError("Reader failed: \(reader.error?.localizedDescription ?? "?")") }
        guard writer.startWriting() else { throw RepairError("Writer failed: \(writer.error?.localizedDescription ?? "?")") }
        writer.startSession(atSourceTime: timeRange.start)

        print("Output: \(outURL.lastPathComponent)  (\(useCodec.rawValue) \(colorNote), RIFE \(qTier.rawValue), threshold \(threshold), max gap \(maxGap)\(useRetime ? ", retime" : "")\(targetFps.map { String(format: ", %g fps", $0) } ?? ""))")
        let started = Date()

        // Audio: copy straight through on its own queue so the writer can interleave.
        let audioGroup = DispatchGroup()
        for (o, i) in zip(aOuts, aIns) {
            audioGroup.enter()
            let q = DispatchQueue(label: "audio.\(i.hash)")
            i.requestMediaDataWhenReady(on: q) {
                while i.isReadyForMoreMediaData {
                    if let s = o.copyNextSampleBuffer() {
                        if !i.append(s) { i.markAsFinished(); audioGroup.leave(); return }
                    } else {
                        i.markAsFinished(); audioGroup.leave(); return
                    }
                }
            }
        }

        // Video
        let stats: Stats
        if let plan = retimePlan {
            stats = try processRetime(plan: plan, vOut: vOut, vIn: vIn, adaptor: adaptor, writer: writer, rife: rife)
        } else {
            stats = try processVideo(vOut: vOut, vIn: vIn, adaptor: adaptor, writer: writer,
                                     rife: rife, estFrames: estFrames)
        }

        vIn.markAsFinished()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            audioGroup.notify(queue: .global()) { c.resume() }
        }
        await writer.finishWriting()
        if writer.status != .completed {
            throw RepairError("Writing failed: \(writer.error?.localizedDescription ?? "unknown")")
        }
        if reader.status == .failed {
            throw RepairError("Reading failed: \(reader.error?.localizedDescription ?? "unknown")")
        }

        let secs = Date().timeIntervalSince(started)
        print("Done in \(String(format: "%.1f", secs)) s (\(String(format: "%.1f", Double(stats.frames) / max(secs, 0.001))) fps)")
        print("  frames written:        \(stats.frames)")
        if let plan = retimePlan, plan.group > 1 {
            print("  motion blur:           \(String(format: "%g", shutter ?? 0)) degree shutter, \(plan.group) moments per frame (\(stats.fixedFrames) of \(plan.steps.count) generated by RIFE)")
        } else if useRetime {
            print("  generated by RIFE:     \(stats.fixedFrames)   kept as-is: \(stats.frames - stats.fixedFrames - stats.freezes)")
        } else {
            print("  dropped frames fixed:  \(stats.fixedFrames) in \(stats.fixedRuns) spot(s)")
        }
        if stats.freezes > 0 {
            print("  freezes left alone:    \(stats.freezes) (runs longer than --max-gap \(maxGap))")
        }
        if stats.trailing > 0 {
            print("  trailing repeats kept: \(stats.trailing) (no next frame to interpolate toward)")
        }
        print("  -> \(outURL.path)")
        Clock.report()
        if Progress.machine { print("@output \(outURL.path)") }
    }

    struct Stats { var frames = 0, fixedFrames = 0, fixedRuns = 0, freezes = 0, trailing = 0 }

    func processVideo(vOut: AVAssetReaderTrackOutput, vIn: AVAssetWriterInput,
                      adaptor: AVAssetWriterInputPixelBufferAdaptor, writer: AVAssetWriter,
                      rife: RifeInterpolator, estFrames: Int) throws -> Stats {
        var st = Stats()
        let progress = Progress("render")

        func append(_ pb: CVPixelBuffer, _ t: CMTime) throws {
            while !vIn.isReadyForMoreMediaData {
                if writer.status == .failed { throw RepairError("Writer failed: \(writer.error?.localizedDescription ?? "?")") }
                usleep(500)
            }
            guard adaptor.append(pb, withPresentationTime: t) else {
                throw RepairError("Append failed at \(fmtTime(t)): \(writer.error?.localizedDescription ?? "?")")
            }
            st.frames += 1
        }

        var anchor: (pb: CVPixelBuffer, t: CMTime)?
        var pending: [CMTime] = []   // timestamps of repeated frames after the anchor

        while let sample = vOut.copyNextSampleBuffer() {
            guard let pb = CMSampleBufferGetImageBuffer(sample) else { continue }
            let t = CMSampleBufferGetPresentationTimeStamp(sample)
            progress.tick(st.frames + pending.count, estFrames, "fixed \(st.fixedFrames)")

            guard let a = anchor else {
                try append(pb, t); anchor = (pb, t); continue
            }
            if frameDifference(a.pb, pb) < threshold {
                pending.append(t); continue
            }

            // pb is the next real frame: fill the repeats between the anchor and it.
            // autoreleasepool: Metal/AVFoundation temporaries are freed per frame,
            // not at process exit (a CLI loop has no run loop to drain them).
            try autoreleasepool {
                if !pending.isEmpty {
                    if pending.count <= maxGap {
                        let span = (t - a.t).seconds
                        let steps = pending.map { Float(($0 - a.t).seconds / span) }
                        let frames = try rife.interpolate(previous: a.pb, current: pb, timesteps: steps)
                        for (f, pt) in zip(frames, pending) { try append(f, pt) }
                        st.fixedFrames += pending.count
                        st.fixedRuns += 1
                    } else {
                        for pt in pending { try append(a.pb, pt) }
                        st.freezes += pending.count
                    }
                    pending.removeAll()
                }
                try append(pb, t)
            }
            anchor = (pb, t)
        }
        if let a = anchor {
            for pt in pending { try append(a.pb, pt) }
            st.trailing = pending.count
        }
        progress.done()
        return st
    }

    // MARK: Retime

    /// What to show at each output frame: a real game frame as-is, or a RIFE blend
    /// between real frames k and k+1 at position s.
    enum Step { case copy(Int), blend(Int, Float) }

    struct RetimePlan {
        var steps: [Step]           // one per sampled moment: `group` consecutive steps make one output frame
        var pts: [CMTime]           // output frame timestamps (input timestamps unless --fps)
        var group: Int              // moments averaged per output frame (1 = no motion blur)
        var uniqueIndex: [Int]      // input frame -> real-frame index k, or -1 for a repeat
        var summary: String
    }

    /// Pass 1: decode every frame and mark which ones are new game frames.
    func scan(asset: AVURLAsset, track: AVAssetTrack, estFrames: Int) throws -> ([CMTime], [Bool]) {
        let reader = try AVAssetReader(asset: asset)
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ])
        out.alwaysCopiesSampleData = false
        reader.add(out)
        guard reader.startReading() else { throw RepairError("Reader failed: \(reader.error?.localizedDescription ?? "?")") }
        var pts: [CMTime] = [], unique: [Bool] = []
        var anchor: CVPixelBuffer?
        let progress = Progress("scan")
        while let s = out.copyNextSampleBuffer() {
            guard let pb = CMSampleBufferGetImageBuffer(s) else { continue }
            pts.append(CMSampleBufferGetPresentationTimeStamp(s))
            progress.tick(pts.count, estFrames, "")
            if let a = anchor, frameDifference(a, pb) < threshold {
                unique.append(false)
            } else {
                unique.append(true)
                anchor = pb
            }
        }
        progress.done()
        if reader.status == .failed { throw RepairError("Reading failed: \(reader.error?.localizedDescription ?? "?")") }
        return (pts, unique)
    }

    /// Estimate when each real game frame was rendered, then decide what every output
    /// tick shows.
    ///
    /// The recorder grabs a picture every tick and shows the newest game frame, so a
    /// frame first seen at capture time c was rendered somewhere in (c - tick, c].
    /// Using c as-is leaves up to a tick of timing error per frame, which is the judder
    /// you see when a game runs at e.g. 45 fps in a 60 fps recording. We fit a local
    /// straight line through the capture times of nearby real frames (the game's local
    /// frame rate), clamp each estimate into that physical window, and keep them in order.
    /// Runs of repeats longer than --max-gap are treated as real freezes: no smoothing
    /// or blending across them.
    func buildRetimePlan(pts: [CMTime], unique: [Bool], outFps: Double? = nil) -> RetimePlan {
        let secs = pts.map(\.seconds)
        var gaps = zip(secs.dropFirst(), secs).map { $0 - $1 }.filter { $0 > 0 }.sorted()
        if gaps.isEmpty { gaps = [1.0 / 60] }
        let tick = gaps[gaps.count / 2]

        var cap: [Double] = [], seg: [Int] = []
        var uniqueIndex = [Int](repeating: -1, count: pts.count)
        var segId = 0, run = 0, freezeFrames = 0
        for i in pts.indices {
            if unique[i] {
                if run > maxGap { segId += 1; freezeFrames += run }
                run = 0
                uniqueIndex[i] = cap.count
                cap.append(secs[i]); seg.append(segId)
            } else { run += 1 }
        }
        let n = cap.count

        // Local linear fit of capture time vs real-frame index, within the segment.
        var t = cap
        let w = max(1, smooth)
        for k in 0..<n {
            var lo = max(0, k - w), hi = min(n - 1, k + w)
            while seg[lo] != seg[k] { lo += 1 }
            while seg[hi] != seg[k] { hi -= 1 }
            let m = Double(hi - lo + 1)
            guard m >= 3 else { continue }
            var sx = 0.0, sy = 0.0, sxx = 0.0, sxy = 0.0
            for j in lo...hi {
                let x = Double(j - k), y = cap[j]
                sx += x; sy += y; sxx += x * x; sxy += x * y
            }
            let den = m * sxx - sx * sx
            let slope = den != 0 ? (m * sxy - sx * sy) / den : 0
            let est = (sy - slope * sx) / m          // value of the fit at x = 0 (this frame)
            t[k] = min(cap[k], max(cap[k] - tick, est))
        }
        for k in 1..<max(1, n) where seg[k] == seg[k - 1] {
            t[k] = min(cap[k], max(t[k], t[k - 1] + 0.05 * tick))
        }

        // Output ticks: the input timestamps, or an even grid at --fps over the same span.
        let outPts: [CMTime]
        if let r = outFps, let first = pts.first, let last = secs.last {
            outPts = Self.grid(start: first, duration: last - secs[0] + tick, rate: r)
        } else {
            outPts = pts
        }

        // Moments to render. Without --shutter: one per output frame, at its timestamp.
        // With --shutter: `group` moments spread across the exposure, which opens at the
        // frame's timestamp and lasts (angle / 360) of a frame; they get averaged later.
        var group = 1
        var moments: [Double] = []
        if let angle = shutter {
            let rate = outFps ?? 1 / tick
            let exposure = angle / 360 / rate
            group = samples ?? max(2, Int((exposure * 240 - 1e-9).rounded(.up)))
            moments.reserveCapacity(outPts.count * group)
            for tp in outPts {
                for i in 0..<group { moments.append(tp.seconds + Double(i) * exposure / Double(group)) }
            }
        } else {
            moments = outPts.map(\.seconds)
        }

        // Each moment shows the content time of that moment.
        var steps: [Step] = []
        steps.reserveCapacity(moments.count)
        var k = 0, blends = 0
        for T in moments {
            while k + 1 < n && t[k + 1] <= T { k += 1 }
            if n == 0 { break }
            if T <= t[k] || k + 1 >= n || seg[k + 1] != seg[k] {
                steps.append(.copy(k)); continue
            }
            let s = (T - t[k]) / (t[k + 1] - t[k])
            if s < 0.02 { steps.append(.copy(k)) }
            else if s > 0.98 { steps.append(.copy(k + 1)) }
            else { steps.append(.blend(k, Float(s))); blends += 1 }
        }

        // Game frame-rate estimate per second of recording.
        var perSec: [Int: Int] = [:]
        for c in cap { perSec[Int(c), default: 0] += 1 }
        let rates = perSec.keys.sorted().dropLast().map { perSec[$0]! }.sorted()   // last second is partial
        let rateText = rates.isEmpty ? "n/a"
            : "min \(rates.first!)  median \(rates[rates.count / 2])  max \(rates.last!)"
        let summary = """
          real game frames: \(n) of \(pts.count)   game fps per second: \(rateText)
          plan: \(outPts.count) output frames\(group > 1 ? " x \(group) moments each for motion blur" : ""): \(blends) generated, \(moments.count - blends) kept\(freezeFrames > 0 ? ", \(freezeFrames) in freezes left alone" : "")
        """
        return RetimePlan(steps: steps, pts: outPts, group: group, uniqueIndex: uniqueIndex, summary: summary)
    }

    /// Pass 2: decode again, keep the few real frames the plan still needs, and emit
    /// every tick in order. Consecutive ticks between the same pair go to RIFE as one call.
    func processRetime(plan: RetimePlan, vOut: AVAssetReaderTrackOutput, vIn: AVAssetWriterInput,
                       adaptor: AVAssetWriterInputPixelBufferAdaptor, writer: AVAssetWriter,
                       rife: RifeInterpolator) throws -> Stats {
        var st = Stats()
        let progress = Progress("render")
        var store: [Int: CVPixelBuffer] = [:]
        var have = -1          // highest real-frame index decoded so far
        var next = 0           // next tick to emit
        var input = 0          // input frame counter

        func append(_ pb: CVPixelBuffer, _ t: CMTime) throws {
            try Clock.time("encode") {
                while !vIn.isReadyForMoreMediaData {
                    if writer.status == .failed { throw RepairError("Writer failed: \(writer.error?.localizedDescription ?? "?")") }
                    usleep(500)
                }
                guard adaptor.append(pb, withPresentationTime: t) else {
                    throw RepairError("Append failed at \(fmtTime(t)): \(writer.error?.localizedDescription ?? "?")")
                }
            }
            st.frames += 1
        }

        // RIFE through a stream: real frames are encoded once and cached, and all
        // moments between a pair render in one GPU submission. Pairs come in order,
        // so the cached frame is usually already the left side of the next pair;
        // otherwise one cheap "rebase" push sets it.
        var stream: RifeStream?
        var streamAt = -1        // real-frame index currently cached in the stream
        func interpolate(_ k: Int, _ ts: [Float]) throws -> [CVPixelBuffer] {
            try Clock.time("rife") {
                let a = store[k]!, b = store[k + 1]!
                if stream == nil {
                    stream = try rife.makeStream(width: CVPixelBufferGetWidth(a), height: CVPixelBufferGetHeight(a))
                }
                if streamAt != k { _ = try stream!.push(a, timesteps: []) }
                let out = try stream!.push(b, timesteps: ts)
                streamAt = k + 1
                return out
            }
        }
        // Without blur each moment is an output frame; with blur, every `group`
        // consecutive moments are averaged into one.
        let averager = plan.group > 1 ? FrameAverager() : nil
        var pendingMoments: [CVPixelBuffer] = []
        func deliver(_ pb: CVPixelBuffer, _ index: Int) throws {
            guard let avg = averager else { try append(pb, plan.pts[index]); return }
            pendingMoments.append(pb)
            if pendingMoments.count == plan.group {
                let blurred = try Clock.time("blur average") { try avg.average(pendingMoments) }
                try append(blurred, plan.pts[index / plan.group])
                pendingMoments.removeAll(keepingCapacity: true)
            }
        }
        func needs(_ s: Step) -> Int { if case .blend(let k, _) = s { return k + 1 }; if case .copy(let k) = s { return k }; return 0 }
        func low(_ s: Step) -> Int { switch s { case .copy(let k): return k; case .blend(let k, _): return k } }

        func emitReady() throws {
            while next < plan.steps.count && needs(plan.steps[next]) <= have {
                switch plan.steps[next] {
                case .copy(let k):
                    try deliver(store[k]!, next); next += 1
                case .blend(let k, _):
                    var ts: [Float] = [], at: [Int] = []
                    while next < plan.steps.count, case .blend(let k2, let s) = plan.steps[next], k2 == k {
                        ts.append(s); at.append(next); next += 1
                    }
                    let frames = try interpolate(k, ts)
                    for (f, i) in zip(frames, at) { try deliver(f, i) }
                    st.fixedFrames += frames.count
                }
                if next < plan.steps.count {
                    let keep = low(plan.steps[next])
                    for key in store.keys where key < keep { store[key] = nil }
                }
            }
        }

        while let sample = Clock.time("decode", { vOut.copyNextSampleBuffer() }) {
            guard let pb = CMSampleBufferGetImageBuffer(sample) else { continue }
            if input < plan.uniqueIndex.count, plan.uniqueIndex[input] >= 0 {
                have = plan.uniqueIndex[input]
                store[have] = pb
            }
            input += 1
            try autoreleasepool { try emitReady() }
            progress.tick(st.frames, plan.pts.count, "generated \(st.fixedFrames)")
        }
        try emitReady()
        progress.done()
        if next != plan.steps.count {
            throw RepairError("Second read returned a different frame sequence (\(input) frames vs \(plan.uniqueIndex.count)).")
        }
        return st
    }

    /// --dry-run: report duplicate runs and the distribution of frame differences.
    func analyse(asset: AVURLAsset, track: AVAssetTrack, estFrames: Int, fps: Float) throws {
        let reader = try AVAssetReader(asset: asset)
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ])
        out.alwaysCopiesSampleData = false
        reader.add(out)
        guard reader.startReading() else { throw RepairError("Reader failed: \(reader.error?.localizedDescription ?? "?")") }

        let edges: [Double] = [0.1, 0.5, 1, 1.5, 2, 3, 5, 10]
        var hist = [Int](repeating: 0, count: edges.count + 1)
        var runs: [(start: CMTime, len: Int)] = []
        var anchor: CVPixelBuffer?
        var runStart: CMTime?, runLen = 0, n = 0
        var uniquePerSec: [Int: Int] = [:]
        let progress = Progress("analyse")

        while let s = out.copyNextSampleBuffer() {
            guard let pb = CMSampleBufferGetImageBuffer(s) else { continue }
            let t = CMSampleBufferGetPresentationTimeStamp(s)
            n += 1
            progress.tick(n, estFrames, "runs \(runs.count)")
            guard let a = anchor else { anchor = pb; uniquePerSec[Int(t.seconds), default: 0] += 1; continue }
            let d = frameDifference(a, pb)
            hist[edges.firstIndex(where: { d < $0 }) ?? edges.count] += 1
            if d < threshold {
                if runLen == 0 { runStart = t }
                runLen += 1
            } else {
                if runLen > 0, let rs = runStart { runs.append((rs, runLen)) }
                runLen = 0
                anchor = pb
                uniquePerSec[Int(t.seconds), default: 0] += 1
            }
        }
        if runLen > 0, let rs = runStart { runs.append((rs, runLen)) }
        progress.done()

        print("\nFrame-to-frame difference (worst cell, 0-255) — duplicates sit near 0:")
        var lo = 0.0
        let total = max(1, hist.reduce(0, +))
        for (i, c) in hist.enumerated() {
            let label = i < edges.count ? String(format: "%5.1f - %-5.1f", lo, edges[i]) : String(format: "%5.1f +      ", lo)
            let bar = String(repeating: "#", count: c * 50 / total)
            print("  \(label) \(String(format: "%7d", c))  \(bar)")
            if i < edges.count { lo = edges[i] }
        }

        let rates = uniquePerSec.keys.sorted().dropLast().map { uniquePerSec[$0]! }.sorted()
        if !rates.isEmpty {
            let full = Int((Double(fps) * 0.95).rounded(.down))
            let below = rates.filter { $0 < full }.count
            print("\nGame frame rate (real frames per second of recording): min \(rates.first!)  median \(rates[rates.count / 2])  max \(rates.last!)")
            print("  seconds under \(full) fps: \(below) of \(rates.count)" +
                  (below * 5 > rates.count ? "  -> unsteady/low: use --retime for even motion" : "  -> mostly full rate: default fill mode is enough"))
        }

        let fixable = runs.filter { $0.len <= maxGap }
        let freezes = runs.filter { $0.len > maxGap }
        print("\nBelow threshold \(threshold): \(runs.reduce(0) { $0 + $1.len }) repeated frames in \(runs.count) run(s)")
        print("  would repair: \(fixable.reduce(0) { $0 + $1.len }) frames in \(fixable.count) run(s) (length <= \(maxGap))")
        print("  would leave:  \(freezes.reduce(0) { $0 + $1.len }) frames in \(freezes.count) freeze(s)")
        var lens: [Int: Int] = [:]
        for r in runs { lens[r.len, default: 0] += 1 }
        if !lens.isEmpty {
            print("  run lengths:  " + lens.keys.sorted().prefix(12).map { "\($0)x\(lens[$0]!)" }.joined(separator: "  "))
        }
        if !runs.isEmpty {
            print("\nFirst runs (time, repeated frames):")
            for r in runs.prefix(25) {
                print("  \(fmtTime(r.start))  \(r.len)\(r.len > maxGap ? "  (freeze, left alone)" : "")")
            }
            if runs.count > 25 { print("  ... \(runs.count - 25) more") }
        }
    }
}
