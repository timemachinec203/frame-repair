import AppKit
import FrameRepairKit
import SwiftUI

struct SettingsPanel: View {
    @Environment(AppModel.self) private var model

    private static let rates: [RepairSettings.FrameRate] =
        [.source, .fps(24), .fps(25), .fps(30), .fps(48), .fps(50), .fps(59.94), .fps(60), .fps(120)]

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                Picker("Preset", selection: $model.preset) {
                    ForEach(Preset.allCases.filter { $0 != .manual }) { Text($0.label).tag($0) }
                    Divider()
                    Text(Preset.manual.label).tag(Preset.manual)
                }
                .pickerStyle(.menu)
                if model.preset == .manual {
                    manualLook
                } else {
                    presetSummary
                }
            } header: {
                Text("Look")
            } footer: {
                Text(lookFooter).font(.caption).foregroundStyle(.secondary)
            }

            Section {
                Picker("Format", selection: $model.settings.format) {
                    ForEach(RepairSettings.Format.allCases, id: \.self) { Text($0.label).tag($0) }
                }
                Picker("Clips at once", selection: $model.clipsAtOnce) {
                    Text("1").tag(1)
                    Text("2").tag(2)
                    Text("3").tag(3)
                }
                LabeledContent("Save to") {
                    HStack {
                        Text(model.settings.outputFolder?.lastPathComponent ?? "Next to original")
                            .lineLimit(1).truncationMode(.middle)
                            .foregroundStyle(.secondary)
                        Button("Choose…") { chooseFolder() }
                        if model.settings.outputFolder != nil {
                            Button { model.settings.outputFolder = nil } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.borderless)
                                .help("Save next to the original again")
                        }
                    }
                }
            } header: {
                Text("Output")
            } footer: {
                Text("Saved as “clip name [\(model.settings.fileTag)].mov”. Originals are never changed. Settings apply when you press Process; clips already queued keep theirs. Two at once was about 17% faster than one on the test Mac; three suits Macs with bigger GPUs and more memory (each clip uses about 2–4 GB).")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if model.preset == .manual {
                Section {
                    LabeledContent("Repeat threshold") {
                        Stepper(value: $model.settings.threshold, in: 0.5...10, step: 0.5) {
                            Text(String(format: "%.1f", model.settings.threshold)).monospacedDigit()
                        }
                    }
                    LabeledContent("Longest gap to fill") {
                        Stepper(value: $model.settings.maxGap, in: 1...30) {
                            Text("\(model.settings.maxGap) frames").monospacedDigit()
                        }
                    }
                    if model.settings.motionBlur {
                        LabeledContent("Blur samples") {
                            Stepper(value: samplesBinding, in: 0...32) {
                                Text(model.settings.samples.map { "\($0)" } ?? "Auto").monospacedDigit()
                            }
                        }
                    }
                } header: {
                    Text("Repeat detection")
                } footer: {
                    Text("Longer runs of repeated frames are treated as real pauses and left alone. Raise the threshold only if the analysis finds too few repeats.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: Look

    /// Named preset: show what it does, read-only.
    @ViewBuilder
    private var presetSummary: some View {
        let s = model.settings
        LabeledContent("Frame rate", value: s.frameRate.label)
        LabeledContent("Timing", value: s.effectiveTiming.label)
        LabeledContent("Motion blur", value: s.motionBlur ? "\(Int(s.shutter))° shutter" : "Off")
        LabeledContent("Quality", value: qualityText(s))
    }

    /// Manual: every look control is editable.
    @ViewBuilder
    private var manualLook: some View {
        @Bindable var model = model
        Picker("Frame rate", selection: $model.settings.frameRate) {
            ForEach(Self.rates, id: \.self) { Text($0.label).tag($0) }
        }
        Picker("Timing", selection: timingBinding) {
            ForEach(RepairSettings.Timing.allCases, id: \.self) { Text($0.label).tag($0) }
        }
        .disabled(timingLocked)
        .help(timingLocked ? timingLockReason : "Fill keeps every real frame where it was; Even rebuilds every frame on a steady grid.")
        if timingLocked {
            Text(timingLockReason)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        Toggle("Motion blur", isOn: $model.settings.motionBlur)
        if model.settings.motionBlur {
            LabeledContent("Shutter") {
                HStack {
                    Slider(value: $model.settings.shutter, in: 45...360, step: 15)
                    Text("\(Int(model.settings.shutter))°").monospacedDigit().frame(width: 40, alignment: .trailing)
                }
            }
        }
        Picker("Quality", selection: $model.settings.quality) {
            ForEach(RepairSettings.Quality.allCases, id: \.self) { Text($0.label).tag($0) }
        }
    }

    /// Timing is only a choice at the source frame rate without blur; otherwise every
    /// frame has to be rebuilt, so the menu shows what will actually run.
    private var timingLocked: Bool {
        model.settings.frameRate != .source || model.settings.motionBlur
    }

    private var timingLockReason: String {
        let why = model.settings.frameRate != .source
            ? (model.settings.motionBlur ? "changing the frame rate and adding motion blur" : "changing the frame rate")
            : "adding motion blur"
        return "Locked to Even out timing: \(why) needs every frame rebuilt. Set Frame rate to Same as source with Motion blur off to choose Fill."
    }

    private var timingBinding: Binding<RepairSettings.Timing> {
        Binding(get: { model.settings.effectiveTiming },
                set: { if !timingLocked { model.settings.timing = $0 } })
    }

    private func qualityText(_ s: RepairSettings) -> String {
        guard s.quality == .auto else { return s.quality.label }
        return s.motionBlur ? "Balanced (auto)" : "Best (auto)"
    }

    private var lookFooter: String {
        let p = model.preset
        guard p == .manual else { return p.description + " Choose Manual to change these." }
        let s = model.settings
        var parts: [String] = []
        if s.motionBlur {
            parts.append("Each frame blends moments across a \(Int(s.shutter))° shutter (180° is the classic film look).")
        }
        if s.frameRate != .source || s.motionBlur {
            // the note under Timing explains the lock
        } else if s.timing == .fill {
            parts.append("Only repeated frames are replaced; everything else is untouched.")
        } else {
            parts.append("Every frame is rebuilt on an even grid, for games running below the recording rate.")
        }
        parts.append("Auto quality is Balanced with blur and Best without.")
        return parts.joined(separator: " ")
    }

    private var samplesBinding: Binding<Int> {
        Binding(get: { model.settings.samples ?? 0 },
                set: { model.settings.samples = $0 == 0 ? nil : $0 })
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Save Here"
        if panel.runModal() == .OK, let url = panel.url { model.settings.outputFolder = url }
    }
}
