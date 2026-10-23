use super::timing::CallbackTiming;
use super::{c_int, c_ulong, c_void, CallbackTimeInfo, PA_ABORT, PA_COMPLETE, PA_OUTPUT_UNDERFLOW};
use std::cell::UnsafeCell;
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

// The main loop alone writes free slots; the callback alone reads occupied slots.
// Release/acquire indices transfer slot ownership without locks or allocation.
struct Ring {
    samples: Box<[UnsafeCell<f32>]>,
    channels: usize,
    capacity: u64,
    read: AtomicU64,
    write: AtomicU64,
}
unsafe impl Sync for Ring {}

impl Ring {
    fn new(capacity: u64, channels: usize) -> Self {
        Self {
            samples: (0..capacity as usize * channels)
                .map(|_| UnsafeCell::new(0.0))
                .collect(),
            channels,
            capacity,
            read: AtomicU64::new(0),
            write: AtomicU64::new(0),
        }
    }

    fn push(&self, pcm: &[f32]) -> usize {
        let write = self.write.load(Ordering::Relaxed);
        let read = self.read.load(Ordering::Acquire);
        let frames = (pcm.len() / self.channels).min((self.capacity - (write - read)) as usize);
        let start = (write % self.capacity) as usize * self.channels;
        let count = frames * self.channels;
        let first = count.min(self.samples.len() - start);
        unsafe {
            ptr::copy_nonoverlapping(
                pcm.as_ptr(),
                self.samples.as_ptr().add(start).cast_mut().cast::<f32>(),
                first,
            );
            ptr::copy_nonoverlapping(
                pcm.as_ptr().add(first),
                self.samples.as_ptr().cast_mut().cast::<f32>(),
                count - first,
            );
        }
        self.write.store(write + frames as u64, Ordering::Release);
        count
    }

    fn pop(&self, output: &mut [f32]) -> usize {
        let read = self.read.load(Ordering::Relaxed);
        let write = self.write.load(Ordering::Acquire);
        let frames = (output.len() / self.channels).min((write - read) as usize);
        let start = (read % self.capacity) as usize * self.channels;
        let count = frames * self.channels;
        let first = count.min(self.samples.len() - start);
        unsafe {
            ptr::copy_nonoverlapping(
                self.samples.as_ptr().add(start).cast::<f32>(),
                output.as_mut_ptr(),
                first,
            );
            ptr::copy_nonoverlapping(
                self.samples.as_ptr().cast::<f32>(),
                output.as_mut_ptr().add(first),
                count - first,
            );
        }
        self.read.store(read + frames as u64, Ordering::Release);
        count
    }

    fn empty(&self) -> bool {
        self.read.load(Ordering::Acquire) == self.write.load(Ordering::Acquire)
    }
}

pub(super) struct CallbackState {
    pub(super) timing: CallbackTiming,
    pcm: Vec<f32>,
    ring: Option<Ring>,
    pub(super) channels: usize,
    pub(super) position: AtomicU64,
    pub(super) total: AtomicU64,
    pub(super) cancelled: AtomicBool,
    pub(super) paused: AtomicBool,
    pub(super) pause_applied: AtomicBool,
    pub(super) callbacks: AtomicU64,
    pub(super) underflows: AtomicU64,
    pub(super) starvations: AtomicU64,
    pub(super) failed: AtomicBool,
    pub(super) first_written: AtomicU64,
    pub(super) first_current_time: AtomicU64,
    pub(super) first_dac_time: AtomicU64,
    pub(super) timeline_position: AtomicU64,
    pub(super) timeline_written: AtomicU64,
    pub(super) timeline_dac: AtomicU64,
    pub(super) eos: AtomicBool,
    pub(super) seek_position: AtomicU64,
    pub(super) seek_request: AtomicU64,
    pub(super) seek_applied: AtomicU64,
}

