import Foundation

/// Named looks. A preset sets only how the video looks and how repeats are found
/// (frame rate, timing, blur, quality, detection). Format, save location and the
/// number of clips at once are left alone. `.manual` changes nothing: every
/// control is editable.
public enum Preset: String, Codable, CaseIterable, Identifiable, Sendable {
    case filmLook, fixOnly, smooth60, manual

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .filmLook: return "Film look (24 fps, motion blur)"
        case .fixOnly: return "Fix dropped frames only"
        case .smooth60: return "Smooth 60 fps"
        case .manual: return "Manual"
        }
    }

    public var description: String {
        switch self {
        case .filmLook:
            return "24 fps with a 180° shutter, so motion smears like a film camera. Timing is evened out first. For cinematic edits."
        case .fixOnly:
            return "Replaces only the repeated frames. Frame rate, length and everything else stay as recorded."
        case .smooth60:
            return "Evens out timing and rebuilds every frame on a steady 60 fps grid, with no blur. For gameplay that should feel fluid."
        case .manual:
            return "Set frame rate, timing, blur, quality and repeat detection yourself."
        }
    }

    /// Applies this preset's look to `s`, keeping the user's output choices.
    public func apply(to s: inout RepairSettings) {
        guard self != .manual else { return }
        s.threshold = 1.5
        s.maxGap = 4
        s.samples = nil
        s.quality = .auto
        switch self {
        case .filmLook:
            s.frameRate = .fps(24); s.timing = .even; s.motionBlur = true; s.shutter = 180
        case .fixOnly:
            s.frameRate = .source; s.timing = .fill; s.motionBlur = false
        case .smooth60:
            s.frameRate = .fps(60); s.timing = .even; s.motionBlur = false
        case .manual:
            break
        }
    }

    /// The named preset whose look `s` currently has, if any.
    public static func matching(_ s: RepairSettings) -> Preset? {
        allCases.first { p in
            guard p != .manual else { return false }
            var t = s
            p.apply(to: &t)
            return t == s
        }
    }
}
