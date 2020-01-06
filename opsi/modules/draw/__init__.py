from dataclasses import dataclass

import cv2
import numpy as np

from opsi.manager.manager_schema import Function
from opsi.util.cv import Contours, Mat, MatBW

from .fps import DrawFPS
from .shapes import DrawCircles, DrawSegments

__package__ = "opsi.draw"
__version__ = "0.123"


class DrawContours(Function):
    @dataclass
    class Settings:
        bounding_rect: bool
        min_area_rect: bool

    @dataclass
    class Inputs:
        contours: Contours
        img: Mat

    @dataclass
    class Outputs:
        img: Mat

    def run(self, inputs):
        draw = np.copy(inputs.img.mat.img)

        # Draw the outline of the contours
        cv2.drawContours(draw, inputs.contours.raw, -1, (255, 255, 0), 2)

        # Draw the non-rotated rectangle bounding each contour
        if self.settings.bounding_rect:
            for contour in inputs.contours.l:
                rect = contour.to_rect
                cv2.rectangle(draw,
                              (int(rect.tl.x), int(rect.tl.y)),  # Top left coord
                              (int(rect.tl.x + rect.dim.x), int(rect.tl.y + rect.dim.y)),  # Bottom right coord
                              (0, 0, 255), 2)

        # Draw the smallest possible (rotated) rectangle bounding each contour
        if self.settings.min_area_rect:
            for contour in inputs.contours.l:
                points = np.int0(contour.to_min_area_rect.box_points)
                cv2.drawContours(draw, [points], -1, (0, 255, 255), 2)

        draw = Mat(draw)
        return self.Outputs(img=draw)


class BitwiseAND(Function):
    @dataclass
    class Inputs:
        img: Mat
        mask: MatBW

    @dataclass
    class Outputs:
        img: Mat

    def run(self, inputs):
        img = inputs.img.mat.img
        mask = inputs.mask.matBW.img

        out = Mat(cv2.bitwise_and(np.copy(img), img, mask))

        return self.Outputs(img=out)
