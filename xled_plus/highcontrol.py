# -*- coding: utf-8 -*-

"""
xled++.highcontrol
~~~~~~~~~~~~~

This module contains interface to control specific device

"""

from __future__ import absolute_import

import collections
import io
import logging
import struct
import binascii
import time
import uuid
import datetime
import math as m
from operator import xor

from xled.control import ControlInterface
from xled.util import date_from_seconds_after_midnight
from xled.security import sha1sum
from xled.exceptions import HighInterfaceError

log = logging.getLogger(__name__)

#: Time format as defined by C standard
TIME_FORMAT = "%H:%M:%S"
SHORT_TIME_FORMAT = "%H:%M"


# Should be in util.py
def seconds_after_midnight_from_string(timestr, form):
    dt = datetime.datetime.strptime(timestr, form)
    return dt.hour * 3600 + dt.minute * 60 + dt.second


class HighControlInterface(ControlInterface):
    """
    High level interface to control specific device
    """

    def __init__(self, host, hw_address=None):
        super(HighControlInterface, self).__init__(host, hw_address)
        info = self.get_device_info()
        self.num_leds = info["number_of_led"]
        self.family = info["fw_family"] if "fw_family" in info else "D"
        self.led_bytes = info["bytes_per_led"] if "bytes_per_led" in info else 3
        self.led_profile = info["led_profile"] if "led_profile" in info else "RGB"
        self.version = tuple(map(int, self.firmware_version()["version"].split(".")))
        self.string_config = self.get_led_config()["strings"]
        if not self.hw_address:
            self.hw_address = info["mac"]
        self.layout = False
        self.layout_bounds = False
        self.last_mode = None
        self.last_rt_time = 0
        self.curr_mode = self.get_mode()["mode"]

    def firmware_num_stages(self):
        if self.family == "D":
            return 2
        else:
            return 1

    def update_firmware(self, stage0, stage1=None):
        """
        Uploads firmware and runs update

        :param stage0: file-like seekable object pointing to stage0 of firmware.
        :param stage1: file-like seekable object pointing to stage1 of firmware,
                       or None if there is no stage1.
        :raises ApplicationError: on application error
        :raises HighInterfaceError: on error during update
        """
        twostage = self.fimware_num_stages() == 2
        if twostage:
            assert stage1
        else:
            assert stage1 is None
        fw_stage_sums = [None, None]
        fw_images = [stage0, stage1]
        fw_funcalls = [self.firmware_0_update, self.firmware_1_update]
        stages = [0, 1] if twostage else [0]
        for stage in stages:
            fw_images[stage].seek(0)
            fw_stage_sums[stage] = sha1sum(fw_images[stage])
            log.debug("Firmware stage %d SHA1SUM: %r", stage, fw_stage_sums[stage])
            if not fw_stage_sums[stage]:
                msg = "Failed to compute SHA1SUM for firmware stage {}.".format(stage)
                raise HighInterfaceError(msg)
                assert False

        uploaded_stage_sums = [None, None]
        for stage in stages:
            log.debug("Uploading firmware stage %d...", stage)
            fw_images[stage].seek(0)
            response = fw_funcalls[stage](fw_images[stage])
            log.debug("Firmware stage %d uploaded.", stage)
            if not response.ok:
                msg = "Failed to upload stage {}: {}".format(
                    stage, response.status_code
                )
                raise HighInterfaceError(msg)
                assert False

            uploaded_stage_sums[stage] = response.get("sha1sum")
            log.debug(
                "Uploaded stage %d SHA1SUM: %r", stage, uploaded_stage_sums[stage]
            )
            if not uploaded_stage_sums[stage]:
                msg = "Device didn't return SHA1SUM for stage {}.".format(stage)
                raise HighInterfaceError(msg)
                assert False

        if fw_stage_sums != uploaded_stage_sums:
            log.error("Firmware SHA1SUMs: %r != uploaded SHA1SUMs", fw_stage_sums)
            msg = "Firmware SHA1SUMs doesn't match uploaded SHA1SUMs."
            raise HighInterfaceError(msg)
            assert False
        else:
            log.debug("Firmware SHA1SUMs matches.")

        response = self.firmware_update(fw_stage_sums[0], fw_stage_sums[1])
        if not response.ok:
            msg = "Failed to update firmware: {}.".format(response.status_code)
            raise HighInterfaceError(msg)
            assert False

    def disable_timer(self):
        """
        Disables timer
        """
        return self.set_timer(-1, -1)

    def get_formatted_timer(self):
        """
        Gets current time and timer

        :return: namedtuple of formatted entries: current time, turn on time,
            turn off time.
        :rtype: namedtuple
        """
        Timer = collections.namedtuple("Timer", ["now", "on", "off"])

        device_response = self.get_timer()

        if xor(device_response["time_on"] == -1, device_response["time_off"] == -1):
            msg = "Inconsistent timer configuration. On: {timer_on}, off: {timer_off}".format(
                **device_response
            )
            raise HighInterfaceError(msg)

        now = device_response["time_now"]
        now_formatted = date_from_seconds_after_midnight(now).strftime(TIME_FORMAT)

        if device_response["time_on"] == -1 and device_response["time_off"] == -1:
            return Timer(now_formatted, False, False)

        on = device_response["time_on"]
        on_formatted = date_from_seconds_after_midnight(on).strftime(TIME_FORMAT)

        off = device_response["time_off"]
        off_formatted = date_from_seconds_after_midnight(off).strftime(TIME_FORMAT)

        return Timer(now_formatted, on_formatted, off_formatted)

    def set_formatted_timer(self, timestr_on, timestr_off):
        """
        Sets timer on and off times, given as strings in H:M:S or H:M format

        :param str timestr_on: time to turn on
        :param str timestr_off: time to turn off
        """
        try:
            time_on = seconds_after_midnight_from_string(timestr_on, TIME_FORMAT)
        except ValueError:
            time_on = seconds_after_midnight_from_string(timestr_on, SHORT_TIME_FORMAT)
        try:
            time_off = seconds_after_midnight_from_string(timestr_off, TIME_FORMAT)
        except ValueError:
            time_off = seconds_after_midnight_from_string(
                timestr_off, SHORT_TIME_FORMAT
            )

        return self.set_timer(time_on, time_off)

    def turn_on(self):
        """
        Turns on the device.

        Sets the mode to the last used mode before turn_off().
        If the last mode is not known, sets 'movie' mode if there is an
        uploaded movie, else 'effect' mode.
        """
        if self.last_mode:
            return self.set_mode(self.last_mode)
        else:
            if self.family == "D" or self.version < (2, 5, 6):
                response = self.get_led_movie_config()["frames_number"]
            else:
                response = self.get_movies()["movies"]
            return self.set_mode("effect" if not response else "movie")

    def turn_off(self):
        """
        Turns off the device.

        Remembers the previous mode, so that turn_on() can return to it.
        """
        mode = self.get_mode()["mode"]
        if mode != "off" and mode != "rt":
            self.last_mode = mode
        return self.set_mode("off")

    def is_on(self):
        """
        Returns True if device is on
        """
        return self.get_mode()["mode"] != "off"

    def set_mode(self, mode):
        """
        Sets new LED operation mode.

        :param str mode: Mode to set. One of 'movie', 'playlist', 'rt', 'demo', 'effect', 'color' or 'off'.
        This function is a wrapper around the low-level ControlInterface.set_mode,
        to remember the currently used mode.
        """
        assert mode in ("movie", "playlist", "rt", "demo", "effect", "color", "off")
        self.curr_mode = mode
        if mode != "off" and mode != "rt":
            self.last_mode = mode
        if mode == "rt":
            self.last_rt_time = time.time()
        super(HighControlInterface, self).set_mode(mode)

    # Functions for selecting what to show

    def show_movie(self, movie_or_id, fps=None):
        """
        Either starts playing an already uploaded movie with the provided id,
        or uploads a new movie and starts playing it at the provided frames-per-second.
        Note: if the movie do not fit in the remaining capacity, the old movie list is cleared.
        Switches to movie mode if necessary.
        The movie is an object suitable created with to_movie or make_func_movie.

        :param movie_or_id: either an integer id or a file-like object that points to movie
        :param fps: frames per second, or None if a movie id is given
        """
        if self.family == "D" or self.version < (2, 5, 6):
            if isinstance(movie_or_id, int) and fps is None:
                if movie_or_id != 0:
                    return False
            else:
                assert fps
                movie = movie_or_id
                numframes = movie.seek(0, 2) // (self.led_bytes * self.num_leds)
                movie.seek(0)
                self.set_led_movie_config(1000 // max(1, fps), numframes, self.num_leds)
                self.set_led_movie_full(movie)
        else:
            if isinstance(movie_or_id, int) and fps is None:
                movies = self.get_movies()["movies"]
                if movie_or_id in [entry["id"] for entry in movies]:
                    self.set_movies_current(movie_or_id)
                else:
                    return False
            else:
                assert fps
                movie = movie_or_id
                numframes = movie.seek(0, 2) // (self.led_bytes * self.num_leds)
                movie.seek(0)
                res = self.get_movies()
                capacity = res["available_frames"] - 1
                if numframes > capacity or len(res["movies"]) > 15:
                    if self.curr_mode == "movie" or self.curr_mode == "playlist":
                        self.set_mode("off")
                    self.delete_movies()
                self.set_movies_new(
                    "",
                    str(uuid.uuid4()),
                    self.led_profile.lower() + "_raw",
                    self.num_leds,
                    numframes,
                    fps,
                )
                self.set_movies_full(movie)
        if self.curr_mode != "movie":
            self.set_mode("movie")
        return True

    def upload_movie(self, movie, fps, force=False):
        """
        Uploads a new movie with the provided frames-per-second.
        Note: if the movie does not fit in the remaining capacity, and force is
        not set to True, the function just returns False, in which case the user
        can try clear_movies first.
        Does not switch to movie mode, use show_movie instead for that.
        The movie is an object suitable created with to_movie or make_func_movie.
        Returns the new movie id, which can be used in calls to show_movie or
        show_playlist.

        :param movie: a file-like object that points to movie
        :param fps: frames per second, or None if a movie id is given
        :param bool force: if remaining capacity is too low, previous movies will be removed
        :rtype: int
        """
        numframes = movie.seek(0, 2) // (self.led_bytes * self.num_leds)
        movie.seek(0)
        if self.family == "D" or self.version < (2, 5, 6):
            self.set_led_movie_config(1000 // fps, numframes, self.num_leds)
            self.set_led_movie_full(movie)
            return 0
        else:
            res = self.get_movies()
            capacity = res["available_frames"] - 1
            if numframes > capacity or len(res["movies"]) > 15:
                if force:
                    if self.curr_mode == "movie" or self.curr_mode == "playlist":
                        self.set_mode("effect")
                    self.delete_movies()
                else:
                    return False
            if self.curr_mode == "movie":
                oldid = self.get_movies_current()["id"]
            res = self.set_movies_new(
                "",
                str(uuid.uuid4()),
                self.led_profile.lower() + "_raw",
                self.num_leds,
                numframes,
                fps,
            )
            self.set_movies_full(movie)
            if self.curr_mode == "movie":
                self.set_movies_current(oldid)  # Dont change currently shown movie
            return res["id"]

    def show_pattern(self, pat):
        """
        Uploads a single pattern as a static movie, and shows it.
        Switches to movie mode if necessary.
        The parameter is a pattern object eg created with make_solid_pattern or make_func_pattern.

        :param pat: list of byte strings representing a single frame pattern
        """
        self.show_movie(self.to_movie(pat), 1)

    def show_playlist(self, lst_or_id, duration=None):
        """
        Either switches to the movie with the given id in the playlist,
        or uploads a new playlist in the form of a list where each entry is
        either an id, or a tuple with an id and a duration. The optional
        parameter duration is used for those entries without a duration.
        Switches to playlist mode if necessary.

        :param lst_or_id: integer movie id, or list of ids and durations
        :param duration: default duration to use for entries without duration
        """
        if self.family == "D" or self.version < (2, 5, 6):
            return False
        else:
            if isinstance(lst_or_id, int) and duration is None:
                plist = self.get_playlist()["entries"]
                if lst_or_id in [entry["id"] for entry in plist]:
                    if self.curr_mode != "playlist":
                        self.set_mode("playlist")
                    self.set_playlist_current(lst_or_id)
                else:
                    return False
            else:
                assert isinstance(lst_or_id, list)
                mlist = self.get_movies()["movies"]
                mdict = {entry["id"]: entry["unique_id"] for entry in mlist}
                plist = []
                for ele in lst_or_id:
                    if isinstance(ele, int):
                        plist.append(
                            {"unique_id": mdict[ele], "duration": duration or 60}
                        )
                    else:
                        plist.append({"unique_id": mdict[ele[0]], "duration": ele[1]})
                self.set_playlist(plist)
                if self.curr_mode != "playlist":
                    self.set_mode("playlist")
            return True

    def show_rt_frame(self, frame):
        """
        Uploads a frame as the next real time frame, and shows it.
        Switches to rt mode if necessary.
        The frame is either a pattern or a one-frame movie

        :param frame: a pattern or file-like object representing the frame
        """
        if self.is_pattern(frame):
            frame = self.to_movie(frame)
        if self.curr_mode != "rt" or self.last_rt_time + 50.0 < time.time():
            self.set_mode("rt")
        else:
            self.last_rt_time = time.time()
        frame.seek(0)
        # self.set_rt_frame_rest(frame)
        if self.family == "D":
            self.set_rt_frame_socket(frame, 1, self.num_leds)
        elif self.version < (2, 4, 14):
            self.set_rt_frame_socket(frame, 2)
        else:
            self.set_rt_frame_socket(frame, 3)

    def show_effect(self, effect_id):
        """
        Shows the builtin effect with the provided id.
        Switches to effect mode if necessary.

        :param int effect_id: The effect id to show
        """
        self.set_led_effects_current(effect_id)
        if self.curr_mode != "effect":
            self.set_mode("effect")

    def show_demo(self, effect_id=None):
        """
        Switches to demo mode if not there already.
        Starts from the optional provided effect id.

        :param effect_id: The optional effect id to start demo from
        """
        if effect_id:
            self.set_led_effects_current(effect_id)
        if self.curr_mode != "demo":
            self.set_mode("demo")

    def show_color(self, rgb):
        """
        Shows the given rgb color on all leds.
        Switches to color mode if necessary.

        :param tuple rgb: Tuple representing the red, green, and blue components
        """
        if self.version < (2, 7, 1):
            self.show_pattern(self.make_solid_pattern(rgb))
        else:
            self.set_led_color_rgb(*rgb)
            if self.curr_mode != "color":
                self.set_mode("color")

    def clear_movies(self):
        """
        Removes all uploaded movies and any playlist.
        If the current mode is 'movie' or 'playlist' it switches mode to 'effect'
        """
        if self.curr_mode == "movie" or self.curr_mode == "playlist":
            self.set_mode("effect")
        if self.family == "D" or self.version < (2, 5, 6):
            # No list of movies to remove in this version,
            # but disable movie mode until new movie is uploaded
            self.set_led_movie_config(1000, 0, self.num_leds)
        else:
            # The playlist is removed automatically when movies are removed
            self.delete_movies()

    # Functions for creating and manipulating movies and patterns (single frames of movies)

    def make_func_movie(self, numframes, func):
        """
        Creates a movie of a number of frames by calling a function to create each frame.
        The function is expected to take the frame index as argument and to return a
        pattern object representing the frame.

        :param int numframes: The number of frames for the movie
        :param function func: A function to produce each frame
        :rtype: _io.BytesIO
        """
        pl = []
        for i in range(numframes):
            pl.append(func(i))
        return self.to_movie(pl)

    def make_empty_movie(self):
        """
        Creates a movie of zero frames.
        Meant to be followed by several calls to add_to_movie to add frames to it.

        :rtype: _io.BytesIO
        """
        movie = io.BytesIO()
        return movie

    def is_pattern(self, pat):
        """
        Checks whether the given argument has the format of a single frame pattern.

        :param pat: object to check whether it is a pattern
        :rtype: bool
        """
        return (
            isinstance(pat, list)
            and len(pat) == self.num_leds
            and isinstance(pat[0], bytes)
        )

    def is_movie(self, movie):
        """
        Checks whether the given argument has the format of a movie.

        :param movie: object to check whether it is a movie
        :rtype: bool
        """
        return isinstance(movie, io.BytesIO)

    def add_to_movie(self, movie, pat):
        """
        Adds one pattern as a frame to the end of a movie.

        :param movie: file-like object representing the movie
        :param pat: object representing the pattern
        :rtype: _io.BytesIO
        """
        assert self.is_pattern(pat)
        movie.seek(0, 2)
        movie.write(b"".join(pat))
        movie.seek(0, 0)

    def to_movie(self, patlst):
        """
        Creates a movie from either a single pattern or a list of patterns.

        :param patlst: pattern or list of patterns
        :rtype: _io.BytesIO
        """
        movie = io.BytesIO()
        if isinstance(patlst, list):
            for ele in patlst:
                if isinstance(ele, list):
                    ele = b"".join(ele)
                movie.write(ele)
        else:
            movie.write(patlst)
        movie.seek(0)
        return movie

    def circind(self, ind):
        """
        Internal function used to fascilitate linear or circular effects. That
        is, if the device consists of two strings, flip the led indices of one
        of the strings so they start at the extreme end of the first string
        and runs into the middle where the strings meet and then continue out
        on the other string. If the extreme ends of the two strings are
        arranged to meet again, it allows for circular patterns.
        """
        if len(self.string_config) == 2 and ind < self.string_config[0]["length"]:
            return self.string_config[0]["length"] - 1 - ind
        else:
            return ind

    def make_pixel(self, r, g, b):
        """
        Internal function to produce one pixel of a pattern from given r, g
        and b values. Handles both RGB and RGBW led profiles (for now always
        setting the white led to zero).

        :param int r: red component
        :param int g: green component
        :param int b: blue component
        :rtype: bytes
        """
        if self.led_bytes == 4:
            return struct.pack(">BBBB", 0, r, g, b)
        else:
            return struct.pack(">BBB", r, g, b)

    def make_solid_pattern(self, rgb):
        """
        Creates a one-colored pattern with the given rgb value tuple.

        :param tuple rgb: color as an rgb tuple
        :rtype: list representing the pattern
        """
        pat = [self.make_pixel(*rgb)] * self.num_leds
        return pat

    def make_func_pattern(self, func, circular=False):
        """
        Creates a pattern by calling the given function for each led.
        The function is expected to take the led index as argument and to
        return a color as an rgb tuple for that led.

        :param function func: function to return the color of each pixel
        :param bool circular: Flip the led indices on two-string devices to enable circular patterns
        :rtype: list representing the pattern
        """
        pat = [False] * self.num_leds
        for i in range(self.num_leds):
            (r, g, b) = func(i)
            if circular:
                pat[self.circind(i)] = self.make_pixel(r, g, b)
            else:
                pat[i] = self.make_pixel(r, g, b)
        return pat

    def fetch_layout(self, aspect=False):
        if self.version > (2, 2, 1):
            res = self.get_led_layout()
            if res["source"] == "3d":
                if aspect:
                    self.layout = [
                        (p["x"] * aspect[0] * 0.5, p["y"], p["z"] * aspect[1] * 0.5)
                        for p in res["coordinates"]
                    ]
                else:
                    self.layout = [
                        (p["x"] * 0.5, p["y"], p["z"] * 0.5) for p in res["coordinates"]
                    ]
                dim = 3
            elif res["source"] == "2d":
                if aspect:
                    self.layout = [
                        (p["x"] * aspect[0] * 0.5, p["y"]) for p in res["coordinates"]
                    ]
                else:
                    self.layout = [(p["x"] * 0.5, p["y"]) for p in res["coordinates"]]
                dim = 2
            else:
                self.layout = [(p["x"],) for p in res["coordinates"]]
                dim = 1
        else:
            self.layout = [(float(self.circind(i)) / (self.num_leds - 1),) for i in range(self.num_leds)]
            dim = 1
        bounds = []
        cent = []
        rad = 0.0
        for d in range(dim):
            vals = [p[d] for p in self.layout]
            bounds.append((min(vals), max(vals)))
            cent.append(sum(vals) / len(vals))
        for p in self.layout:
            r2 = sum([(p[d] - cent[d]) ** 2 for d in range(dim)])
            if r2 > rad:
                rad = r2
        self.layout_bounds = {
            "dim": dim,
            "bounds": bounds,
            "center": cent,
            "radius": rad ** 0.5,
        }
        if dim == 3:
            crad = 0.0
            for p in self.layout:
                r2 = (p[0] - cent[0]) ** 2 + (p[2] - cent[2]) ** 2
                if r2 > crad:
                    crad = r2
            self.layout_bounds["cylradius"] = crad ** 0.5

    def layout_transform(self, pos, style):
        # style == 'square', 'rect', 'centered', 'cylinder', 'sphere'
        if style == "square":
            # Stretch everything into [0, 1] in each coordinate
            return tuple(
                (v - b[0]) / (b[1] - b[0])
                for v, b in zip(pos, self.layout_bounds["bounds"])
            )
        elif style == "rect":
            # Keep aspect ratio, largest into [-1,1]
            cent = ((b[0] + b[1]) / 2 for b in self.layout_bounds["bounds"])
            width = max((b[1] - b[0]) / 2 for b in self.layout_bounds["bounds"])
            return tuple((v - c) / width for v, c in zip(pos, cent))
        elif style == "centered":
            # Origo in center, max radius 1.0
            rad = self.layout_bounds["radius"]
            return tuple(
                (v - c) / rad for v, c in zip(pos, self.layout_bounds["center"])
            )
        elif style == "cylinder" and self.layout_bounds["dim"] == 3:
            # xz-radius max 1, angle in [-180,180], y in [0, 1]
            crad = self.layout_bounds["cylradius"]
            ybounds = self.layout_bounds["bounds"][1]
            p = ((v - c) / crad for v, c in zip(pos, self.layout_bounds["center"]))
            return (
                m.sqrt(p[0] ** 2 + p[2] ** 2),
                m.atan2(p[2], p[0]) * 180.0 / m.pi,
                (p[1] * crad - ybounds[0]) / (ybounds[1] - ybounds[0]),
            )
        elif style == "sphere" and self.layout_bounds["dim"] == 3:
            # radius max 1, longitude [-180,180], latitude [-90,90]
            rad = self.layout_bounds["radius"]
            p = ((v - c) / rad for v, c in zip(pos, self.layout_bounds["center"]))
            return (
                m.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2),
                m.atan2(p[2], p[0]) * 180.0 / m.pi,
                m.atan2(p[1], m.sqrt(p[0] ** 2 + p[2] ** 2)) * 180.0 / m.pi,
            )
        else:
            return pos

    def make_layout_pattern(self, func, style=None, index=False):
        """
        Creates a pattern by calling the given function for each led.
        The function is expected to take the led physical position as
        argument (1d, 2d, or 3d depending on the layout source) and to
        return a color as an rgb tuple for that led.

        :param function func: function to return the color of each pixel
        :rtype: list representing the pattern
        """
        if not self.layout:
            self.fetch_layout()
        pat = [False] * self.num_leds
        for i in range(self.num_leds):
            pos = self.layout_transform(self.layout[i], style)
            if index:
                (r, g, b) = func(pos, i)
            else:
                (r, g, b) = func(pos)
            pat[i] = self.make_pixel(r, g, b)
        return pat

    def adjust_layout_aspect(self, aspect_xy, aspect_zy=False):
        if aspect_xy and aspect_zy:
            self.fetch_layout((aspect_xy, aspect_zy))
        elif aspect_xy:
            self.fetch_layout((aspect_xy, aspect_xy))
        else:
            self.fetch_layout()

    def get_layout_bounds(self):
        if not self.layout:
            self.fetch_layout()
        return self.layout_bounds

    def copy_pattern(self, pat):
        """
        Make a copy of a pattern.
        In case you want to make destructive operations on one of them.

        :param pat: object representing the pattern
        :rtype: list representing the pattern
        """
        return [ele for ele in pat]

    def modify_pattern(self, pat, ind, rgb, circular=False):
        """
        Modifies one pixel in a pattern.
        Changes the pattern in place. Make sure to copy it if you need the old one.

        :param pat: object representing the pattern
        :param int ind: led index in the pattern
        :param tuple rgb: color as an rgb tuple
        :param bool circular: Flip the led indices on two-string devices to enable circular patterns
        :rtype: list representing the pattern (the same object as pat)
        """
        if circular:
            pat[self.circind(ind)] = self.make_pixel(*rgb)
        else:
            pat[ind] = self.make_pixel(*rgb)
        return pat

    def shift_pattern(self, pat, step, rgb, circular=False):
        """
        Shifts the pattern a number of steps, padding with the provided rgb color.
        Non-destructive, leaving the original pattern unmodified.

        :param pat: object representing the pattern
        :param int step: steps to shift, can be positive or negative
        :param tuple rgb: color as an rgb tuple
        :param bool circular: Flip the led indices on two-string devices to enable circular patterns
        :rtype: list representing the pattern
        """
        pix = self.make_pixel(*rgb)
        if circular and len(self.string_config) == 2:
            n1 = self.string_config[0]["length"]
            n2 = self.num_leds
            p1 = pat[0:n1]
            p2 = pat[n1:n2]
            if step > 0:
                for i in range(step):
                    p2 = p1[:1] + p2[:-1]
                    p1 = p1[1:] + [pix]
            else:
                for i in range(-step):
                    p1 = p2[:1] + p1[:-1]
                    p2 = p2[1:] + [pix]
            pat = p1 + p2
        else:
            if step > 0:
                pat = [pix] * step + pat[:-step]
            else:
                pat = pat[-step:] + [pix] * -step
        return pat

    def rotate_pattern(self, pat, step, circular=False):
        """
        Shifts the pattern a number of steps with rotation, so that pixels
        shifted out at one end emerges at the other end.
        Non-destructive, leaving the original pattern unmodified.

        :param pat: object representing the pattern
        :param int step: steps to shift, can be positive or negative
        :param bool circular: Flip the led indices on two-string devices to enable circular patterns
        :rtype: list representing the pattern
        """
        if circular and len(self.string_config) == 2:
            n1 = self.string_config[0]["length"]
            n2 = self.num_leds
            p1 = pat[0:n1]
            p2 = pat[n1:n2]
            if step > 0:
                for i in range(step):
                    tmp = p1[0]
                    p1 = p1[1:] + p2[-1:]
                    p2 = [tmp] + p2[:-1]
            else:
                for i in range(-step):
                    tmp = p1[-1]
                    p1 = p2[:1] + p1[:-1]
                    p2 = p2[1:] + [tmp]
            pat = p1 + p2
        else:
            pat = pat[-step:] + pat[:-step]
        return pat

    def permute_pattern(self, pat, perm, circular=False):
        """
        Permutes the pattern according to the provided permutation list.
        The new index 'i' will get the same color as the old index 'perm[i]'.
        Non-destructive, leaving the original pattern unmodified.

        :param pat: object representing the pattern
        :param list perm: permutation list of source indices
        :param bool circular: Flip the led indices on two-string devices to enable circular patterns
        :rtype: list representing the pattern
        """
        newpat = [False] * len(pat)
        if circular:
            for i, k in enumerate(perm):
                newpat[self.circind(i)] = pat[self.circind(k)]
        else:
            for i, k in enumerate(perm):
                newpat[i] = pat[k]
        return newpat

    def save_movie(self, name, movie, fps):
        """
        Save the movie object on file.
        The movie file is text based and starts with a header containing
        the number of frames, number of leds, number of bytes per led, and
        the suggested frames per second. After the header follows one line per
        frame as a hexadecimal string. This format makes it easier to share
        movies between different devices and even different led profiles.
        """
        bytesperframe = self.led_bytes * self.num_leds
        numframes = movie.seek(0, 2) // bytesperframe
        movie.seek(0)
        f = open(name, "w")
        f.write("{} {} {} {}\n".format(numframes, self.num_leds, self.led_bytes, fps))
        for i in range(numframes):
            f.write(binascii.hexlify(movie.read(bytesperframe)).decode() + "\n")
        f.close()

    def load_movie(self, name):
        """
        Read a movie from a file (produced by save_movie).
        Returns both the movie object and the suggested frames-per-second in a tuple.
        Some effort is made to convert movies between different devices:
        If the number of leds are different, each frame is padded or truncated
        at both ends. If the led profile is different, the white component is
        removed or added (as zero).
        """
        f = open(name, "r")
        head = list(map(int, f.readline().strip("\n").split(" ")))
        numframes = head[0]
        fps = head[3]
        movie = io.BytesIO()
        if head[1] == self.num_leds and head[2] == self.led_bytes:
            for i in range(numframes):
                movie.write(binascii.unhexlify(f.readline().strip("\n")))
        else:
            for i in range(numframes):
                s = binascii.unhexlify(f.readline().strip("\n"))
                if head[2] == 3 and self.led_bytes == 4:
                    s = b"".join(
                        [chr(0) + s[3 * i : 3 * i + 3] for i in range(len(s) // 3)]
                    )
                elif head[2] == 4 and self.led_bytes == 3:
                    s = b"".join([s[4 * i + 1 : 4 * i + 4] for i in range(len(s) // 4)])
                if head[1] < self.num_leds:
                    diff = self.num_leds - head[1]
                    s = (
                        chr(0) * (diff // 2 * self.led_bytes)
                        + s
                        + chr(0) * ((diff - diff // 2) * self.led_bytes)
                    )
                elif head[1] > self.num_leds:
                    hdiff = (head[1] - self.num_leds) // 2
                    s = s[hdiff : hdiff + self.num_leds * self.led_bytes]
                movie.write(s)
        movie.seek(0)
        return (movie, fps)
