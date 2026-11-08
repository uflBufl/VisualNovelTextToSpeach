use super::CallbackTimeInfo;
use serde_json::{json, Value};
use std::cell::UnsafeCell;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

#[derive(Clone, Copy, serde::Serialize)]
pub(super) struct CallbackSample {
    position_frames: u64,
    output_frame: u64,
    frame_count: u64,
    flags: u64,
    entry_seconds: f64,
    exit_seconds: f64,
    current_time: Option<f64>,
    dac_time: Option<f64>,
    #[serde(skip)]
    dac_discontinuity: bool,
}

#[derive(Clone, Copy, serde::Serialize)]
struct CallbackPair {
    previous: CallbackSample,
    current: CallbackSample,
}

#[derive(Default)]
struct TraceSamples {
    previous: Option<CallbackSample>,
    events: [Option<CallbackPair>; 8],
    count: usize,
    dropped: u64,
}

struct CallbackTrace {
    epoch: Instant,
    // The sole audio callback writes; control reads mutably only after stream close.
    samples: UnsafeCell<TraceSamples>,
}
// Only unsafe end_callback writes; the producer never accesses samples.
unsafe impl Sync for CallbackTrace {}

impl CallbackTrace {
    unsafe fn record(&self, sample: CallbackSample) {
        let samples = unsafe { &mut *self.samples.get() };
        if let Some(previous) = samples.previous {
            if sample.dac_discontinuity {
                if samples.count < samples.events.len() {
                    samples.events[samples.count] = Some(CallbackPair {
                        previous,
                        current: sample,
                    });
                    samples.count += 1;
                } else {
                    samples.dropped += 1;
                }
            }
        }
        samples.previous = Some(sample);
    }
}

#[derive(Default)]
struct ClockTiming {
    previous: AtomicU64,
    previous_frames: AtomicU64,
    observations: AtomicU64,
    unavailable: AtomicU64,
    discontinuities: AtomicU64,
    max_gap: AtomicU64,
    max_offset: AtomicU64,
    max_position: AtomicU64,
    max_output_frame: AtomicU64,
}

impl ClockTiming {
    fn observe(
        &self,
        time: Option<f64>,
        frames: u64,
        position: u64,
        output_frame: u64,
        rate: f64,
    ) -> bool {
        let Some(time) = time.filter(|t| t.is_finite() && *t > 0.0) else {
            self.unavailable.fetch_add(1, Ordering::Relaxed);
            self.previous.store(0, Ordering::Relaxed);
            return false;
        };
        self.observations.fetch_add(1, Ordering::Relaxed);
        let previous = f64::from_bits(self.previous.swap(time.to_bits(), Ordering::Relaxed));
        let previous_frames = self.previous_frames.swap(frames, Ordering::Relaxed);
        if previous == 0.0 || !rate.is_finite() || rate <= 0.0 {
            return false;
        }
        let gap = time - previous;
        let offset = gap - previous_frames as f64 / rate;
        if gap > f64::from_bits(self.max_gap.load(Ordering::Relaxed)) {
            self.max_gap.store(gap.to_bits(), Ordering::Relaxed);
        }
        // Host scheduling variation is evidence, not proof of an audible underrun.
        let discontinuity = offset.abs() > 1.0 / rate + 1e-9;
        if discontinuity {
            self.discontinuities.fetch_add(1, Ordering::Relaxed);
        }
        if offset.abs() > f64::from_bits(self.max_offset.load(Ordering::Relaxed)).abs() {
            self.max_offset.store(offset.to_bits(), Ordering::Relaxed);
            self.max_position.store(position, Ordering::Relaxed);
            self.max_output_frame.store(output_frame, Ordering::Relaxed);
        }
        discontinuity
    }

    fn snapshot(&self) -> Value {
        json!({
            "observation_count": self.observations.load(Ordering::Relaxed),
            "unavailable_count": self.unavailable.load(Ordering::Relaxed),
            "discontinuity_count": self.discontinuities.load(Ordering::Relaxed),
            "max_gap_seconds": f64::from_bits(self.max_gap.load(Ordering::Relaxed)),
            "max_offset_seconds": f64::from_bits(self.max_offset.load(Ordering::Relaxed)),
            "max_offset_position_frames": self.max_position.load(Ordering::Relaxed),
            "max_offset_output_frame": self.max_output_frame.load(Ordering::Relaxed),
        })
    }
}

