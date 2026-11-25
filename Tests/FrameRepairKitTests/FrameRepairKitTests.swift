import XCTest
@testable import FrameRepairKit

final class FrameRepairKitTests: XCTestCase {
    let input = URL(fileURLWithPath: "/clips/Flyby 1.mov")
    let output = URL(fileURLWithPath: "/clips/out.mov")

    // MARK: arguments

    func testFilmLookArguments() {
        let a = RepairSettings.filmLook.arguments(input: input, output: output)
        XCTAssertEqual(a, ["/clips/Flyby 1.mov", "-o", "/clips/out.mov", "--machine-progress",
                           "--fps", "24", "--shutter", "180", "--codec", "prores"])
    }

    func testFixOnlyArguments() {
        let a = RepairSettings.fixOnly.arguments(input: input, output: output)
        XCTAssertEqual(a, ["/clips/Flyby 1.mov", "-o", "/clips/out.mov", "--machine-progress", "--codec", "auto"])
    }

    func testEvenTimingWithoutRateChangeUsesRetime() {
        var s = RepairSettings.fixOnly
        s.timing = .even
        XCTAssertTrue(s.arguments(input: input, output: output).contains("--retime"))
    }

    func testRateChangeNeverPassesRetimeFlagButIsEven() {
        var s = RepairSettings.fixOnly
        s.frameRate = .fps(59.94)
        s.timing = .fill
        let a = s.arguments(input: input, output: output)
        XCTAssertFalse(a.contains("--retime"))           // --fps already implies it
        XCTAssertEqual(s.effectiveTiming, .even)
        XCTAssertEqual(Array(a[4...5]), ["--fps", "59.94"])
    }

    func testAdvancedOptionsOnlyWhenChanged() {
        var s = RepairSettings.filmLook
        s.quality = .hq; s.samples = 10; s.threshold = 2; s.maxGap = 6
        let a = s.arguments(input: input, output: output)
        for pair in [["--tier", "hq"], ["--samples", "10"], ["--threshold", "2"], ["--max-gap", "6"]] {
            let i = a.firstIndex(of: pair[0])
            XCTAssertNotNil(i, pair[0])
            if let i { XCTAssertEqual(a[i + 1], pair[1]) }
        }
        s.motionBlur = false
        XCTAssertFalse(s.arguments(input: input, output: output).contains("--samples"))
    }

    // MARK: output names

    func testOutputNameNextToOriginal() {
        let u = RepairSettings.filmLook.outputURL(for: input, exists: { _ in false })
        XCTAssertEqual(u.path, "/clips/Flyby 1 [24fps 180deg].mov")
    }

    func testOutputNameNeverOverwrites() {
        var taken: Set<String> = ["/clips/Flyby 1 [fixed].mov", "/clips/Flyby 1 [fixed] 2.mov"]
        let u = RepairSettings.fixOnly.outputURL(for: input, exists: { taken.contains($0.path) })
        XCTAssertEqual(u.path, "/clips/Flyby 1 [fixed] 3.mov")
        taken.removeAll()
    }

    func testOutputFolder() {
        var s = RepairSettings.fixOnly
        s.outputFolder = URL(fileURLWithPath: "/exports")
        s.timing = .even
        XCTAssertEqual(s.outputURL(for: input, exists: { _ in false }).path, "/exports/Flyby 1 [smoothed].mov")
    }

    // MARK: engine output

    func testParseEvents() {
        XCTAssertEqual(EngineEvent.parse("@progress render 12 91"), .progress(phase: "render", done: 12, total: 91))
        XCTAssertEqual(EngineEvent.parse("@output /a b/c.mov"), .output("/a b/c.mov"))
        XCTAssertEqual(EngineEvent.parse("Done in 3 s"), .text("Done in 3 s"))
        XCTAssertEqual(EngineEvent.parse("@progress render x 91"), .text("@progress render x 91"))
    }

