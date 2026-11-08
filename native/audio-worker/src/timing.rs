use super::CallbackTimeInfo;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU64, Ordering};

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
    fn observe(&self, time: Option<f64>, frames: u64, position: u64, output_frame: u64, rate: f64) {
        let Some(time) = time.filter(|t| t.is_finite() && *t > 0.0) else {
            self.unavailable.fetch_add(1, Ordering::Relaxed);
            self.previous.store(0, Ordering::Relaxed);
            return;
        };
        self.observations.fetch_add(1, Ordering::Relaxed);
        let previous = f64::from_bits(self.previous.swap(time.to_bits(), Ordering::Relaxed));
        let previous_frames = self.previous_frames.swap(frames, Ordering::Relaxed);
        if previous == 0.0 || !rate.is_finite() || rate <= 0.0 {
            return;
        }
        let gap = time - previous;
        let offset = gap - previous_frames as f64 / rate;
        if gap > f64::from_bits(self.max_gap.load(Ordering::Relaxed)) {
            self.max_gap.store(gap.to_bits(), Ordering::Relaxed);
        }
        // Host scheduling variation is evidence, not proof of an audible underrun.
        if offset.abs() > 1.0 / rate + 1e-9 {
            self.discontinuities.fetch_add(1, Ordering::Relaxed);
        }
        if offset.abs() > f64::from_bits(self.max_offset.load(Ordering::Relaxed)).abs() {
            self.max_offset.store(offset.to_bits(), Ordering::Relaxed);
            self.max_position.store(position, Ordering::Relaxed);
            self.max_output_frame.store(output_frame, Ordering::Relaxed);
        }
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
}

impl CallbackTiming {
    pub(super) fn observe(&self, time: Option<&CallbackTimeInfo>, frames: u64, position: u64) {
        let output_frame = self.output_frames.fetch_add(frames, Ordering::Relaxed);
        self.callback.observe(
            time.map(|t| t.current_time),
            frames,
            position,
            output_frame,
            self.sample_rate,
        );
        self.dac.observe(
            time.map(|t| t.output_buffer_dac_time),
            frames,
            position,
            output_frame,
            self.sample_rate,
        );
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
}