impl CallbackState {
    pub(super) fn new(pcm: Vec<f32>, channels: u8) -> Self {
        let total = (pcm.len() / usize::from(channels)) as u64;
        Self {
            timing: CallbackTiming::default(),
            pcm,
            ring: None,
            channels: usize::from(channels),
            position: AtomicU64::new(0),
            total: AtomicU64::new(total),
            cancelled: AtomicBool::new(false),
            paused: AtomicBool::new(false),
            pause_applied: AtomicBool::new(false),
            callbacks: AtomicU64::new(0),
            underflows: AtomicU64::new(0),
            starvations: AtomicU64::new(0),
            failed: AtomicBool::new(false),
            first_written: AtomicU64::new(u64::MAX),
            first_current_time: AtomicU64::new(0),
            first_dac_time: AtomicU64::new(0),
            timeline_position: AtomicU64::new(0),
            timeline_written: AtomicU64::new(0),
            timeline_dac: AtomicU64::new(0),
            eos: AtomicBool::new(false),
            seek_position: AtomicU64::new(0),
            seek_request: AtomicU64::new(0),
            seek_applied: AtomicU64::new(0),
        }
    }

    pub(super) fn stream(capacity: u64, channels: u8) -> Self {
        let mut state = Self::new(Vec::new(), channels);
        state.ring = Some(Ring::new(capacity, usize::from(channels)));
        state
    }

    pub(super) fn streaming(&self) -> bool {
        self.ring.is_some()
    }

    pub(super) fn append(&self, pcm: &[f32]) -> usize {
        let copied = self.ring.as_ref().unwrap().push(pcm);
        self.total
            .fetch_add((copied / self.channels) as u64, Ordering::Relaxed);
        copied
    }

    pub(super) fn render(&self, output: &mut [f32], flags: c_ulong) -> c_int {
        self.callbacks.fetch_add(1, Ordering::Relaxed);
        if flags & PA_OUTPUT_UNDERFLOW != 0 {
            self.underflows.fetch_add(1, Ordering::Relaxed);
        }
        output.fill(0.0);
        if self.cancelled.load(Ordering::Acquire) {
            return PA_ABORT;
        }
        let request = self.seek_request.load(Ordering::Acquire);
        if request != self.seek_applied.load(Ordering::Relaxed) {
            let position = self.seek_position.load(Ordering::Relaxed);
            self.position.store(position, Ordering::Relaxed);
            self.timeline_position.store(position, Ordering::Relaxed);
            self.first_written.store(u64::MAX, Ordering::Relaxed);
            self.timeline_written.store(0, Ordering::Relaxed);
            self.seek_applied.store(request, Ordering::Release);
        }
        if self.paused.load(Ordering::Acquire) {
            self.pause_applied.store(true, Ordering::Release);
            return 0;
        }
        self.pause_applied.store(false, Ordering::Release);
        let position = self.position.load(Ordering::Relaxed);
        // Acquire EOS before reading ring indices so the final producer write is visible.
        let ended = self.eos.load(Ordering::Acquire);
        let count = if let Some(ring) = &self.ring {
            ring.pop(output)
        } else {
            let start = position as usize * self.channels;
            let count = output.len().min(self.pcm.len() - start);
            output[..count].copy_from_slice(&self.pcm[start..start + count]);
            count
        };
        self.position
            .store(position + (count / self.channels) as u64, Ordering::Release);
        if let Some(ring) = &self.ring {
            if ended && ring.empty() {
                return PA_COMPLETE;
            }
            if count < output.len() {
                self.starvations.fetch_add(1, Ordering::Relaxed);
            }
            0
        } else if position + (count / self.channels) as u64 == self.total.load(Ordering::Relaxed) {
            PA_COMPLETE
        } else {
            0
        }
    }
}