#[derive(Default)]
pub(super) struct CallbackTiming {
    pub(super) sample_rate: f64,
    callback: ClockTiming,
    dac: ClockTiming,
    output_frames: AtomicU64,
    trace: Option<CallbackTrace>,
}

impl CallbackTiming {
    pub(super) fn observe(
        &self,
        time: Option<&CallbackTimeInfo>,
        frames: u64,
        position: u64,
    ) -> (u64, bool) {
        let output_frame = self.output_frames.fetch_add(frames, Ordering::Relaxed);
        self.callback.observe(
            time.map(|t| t.current_time),
            frames,
            position,
            output_frame,
            self.sample_rate,
        );
        let discontinuity = self.dac.observe(
            time.map(|t| t.output_buffer_dac_time),
            frames,
            position,
            output_frame,
            self.sample_rate,
        );
        (output_frame, discontinuity)
    }

    pub(super) fn enable_trace(&mut self) {
        self.trace = Some(CallbackTrace {
            epoch: Instant::now(),
            samples: UnsafeCell::new(TraceSamples::default()),
        });
    }

    pub(super) fn begin_callback(
        &self,
        time: Option<&CallbackTimeInfo>,
        frames: u64,
        position: u64,
        flags: u64,
    ) -> Option<CallbackSample> {
        let entry = self
            .trace
            .as_ref()
            .map(|trace| trace.epoch.elapsed().as_secs_f64());
        let (output_frame, discontinuity) = self.observe(time, frames, position);
        entry.map(|entry_seconds| CallbackSample {
            position_frames: position,
            output_frame,
            frame_count: frames,
            flags,
            entry_seconds,
            exit_seconds: entry_seconds,
            current_time: time
                .map(|t| t.current_time)
                .filter(|t| t.is_finite() && *t > 0.0),
            dac_time: time
                .map(|t| t.output_buffer_dac_time)
                .filter(|t| t.is_finite() && *t > 0.0),
            dac_discontinuity: discontinuity,
        })
    }

    // Caller must be the stream's sole, non-overlapping audio callback.
    pub(super) unsafe fn end_callback(&self, sample: Option<CallbackSample>) {
        if let (Some(trace), Some(mut sample)) = (&self.trace, sample) {
            sample.exit_seconds = trace.epoch.elapsed().as_secs_f64();
            trace.record(sample);
        }
    }

    // A mutable borrow is valid only after native close has joined all callbacks.
    pub(super) fn add_trace_to(&mut self, event: &mut Value) {
        if let Some(trace) = &mut self.trace {
            let samples = trace.samples.get_mut();
            event["callback_trace"] = json!({
                "events": &samples.events[..samples.count],
                "dropped_events": samples.dropped,
            });
        }
    }