    func testLineBufferSplitsOnCRAndLFAcrossChunks() {
        var b = LineBuffer()
        XCTAssertEqual(b.feed("@progress scan 1 10\r@prog"), ["@progress scan 1 10"])
        XCTAssertEqual(b.feed("ress scan 2 10\nDone"), ["@progress scan 2 10"])
        XCTAssertEqual(b.flush(), ["Done"])
    }

    func testOverallFraction() {
        XCTAssertEqual(overallFraction(phase: "render", done: 50, total: 100, twoPass: false), 0.5)
        XCTAssertEqual(overallFraction(phase: "scan", done: 100, total: 100, twoPass: true), 0.1, accuracy: 1e-9)
        XCTAssertEqual(overallFraction(phase: "render", done: 50, total: 100, twoPass: true), 0.55, accuracy: 1e-9)
    }

    // Captured from `rife-repair gameplay.mov --dry-run`.
    let dryRun = """
    Input:  gameplay.mov
            1920x1080  60.000 fps  hvc1  12.8 Mbps  0:10.000  ~600 frames  1 audio track(s)

    Frame-to-frame difference (worst cell, 0-255) — duplicates sit near 0:
        0.0 - 0.1         1
       10.0 +           542  #############################################

    Game frame rate (real frames per second of recording): min 30  median 59  max 60
      seconds under 57 fps: 2 of 9  -> unsteady/low: use --retime for even motion

    Below threshold 1.5: 57 repeated frames in 5 run(s)
      would repair: 7 frames in 4 run(s) (length <= 4)
      would leave:  50 frames in 1 freeze(s)
      run lengths:  1x2  2x1  3x1  50x1
    """

    func testParseAnalysis() {
        let a = ClipAnalysis.parse(dryRun)
        XCTAssertEqual(a.width, 1920); XCTAssertEqual(a.height, 1080)
        XCTAssertEqual(a.fps, 60); XCTAssertEqual(a.duration, "0:10.000")
        XCTAssertEqual(a.gameFpsMin, 30); XCTAssertEqual(a.gameFpsMedian, 59); XCTAssertEqual(a.gameFpsMax, 60)
        XCTAssertEqual(a.repeated, 57); XCTAssertEqual(a.fixable, 7); XCTAssertEqual(a.freezes, 50)
        XCTAssertTrue(a.unsteady)
        XCTAssertEqual(a.summary, "1920x1080  60 fps  0:10.000  |  game ~59 fps (30-60)  |  57 repeated frames, 50 in pauses")
    }

    // MARK: presets

    func testPresetKeepsOutputChoices() {
        var s = RepairSettings.filmLook
        s.format = .hevc
        s.outputFolder = URL(fileURLWithPath: "/exports")
        Preset.fixOnly.apply(to: &s)
        XCTAssertEqual(s.format, .hevc)
        XCTAssertEqual(s.outputFolder?.path, "/exports")
        XCTAssertEqual(s.frameRate, .source)
        XCTAssertFalse(s.motionBlur)
    }

    func testFilmLookPresetMatchesDefaultSettings() {
        XCTAssertEqual(Preset.matching(.filmLook), .filmLook)
        var s = RepairSettings()
        Preset.filmLook.apply(to: &s)
        XCTAssertEqual(s, .filmLook)
    }

    func testEditingALookControlLeavesThePreset() {
        var s = RepairSettings.filmLook
        s.shutter = 90
        XCTAssertNil(Preset.matching(s))
        Preset.smooth60.apply(to: &s)
        XCTAssertEqual(Preset.matching(s), .smooth60)
        XCTAssertEqual(s.arguments(input: input, output: output).filter { $0.hasPrefix("--") },
                       ["--machine-progress", "--fps", "--codec"])
    }

    func testManualChangesNothing() {
        var s = RepairSettings.filmLook
        s.shutter = 270; s.maxGap = 8
        let before = s
        Preset.manual.apply(to: &s)
        XCTAssertEqual(s, before)
    }
}