pub(super) unsafe extern "C" fn audio_callback(
    _input: *const c_void,
    output: *mut c_void,
    frame_count: c_ulong,
    time: *const CallbackTimeInfo,
    flags: c_ulong,
    user: *mut c_void,
) -> c_int {
    let state = &*user.cast::<CallbackState>();
    let Some(samples) = (frame_count as usize).checked_mul(state.channels) else {
        state.failed.store(true, Ordering::Release);
        return PA_ABORT;
    };
    if output.is_null() || samples > isize::MAX as usize / std::mem::size_of::<f32>() {
        state.failed.store(true, Ordering::Release);
        return PA_ABORT;
    }
    let before = state.position.load(Ordering::Relaxed);
    state
        .timing
        .observe(time.as_ref(), frame_count as u64, before);
    let seek_before = state.seek_applied.load(Ordering::Relaxed);
    let result = state.render(
        std::slice::from_raw_parts_mut(output.cast::<f32>(), samples),
        flags,
    );
    let after = state.position.load(Ordering::Relaxed);
    let origin = if seek_before != state.seek_applied.load(Ordering::Relaxed) {
        state.timeline_position.load(Ordering::Relaxed)
    } else {
        before
    };
    let written = after.saturating_sub(origin);
    if written > 0 {
        if !time.is_null() {
            state.timeline_position.store(origin, Ordering::Relaxed);
            state
                .timeline_dac
                .store((*time).output_buffer_dac_time.to_bits(), Ordering::Relaxed);
            state.timeline_written.store(written, Ordering::Release);
        }
        if state.first_written.load(Ordering::Relaxed) == u64::MAX {
            if !time.is_null() {
                state
                    .first_current_time
                    .store((*time).current_time.to_bits(), Ordering::Relaxed);
                state
                    .first_dac_time
                    .store((*time).output_buffer_dac_time.to_bits(), Ordering::Relaxed);
            }
            state.first_written.store(written, Ordering::Release);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_stop_retains_timing_without_consuming_audio() {
        let mut state = CallbackState::new(vec![0.5; 1440], 1);
        state.timing.sample_rate = 48000.0;
        let mut output = [0.0_f32; 480];
        let user = (&mut state as *mut CallbackState).cast();
        for (index, current) in [10.0, 10.01, 10.04].into_iter().enumerate() {
            if index == 2 {
                state.cancelled.store(true, Ordering::Release);
            }
            let time = CallbackTimeInfo {
                input_buffer_adc_time: 0.0,
                current_time: current,
                output_buffer_dac_time: current + 0.1,
            };
            let result = unsafe {
                audio_callback(ptr::null(), output.as_mut_ptr().cast(), 480, &time, 0, user)
            };
            assert_eq!(result, if index == 2 { PA_ABORT } else { 0 });
        }
        assert_eq!(state.position.load(Ordering::Acquire), 960);
        assert_eq!(output, [0.0; 480]);
        assert_eq!(state.underflows.load(Ordering::Relaxed), 0);
        let mut event = serde_json::json!({});
        state.timing.add_to(&mut event);
        assert_eq!(event["callback_timing"]["discontinuity_count"], 1);
        assert_eq!(event["callback_timing"]["max_offset_position_frames"], 960);
        assert_eq!(event["timing_output_frames"], 1440);
    }

    #[test]
    fn null_time_info_keeps_first_audio_and_timing_unknown() {
        let mut state = CallbackState::new(vec![0.5; 480], 1);
        state.timing.sample_rate = 48000.0;
        let mut output = [0.0_f32; 480];
        let user = (&mut state as *mut CallbackState).cast();
        let result = unsafe {
            audio_callback(
                ptr::null(),
                output.as_mut_ptr().cast(),
                480,
                ptr::null(),
                0,
                user,
            )
        };
        assert_eq!(result, PA_COMPLETE);
        assert_eq!(output, [0.5; 480]);
        assert_eq!(state.first_written.load(Ordering::Acquire), 480);
        let mut event = serde_json::json!({});
        state.timing.add_to(&mut event);
        assert_eq!(event["callback_timing"]["unavailable_count"], 1);
        assert_eq!(event["dac_timing"]["observation_count"], 0);
    }

    #[test]
    fn ring_preserves_stereo_order_across_wrap_and_full_backpressure() {
        let state = CallbackState::stream(3, 2);
        assert_eq!(state.append(&[1., 2., 3., 4., 5., 6., 7., 8.]), 6);
        assert_eq!(state.append(&[9., 10.]), 0);
        let mut out = [0.; 4];
        assert_eq!(state.render(&mut out, 0), 0);
        assert_eq!(out, [1., 2., 3., 4.]);
        assert_eq!(state.append(&[7., 8., 9., 10.]), 4);
        assert_eq!(state.render(&mut out, 0), 0);
        assert_eq!(out, [5., 6., 7., 8.]);
        state.eos.store(true, Ordering::Release);
        assert_eq!(state.render(&mut out, 0), PA_COMPLETE);
        assert_eq!(out, [9., 10., 0., 0.]);
        assert_eq!(state.starvations.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn starvation_eos_and_cancel_are_distinct() {
        let state = CallbackState::stream(4, 1);
        let mut out = [8.; 4];
        assert_eq!(state.render(&mut out, PA_OUTPUT_UNDERFLOW), 0);
        assert_eq!(out, [0.; 4]);
        assert_eq!(state.starvations.load(Ordering::Relaxed), 1);
        assert_eq!(state.underflows.load(Ordering::Relaxed), 1);
        state.eos.store(true, Ordering::Release);
        assert_eq!(state.render(&mut out, 0), PA_COMPLETE);
        assert_eq!(state.starvations.load(Ordering::Relaxed), 1);
        state.cancelled.store(true, Ordering::Release);
        assert_eq!(state.render(&mut out, 0), PA_ABORT);
    }

    #[test]
    fn seek_applies_even_while_paused_without_consuming_audio() {
        let state = CallbackState::new(vec![1., 2., 3., 4.], 1);
        state.paused.store(true, Ordering::Release);
        state.seek_position.store(2, Ordering::Relaxed);
        state.seek_request.store(1, Ordering::Release);
        assert_eq!(state.render(&mut [8.; 2], 0), 0);
        assert_eq!(state.seek_applied.load(Ordering::Acquire), 1);
        assert_eq!(state.position.load(Ordering::Acquire), 2);
        state.paused.store(false, Ordering::Release);
        let mut out = [0.; 2];
        assert_eq!(state.render(&mut out, 0), PA_COMPLETE);
        assert_eq!(out, [3., 4.]);
    }
    #[test]
    fn concurrent_producer_drains_all_frames_once_and_in_order() {
        let state = CallbackState::stream(23, 2);
        std::thread::scope(|scope| {
            scope.spawn(|| {
                for frame in 0..100_000_u32 {
                    let pcm = [frame as f32, -(frame as f32)];
                    while state.append(&pcm) == 0 {
                        std::thread::yield_now();
                    }
                }
                state.eos.store(true, Ordering::Release);
            });
            let mut seen = 0;
            loop {
                let before = state.position.load(Ordering::Relaxed);
                let mut output = [0.; 14];
                let result = state.render(&mut output, 0);
                let count = state.position.load(Ordering::Relaxed) - before;
                for frame in output[..count as usize * 2].as_chunks::<2>().0 {
                    assert_eq!(*frame, [seen as f32, -(seen as f32)]);
                    seen += 1;
                }
                if result == PA_COMPLETE {
                    break;
                }
                std::thread::yield_now();
            }
            assert_eq!(seen, 100_000);
            assert_eq!(state.total.load(Ordering::Acquire), 100_000);
        });
    }

    #[test]
    fn paused_stream_preserves_ring_and_cancel_does_not_consume_late_data() {
        let state = CallbackState::stream(2, 1);
        assert_eq!(state.append(&[1., 2.]), 2);
        state.paused.store(true, Ordering::Release);
        let mut out = [3.; 2];
        assert_eq!(state.render(&mut out, 0), 0);
        assert_eq!(out, [0.; 2]);
        assert_eq!(state.append(&[3.]), 0);
        assert_eq!(state.starvations.load(Ordering::Relaxed), 0);
        state.cancelled.store(true, Ordering::Release);
        assert_eq!(state.render(&mut out, 0), PA_ABORT);
        assert_eq!(state.position.load(Ordering::Relaxed), 0);
    }
}
