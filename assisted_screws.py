# Guided bed levelling for Klipper: mechanical gantry alignment on stops,
# live probe feedback while turning bed screws, and recalibration reminders.
#
# Screws, thread, horizontal_move_z and speed are read from [screws_tilt_adjust].
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import time

THREAD_PITCH = {'CW-M3': 0.5, 'CCW-M3': 0.5, 'CW-M4': 0.7, 'CCW-M4': 0.7,
                'CW-M5': 0.8, 'CCW-M5': 0.8, 'CW-M6': 1.0, 'CCW-M6': 1.0}
Z_STEPPERS = ['stepper_z', 'stepper_z1']
VAR_HOURS = 'assisted_screws_print_hours'
VAR_CAL_HOURS = 'assisted_screws_calibrated_hours'
VAR_CAL_TIME = 'assisted_screws_calibrated_at'

class ScrewUnreachable(Exception):
    def __init__(self, name, z_min):
        Exception.__init__(self, "%s: probe not triggered down to z=%.2f; the bed"
                           " corner is too low or the probe is off the bed"
                           % (name, z_min))
        self.name = name

class AssistedScrews:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        src = config.getsection('screws_tilt_adjust')
        self.screws = []
        for i in range(99):
            prefix = "screw%d" % (i + 1,)
            if src.get(prefix, None) is None:
                break
            coord = src.getfloatlist(prefix, count=2)
            self.screws.append((coord, src.get(prefix + "_name", prefix)))
        if len(self.screws) < 2:
            raise config.error("assisted_screws: [screws_tilt_adjust] needs"
                               " at least two screws")
        thread = src.get('screw_thread', 'CW-M3')
        if thread not in THREAD_PITCH:
            raise config.error("assisted_screws: unsupported screw_thread %s"
                               % (thread,))
        self.pitch = THREAD_PITCH[thread]
        self.clockwise = thread.startswith('CW')
        self.move_z = src.getfloat('horizontal_move_z', 5., above=0.)
        self.speed = src.getfloat('speed', 50., above=0.)
        self.lead = config.getsection('stepper_z').getfloat(
            'rotation_distance', 8., above=0.)
        # Screw levelling
        self.tolerance = config.getfloat('tolerance', 0.02, above=0.)
        self.confirm = config.getint('confirm', 3, minval=1)
        self.interval = config.getfloat('interval', 3., minval=0.)
        self.timeout = config.getfloat('timeout', 300., above=0.)
        self.lift = config.getfloat('lift', 2., above=0.)
        self.max_rounds = config.getint('max_rounds', 3, minval=1)
        # Each screw is checked by stepping the nozzle down from check_z_start to
        # check_z_min until the probe reports TRIGGERED
        self.check_z_start = config.getfloat('check_z_start', 2., above=0.)
        self.check_z_min = config.getfloat('check_z_min', -1., maxval=self.check_z_start)
        self.check_step = config.getfloat('check_step', 0.2, above=0.)
        config.getfloat('check_z', None)  # deprecated, accepted so old configs still load
        # Gantry alignment
        self.gantry_height = config.getfloat('gantry_height', 50., above=0.)
        self.stall_current = config.getfloat('stall_current', 0.25, above=0.)
        self.stop_height = config.getfloat('stop_height', None, above=0.)
        # Height of the X axis end bottom above the stop base with the nozzle at z=0
        self.gantry_base_offset = config.getfloat('gantry_base_offset', None)
        self.stall_margin = config.getfloat('stall_margin', 3., above=0.)
        self.min_nozzle_z = config.getfloat('min_nozzle_z', 5., above=0.)
        self.stops_z = config.getfloat('stops_z', 60., above=0.)
        # Nozzle X off the bed while lowering onto the stops
        self.stall_x = config.getfloat('stall_x', None)
        self.raise_after_stall = config.getfloat('raise_after_stall', 30., above=0.)
        self.recalibrate_hours = config.getfloat('recalibrate_hours', 200., above=0.)
        self.state = 'ready'
        self.print_hours = 0.
        self.calibrated_hours = None
        self.calibrated_at = None
        self.unsaved_minutes = 0
        self.gcode = self.printer.lookup_object('gcode')
        for cmd in ['ASSISTED_BED_CALIBRATE', 'ASSISTED_SCREWS_LEVEL',
                    'ASSISTED_GANTRY_CHECK', 'ASSISTED_GANTRY_STALL']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd),
                                        desc=getattr(self, 'cmd_%s_help' % (cmd,)))
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def get_status(self, eventtime):
        hours_since = None
        if self.calibrated_hours is not None:
            hours_since = self.print_hours - self.calibrated_hours
        return {'state': self.state, 'print_hours': self.print_hours,
                'calibrated_at': self.calibrated_at, 'hours_since': hours_since,
                'recalibrate_hours': self.recalibrate_hours}

    # Persistence and reminders -------------------------------------------------
    def _handle_ready(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is not None:
            variables = sv.get_status(None)['variables']
            self.print_hours = float(variables.get(VAR_HOURS, 0.))
            self.calibrated_hours = variables.get(VAR_CAL_HOURS)
            self.calibrated_at = variables.get(VAR_CAL_TIME)
        now = self.reactor.monotonic()
        self.reactor.register_timer(self._count_hours, now + 60.)
        self.reactor.register_timer(self._boot_check, now + 15.)

    def _save(self, name, value, from_command=False):
        if self.printer.lookup_object('save_variables', None) is None:
            return
        script = "SAVE_VARIABLE VARIABLE=%s VALUE=%r" % (name, value)
        if from_command:
            self.gcode.run_script_from_command(script)
        else:
            self.gcode.run_script(script)

    def _count_hours(self, eventtime):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None and ps.get_status(eventtime)['state'] == 'printing':
            self.print_hours += 1. / 60.
            self.unsaved_minutes += 1
            if self.unsaved_minutes >= 10:
                self.unsaved_minutes = 0
                self._save(VAR_HOURS, round(self.print_hours, 3))
        return eventtime + 60.

    def _boot_check(self, eventtime):
        if self.calibrated_hours is None:
            reason = "The bed has never been calibrated with assisted_screws."
        elif self.print_hours - self.calibrated_hours >= self.recalibrate_hours:
            reason = ("%.0f print hours since the last bed calibration."
                      % (self.print_hours - self.calibrated_hours,))
        else:
            return self.reactor.NEVER
        self._prompt("Bed calibration recommended", [reason],
                     [("Calibrate now", "ASSISTED_BED_CALIBRATE", "primary"),
                      ("Later", "RESPOND TYPE=command MSG=action:prompt_end",
                       "secondary")])
        return self.reactor.NEVER

    # Helpers -------------------------------------------------------------------
    def _run(self, script):
        self.gcode.run_script_from_command(script)

    def _prompt(self, title, lines, buttons):
        self.gcode.respond_raw("// action:prompt_begin %s" % (title,))
        for line in lines:
            self.gcode.respond_raw("// action:prompt_text %s" % (line,))
        for label, script, color in buttons:
            self.gcode.respond_raw("// action:prompt_button %s|%s|%s"
                                   % (label, script, color))
        self.gcode.respond_raw("// action:prompt_show")

    def _prompt_end(self):
        self.gcode.respond_raw("// action:prompt_end")

    def _homed(self, axes):
        status = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic())
        return all(a in status['homed_axes'] for a in axes), status

    def _goto(self, coord):
        self._run("G90\nG1 Z%.3f F%d\nG1 X%.3f Y%.3f F%d"
                  % (self.move_z, 15 * 60, coord[0], coord[1],
                     self.speed * 60))

    def _probe(self):
        self._run("PROBE")
        z = self.printer.lookup_object('probe').get_status(
            None)['last_probe_position'][2]
        self._run("G1 Z%.3f F%d" % (z + self.lift, 15 * 60))
        return z

    def _advice(self, diff):
        turns = diff / self.pitch
        if self.clockwise:
            sign = "CW" if turns >= 0 else "CCW"
        else:
            sign = "CCW" if turns >= 0 else "CW"
        turns = abs(turns)
        full = int(turns)
        return "%s %02d:%02d" % (sign, full, int(round((turns - full) * 60.)))

    def _check_reachable(self, gcmd):
        # PROBE descends to the Z limit when it never triggers, so first make
        # sure every screw position is detectable from a safe nozzle height
        probe = self.printer.lookup_object('probe')
        for coord, name in self.screws:
            self._goto(coord)
            z = self.check_z_start
            triggered = False
            while z >= self.check_z_min - 1e-6:
                self._run("G1 Z%.3f F%d\nM400\nQUERY_PROBE" % (z, 3 * 60))
                triggered = probe.get_status(None)['last_query']
                if triggered:
                    break
                z -= self.check_step
            self._run("G1 Z%.3f F%d" % (self.move_z, 15 * 60))
            if not triggered:
                raise ScrewUnreachable(name, self.check_z_min)

    def _level_round(self, gcmd, tol, confirm, interval, timeout):
        """Reference screw + live loop on every other screw; returns final deviations."""
        self._check_reachable(gcmd)
        ref_coord, ref_name = self.screws[0]
        self._goto(ref_coord)
        samples = [self._probe() for _ in range(confirm)]
        ref = sum(samples) / len(samples)
        gcmd.respond_info("Reference %s: z=%.4f (spread %.4f), do not touch it"
                          % (ref_name, ref, max(samples) - min(samples)))
        for coord, name in self.screws[1:]:
            self._goto(coord)
            start = self.reactor.monotonic()
            in_tol = 0
            while True:
                diff = ref - self._probe()
                in_tol = in_tol + 1 if abs(diff) <= tol else 0
                if in_tol:
                    gcmd.respond_info("%s: %+.4f mm OK (%d/%d)"
                                      % (name, -diff, in_tol, confirm))
                else:
                    gcmd.respond_info("%s: %+.4f mm -> adjust %s"
                                      % (name, -diff, self._advice(diff)))
                if in_tol >= confirm:
                    break
                if self.reactor.monotonic() - start > timeout:
                    self._run("G1 Z%.3f F%d" % (self.move_z, 15 * 60))
                    raise gcmd.error("%s not within %.3f mm after %.0fs"
                                     % (name, tol, timeout))
                if interval:
                    self._run("G4 P%d" % (interval * 1000.,))
        deviations = []
        for coord, name in self.screws:
            self._goto(coord)
            deviations.append((name, self._probe() - ref))
        self._run("G1 Z%.3f F%d" % (self.move_z + 10., 15 * 60))
        gcmd.respond_info("Check against reference:\n" + "\n".join(
            "%s: %+.4f mm" % d for d in deviations))
        return deviations

    def _stall(self, gcmd, distance, current, velocity):
        eventtime = self.reactor.monotonic()
        original = {}
        for s in Z_STEPPERS:
            tmc = self.printer.lookup_object('tmc2209 ' + s, None)
            if tmc is None:
                raise gcmd.error("assisted_screws: needs a tmc2209 on %s" % (s,))
            original[s] = tmc.get_status(eventtime)['run_current']
        try:
            for s in Z_STEPPERS:
                self._run("SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f" % (s, current))
            # Lower both motors together so the gantry never tilts; the side
            # that reaches its stop first skips steps until the other arrives
            gcmd.respond_info("Lowering the gantry up to %.1f mm onto the stops"
                              % (distance,))
            self._run("SET_KINEMATIC_POSITION Z=%.3f\nG90\nG1 Z0 F%d\nM400"
                      % (distance, velocity * 60))
        finally:
            for s in Z_STEPPERS:
                self._run("SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f" % (s, original[s]))

    def _stall_distance(self, gcmd):
        ok, status = self._homed('z')
        if not ok:
            raise gcmd.error("Home Z first or pass DISTANCE=")
        z = status['position'][2]
        if self.stop_height is not None and self.gantry_base_offset is not None:
            gap = z + self.gantry_base_offset - self.stop_height
            if gap <= 0.:
                raise gcmd.error("Gantry is already at or below the stops")
            return gap + self.stall_margin
        # Unknown gap: lower until the nozzle would be at min_nozzle_z;
        # the gantry rests on the stops long before that
        if z <= self.min_nozzle_z:
            raise gcmd.error("Raise Z above %.1f mm first" % (self.min_nozzle_z,))
        return z - self.min_nozzle_z

    # Guided calibration ---------------------------------------------------------
    cmd_ASSISTED_BED_CALIBRATE_help = ("Guided calibration: gantry on stops,"
                                       " assisted bed screws, then z_tilt")
    def cmd_ASSISTED_BED_CALIBRATE(self, gcmd):
        step = gcmd.get('STEP', 'START').upper()
        if step == 'CANCEL':
            self.state = 'ready'
            self._prompt_end()
            gcmd.respond_info("Bed calibration cancelled")
            return
        if step == 'SCREWS':
            # Allowed on its own: skips the gantry alignment
            self._check_idle(gcmd)
            self.state = 'calibrating'
        elif step != 'START' and self.state != 'calibrating':
            raise gcmd.error("No calibration in progress, run ASSISTED_BED_CALIBRATE")
        cancel = ("Cancel", "ASSISTED_BED_CALIBRATE STEP=CANCEL", "error")
        try:
            if step == 'START':
                self._step_start(gcmd, cancel)
            elif step == 'STOPS_IN':
                self._step_stops_in(gcmd, cancel)
            elif step in ('STOPS_OUT', 'SCREWS'):
                self._step_screws(gcmd, cancel)
            else:
                raise gcmd.error("Unknown STEP=%s" % (step,))
        except Exception:
            self.state = 'ready'
            self._prompt_end()
            raise

    def _check_idle(self, gcmd):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None and ps.get_status(self.reactor.monotonic())['state'] in (
                'printing', 'paused'):
            raise gcmd.error("Cannot calibrate while a print is active")

    def _step_start(self, gcmd, cancel):
        self._check_idle(gcmd)
        self.state = 'calibrating'
        self._prompt_end()
        self._run("G28")
        moves = "G90\nG1 Z%.3f F600" % (self.stops_z,)
        if self.stall_x is not None:
            moves += "\nG1 X%.3f F3000" % (self.stall_x,)
        self._run(moves + "\nM400")
        self._prompt("Bed calibration 1/3: insert the stops",
                     ["Place both stops under the X axis ends, next to each Z lead screw.",
                      "The nozzle is parked off the bed."],
                     [("Stops inserted", "ASSISTED_BED_CALIBRATE STEP=STOPS_IN",
                       "primary"),
                      ("Skip (gantry already aligned)",
                       "ASSISTED_BED_CALIBRATE STEP=SCREWS", "secondary"), cancel])

    def _step_stops_in(self, gcmd, cancel):
        self._prompt_end()
        self._stall(gcmd, self._stall_distance(gcmd), self.stall_current, 3.)
        # Both sides rest on the stops: declare that height and raise both together
        self._run("SET_KINEMATIC_POSITION Z=0\nG91\nG1 Z%.3f F600\nG90\nM400"
                  % (self.raise_after_stall,))
        self._prompt("Bed calibration 2/3: remove the stops",
                     ["The X axis is now level with the frame.",
                      "Remove BOTH stops before continuing: the printer homes next."],
                     [("Stops removed", "ASSISTED_BED_CALIBRATE STEP=STOPS_OUT",
                       "primary"), cancel])

    def _step_screws(self, gcmd, cancel):
        self._prompt_end()
        self._run("G28")
        deviations = []
        for rnd in range(1, self.max_rounds + 1):
            gcmd.respond_info("Screw levelling round %d of %d" % (rnd, self.max_rounds))
            try:
                deviations = self._level_round(gcmd, self.tolerance, self.confirm,
                                               self.interval, self.timeout)
            except ScrewUnreachable as e:
                # Stay in calibration: the user raises that corner and retries
                self._run("G1 Z%.3f F%d" % (self.move_z + 10., 15 * 60))
                self._prompt("Bed corner too low",
                             [str(e), "Raise that corner (turn its screw to lift the"
                              " bed a few turns), then retry."],
                             [("Retry", "ASSISTED_BED_CALIBRATE STEP=SCREWS", "primary"),
                              cancel])
                return
            if all(abs(d) <= self.tolerance for _, d in deviations):
                break
        worst = max(abs(d) for _, d in deviations)
        levelled = worst <= self.tolerance
        if self.printer.lookup_object('z_tilt', None) is not None:
            self._run("Z_TILT_ADJUST")
        self.calibrated_hours = round(self.print_hours, 3)
        self.calibrated_at = int(time.time())
        self._save(VAR_HOURS, self.calibrated_hours, True)
        self._save(VAR_CAL_HOURS, self.calibrated_hours, True)
        self._save(VAR_CAL_TIME, self.calibrated_at, True)
        self.state = 'ready'
        title = "Bed calibration 3/3: done"
        lines = ["Worst screw deviation %.3f mm (tolerance %.3f)."
                 % (worst, self.tolerance)]
        if not levelled:
            title = "Bed calibration finished outside tolerance"
            lines.append("Still out of tolerance after %d rounds: check the bed"
                         " and springs." % (self.max_rounds,))
        self._prompt(title, lines,
                     [("Close", "RESPOND TYPE=command MSG=action:prompt_end", "primary")])

    # Individual tools -------------------------------------------------------------
    cmd_ASSISTED_SCREWS_LEVEL_help = ("Probe each bed screw in a loop while"
                                      " it is adjusted, then move on")
    def cmd_ASSISTED_SCREWS_LEVEL(self, gcmd):
        if not self._homed('xyz')[0]:
            raise gcmd.error("Home all axes before ASSISTED_SCREWS_LEVEL")
        try:
            self._level_round(
                gcmd, gcmd.get_float('TOLERANCE', self.tolerance, above=0.),
                gcmd.get_int('CONFIRM', self.confirm, minval=1),
                gcmd.get_float('INTERVAL', self.interval, minval=0.),
                gcmd.get_float('TIMEOUT', self.timeout, above=0.))
        except ScrewUnreachable as e:
            raise gcmd.error(str(e))

    cmd_ASSISTED_GANTRY_STALL_help = ("Lower each Z motor at reduced current onto"
                                      " mechanical stops so both sides match")
    def cmd_ASSISTED_GANTRY_STALL(self, gcmd):
        distance = gcmd.get_float('DISTANCE', None, above=0., maxval=80.)
        if distance is None:
            distance = self._stall_distance(gcmd)
        if distance > 80.:
            raise gcmd.error("Computed distance %.1f mm is too large" % (distance,))
        self._stall(gcmd, distance,
                    gcmd.get_float('CURRENT', self.stall_current, above=0.),
                    gcmd.get_float('VELOCITY', 3., above=0., maxval=10.))
        self._run("M84")
        gcmd.respond_info("Both sides lowered onto the stops. Remove the stops,"
                          " then home (G28) and run Z_TILT_ADJUST")

    cmd_ASSISTED_GANTRY_CHECK_help = ("Raise the gantry for a manual left/right"
                                      " height check; LEFT=/RIGHT= evaluates it")
    def cmd_ASSISTED_GANTRY_CHECK(self, gcmd):
        left = gcmd.get_float('LEFT', None)
        right = gcmd.get_float('RIGHT', None)
        if left is None or right is None:
            height = gcmd.get_float('HEIGHT', self.gantry_height, above=0.)
            self._run("G90\nG1 Z%.3f F%d\nM400" % (height, 10 * 60))
            gcmd.respond_info(
                "Gantry at z=%.1f. Measure on both sides the same reference"
                " (e.g. top of the X axis end to the Z motor mount), then run:\n"
                "ASSISTED_GANTRY_CHECK LEFT=<mm> RIGHT=<mm>" % (height,))
            return
        diff = left - right
        if abs(diff) < 0.05:
            gcmd.respond_info("Gantry level: left %.2f / right %.2f mm" % (left, right))
            return
        high = "left" if diff > 0 else "right"
        if gcmd.get_int('APPLY', 0, minval=0, maxval=1):
            # stepper_z drives the left side and stepper_z1 the right
            stepper = Z_STEPPERS[0] if diff > 0 else Z_STEPPERS[1]
            self._run("FORCE_MOVE STEPPER=%s DISTANCE=%.3f VELOCITY=2"
                      % (stepper, -abs(diff)))
            self._run("M84")
            gcmd.respond_info(
                "Lowered the %s side by %.2f mm (%s). Motors released: measure"
                " again, then home (G28) and run Z_TILT_ADJUST"
                % (high, abs(diff), stepper))
            return
        gcmd.respond_info(
            "The %s side is %.2f mm higher (%.2f lead screw turns, lead %.1f mm)."
            " With the Z motors disabled, lower that side by hand, then home"
            " and run Z_TILT_ADJUST." % (high, abs(diff), abs(diff) / self.lead,
                                         self.lead))

def load_config(config):
    return AssistedScrews(config)