    // Only the control thread serializes metrics; the audio callback only updates atomics.
    pub(super) fn add_to(&self, event: &mut Value) {
        event["callback_timing"] = self.callback.snapshot();
        event["dac_timing"] = self.dac.snapshot();
        event["timing_output_frames"] = json!(self.output_frames.load(Ordering::Relaxed));
        event["timing_sample_rate"] = json!(self.sample_rate);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn time(current: f64, dac: f64) -> CallbackTimeInfo {
        CallbackTimeInfo {
            input_buffer_adc_time: 0.0,
            current_time: current,
            output_buffer_dac_time: dac,
        }
    }

    #[test]
    fn variable_buffers_follow_native_rate_without_false_discontinuities() {
        for rate in [24000.0, 44100.0, 48000.0, 96000.0] {
            let timing = CallbackTiming {
                sample_rate: rate,
                ..Default::default()
            };
            let mut cursor = 0;
            for frames in [192, 256, 960, 128, 192] {
                timing.observe(
                    Some(&time(
                        10.0 + cursor as f64 / rate,
                        10.1 + cursor as f64 / rate,
                    )),
                    frames,
                    cursor,
                );
                cursor += frames;
            }
            let mut event = json!({});
            timing.add_to(&mut event);
            for clock in ["callback_timing", "dac_timing"] {
                assert_eq!(event[clock]["observation_count"], 5);
                assert_eq!(event[clock]["discontinuity_count"], 0);
            }
            assert_eq!(event["timing_output_frames"], cursor);
        }
    }

    #[test]
    fn added_dac_silence_and_host_delays_keep_signed_offset_and_frame_location() {
        for extra in [192, 960] {
            let timing = CallbackTiming {
                sample_rate: 48000.0,
                ..Default::default()
            };
            timing.observe(Some(&time(10.0, 10.1)), 480, 0);
            timing.observe(Some(&time(10.03, 10.11 + extra as f64 / 48000.0)), 480, 480);
            let mut event = json!({});
            timing.add_to(&mut event);
            assert_eq!(event["callback_timing"]["discontinuity_count"], 1);
            assert_eq!(event["dac_timing"]["discontinuity_count"], 1);
            assert!(
                (event["dac_timing"]["max_offset_seconds"].as_f64().unwrap()
                    - extra as f64 / 48000.0)
                    .abs()
                    < 1e-9
            );
            assert_eq!(event["dac_timing"]["max_offset_position_frames"], 480);
            assert_eq!(event["dac_timing"]["max_offset_output_frame"], 480);
            assert!(
                (event["callback_timing"]["max_gap_seconds"]
                    .as_f64()
                    .unwrap()
                    - 0.03)
                    .abs()
                    < 1e-9
            );
        }
    }

    #[test]
    fn missing_invalid_and_zero_clocks_reset_continuity_without_inventing_gaps() {
        let timing = CallbackTiming {
            sample_rate: 48000.0,
            ..Default::default()
        };
        timing.observe(Some(&time(1.0, 1.1)), 480, 0);
        for invalid in [
            None,
            Some(time(f64::NAN, f64::INFINITY)),
            Some(time(0.0, -1.0)),
        ] {
            timing.observe(invalid.as_ref(), 480, 480);
        }
        timing.observe(Some(&time(20.0, 20.1)), 480, 480);
        let mut event = json!({});
        timing.add_to(&mut event);
        for clock in ["callback_timing", "dac_timing"] {
            assert_eq!(event[clock]["observation_count"], 2);
            assert_eq!(event[clock]["unavailable_count"], 3);
            assert_eq!(event[clock]["discontinuity_count"], 0);
            assert_eq!(event[clock]["max_gap_seconds"], 0.0);
        }
    }

    #[test]
    fn backward_clock_is_a_negative_offset_and_other_clock_remains_independent() {
        let timing = CallbackTiming {
            sample_rate: 48000.0,
            ..Default::default()
        };
        timing.observe(Some(&time(2.0, 3.0)), 480, 0);
        timing.observe(Some(&time(1.0, 3.01)), 480, 480);
        let mut event = json!({});
        timing.add_to(&mut event);
        assert_eq!(event["callback_timing"]["discontinuity_count"], 1);
        assert!(
            event["callback_timing"]["max_offset_seconds"]
                .as_f64()
                .unwrap()
                < -1.0
        );
        assert_eq!(event["dac_timing"]["discontinuity_count"], 0);
    }

    fn record_sample(
        timing: &CallbackTiming,
        clock: &CallbackTimeInfo,
        position: u64,
        entry: f64,
        exit: f64,
    ) {
        let mut sample = timing
            .begin_callback(Some(clock), 480, position, 4)
            .unwrap();
        sample.entry_seconds = entry;
        sample.exit_seconds = exit;
        unsafe {
            timing.trace.as_ref().unwrap().record(sample);
        }
    }

    #[test]
    fn trace_separates_late_entry_long_previous_residence_and_dac_only_jump() {
        for (previous_exit, entry, exit) in [
            (0.0001, 0.0300, 0.0301),
            (0.0300, 0.0301, 0.0302),
            (0.0001, 0.0100, 0.0101),
        ] {
            let mut timing = CallbackTiming {
                sample_rate: 48000.0,
                ..Default::default()
            };
            timing.enable_trace();
            record_sample(&timing, &time(10.0, 10.1), 0, 0.0, previous_exit);
            record_sample(&timing, &time(10.01, 10.13), 480, entry, exit);
            let mut event = json!({});
            timing.add_trace_to(&mut event);
            let pair = &event["callback_trace"]["events"][0];
            assert_eq!(pair["previous"]["entry_seconds"], 0.0);
            assert_eq!(pair["previous"]["exit_seconds"], previous_exit);
            assert_eq!(pair["current"]["entry_seconds"], entry);
            assert_eq!(pair["current"]["exit_seconds"], exit);
            assert_eq!(pair["current"]["output_frame"], 480);
            assert_eq!(pair["current"]["position_frames"], 480);
            assert_eq!(pair["previous"]["frame_count"], 480);
            assert_eq!(pair["current"]["flags"], 4);
            assert_eq!(pair["current"]["dac_time"], 10.13);
            assert_eq!(event["callback_trace"]["dropped_events"], 0);
        }
    }

    #[test]
    fn trace_keeps_all_three_native_gap_positions_with_variable_buffers() {
        let mut timing = CallbackTiming {
            sample_rate: 44100.0,
            ..Default::default()
        };
        timing.enable_trace();
        let mut cursor = 0;
        for (frames, added) in [(64, 0), (128, 256), (64, 832), (32, 960)] {
            let clock = time(
                10.0 + cursor as f64 / 44100.0,
                10.1 + (cursor + added) as f64 / 44100.0,
            );
            let sample = timing.begin_callback(Some(&clock), frames, cursor, 0);
            unsafe {
                timing.end_callback(sample);
            }
            cursor += frames;
        }
        let mut event = json!({});
        timing.add_to(&mut event);
        timing.add_trace_to(&mut event);
        assert_eq!(event["dac_timing"]["discontinuity_count"], 3);
        let events = event["callback_trace"]["events"].as_array().unwrap();
        assert_eq!(events.len(), 3);
        for (pair, (position, extra)) in events.iter().zip([(64, 256), (192, 576), (256, 128)]) {
            assert_eq!(pair["current"]["position_frames"], position);
            let previous = &pair["previous"];
            let current = &pair["current"];
            let offset = current["dac_time"].as_f64().unwrap()
                - previous["dac_time"].as_f64().unwrap()
                - previous["frame_count"].as_f64().unwrap() / 44100.0;
            assert!((offset - extra as f64 / 44100.0).abs() < 1e-9);
            assert!(
                previous["exit_seconds"].as_f64().unwrap()
                    <= current["entry_seconds"].as_f64().unwrap()
            );
        }
    }

    #[test]
    fn trace_bounds_pairs_and_reports_every_dropped_event() {
        let mut timing = CallbackTiming {
            sample_rate: 48000.0,
            ..Default::default()
        };
        timing.enable_trace();
        for index in 0..13 {
            record_sample(
                &timing,
                &time(10.0 + index as f64 * 0.01, 10.1 + index as f64 * 0.03),
                index * 480,
                index as f64 * 0.01,
                index as f64 * 0.01 + 0.0001,
            );
        }
        let mut event = json!({});
        timing.add_to(&mut event);
        timing.add_trace_to(&mut event);
        assert_eq!(
            event["callback_trace"]["events"].as_array().unwrap().len(),
            8
        );
        assert_eq!(event["callback_trace"]["dropped_events"], 4);
        assert_eq!(event["dac_timing"]["discontinuity_count"], 12);
    }

    #[test]
    fn disabled_trace_reads_no_independent_clock_and_has_no_payload() {
        let mut timing = CallbackTiming {
            sample_rate: 48000.0,
            ..Default::default()
        };
        for clock in [time(10.0, 10.1), time(10.01, 10.13)] {
            let sample = timing.begin_callback(Some(&clock), 480, 0, 0);
            assert!(sample.is_none());
            unsafe {
                timing.end_callback(sample);
            }
        }
        let mut event = json!({});
        timing.add_to(&mut event);
        timing.add_trace_to(&mut event);
        assert!(event.get("callback_trace").is_none());
        assert_eq!(event["dac_timing"]["discontinuity_count"], 1);
    }

    #[test]
    fn trace_invalid_clocks_reset_dac_continuity_and_keep_other_clock_nullable() {
        let mut timing = CallbackTiming {
            sample_rate: 48000.0,
            ..Default::default()
        };
        timing.enable_trace();
        for clock in [
            Some(time(10.0, 10.1)),
            None,
            Some(time(f64::NAN, f64::INFINITY)),
            Some(time(20.0, 20.1)),
            Some(time(f64::NAN, 20.13)),
        ] {
            let sample = timing.begin_callback(clock.as_ref(), 480, 0, 0);
            unsafe {
                timing.end_callback(sample);
            }
        }
        let mut event = json!({});
        timing.add_trace_to(&mut event);
        let pairs = event["callback_trace"]["events"].as_array().unwrap();
        assert_eq!(pairs.len(), 1);
        assert_eq!(pairs[0]["previous"]["dac_time"], 20.1);
        assert_eq!(pairs[0]["current"]["current_time"], Value::Null);
        assert_eq!(pairs[0]["current"]["dac_time"], 20.13);
    }
}
