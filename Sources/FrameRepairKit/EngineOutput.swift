import Foundation

/// One line from the engine's stdout.
public enum EngineEvent: Equatable, Sendable {
    case progress(phase: String, done: Int, total: Int)
    case output(String)
    case text(String)

    public static func parse(_ line: String) -> EngineEvent {
        if line.hasPrefix("@progress ") {
            let p = line.split(separator: " ")
            if p.count == 4, let d = Int(p[2]), let t = Int(p[3]) {
                return .progress(phase: String(p[1]), done: d, total: t)
            }
        }
        if line.hasPrefix("@output ") { return .output(String(line.dropFirst(8))) }
        return .text(line)
    }
}

/// Splits a byte stream into lines on \n or \r, keeping any partial line for later.
public struct LineBuffer: Sendable {
    private var pending = ""
    public init() {}
    public mutating func feed(_ chunk: String) -> [String] {
        pending += chunk
        var lines: [String] = []
        while let r = pending.firstIndex(where: { $0 == "\n" || $0 == "\r" }) {
            let line = String(pending[..<r])
            pending = String(pending[pending.index(after: r)...])
            if !line.trimmingCharacters(in: .whitespaces).isEmpty { lines.append(line) }
        }
        return lines
    }
    public mutating func flush() -> [String] {
        defer { pending = "" }
        let l = pending.trimmingCharacters(in: .whitespaces)
        return l.isEmpty ? [] : [pending]
    }
}

/// Overall 0...1 progress from per-phase engine progress.
/// The retime engine reads the clip twice: a quick scan, then the render.
public func overallFraction(phase: String, done: Int, total: Int, twoPass: Bool) -> Double {
    let f = total > 0 ? min(1, max(0, Double(done) / Double(total))) : 0
    guard twoPass else { return f }
    return phase == "scan" ? f * 0.1 : 0.1 + f * 0.9
}

/// What --dry-run found, for the clip list.
public struct ClipAnalysis: Equatable, Sendable {
    public var width = 0, height = 0
    public var fps = 0.0
    public var duration = ""
    public var gameFpsMin: Int?, gameFpsMedian: Int?, gameFpsMax: Int?
    public var repeated = 0
    public var fixable = 0
    public var freezes = 0
    public var unsteady = false

    public var summary: String {
        var s = "\(width)x\(height)  \(SettingsFormat.fps(fps))  \(duration)"
        if let m = gameFpsMedian { s += "  |  game ~\(m) fps" }
        if let lo = gameFpsMin, let hi = gameFpsMax, lo != hi { s += " (\(lo)-\(hi))" }
        s += "  |  \(repeated) repeated frame\(repeated == 1 ? "" : "s")"
        if freezes > 0 { s += ", \(freezes) in pauses" }
        return s
    }

    public var advice: String {
        if repeated == 0 { return "No repeated frames found." }
        return unsteady
            ? "Game ran below the recording rate: even out timing (or a frame-rate change) works best."
            : "Mostly full rate: filling the repeated frames is enough."
    }

    public static func parse(_ text: String) -> ClipAnalysis {
        var a = ClipAnalysis()
        for raw in text.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if a.width == 0, let m = line.firstMatch(of: #/^(\d+)x(\d+)\s+([\d.]+) fps\s+\S+\s+[\d.]+ Mbps\s+(\S+)/#) {
                a.width = Int(m.1) ?? 0; a.height = Int(m.2) ?? 0
                a.fps = Double(m.3) ?? 0; a.duration = String(m.4)
            } else if let m = line.firstMatch(of: #/^Game frame rate.*min (\d+)\s+median (\d+)\s+max (\d+)/#) {
                a.gameFpsMin = Int(m.1); a.gameFpsMedian = Int(m.2); a.gameFpsMax = Int(m.3)
            } else if line.contains("-> unsteady/low") {
                a.unsteady = true
            } else if let m = line.firstMatch(of: #/^Below threshold [\d.]+: (\d+) repeated frames/#) {
                a.repeated = Int(m.1) ?? 0
            } else if let m = line.firstMatch(of: #/^would repair: (\d+) frames/#) {
                a.fixable = Int(m.1) ?? 0
            } else if let m = line.firstMatch(of: #/^would leave:\s+(\d+) frames/#) {
                a.freezes = Int(m.1) ?? 0
            }
        }
        return a
    }
}

public enum SettingsFormat {
    public static func fps(_ v: Double) -> String {
        (v - v.rounded()).magnitude < 0.01 ? "\(Int(v.rounded())) fps" : String(format: "%.2f fps", v)
    }
}
