"""The app's settings logic: same arguments, names and parsing as the Mac app's
FrameRepairKit (these mirror Tests/FrameRepairKitTests). No Qt needed."""

import unittest
from pathlib import Path

from frame_repair.appkit import (FILM_LOOK, FIX_ONLY, PRESET_BY_KEY, ClipAnalysis, LineBuffer, RepairSettings,
                                 matching_preset, overall_fraction, parse_event)

INPUT = Path("/clips/Flyby 1.mov")
OUTPUT = Path("/clips/out.mov")


class ArgumentTests(unittest.TestCase):
    def test_film_look(self):
        self.assertEqual(FILM_LOOK.arguments(INPUT, OUTPUT),
                         [str(INPUT), "-o", str(OUTPUT), "--machine-progress",
                          "--fps", "24", "--shutter", "180", "--codec", "prores"])

    def test_fix_only(self):
        self.assertEqual(FIX_ONLY.arguments(INPUT, OUTPUT),
                         [str(INPUT), "-o", str(OUTPUT), "--machine-progress", "--codec", "auto"])

    def test_even_timing_without_rate_change_uses_retime(self):
        s = FIX_ONLY.copy()
        s.timing = "even"
        self.assertIn("--retime", s.arguments(INPUT, OUTPUT))

    def test_rate_change_never_passes_retime_flag_but_is_even(self):
        s = FIX_ONLY.copy()
        s.frame_rate, s.timing = 59.94, "fill"
        a = s.arguments(INPUT, OUTPUT)
        self.assertNotIn("--retime", a)
        self.assertEqual(s.effective_timing, "even")
        self.assertEqual(a[4:6], ["--fps", "59.94"])

    def test_advanced_options_only_when_changed(self):
        s = FILM_LOOK.copy()
        s.quality, s.samples, s.threshold, s.max_gap = "hq", 10, 2.0, 6
        a = s.arguments(INPUT, OUTPUT)
        for flag, value in [("--tier", "hq"), ("--samples", "10"), ("--threshold", "2"), ("--max-gap", "6")]:
            self.assertEqual(a[a.index(flag) + 1], value)
        s.motion_blur = False
        self.assertNotIn("--samples", s.arguments(INPUT, OUTPUT))


class OutputNameTests(unittest.TestCase):
    def test_next_to_original(self):
        self.assertEqual(FILM_LOOK.output_path(INPUT, exists=lambda p: False),
                         Path("/clips/Flyby 1 [24fps 180deg].mov"))

    def test_never_overwrites(self):
        taken = {Path("/clips/Flyby 1 [fixed].mov"), Path("/clips/Flyby 1 [fixed] 2.mov")}
        self.assertEqual(FIX_ONLY.output_path(INPUT, exists=lambda p: p in taken),
                         Path("/clips/Flyby 1 [fixed] 3.mov"))

    def test_output_folder(self):
        s = FIX_ONLY.copy()
        s.output_folder, s.timing = "/exports", "even"
        self.assertEqual(s.output_path(INPUT, exists=lambda p: False), Path("/exports/Flyby 1 [smoothed].mov"))

    def test_mkv_input_gives_mov(self):
        self.assertEqual(FIX_ONLY.output_path(Path("/rec/2026-11-09 20-32-03.mkv"), exists=lambda p: False),
                         Path("/rec/2026-11-09 20-32-03 [fixed].mov"))


class EngineOutputTests(unittest.TestCase):
    def test_parse_events(self):
        self.assertEqual(parse_event("@progress render 12 91"), ("progress", "render", 12, 91))
        self.assertEqual(parse_event("@output C:\\a b\\c.mov"), ("output", "C:\\a b\\c.mov"))
        self.assertEqual(parse_event("Done in 3 s"), ("text", "Done in 3 s"))
        self.assertEqual(parse_event("@progress render x 91"), ("text", "@progress render x 91"))

    def test_line_buffer_splits_on_cr_and_lf_across_chunks(self):
        b = LineBuffer()
        self.assertEqual(b.feed("@progress scan 1 10\r@prog"), ["@progress scan 1 10"])
        self.assertEqual(b.feed("ress scan 2 10\r\nDone"), ["@progress scan 2 10"])
        self.assertEqual(b.flush(), ["Done"])

    def test_overall_fraction(self):
        self.assertEqual(overall_fraction("render", 50, 100, False), 0.5)
        self.assertAlmostEqual(overall_fraction("scan", 100, 100, True), 0.1)
        self.assertAlmostEqual(overall_fraction("render", 50, 100, True), 0.55)

    DRY_RUN = """\
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

    def test_parse_analysis(self):
        a = ClipAnalysis.parse(self.DRY_RUN)
        self.assertEqual((a.width, a.height, a.fps, a.duration), (1920, 1080, 60.0, "0:10.000"))
        self.assertEqual((a.game_fps_min, a.game_fps_median, a.game_fps_max), (30, 59, 60))
        self.assertEqual((a.repeated, a.fixable, a.freezes, a.unsteady), (57, 7, 50, True))
        self.assertEqual(a.summary, "1920x1080  60 fps  0:10.000  |  game ~59 fps (30-60)  |  57 repeated frames, 50 in pauses")


class PresetTests(unittest.TestCase):
    def test_preset_keeps_output_choices(self):
        s = FILM_LOOK.copy()
        s.format, s.output_folder = "hevc", "/exports"
        s = PRESET_BY_KEY["fix"].apply(s)
        self.assertEqual((s.format, s.output_folder, s.frame_rate, s.motion_blur), ("hevc", "/exports", None, False))

    def test_film_look_preset_matches_default_settings(self):
        self.assertEqual(matching_preset(FILM_LOOK).key, "film")
        self.assertEqual(PRESET_BY_KEY["film"].apply(RepairSettings()), FILM_LOOK)

    def test_editing_a_look_control_leaves_the_preset(self):
        s = FILM_LOOK.copy()
        s.shutter = 90.0
        self.assertIsNone(matching_preset(s))
        s = PRESET_BY_KEY["smooth60"].apply(s)
        self.assertEqual(matching_preset(s).key, "smooth60")
        self.assertEqual([x for x in s.arguments(INPUT, OUTPUT) if x.startswith("--")],
                         ["--machine-progress", "--fps", "--codec"])

    def test_manual_changes_nothing(self):
        s = FILM_LOOK.copy()
        s.shutter, s.max_gap = 270.0, 8
        self.assertEqual(PRESET_BY_KEY["manual"].apply(s), s)

    def test_settings_round_trip_and_sanitise(self):
        s = FIX_ONLY.copy()
        s.output_folder = "D:/exports"
        self.assertEqual(RepairSettings.from_dict(s.to_dict()), s)
        bad = RepairSettings.from_dict({"quality": "ultra", "format": "gif", "unknown": 1})
        self.assertEqual((bad.quality, bad.format), ("auto", "prores"))


if __name__ == "__main__":
    unittest.main()
