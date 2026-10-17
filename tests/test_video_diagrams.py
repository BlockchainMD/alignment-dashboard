"""Behavioral checks for original, disclosed schematic video illustrations."""

import math
import unittest

from scripts import video_diagrams as diagrams

try:
    from PIL import Image,ImageChops
except ImportError:
    Image=ImageChops=None


PALETTE={"background":"#09252B","foreground":"#F4F1E9","muted":"#A5BAB7",
         "line":"#355057","accent":"#D8F580","aqua":"#79D8D0",
         "coral":"#F3967D","panel":"#12343A"}


class SemanticDiagramTests(unittest.TestCase):
    def test_traffic_signal_requires_an_explicit_analogy(self):
        self.assertEqual(diagrams.analogy_mode({"narration":"Imagine sorting photographs of dogs."}),"generic")
        self.assertEqual(diagrams.analogy_mode({"narration":"Imagine a traffic-light rule."}),"traffic_light")

    def test_quantitative_marks_require_relevant_labels(self):
        self.assertEqual(diagrams.comparison_mode({"labels":["Output probabilities"]}),"probability")
        self.assertEqual(diagrams.comparison_mode({"labels":["Activation strength"]}),"activation")
        self.assertEqual(diagrams.comparison_mode({"labels":["Two possible explanations"]}),"generic")

    def test_illustrative_distributions_stay_normalized(self):
        for progress in (0,.2,.4,.6,.8,1.):
            for values in diagrams.toy_profiles(progress):
                self.assertAlmostEqual(sum(values),1.)
                self.assertTrue(all(0<=value<=1 for value in values))


@unittest.skipIf(Image is None,"Pillow is an optional video dependency")
class DiagramDrawingTests(unittest.TestCase):
    def test_all_diagrams_are_clipped_to_the_supplied_safe_box(self):
        for kind in diagrams.VISUALS:
            with self.subTest(kind=kind):
                image=Image.new("RGB",(760,560),"#FF00FF")
                diagrams.draw_diagram(image,{"visual":kind,"heading":"Research",
                    "labels":["A very long technical explanation that must wrap", "A response", "A useful limit"]},
                    (30,40,700,470),7,14,False,{"title":"Original research","authors":["An author"],
                    "source_url":"https://example.org/paper"},PALETTE)
                self.assertEqual(image.getpixel((15,15)),(255,0,255))
                self.assertEqual(image.getpixel((750,550)),(255,0,255))
                self.assertIsNotNone(ImageChops.difference(image.crop((30,40,730,510)),
                    Image.new("RGB",(700,470),"#FF00FF")).getbbox())

    def test_focus_and_probes_continue_changing_late_in_a_scene(self):
        for kind in ("neuron","test","takeaway","source"):
            frames=[]
            for elapsed in (8,11):
                image=Image.new("RGB",(700,470),PALETTE["background"])
                diagrams.draw_diagram(image,{"visual":kind},(0,0,700,470),elapsed,14,False,
                                      {"title":"Source","authors":["A"],"source_url":"https://example.org"},PALETTE)
                frames.append(image)
            with self.subTest(kind=kind):
                self.assertIsNotNone(ImageChops.difference(*frames).getbbox())

    def test_explanation_levels_change_neuron_detail(self):
        frames=[]
        for level in ("technical","plain","simple"):
            image=Image.new("RGB",(700,470),PALETTE["background"])
            diagrams.draw_diagram(image,{"visual":"neuron","explanation_level":level},
                                  (0,0,700,470),7,14,False,{},PALETTE)
            frames.append(image)
        self.assertIsNotNone(ImageChops.difference(frames[0],frames[1]).getbbox())
        self.assertIsNotNone(ImageChops.difference(frames[1],frames[2]).getbbox())

    def test_nonfinite_timing_is_rejected(self):
        image=Image.new("RGB",(700,470))
        for duration,elapsed in ((0,1),(math.inf,1),(12,math.nan)):
            with self.subTest(duration=duration,elapsed=elapsed),self.assertRaises(ValueError):
                diagrams.draw_diagram(image,{},(0,0,700,470),elapsed,duration,False,{},PALETTE)


if __name__=="__main__":
    unittest.main()
