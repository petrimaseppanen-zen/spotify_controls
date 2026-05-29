import ctypes
import threading
import tkinter as tk
import time
from ctypes import wintypes
from pathlib import Path


APP_NAME = "Spotify Mini Controller"
DEFAULT_APP_VERSION = "1.0.0"


def load_app_version():
    version_file = Path(__file__).with_name("VERSION.txt")
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return DEFAULT_APP_VERSION


APP_VERSION = load_app_version()

try:
    import numpy as np
    HAS_NUMPY = True
    NUMPY_IMPORT_ERROR = ""
except ImportError as exc:
    np = None
    HAS_NUMPY = False
    NUMPY_IMPORT_ERROR = str(exc)

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
    SOUNDDEVICE_IMPORT_ERROR = ""
except ImportError as exc:
    sd = None
    HAS_SOUNDDEVICE = False
    SOUNDDEVICE_IMPORT_ERROR = str(exc)

try:
    import soundcard as sc
    HAS_SOUNDCARD = True
    SOUNDCARD_IMPORT_ERROR = ""
except ImportError as exc:
    sc = None
    HAS_SOUNDCARD = False
    SOUNDCARD_IMPORT_ERROR = str(exc)

HAS_SPECTRUM_DEPS = HAS_NUMPY and (HAS_SOUNDCARD or HAS_SOUNDDEVICE)
if not HAS_NUMPY:
    SPECTRUM_IMPORT_ERROR = NUMPY_IMPORT_ERROR
elif not HAS_SOUNDCARD and not HAS_SOUNDDEVICE:
    SPECTRUM_IMPORT_ERROR = f"soundcard: {SOUNDCARD_IMPORT_ERROR}; sounddevice: {SOUNDDEVICE_IMPORT_ERROR}"
else:
    SPECTRUM_IMPORT_ERROR = ""

# Windows virtual key codes for media controls
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1

KEYEVENTF_KEYUP = 0x0002

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class TrackInfo:
    def __init__(self, title="Not playing", artist="Open Spotify and play a track"):
        self.title = title
        self.artist = artist


