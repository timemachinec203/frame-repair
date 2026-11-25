import Foundation

/// Everything the user can choose in the app, turned into rife-repair arguments.
public struct RepairSettings: Codable, Equatable, Sendable {

    public enum FrameRate: Codable, Equatable, Sendable, Hashable {
        case source
        case fps(Double)

        public var label: String {
            switch self {
            case .source: return "Same as source"
            case .fps(let r): return r == r.rounded() ? "\(Int(r)) fps" : String(format: "%.3g fps", r)
            }
        }
    }

    public enum Timing: String, Codable, CaseIterable, Sendable {
        case fill      // replace repeated frames only
        case even      // retime: rebuild every frame on an even grid
        public var label: String { self == .fill ? "Fill repeated frames" : "Even out timing" }
    }

    public enum Quality: String, Codable, CaseIterable, Sendable {
        case auto, hq, balanced, fast
        public var label: String {
            switch self {
            case .auto: return "Auto"
            case .hq: return "Best"
            case .balanced: return "Balanced"
            case .fast: return "Fast"
            }
        }
    }

    public enum Format: String, Codable, CaseIterable, Sendable {
        case prores, hevc, h264, auto
        public var label: String {
            switch self {
            case .prores: return "ProRes 422 HQ (for editing)"
            case .hevc: return "HEVC"
            case .h264: return "H.264"
            case .auto: return "Match source"
            }
        }
    }

    public var frameRate: FrameRate = .fps(24)
    public var timing: Timing = .even
    public var motionBlur = true
    public var shutter: Double = 180
    public var quality: Quality = .auto
    public var format: Format = .prores
    /// nil = save next to the original clip
    public var outputFolder: URL?
    public var threshold: Double = 1.5
    public var maxGap = 4
    /// nil = engine default (one moment per 1/240 s of exposure)
    public var samples: Int?

    public init() {}

    public static let filmLook = RepairSettings()
    public static let fixOnly: RepairSettings = {
        var s = RepairSettings()
        s.frameRate = .source; s.timing = .fill; s.motionBlur = false; s.format = .auto
        return s
    }()

    /// Changing the frame rate or adding blur always uses the retime engine.
    public var effectiveTiming: Timing {
        (frameRate != .source || motionBlur) ? .even : timing
    }

    /// Arguments for `rife-repair` (without the executable).
    public func arguments(input: URL, output: URL) -> [String] {
        var a = [input.path, "-o", output.path, "--machine-progress"]
        if case .fps(let r) = frameRate { a += ["--fps", Self.num(r)] }
        if effectiveTiming == .even && frameRate == .source && !motionBlur { a.append("--retime") }
        if motionBlur { a += ["--shutter", Self.num(shutter)] }
        if let n = samples, motionBlur { a += ["--samples", String(n)] }
        if quality != .auto { a += ["--tier", quality.rawValue] }
        a += ["--codec", format.rawValue]
        if threshold != 1.5 { a += ["--threshold", Self.num(threshold)] }
        if maxGap != 4 { a += ["--max-gap", String(maxGap)] }
        return a
    }

    public static func analysisArguments(input: URL) -> [String] { [input.path, "--dry-run", "--machine-progress"] }

    /// Short tag for file names, e.g. "24fps 180deg", "60fps", "fixed".
    public var fileTag: String {
        var parts: [String] = []
        if case .fps(let r) = frameRate { parts.append("\(Self.num(r))fps") }
        if motionBlur { parts.append("\(Self.num(shutter))deg") }
        if parts.isEmpty { parts.append(effectiveTiming == .even ? "smoothed" : "fixed") }
        return parts.joined(separator: " ")
    }

    /// Output file: "<name> [<tag>].mov" next to the original (or in the chosen
    /// folder), with " 2", " 3" ... added rather than overwriting anything.
    public func outputURL(for input: URL, exists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) }) -> URL {
        let folder = outputFolder ?? input.deletingLastPathComponent()
        let base = "\(input.deletingPathExtension().lastPathComponent) [\(fileTag)]"
        var url = folder.appendingPathComponent(base).appendingPathExtension("mov")
        var n = 2
        while exists(url) {
            url = folder.appendingPathComponent("\(base) \(n)").appendingPathExtension("mov")
            n += 1
        }
        return url
    }

    static func num(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(format: "%g", v)
    }
}