class SpectrumAnalyzer:
    def __init__(self, bars=40):
        self.bars = bars
        if HAS_SPECTRUM_DEPS:
            self.levels = np.zeros(self.bars, dtype=np.float32)
            self._last_levels = np.zeros(self.bars, dtype=np.float32)
        else:
            self.levels = [0.0] * self.bars
            self._last_levels = [0.0] * self.bars
        self._lock = threading.Lock()
        self._stream = None
        self.available = False
        self.source_mode = "none"
        self._worker = None
        self._stop_event = threading.Event()
        self.selected_device_name = ""
        self.preferred_source = "auto"
        self.last_error = "" if HAS_SPECTRUM_DEPS else SPECTRUM_IMPORT_ERROR
        self.blocksize = 512
        self.sample_rate = 48000
        self.analysis_size = 1024
        self.attack = 0.9
        self.release = 0.72
        self.silence_decay = 0.45
        self.peak_decay = 0.9
        self.noise_gate = 0.04
        self.gravity = 0.92
        self.min_fall_per_update = 0.014
        self.calibration_frames = 50
        self.floor_rise_per_frame = 0.35
        self.floor_fall_per_frame = 0.35
        self.peak_fall_per_frame = 0.55
        self.transient_sensitivity = 0.18
        self.accuracy = 0.68
        self.beat_rise = 0.62
        self.beat_decay = 0.86
        self.min_bpm = 72.0
        self.max_bpm = 178.0
        self.tempo_recalc_interval = 8
        self.bass_presence_gate = 0.012
        self.direct_frequency_mode = True
        self.transition_frames = 18
        self._transition_left = 0
        self.gain_lock_seconds = 10.0
        self._gain_locked = False
        self._frames_seen = 0
        if HAS_SPECTRUM_DEPS:
            self._analysis_buffer = np.zeros(self.analysis_size, dtype=np.float32)
            self._window = np.hanning(self.analysis_size).astype(np.float32)
            self._bands = None
            self._band_peak_db = np.full(self.bars, -36.0, dtype=np.float32)
            self._band_floor_db = np.full(self.bars, -84.0, dtype=np.float32)
            self._prev_db = np.full(self.bars, -120.0, dtype=np.float32)
            self._beat_env = 0.0
            self._last_low_energy = 0.0
            self._low_energy_ema = 1e-6
            self._low_flux_ema = 0.0
            self._tempo_hist = np.zeros(384, dtype=np.float32)
            self._tempo_hist_idx = 0
            self._tempo_hist_filled = 0
            self._tempo_period_frames = 0.0
            self._tempo_confidence = 0.0
            self._beat_phase = 0.0
            self._tempo_recalc_counter = 0
            self._current_bpm = 0.0
            self._signal_level = 0.0
    
    def set_bars(self, bars):
        bars = max(16, min(96, int(bars)))
        if bars == self.bars:
            return

        self.bars = bars
        if HAS_SPECTRUM_DEPS:
            with self._lock:
                self.levels = np.zeros(self.bars, dtype=np.float32)
                self._last_levels = np.zeros(self.bars, dtype=np.float32)
                self._band_peak_db = np.full(self.bars, -36.0, dtype=np.float32)
                self._band_floor_db = np.full(self.bars, -84.0, dtype=np.float32)
                self._prev_db = np.full(self.bars, -120.0, dtype=np.float32)
                self._beat_env = 0.0
                self._last_low_energy = 0.0
                self._low_energy_ema = 1e-6
                self._low_flux_ema = 0.0
                self._tempo_hist = np.zeros(384, dtype=np.float32)
                self._tempo_hist_idx = 0
                self._tempo_hist_filled = 0
                self._tempo_period_frames = 0.0
                self._tempo_confidence = 0.0
                self._beat_phase = 0.0
                self._tempo_recalc_counter = 0
                self._current_bpm = 0.0
                self._signal_level = 0.0
                self._transition_left = 0
                self._gain_locked = False
                self._frames_seen = 0
            self._bands = None
        else:
            self.levels = [0.0] * self.bars
            self._last_levels = [0.0] * self.bars

    def reset_calibration(self):
        if not HAS_SPECTRUM_DEPS:
            return
        with self._lock:
            self._frames_seen = 0
            self._band_peak_db = np.full(self.bars, -36.0, dtype=np.float32)
            self._band_floor_db = np.full(self.bars, -84.0, dtype=np.float32)
            self._prev_db = np.full(self.bars, -120.0, dtype=np.float32)
            self._beat_env = 0.0
            self._last_low_energy = 0.0
            self._low_energy_ema = 1e-6
            self._low_flux_ema = 0.0
            self._tempo_hist = np.zeros(384, dtype=np.float32)
            self._tempo_hist_idx = 0
            self._tempo_hist_filled = 0
            self._tempo_period_frames = 0.0
            self._tempo_confidence = 0.0
            self._beat_phase = 0.0
            self._tempo_recalc_counter = 0
            self._current_bpm = 0.0
            self._signal_level = 0.0
            self._transition_left = 0
            self._gain_locked = False
            self._last_levels = np.zeros(self.bars, dtype=np.float32)
            self.levels = np.zeros(self.bars, dtype=np.float32)

    def notify_track_change(self):
        if not HAS_SPECTRUM_DEPS:
            return
        with self._lock:
            self._transition_left = self.transition_frames

    def _current_fps(self):
        return max(1.0, float(self.sample_rate) / float(self.blocksize))

    def _update_tempo_lock(self, onset_value):
        if self._tempo_hist.shape[0] == 0:
            return

        self._tempo_hist[self._tempo_hist_idx] = float(onset_value)
        self._tempo_hist_idx = (self._tempo_hist_idx + 1) % self._tempo_hist.shape[0]
        self._tempo_hist_filled = min(self._tempo_hist.shape[0], self._tempo_hist_filled + 1)

        if self._tempo_recalc_counter > 0:
            self._tempo_recalc_counter -= 1
            return
        self._tempo_recalc_counter = self.tempo_recalc_interval

        fps = self._current_fps()
        min_lag = max(4, int(round((60.0 / self.max_bpm) * fps)))
        max_lag = int(round((60.0 / self.min_bpm) * fps))
        if self._tempo_hist_filled < (max_lag * 2 + 8):
            return

        if self._tempo_hist_filled < self._tempo_hist.shape[0]:
            series = self._tempo_hist[: self._tempo_hist_filled]
        else:
            series = np.concatenate((self._tempo_hist[self._tempo_hist_idx :], self._tempo_hist[: self._tempo_hist_idx]))

        series = series.astype(np.float32, copy=False)
        centered = series - float(np.mean(series))
        variance = float(np.dot(centered, centered))
        if variance <= 1e-6:
            self._tempo_confidence *= 0.97
            return

        ac = np.correlate(centered, centered, mode="full")
        ac = ac[ac.shape[0] // 2 :]
        max_lag = min(max_lag, ac.shape[0] - 1)
        if max_lag <= min_lag:
            return

        segment = ac[min_lag : max_lag + 1]
        if segment.size == 0:
            return

        best_idx = int(np.argmax(segment))
        best_lag = min_lag + best_idx
        best_score = float(segment[best_idx]) / max(float(ac[0]), 1e-6)
        if best_score < 0.16:
            self._tempo_confidence *= 0.96
            return

        target_period = float(best_lag)
        if self._tempo_period_frames <= 0.0:
            self._tempo_period_frames = target_period
        else:
            self._tempo_period_frames = (self._tempo_period_frames * 0.84) + (target_period * 0.16)
        self._tempo_confidence = min(1.0, (self._tempo_confidence * 0.75) + (best_score * 0.9))
        self._current_bpm = (60.0 * fps) / max(self._tempo_period_frames, 1e-6)

    def get_telemetry(self):
        with self._lock:
            return {
                "bpm": float(self._current_bpm),
                "tempo_confidence": float(self._tempo_confidence),
                "signal": float(self._signal_level),
                "latency_ms": (float(self.blocksize) / max(float(self.sample_rate), 1.0)) * 1000.0,
                "source": self.source_mode,
            }

    def set_motion(self, speed, falloff, accuracy=None):
        speed = max(0.0, min(1.0, float(speed)))
        falloff = max(0.0, min(1.0, float(falloff)))
        if accuracy is None:
            accuracy = self.accuracy
        accuracy = max(0.0, min(1.0, float(accuracy)))
        self.accuracy = accuracy

        # Speed controls responsiveness, falloff controls how quickly bars return down.
        self.attack = 0.6 + (0.38 * speed)
        self.release = min(0.97, 0.52 + (0.24 * speed) + (0.24 * falloff))
        self.silence_decay = 0.8 - (0.55 * falloff)
        self.peak_decay = 0.98 - (0.2 * falloff)
        self.noise_gate = 0.008 + (0.045 * falloff)
        self.gravity = 0.97 - (0.17 * falloff)
        self.min_fall_per_update = 0.004 + (0.032 * falloff)
        self.floor_rise_per_frame = 0.18 + (0.42 * accuracy)
        self.floor_fall_per_frame = 0.2 + (0.5 * accuracy)
        self.peak_fall_per_frame = 0.35 + (0.7 * accuracy)
        self.transient_sensitivity = 0.08 + (0.14 * speed) + (0.12 * accuracy)
        self.beat_rise = 0.45 + (0.45 * accuracy)
        self.beat_decay = 0.72 + (0.22 * (1.0 - falloff))
        self.bass_presence_gate = 0.007 + (0.018 * accuracy)
        self.transition_frames = int(8 + (18 * accuracy))

    def _device_priority(self, dev_name):
        name = dev_name.lower()
        score = 0
        if "headphone" in name or "headset" in name:
            score += 100
        if "bluetooth" in name:
            score += 10
        if "speaker" in name:
            score += 5
        return score

    def get_source_options(self):
        options = [("Auto (recommended)", "auto")]
        if not HAS_SPECTRUM_DEPS:
            return options

        if HAS_SOUNDCARD:
            try:
                for speaker in sc.all_speakers():
                    options.append((f"Loopback: {speaker.name}", f"loopback::{speaker.id}"))
            except Exception:
                pass

        if HAS_SOUNDDEVICE:
            try:
                for idx, dev in enumerate(sd.query_devices()):
                    if int(dev.get("max_input_channels", 0)) > 0:
                        name = dev.get("name", f"device {idx}")
                        options.append((f"Mic: {name}", f"mic::{idx}"))
            except Exception:
                pass

        return options

    def set_preferred_source(self, source_id):
        if not source_id:
            source_id = "auto"
        self.preferred_source = str(source_id)
        return self.start()

    def _build_bands(self, freqs):
        low_hz = 35.0
        high_hz = min(16000.0, self.sample_rate * 0.5 - 20.0)
        # Linear spacing gives equal Hz width for every bar.
        hz_edges = np.linspace(low_hz, high_hz, self.bars + 1)

        bands = []
        bin_count = freqs.shape[0]
        # Skip DC bin for better musical movement.
        min_bin = 1 if bin_count > 1 else 0
        prev_center = min_bin - 1
        for i in range(self.bars):
            lo = hz_edges[i]
            hi = hz_edges[i + 1]
            lo_idx = int(np.searchsorted(freqs, lo, side="left"))
            hi_idx = int(np.searchsorted(freqs, hi, side="left"))

            lo_idx = max(min_bin, min(lo_idx, bin_count - 1))
            hi_idx = max(lo_idx + 1, min(hi_idx, bin_count))

            idx = np.arange(lo_idx, hi_idx, dtype=np.int32)
            if idx.size == 0:
                nearest = int(np.argmin(np.abs(freqs - ((lo + hi) * 0.5))))
                nearest = max(min_bin, min(nearest, bin_count - 1))
                idx = np.array([nearest], dtype=np.int32)

            # Avoid identical adjacent low-band movement by preferring a new center bin.
            center = int(idx[idx.size // 2])
            if center <= prev_center and prev_center + 1 < bin_count:
                center = prev_center + 1
                idx = np.array([center], dtype=np.int32)
            prev_center = center
            bands.append(idx)
        return bands

    def _push_samples(self, mono):
        n = len(mono)
        if n >= self.analysis_size:
            self._analysis_buffer[:] = mono[-self.analysis_size :]
            return
        self._analysis_buffer[:-n] = self._analysis_buffer[n:]
        self._analysis_buffer[-n:] = mono

    def _process_samples(self, mono):
        self._push_samples(mono)
        frame = self._analysis_buffer

        if not np.any(mono):
            self._beat_env *= self.beat_decay
            self._tempo_confidence *= 0.985
            self._signal_level *= 0.9
            with self._lock:
                self._last_levels *= self.silence_decay
                self.levels = self._last_levels.copy()
            return

        self._signal_level = (self._signal_level * 0.92) + (float(np.sqrt(np.mean(frame * frame))) * 0.08)
        spectrum = np.abs(np.fft.rfft(frame * self._window))
        if spectrum.shape[0] <= 2:
            return

        freqs = np.fft.rfftfreq(len(frame), d=1.0 / float(self.sample_rate))
        if self._bands is None or len(self._bands) != self.bars:
            self._bands = self._build_bands(freqs)

        power = spectrum * spectrum
        bar_count = self.bars
        values = np.zeros(bar_count, dtype=np.float32)

        for i in range(bar_count):
            chunk = power[self._bands[i]]
            if chunk.size:
                values[i] = float(np.sqrt(np.mean(chunk)))

        # Bass/mid onset tracking is only needed for reactive mode.
        reactive_mode = not self.direct_frequency_mode
        low_count = max(2, bar_count // 6)
        low_energy = float(np.mean(values[:low_count]))
        if reactive_mode:
            self._low_energy_ema = (self._low_energy_ema * 0.93) + (low_energy * 0.07)
            low_delta = max(0.0, low_energy - self._last_low_energy)
            self._last_low_energy = low_energy
            low_flux = low_delta / max(self._low_energy_ema, 1e-6)
            self._low_flux_ema = (self._low_flux_ema * 0.9) + (low_flux * 0.1)
            beat_target = np.clip((low_flux - (self._low_flux_ema * 1.06)) * 2.1, 0.0, 1.0)
            if beat_target > self._beat_env:
                self._beat_env += (beat_target - self._beat_env) * self.beat_rise
            else:
                self._beat_env *= self.beat_decay

            self._update_tempo_lock(beat_target)
            if self._tempo_period_frames > 0.0:
                self._beat_phase += 1.0
                if beat_target > 0.55 and self._beat_phase > (self._tempo_period_frames * 0.32):
                    self._beat_phase = 0.0
                if self._beat_phase >= self._tempo_period_frames:
                    self._beat_phase -= self._tempo_period_frames
                phase = self._beat_phase / max(self._tempo_period_frames, 1e-6)
                tempo_pulse = float(((np.cos(2.0 * np.pi * phase) + 1.0) * 0.5) ** 3)
            else:
                tempo_pulse = 0.0
        else:
            self._beat_env *= 0.96
            self._tempo_confidence *= 0.94
            self._current_bpm *= 0.96
            tempo_pulse = 0.0

        # Adaptive per-band dB normalization prevents bars from staying pinned at max.
        db = 20.0 * np.log10(np.maximum(values, 1e-8)).astype(np.float32)

        if self._prev_db.shape[0] != db.shape[0]:
            self._prev_db = np.full(db.shape[0], -120.0, dtype=np.float32)
        flux_db = np.maximum(0.0, db - self._prev_db)
        self._prev_db = db.copy()

        lock_frames = int(self.gain_lock_seconds * self._current_fps())
        if (not self._gain_locked) and self._frames_seen >= lock_frames:
            self._gain_locked = True

        if self._frames_seen == 0:
            # Start with a tight range around measured level to avoid startup spikes.
            self._band_peak_db = db + 7.0
            self._band_floor_db = db - 10.0
        elif self._gain_locked:
            # Lock the learned gain reference so long tracks do not sink over time.
            self._band_peak_db = np.maximum(self._band_peak_db, db)
            self._band_floor_db = np.minimum(self._band_floor_db, db)
        else:
            # Peak follows new highs immediately and falls gradually.
            self._band_peak_db = np.maximum(db, self._band_peak_db - self.peak_fall_per_frame)
            # Floor follows level quickly upward at startup and drops on new lows.
            floor_rise = np.minimum(db, self._band_floor_db + self.floor_rise_per_frame)
            floor_fall = np.minimum(self._band_floor_db, db + self.floor_fall_per_frame)
            self._band_floor_db = np.where(db >= self._band_floor_db, floor_rise, floor_fall)

        band_range = np.maximum(18.0, self._band_peak_db - self._band_floor_db)
        norm = (db - self._band_floor_db) / band_range
        norm = np.clip(norm, 0.0, 1.0)
        norm = np.power(norm, 1.4)
        band_pos = np.linspace(0.0, 1.0, bar_count, dtype=np.float32)
        gate_shape = self.noise_gate + (0.03 * np.clip(0.35 - band_pos, 0.0, 1.0))
        norm = np.maximum(0.0, (norm - gate_shape) / np.maximum(1e-6, 1.0 - gate_shape))

        if self.direct_frequency_mode:
            # Frequency-true mode: each bar follows its own band with no cross-band coupling.
            direct_range = np.maximum(10.0, self._band_peak_db - self._band_floor_db)
            direct_norm = np.clip((db - self._band_floor_db) / direct_range, 0.0, 1.0)
            direct_norm = np.power(direct_norm, 0.92)
            direct_gate = gate_shape * 0.28
            direct_norm = np.maximum(0.0, (direct_norm - direct_gate) / np.maximum(1e-6, 1.0 - direct_gate))

            # Keep only bands that are close enough to the frame's strongest content.
            frame_peak_db = float(np.max(db))
            presence_mask = np.clip((db - (frame_peak_db - 30.0)) / 18.0, 0.0, 1.0)
            direct_norm *= presence_mask

            # If low-end is weak compared to low-mid, suppress the lowest bars.
            low_bars = max(2, bar_count // 8)
            mid_slice = values[low_bars : max(low_bars + 2, low_bars * 3)]
            mid_energy = float(np.mean(mid_slice)) if mid_slice.size else low_energy
            low_ratio = low_energy / max(mid_energy, 1e-8)
            low_presence = np.clip((low_ratio - 0.32) / 0.8, 0.0, 1.0)
            low_shape = np.clip((0.22 - band_pos) / 0.22, 0.0, 1.0)
            low_mask = 1.0 - (0.45 * low_shape * (1.0 - low_presence))
            direct_norm *= low_mask

            norm = np.maximum(norm * 0.55, direct_norm)
        else:
            # Enhanced reactive mode with cross-band smoothing and rhythm accents.
            left = np.roll(norm, 1)
            right = np.roll(norm, -1)
            left[0] = norm[0]
            right[-1] = norm[-1]
            base_neighbor_weight = 0.18 - (0.12 * self.accuracy)
            low_cut = np.clip((0.28 - band_pos) / 0.28, 0.0, 1.0)
            high_cut = np.clip((band_pos - 0.78) / 0.22, 0.0, 1.0)
            neighbor_weight = base_neighbor_weight * (1.0 - (0.7 * low_cut)) * (1.0 - (0.72 * high_cut))
            center_weight = 1.0 - (2.0 * neighbor_weight)
            smooth_norm = (norm * center_weight) + (left * neighbor_weight) + (right * neighbor_weight)
            dry_weight = 0.68 - (0.28 * self.accuracy)
            norm = np.maximum(norm * dry_weight, smooth_norm)

            flux_scale = 24.0 - (10.0 * self.accuracy)
            transient_boost = np.clip(flux_db / flux_scale, 0.0, 1.0) * self.transient_sensitivity
            norm = np.clip(norm + transient_boost, 0.0, 1.0)

            beat_mask = np.clip(1.0 - (band_pos * 1.55), 0.0, 1.0)
            beat_mask = np.power(beat_mask, 1.7)
            rhythm_drive = max(self._beat_env, (self._beat_env * 0.6) + (tempo_pulse * self._tempo_confidence * 0.9))
            bass_presence = np.clip((low_energy - self.bass_presence_gate) / max(1e-6, self.bass_presence_gate * 2.0), 0.0, 1.0)
            rhythm_drive *= bass_presence
            beat_gain = (0.08 + (0.12 * self.accuracy)) * rhythm_drive
            norm = np.clip(norm + (beat_mask * beat_gain), 0.0, 1.0)

        # Warmup avoids transient "all bars max" behavior at song start.
        warmup = min(1.0, self._frames_seen / float(self.calibration_frames))
        norm_cap = 0.18 + (0.82 * warmup)
        norm = np.minimum(norm, norm_cap)

        # During a song switch, quickly fade stale state and suppress inherited tempo pulse.
        if self._transition_left > 0:
            t = self._transition_left / float(max(1, self.transition_frames))
            damp = 1.0 - (0.75 * t)
            norm *= damp
            self._tempo_confidence *= 0.82
            self._beat_env *= 0.78
            self._transition_left -= 1

        self._frames_seen += 1

        with self._lock:
            if self._last_levels.shape[0] != norm.shape[0]:
                self._last_levels = np.zeros_like(norm)
                self.levels = np.zeros_like(norm)
            # Fast attack + aggressive release to return close to zero between hits.
            rising = norm > self._last_levels
            self._last_levels[rising] = (self._last_levels[rising] * (1.0 - self.attack)) + (norm[rising] * self.attack)
            self._last_levels[~rising] = (self._last_levels[~rising] * (1.0 - self.release)) + (norm[~rising] * self.release)
            self._last_levels[~rising] = np.maximum(0.0, self._last_levels[~rising] - self.min_fall_per_update)
            # Extra gravity prevents bars from hanging in the air between bass hits.
            self._last_levels *= self.gravity
            self.levels = self._last_levels.copy()

    def _sounddevice_callback(self, indata, frames, _time, status):
        if status or frames <= 0:
            return
        mono = indata[:, 0].astype(np.float32, copy=False)
        self._process_samples(mono)

    def _start_loopback_speaker(self, speaker):
        self.selected_device_name = speaker.name
        if self._device_priority(speaker.name) >= 100:
            self.source_mode = "loopback-headphones"
        else:
            self.source_mode = "loopback"

        self._worker = threading.Thread(
            target=self._loopback_worker,
            args=(speaker.id,),
            daemon=True,
        )
        self._worker.start()
        self.available = True
        return True

    def _start_mic_fallback(self, preferred_idx=None):
        if not HAS_SOUNDDEVICE:
            return False

        devices = sd.query_devices()
        mic_candidates = []

        if preferred_idx is not None:
            preferred_idx = int(preferred_idx)
            if preferred_idx >= 0:
                mic_candidates.append(preferred_idx)
        else:
            default_input = sd.default.device[0]
            if default_input is not None and default_input >= 0:
                mic_candidates.append(int(default_input))

            for idx, dev in enumerate(devices):
                if int(dev.get("max_input_channels", 0)) > 0 and idx not in mic_candidates:
                    mic_candidates.append(idx)

        for idx in mic_candidates:
            try:
                dev = devices[idx]
                samplerate = int(dev["default_samplerate"]) if dev["default_samplerate"] else 48000
                self.sample_rate = samplerate
                self._bands = None
                self._stream = sd.InputStream(
                    device=idx,
                    channels=1,
                    samplerate=samplerate,
                    blocksize=self.blocksize,
                    callback=self._sounddevice_callback,
                    dtype="float32",
                )
                self._stream.start()
                self.selected_device_name = dev.get("name", f"device {idx}")
                self.available = True
                self.source_mode = "mic"
                return True
            except Exception as exc:
                self.last_error = str(exc)

        return False

    def _loopback_worker(self, speaker_id):
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        try:
            loopback_mic = sc.get_microphone(speaker_id, include_loopback=True)
            self.sample_rate = 48000
            self._bands = None
            with loopback_mic.recorder(samplerate=self.sample_rate, channels=1, blocksize=self.blocksize) as rec:
                while not self._stop_event.is_set():
                    block = rec.record(numframes=self.blocksize)
                    if block is None or block.size == 0:
                        continue
                    mono = block[:, 0].astype(np.float32, copy=False)
                    self._process_samples(mono)
        except Exception as exc:
            self.last_error = str(exc)
            self.available = False
        finally:
            ole32.CoUninitialize()

    def start(self):
        if not HAS_SPECTRUM_DEPS:
            self.available = False
            self.source_mode = "none"
            self.selected_device_name = ""
            self.last_error = SPECTRUM_IMPORT_ERROR
            return False

        self.stop()
        self.last_error = ""
        self._stop_event.clear()

        preferred = self.preferred_source or "auto"

        if preferred.startswith("loopback::") and HAS_SOUNDCARD:
            speaker_id = preferred.split("::", 1)[1]
            try:
                for speaker in sc.all_speakers():
                    if str(speaker.id) == speaker_id:
                        return self._start_loopback_speaker(speaker)
                self.last_error = "Selected loopback device not found"
            except Exception as exc:
                self.last_error = str(exc)

        if preferred.startswith("mic::") and HAS_SOUNDDEVICE:
            mic_idx = preferred.split("::", 1)[1]
            try:
                if self._start_mic_fallback(preferred_idx=int(mic_idx)):
                    return True
                self.last_error = self.last_error or "Selected microphone not available"
            except Exception as exc:
                self.last_error = str(exc)

        if HAS_SOUNDCARD:
            try:
                speakers = list(sc.all_speakers())
                if speakers:
                    speakers = sorted(speakers, key=lambda sp: self._device_priority(sp.name), reverse=True)
                    speaker = speakers[0]
                    return self._start_loopback_speaker(speaker)
            except Exception as exc:
                self.last_error = str(exc)

        if self._start_mic_fallback():
            return True

        self.available = False
        self.source_mode = "none"
        self.selected_device_name = ""
        self.last_error = self.last_error or "No compatible audio source"
        return False

    def get_levels(self):
        with self._lock:
            if HAS_SPECTRUM_DEPS:
                return self.levels.copy()
            return list(self.levels)

    def stop(self):
        self._stop_event.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        finally:
            self._stream = None
        self._worker = None


def send_media_key(vk_code: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(vk_code, 0, 0, 0)
    user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if length <= 0:
        return ""

    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.byref(buffer))
    return buffer.value.strip()


def _process_name_from_hwnd(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""

    try:
        image_path = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(len(image_path))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, image_path, ctypes.byref(size))
        if not ok:
            return ""

        full_path = image_path.value
        if not full_path:
            return ""
        return full_path.split("\\")[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def get_spotify_track_info() -> TrackInfo:
    user32 = ctypes.windll.user32
    blocked_titles = {"spotify", "spotify premium", "spotify free"}
    found_spotify_window = False
    best_info = None

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        nonlocal found_spotify_window, best_info

        if not user32.IsWindowVisible(hwnd):
            return True

        process_name = _process_name_from_hwnd(hwnd)
        if process_name != "spotify.exe":
            return True

        found_spotify_window = True
        title = _window_text(hwnd)
        if not title:
            return True

        normalized = title.strip().lower()
        if normalized in blocked_titles:
            return True

        if " - " in title:
            artist, song = title.split(" - ", 1)
            song = song.strip()
            artist = artist.strip()
            if song and artist:
                best_info = TrackInfo(song, artist)
                return False

        best_info = TrackInfo(title.strip(), "Spotify")
        return False

    user32.EnumWindows(enum_proc(callback), 0)

    if best_info is not None:
        return best_info
    if found_spotify_window:
        return TrackInfo("No track info", "Try playing a track")
    return TrackInfo("Spotify not open", "Open the Spotify app")


class HoverButton(tk.Label):
    def __init__(
        self,
        master,
        text,
        command,
        bg="#1f2937",
        hover_bg="#334155",
        press_bg="#0f172a",
        fg="#f9fafb",
        hover_fg="#ffffff",
        border_color="#334155",
        hover_border="#475569",
        font=("Segoe UI", 9, "bold"),
        pad_x=12,
        pad_y=7,
        **kwargs,
    ):
        super().__init__(
            master,
            text=text,
            bg=bg,
            fg=fg,
            font=font,
            padx=pad_x,
            pady=pad_y,
            cursor="hand2",
            bd=0,
            highlightthickness=1,
            highlightbackground=border_color,
            highlightcolor=border_color,
            **kwargs,
        )
        self.default_bg = bg
        self.hover_bg = hover_bg
        self.press_bg = press_bg
        self.default_fg = fg
        self.hover_fg = hover_fg
        self.default_border = border_color
        self.hover_border = hover_border
        self.command = command
        self.is_pressed = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, _event):
        if self.is_pressed:
            return
        self.configure(bg=self.hover_bg, fg=self.hover_fg, highlightbackground=self.hover_border, highlightcolor=self.hover_border)

    def _on_leave(self, _event):
        if self.is_pressed:
            self.is_pressed = False
        self.configure(bg=self.default_bg, fg=self.default_fg, highlightbackground=self.default_border, highlightcolor=self.default_border)

    def _on_press(self, _event):
        self.is_pressed = True
        self.configure(bg=self.press_bg, fg=self.hover_fg, highlightbackground=self.hover_border, highlightcolor=self.hover_border)

    def _on_click(self, _event):
        # Immediate click action is more reliable for compact Tk labels.
        self.command()
        self.is_pressed = False
        self.configure(bg=self.hover_bg, fg=self.hover_fg, highlightbackground=self.hover_border, highlightcolor=self.hover_border)


class SpotifyMiniController:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("350x390+40+40")
        self.root.configure(bg="#0b1220")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Map>", self._on_restore_from_minimize)
        self.is_closing = False

        self.spectrum = SpectrumAnalyzer(bars=40)
        self.spectrum.start()
        self.render_levels = [0.0] * self.spectrum.bars
        self.peak_levels = [0.0] * self.spectrum.bars
        self.peak_hold = [0] * self.spectrum.bars
        self.frame_ms = 14
        self._last_ui_time = time.perf_counter()
        # Easy rollback switch for centered/mirrored visual mode.
        self.centered_spectrum = True

        self.speed_var = tk.DoubleVar(value=0.0)
        self.falloff_var = tk.DoubleVar(value=0.0)
        self.accuracy_var = tk.DoubleVar(value=0.0)
        self.spectrum.set_motion(self.speed_var.get(), self.falloff_var.get(), self.accuracy_var.get())
        self._last_track_key = None
        self.controls_expanded = True
        self.settings_expanded = False
        self.source_options = []
        self.source_label_to_id = {}
        self.source_var = tk.StringVar(value="Auto (recommended)")

        self.offset_x = 0
        self.offset_y = 0

        self.container = tk.Frame(
            self.root,
            bg="#111827",
            bd=1,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#1f2937",
        )
        self.container.pack(fill="both", expand=True, padx=6, pady=6)

        self.header = tk.Frame(self.container, bg="#111827")
        self.header.pack(fill="x", padx=8, pady=(6, 4))

        self.title = tk.Label(
            self.header,
            text="Spotify Controls",
            bg="#111827",
            fg="#d1d5db",
            font=("Segoe UI", 9, "bold"),
        )
        self.title.pack(side="left")

        self.version_badge = tk.Label(
            self.header,
            text=f"v{APP_VERSION}",
            bg="#111827",
            fg="#6b7280",
            font=("Segoe UI", 7),
            padx=2,
        )
        self.version_badge.pack(side="right", padx=(0, 6))

        self.min_btn = tk.Label(
            self.header,
            text="-",
            bg="#111827",
            fg="#9ca3af",
            font=("Segoe UI", 10, "bold"),
            padx=6,
            cursor="hand2",
        )
        self.min_btn.pack(side="right")
        self.min_btn.bind("<Enter>", lambda _e: self.min_btn.configure(fg="#93c5fd"))
        self.min_btn.bind("<Leave>", lambda _e: self.min_btn.configure(fg="#9ca3af"))
        self.min_btn.bind("<Button-1>", lambda _e: self.minimize())

        self.close_btn = tk.Label(
            self.header,
            text="x",
            bg="#111827",
            fg="#9ca3af",
            font=("Segoe UI", 9, "bold"),
            padx=6,
            cursor="hand2",
        )
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Enter>", lambda _e: self.close_btn.configure(fg="#f87171"))
        self.close_btn.bind("<Leave>", lambda _e: self.close_btn.configure(fg="#9ca3af"))
        self.close_btn.bind("<Button-1>", lambda _e: self.close())

        self.meta = tk.Frame(self.container, bg="#111827")
        self.meta.pack(fill="x", padx=10, pady=(0, 4))

        self.track_var = tk.StringVar(value="Loading...")
        self.artist_var = tk.StringVar(value="")

        self.track_label = tk.Label(
            self.meta,
            textvariable=self.track_var,
            bg="#111827",
            fg="#f9fafb",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        self.track_label.pack(fill="x")

        self.artist_label = tk.Label(
            self.meta,
            textvariable=self.artist_var,
            bg="#111827",
            fg="#9ca3af",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.artist_label.pack(fill="x", pady=(1, 0))

        self.spectrum_wrap = tk.Frame(self.container, bg="#111827")
        self.spectrum_wrap.pack(fill="x", padx=10, pady=(0, 5))

        self.spectrum_canvas = tk.Canvas(
            self.spectrum_wrap,
            width=320,
            height=42,
            bg="#0f172a",
            bd=0,
            highlightthickness=1,
            highlightbackground="#1f2937",
        )
        self.spectrum_canvas.pack(fill="x")

        self.spectrum_hint = tk.Label(
            self.spectrum_wrap,
            text="",
            bg="#111827",
            fg="#64748b",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.spectrum_hint.pack(fill="x", pady=(2, 0))

        self._update_spectrum_hint()

        self.live_status_var = tk.StringVar(value="LIVE ready")
        self.live_status = tk.Label(
            self.container,
            textvariable=self.live_status_var,
            bg="#0f172a",
            fg="#a7f3d0",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=3,
            anchor="w",
        )
        self.live_status.pack(fill="x", padx=10, pady=(0, 5))

        self.buttons = tk.Frame(self.container, bg="#111827")
        self.buttons.pack(fill="x", padx=10, pady=(0, 6))

        self.controls_card = tk.Frame(
            self.buttons,
            bg="#0f172a",
            highlightthickness=1,
            highlightbackground="#1f2937",
            bd=0,
            padx=8,
            pady=7,
        )
        self.controls_card.pack(fill="x")

        self.controls_left = tk.Frame(self.controls_card, bg="#0f172a")
        self.controls_left.pack(side="left", fill="x", expand=True)

        self.controls_center = tk.Frame(self.controls_card, bg="#0f172a")
        self.controls_center.pack(side="left", fill="x", expand=True)

        self.controls_right = tk.Frame(self.controls_card, bg="#0f172a")
        self.controls_right.pack(side="left", fill="x", expand=True)

        prev_btn = HoverButton(
            self.controls_left,
            text="PREV",
            command=lambda: send_media_key(VK_MEDIA_PREV_TRACK),
            bg="#111827",
            hover_bg="#1f2937",
            press_bg="#0b1220",
            border_color="#334155",
            hover_border="#475569",
            font=("Segoe UI", 9, "bold"),
            pad_x=14,
            pad_y=9,
        )
        play_btn = HoverButton(
            self.controls_center,
            text="PLAY / PAUSE",
            command=lambda: send_media_key(VK_MEDIA_PLAY_PAUSE),
            bg="#065f46",
            hover_bg="#047857",
            press_bg="#064e3b",
            border_color="#10b981",
            hover_border="#34d399",
            font=("Segoe UI", 10, "bold"),
            pad_x=18,
            pad_y=10,
        )
        next_btn = HoverButton(
            self.controls_right,
            text="NEXT",
            command=lambda: send_media_key(VK_MEDIA_NEXT_TRACK),
            bg="#111827",
            hover_bg="#1f2937",
            press_bg="#0b1220",
            border_color="#334155",
            hover_border="#475569",
            font=("Segoe UI", 9, "bold"),
            pad_x=14,
            pad_y=9,
        )

        prev_btn.pack(side="left", padx=(0, 5), fill="x", expand=True)
        play_btn.pack(side="left", padx=4, fill="x", expand=True)
        next_btn.pack(side="left", padx=(5, 0), fill="x", expand=True)

        self.controls_dropdown = tk.Frame(self.container, bg="#111827")
        self.controls_dropdown.pack(fill="x", padx=10, pady=(0, 6))

        self.controls_toggle = tk.Label(
            self.controls_dropdown,
            text="Tuning ▲",
            bg="#0f172a",
            fg="#d1d5db",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=4,
            anchor="w",
            cursor="hand2",
        )
        self.controls_toggle.pack(fill="x")
        self.controls_toggle.bind("<Enter>", lambda _e: self.controls_toggle.configure(bg="#1e293b"))
        self.controls_toggle.bind("<Leave>", lambda _e: self.controls_toggle.configure(bg="#0f172a"))
        self.controls_toggle.bind("<Button-1>", lambda _e: self._toggle_controls())

        self.tune_frame = tk.Frame(self.controls_dropdown, bg="#111827")
        self.tune_frame.pack(fill="x", pady=(6, 0))

        self.speed_label = tk.Label(
            self.tune_frame,
            text="Speed",
            bg="#111827",
            fg="#93c5fd",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.speed_label.pack(fill="x")

        self.speed_slider = tk.Scale(
            self.tune_frame,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            resolution=0.01,
            showvalue=False,
            variable=self.speed_var,
            command=self._on_slider_change,
            length=320,
            bg="#111827",
            fg="#d1d5db",
            troughcolor="#1f2937",
            highlightthickness=0,
            bd=0,
            activebackground="#14b8a6",
            width=9,
            sliderlength=18,
        )
        self.speed_slider.pack(fill="x", pady=(0, 4))

        self.falloff_label = tk.Label(
            self.tune_frame,
            text="Falloff",
            bg="#111827",
            fg="#fca5a5",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.falloff_label.pack(fill="x")

        self.falloff_slider = tk.Scale(
            self.tune_frame,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            resolution=0.01,
            showvalue=False,
            variable=self.falloff_var,
            command=self._on_slider_change,
            length=320,
            bg="#111827",
            fg="#d1d5db",
            troughcolor="#1f2937",
            highlightthickness=0,
            bd=0,
            activebackground="#f97316",
            width=9,
            sliderlength=18,
        )
        self.falloff_slider.pack(fill="x")

        self.accuracy_label = tk.Label(
            self.tune_frame,
            text="Accuracy",
            bg="#111827",
            fg="#c4b5fd",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.accuracy_label.pack(fill="x", pady=(4, 0))

        self.accuracy_slider = tk.Scale(
            self.tune_frame,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            resolution=0.01,
            showvalue=False,
            variable=self.accuracy_var,
            command=self._on_slider_change,
            length=320,
            bg="#111827",
            fg="#d1d5db",
            troughcolor="#1f2937",
            highlightthickness=0,
            bd=0,
            activebackground="#a78bfa",
            width=9,
            sliderlength=18,
        )
        self.accuracy_slider.pack(fill="x")

        self.center_toggle_btn = HoverButton(
            self.tune_frame,
            text="Centered: ON",
            command=self._toggle_centered_spectrum,
            bg="#1f2937",
            hover_bg="#334155",
            press_bg="#0f172a",
            border_color="#334155",
            hover_border="#475569",
            font=("Segoe UI", 8, "bold"),
            pad_x=10,
            pad_y=6,
        )
        self.center_toggle_btn.pack(fill="x", pady=(6, 0))
        self._update_centered_toggle_button()

        self.settings_dropdown = tk.Frame(self.container, bg="#111827")
        self.settings_dropdown.pack(fill="x", padx=10, pady=(0, 6))

        self.settings_toggle = tk.Label(
            self.settings_dropdown,
            text="Settings ▼",
            bg="#0f172a",
            fg="#d1d5db",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=4,
            anchor="w",
            cursor="hand2",
        )
        self.settings_toggle.pack(fill="x")
        self.settings_toggle.bind("<Enter>", lambda _e: self.settings_toggle.configure(bg="#1e293b"))
        self.settings_toggle.bind("<Leave>", lambda _e: self.settings_toggle.configure(bg="#0f172a"))
        self.settings_toggle.bind("<Button-1>", lambda _e: self._toggle_settings())

        self.settings_frame = tk.Frame(self.settings_dropdown, bg="#111827")

        self.source_label = tk.Label(
            self.settings_frame,
            text="Audio Source",
            bg="#111827",
            fg="#93c5fd",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.source_label.pack(fill="x")

        self.source_menu = tk.OptionMenu(self.settings_frame, self.source_var, "Auto (recommended)")
        self.source_menu.configure(
            bg="#0f172a",
            fg="#e5e7eb",
            activebackground="#1e293b",
            activeforeground="#e5e7eb",
            highlightthickness=0,
            bd=0,
            anchor="w",
            font=("Segoe UI", 8),
        )
        self.source_menu["menu"].configure(
            bg="#0f172a",
            fg="#e5e7eb",
            activebackground="#1e293b",
            activeforeground="#e5e7eb",
            font=("Segoe UI", 8),
        )
        self.source_menu.pack(fill="x", pady=(2, 6))

        self.settings_actions = tk.Frame(self.settings_frame, bg="#111827")
        self.settings_actions.pack(fill="x")

        self.source_refresh_btn = HoverButton(
            self.settings_actions,
            text="Refresh",
            command=self._refresh_source_options,
            bg="#1f2937",
            hover_bg="#334155",
            font=("Segoe UI", 8, "bold"),
        )
        self.source_refresh_btn.pack(side="left", padx=(0, 6))

        self.source_apply_btn = HoverButton(
            self.settings_actions,
            text="Apply",
            command=self._apply_settings,
            bg="#065f46",
            hover_bg="#047857",
            font=("Segoe UI", 8, "bold"),
        )
        self.source_apply_btn.pack(side="left")

        self._refresh_source_options()

        for widget in (self.container, self.header, self.title):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag_window)

        self._resize_to_content()

        self._refresh_track_info()
        self._refresh_spectrum()

    def start_drag(self, event):
        self.offset_x = event.x
        self.offset_y = event.y

    def drag_window(self, event):
        x = self.root.winfo_pointerx() - self.offset_x
        y = self.root.winfo_pointery() - self.offset_y
        self.root.geometry(f"+{x}+{y}")

    def run(self):
        self.root.mainloop()

    def minimize(self):
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_restore_from_minimize(self, _event):
        if self.root.state() == "normal":
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)

    def close(self):
        self.is_closing = True
        self.spectrum.stop()
        self.root.destroy()

    def _refresh_track_info(self):
        if self.is_closing or not self.root.winfo_exists():
            return
        info = get_spotify_track_info()

        current_track_key = (info.title.strip().lower(), info.artist.strip().lower())
        if self._last_track_key is not None and current_track_key != self._last_track_key:
            self.spectrum.reset_calibration()
            self.spectrum.notify_track_change()
        self._last_track_key = current_track_key

        self.track_var.set(info.title)
        self.artist_var.set(info.artist)
        try:
            self.root.after(1500, self._refresh_track_info)
        except tk.TclError:
            return

    def _on_slider_change(self, _value):
        speed = self.speed_var.get()
        falloff = self.falloff_var.get()
        accuracy = self.accuracy_var.get()
        self.spectrum.set_motion(speed, falloff, accuracy)
        self.frame_ms = int(22 - (12 * speed))

    def _update_spectrum_hint(self):
        if not HAS_SPECTRUM_DEPS:
            self.spectrum_hint.configure(text="Install spectrum packages: python3.12 -m pip install --user numpy soundcard sounddevice")
        elif not self.spectrum.available:
            if self.spectrum.last_error:
                self.spectrum_hint.configure(text=f"Spectrum unavailable: {self.spectrum.last_error}")
            else:
                self.spectrum_hint.configure(text="Spectrum unavailable: check Windows audio and default output")
        elif self.spectrum.source_mode == "loopback-headphones":
            self.spectrum_hint.configure(text=f"Loopback: headphones ({self.spectrum.selected_device_name})")
        elif self.spectrum.source_mode == "loopback":
            self.spectrum_hint.configure(text=f"Loopback: {self.spectrum.selected_device_name}")
        elif self.spectrum.source_mode == "mic":
            self.spectrum_hint.configure(text=f"Mic: {self.spectrum.selected_device_name}")
        else:
            self.spectrum_hint.configure(text="")

    def _refresh_source_options(self):
        options = self.spectrum.get_source_options()
        self.source_options = options
        self.source_label_to_id = {label: source_id for label, source_id in options}
        current_id = self.spectrum.preferred_source
        if current_id not in self.source_label_to_id.values():
            current_id = "auto"

        selected_label = None
        for label, source_id in options:
            if source_id == current_id:
                selected_label = label
                break
        if selected_label is None:
            selected_label = options[0][0]

        menu = self.source_menu["menu"]
        menu.delete(0, "end")
        for label, _source_id in options:
            menu.add_command(label=label, command=lambda v=label: self.source_var.set(v))
        self.source_var.set(selected_label)

    def _apply_settings(self):
        selected_label = self.source_var.get()
        source_id = self.source_label_to_id.get(selected_label, "auto")
        source_changed = source_id != self.spectrum.preferred_source

        if source_changed:
            self.spectrum.set_preferred_source(source_id)
        self._update_spectrum_hint()

    def _apply_source_selection(self):
        # Backward-compatible wrapper.
        self._apply_settings()

    def _resize_to_content(self):
        if self.is_closing or not self.root.winfo_exists():
            return
        self.root.update_idletasks()
        target_w = 350
        target_h = self.container.winfo_reqheight() + 14
        target_h = max(240, min(560, target_h))
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{target_w}x{target_h}+{x}+{y}")

    def _toggle_settings(self):
        self.settings_expanded = not self.settings_expanded
        if self.settings_expanded:
            self.settings_toggle.configure(text="Settings ▲")
            self.settings_frame.pack(fill="x", pady=(6, 0))
        else:
            self.settings_toggle.configure(text="Settings ▼")
            self.settings_frame.pack_forget()
        self._resize_to_content()

    def _toggle_controls(self):
        self.controls_expanded = not self.controls_expanded
        if self.controls_expanded:
            self.controls_toggle.configure(text="Tuning ▲")
            self.tune_frame.pack(fill="x", pady=(6, 0))
        else:
            self.controls_toggle.configure(text="Tuning ▼")
            self.tune_frame.pack_forget()
        self._resize_to_content()

    def _update_centered_toggle_button(self):
        state_text = "ON" if self.centered_spectrum else "OFF"
        self.center_toggle_btn.configure(text=f"Centered: {state_text}")

    def _toggle_centered_spectrum(self):
        self.centered_spectrum = not self.centered_spectrum
        self._update_centered_toggle_button()

    def _mix_color(self, a, b, t):
        t = max(0.0, min(1.0, float(t)))
        ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
        br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
        rr = int(ar + (br - ar) * t)
        rg = int(ag + (bg - ag) * t)
        rb = int(ab + (bb - ab) * t)
        return f"#{rr:02x}{rg:02x}{rb:02x}"

    def _refresh_spectrum(self):
        if self.is_closing or not self.root.winfo_exists():
            return
        width = int(self.spectrum_canvas.winfo_width())
        height = int(self.spectrum_canvas.winfo_height())
        if width <= 1:
            width = 320
        if height <= 1:
            height = 42

        target_bar_count = max(24, min(72, width // 6))
        self.spectrum.set_bars(target_bar_count)
        target_levels = self.spectrum.get_levels()
        self.spectrum_canvas.delete("all")
        count = len(target_levels)
        if count > 0:
            if len(self.render_levels) != count:
                self.render_levels = [0.0] * count
                self.peak_levels = [0.0] * count
                self.peak_hold = [0] * count

            gap = 1
            bar_width = max(1, (width - ((count + 1) * gap)) // count)
            x = gap
            speed = self.speed_var.get()
            falloff = self.falloff_var.get()
            rise_lerp = 0.58 + (0.4 * speed)
            fall_lerp = 0.38 + (0.56 * falloff)
            peak_fall = 0.03 + (0.14 * falloff)
            hold_frames = max(1, int(round(3 - (2 * falloff))))

            for i, target in enumerate(target_levels):
                target = float(target)
                current = self.render_levels[i]
                if target > current:
                    current += (target - current) * rise_lerp
                else:
                    current += (target - current) * fall_lerp
                current = max(0.0, min(1.0, current))
                self.render_levels[i] = current

                # Peak-hold indicator improves readability on fast transients.
                if current >= self.peak_levels[i]:
                    self.peak_levels[i] = current
                    self.peak_hold[i] = hold_frames
                elif self.peak_hold[i] > 0:
                    self.peak_hold[i] -= 1
                else:
                    self.peak_levels[i] = max(0.0, self.peak_levels[i] - peak_fall)

                if self.centered_spectrum:
                    half_h = max(4, (height - 6) // 2)
                    center_y = height // 2
                    bar_h = max(1, int(half_h * current))
                    y1 = max(2, center_y - bar_h)
                    y2 = min(height - 2, center_y + bar_h)
                else:
                    bar_h = max(2, int((height - 4) * current))
                    y1 = height - 2 - bar_h
                    y2 = height - 2

                if current < 0.45:
                    color = self._mix_color("#0ea5e9", "#14b8a6", current / 0.45)
                else:
                    color = self._mix_color("#14b8a6", "#22c55e", (current - 0.45) / 0.55)

                self.spectrum_canvas.create_rectangle(
                    x,
                    y1,
                    x + bar_width,
                    y2,
                    fill=color,
                    outline="",
                )

                if self.centered_spectrum:
                    half_h = max(4, (height - 6) // 2)
                    center_y = height // 2
                    peak_off = int(half_h * self.peak_levels[i])
                    peak_top = max(2, center_y - peak_off)
                    peak_bottom = min(height - 2, center_y + peak_off)
                    self.spectrum_canvas.create_rectangle(
                        x,
                        peak_top - 1,
                        x + bar_width,
                        peak_top + 1,
                        fill="#e2e8f0",
                        outline="",
                    )
                    self.spectrum_canvas.create_rectangle(
                        x,
                        peak_bottom - 1,
                        x + bar_width,
                        peak_bottom + 1,
                        fill="#e2e8f0",
                        outline="",
                    )
                else:
                    peak_y = height - 2 - int((height - 4) * self.peak_levels[i])
                    self.spectrum_canvas.create_rectangle(
                        x,
                        peak_y - 1,
                        x + bar_width,
                        peak_y + 1,
                        fill="#e2e8f0",
                        outline="",
                    )
                x += bar_width + gap

            if self.centered_spectrum:
                center_y = height // 2
                self.spectrum_canvas.create_line(
                    2,
                    center_y,
                    width - 2,
                    center_y,
                    fill="#334155",
                    width=1,
                )

        now = time.perf_counter()
        frame_dt_ms = (now - self._last_ui_time) * 1000.0
        self._last_ui_time = now
        telemetry = self.spectrum.get_telemetry()
        bpm = telemetry["bpm"]
        conf = telemetry["tempo_confidence"]
        signal = telemetry["signal"]
        est_audio_latency = telemetry["latency_ms"]
        source_label = telemetry["source"] or "none"
        bpm_text = "--"
        if conf > 0.2 and bpm >= 55.0:
            bpm_text = f"{bpm:.0f}"
        live_state = "LIVE" if signal > 0.002 else "IDLE"
        self.live_status_var.set(
            f"{live_state} | src: {source_label} | bpm: {bpm_text} | conf: {int(conf * 100)}% | audio: {est_audio_latency:.1f}ms | ui: {frame_dt_ms:.1f}ms"
        )

        try:
            self.root.after(self.frame_ms, self._refresh_spectrum)
        except tk.TclError:
            return


if __name__ == "__main__":
    app = SpotifyMiniController()
    app.run()