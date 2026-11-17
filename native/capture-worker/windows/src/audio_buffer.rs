use std::{
    cell::UnsafeCell,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

pub const CHANNELS: usize = 2;
pub const SAMPLE_RATE: u32 = 48_000;
const DATA_DISCONTINUITY: u32 = 1;
const TIMESTAMP_ERROR: u32 = 4;

#[derive(Clone, Copy, Default, Debug)]
pub struct Packet {
    pub frames: u32,
    pub device_position: u64,
    pub qpc_100ns: u64,
    pub flags: u32,
}

struct Ring {
    pcm: Box<[UnsafeCell<f32>]>,
    packets: Box<[UnsafeCell<Packet>]>,
    read_frames: AtomicU64,
    written_packets: AtomicU64,
}
// Each slot has exactly one owner: the producer before publication, the consumer
// until read_frames is released. Neither endpoint is clonable.
unsafe impl Sync for Ring {}

pub struct Producer {
    ring: Arc<Ring>,
    frames: u64,
    packets: u64,
}
pub struct Consumer {
    ring: Arc<Ring>,
    frames: u64,
    packets: u64,
}

pub fn ring(capacity_frames: usize) -> (Producer, Consumer) {
    assert!(capacity_frames > 0);
    let ring = Arc::new(Ring {
        pcm: (0..capacity_frames * CHANNELS)
            .map(|_| UnsafeCell::new(0.0))
            .collect(),
        // One descriptor per frame also bounds the smallest legal WASAPI packet.
        packets: (0..capacity_frames)
            .map(|_| UnsafeCell::new(Packet::default()))
            .collect(),
        read_frames: AtomicU64::new(0),
        written_packets: AtomicU64::new(0),
    });
    (
        Producer {
            ring: ring.clone(),
            frames: 0,
            packets: 0,
        },
        Consumer {
            ring,
            frames: 0,
            packets: 0,
        },
    )
}

impl Producer {
    pub fn push(&mut self, packet: Packet, samples: Option<&[f32]>) -> Result<(), &'static str> {
        let count = packet.frames as usize;
        let capacity = self.ring.packets.len();
        if count == 0 || samples.is_some_and(|s| s.len() != count * CHANNELS) {
            return Err("Invalid WASAPI PCM packet");
        }
        let used = self.frames - self.ring.read_frames.load(Ordering::Acquire);
        if count > capacity || used + count as u64 > capacity as u64 {
            return Err("Five-second process audio buffer overflow");
        }
        let start = self.frames as usize % capacity * CHANNELS;
        let length = count * CHANNELS;
        let first = length.min(self.ring.pcm.len() - start);
        unsafe {
            let base = self.ring.pcm.as_ptr().cast::<f32>().cast_mut();
            if let Some(samples) = samples {
                std::ptr::copy_nonoverlapping(samples.as_ptr(), base.add(start), first);
                std::ptr::copy_nonoverlapping(samples.as_ptr().add(first), base, length - first);
            } else {
                std::ptr::write_bytes(base.add(start), 0, first);
                std::ptr::write_bytes(base, 0, length - first);
            }
        }
        unsafe {
            *self.ring.packets[self.packets as usize % capacity].get() = packet;
        }
        self.frames += count as u64;
        self.packets += 1;
        self.ring
            .written_packets
            .store(self.packets, Ordering::Release);
        Ok(())
    }
}

impl Consumer {
    pub fn consume<T>(&mut self, write: impl FnOnce(Packet, &[f32], &[f32]) -> T) -> Option<T> {
        if self.packets == self.ring.written_packets.load(Ordering::Acquire) {
            return None;
        }
        let capacity = self.ring.packets.len();
        let packet = unsafe { *self.ring.packets[self.packets as usize % capacity].get() };
        let start = self.frames as usize % capacity * CHANNELS;
        let samples = packet.frames as usize * CHANNELS;
        let first = samples.min(self.ring.pcm.len() - start);
        // Publication and release keep these slots immutable throughout disk I/O.
        let result = unsafe {
            let base = self.ring.pcm.as_ptr().cast::<f32>();
            write(
                packet,
                std::slice::from_raw_parts(base.add(start), first),
                std::slice::from_raw_parts(base, samples - first),
            )
        };
        self.frames += packet.frames as u64;
        self.packets += 1;
        self.ring.read_frames.store(self.frames, Ordering::Release);
        Some(result)
    }
}

pub fn qpc_seconds(qpc_100ns: u64) -> f64 {
    qpc_100ns as f64 / 10_000_000.0
}

pub fn selfcheck() -> Result<(), String> {
    let (mut producer, mut consumer) = ring(SAMPLE_RATE as usize * 5);
    for _ in 0..500 {
        producer.push(
            Packet {
                frames: 480,
                ..Default::default()
            },
            None,
        )?;
    }
    if producer
        .push(
            Packet {
                frames: 1,
                ..Default::default()
            },
            None,
        )
        .is_ok()
    {
        return Err("Audio ring exceeded its five-second frame capacity".into());
    }
    let mut frames = 0;
    let mut silent = true;
    while consumer
        .consume(|packet, first, second| {
            frames += packet.frames;
            silent &= first.iter().chain(second).all(|sample| *sample == 0.0);
        })
        .is_some()
    {}
    if !silent || frames != SAMPLE_RATE * 5 || qpc_seconds(10_000_000) != 1.0 {
        return Err("Audio ring silence, duration, or QPC check failed".into());
    }
    let mut clock = SourceClock::default();
    clock.validate(
        Packet {
            frames: 480,
            device_position: 960,
            qpc_100ns: 10_000_000,
            flags: 0,
        },
        SAMPLE_RATE,
    )?;
    if clock
        .validate(
            Packet {
                frames: 480,
                device_position: 1441,
                qpc_100ns: 10_100_000,
                flags: 0,
            },
            SAMPLE_RATE,
        )
        .is_ok()
    {
        return Err("Audio source clock accepted a lost frame".into());
    }
    Ok(())
}

#[derive(Default)]
pub struct SourceClock {
    previous: Option<Packet>,
}
impl SourceClock {
    pub fn validate(&mut self, packet: Packet, rate: u32) -> Result<(), String> {
        let rejected = |reason| {
            format!(
                "{reason}; previous={:?}; rejected={packet:?}",
                self.previous
            )
        };
        if packet.flags & TIMESTAMP_ERROR != 0 {
            return Err(rejected("WASAPI process packet timestamp error"));
        }
        // At startup there is no previous captured packet to correlate with.
        if self.previous.is_some() && packet.flags & DATA_DISCONTINUITY != 0 {
            return Err(rejected("WASAPI process packet data discontinuity"));
        }
        if packet.frames == 0 || packet.qpc_100ns == 0 || rate == 0 {
            return Err(rejected("Missing process audio source timestamp"));
        }
        if let Some(previous) = self.previous {
            // Process-loopback virtual devices have no position and report zero.
            let virtual_position = previous.device_position == 0 && packet.device_position == 0;
            if !virtual_position
                && previous.device_position.checked_add(previous.frames as u64)
                    != Some(packet.device_position)
            {
                return Err(rejected("Discontinuous process audio device position"));
            }
            if packet.qpc_100ns <= previous.qpc_100ns {
                return Err(rejected("Non-increasing process audio QPC timestamp"));
            }
            let elapsed = qpc_seconds(packet.qpc_100ns - previous.qpc_100ns);
            if (elapsed - previous.frames as f64 / rate as f64).abs() > 0.050 {
                return Err(rejected("Process audio source timestamp jump exceeds 50ms"));
            }
        }
        self.previous = Some(packet);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn packet(frames: u32) -> Packet {
        Packet {
            frames,
            ..Packet::default()
        }
    }

    #[test]
    fn partial_space_rejection_preserves_pcm_and_counters() {
        let (mut producer, mut consumer) = ring(5);
        producer
            .push(packet(3), Some(&[1., 2., 3., 4., 5., 6.]))
            .unwrap();
        assert!(producer.push(packet(3), Some(&[9.; 6])).is_err());
        producer.push(packet(2), Some(&[7., 8., 9., 10.])).unwrap();
        assert!(producer.push(packet(1), None).is_err());
        assert_eq!(
            consumer.consume(|p, a, b| (p.frames, [a, b].concat())),
            Some((3, vec![1., 2., 3., 4., 5., 6.]))
        );
        assert_eq!(
            consumer.consume(|p, a, b| (p.frames, [a, b].concat())),
            Some((2, vec![7., 8., 9., 10.]))
        );
        assert!(consumer.consume(|_, _, _| ()).is_none());
    }

    #[test]
    fn wraps_and_preserves_silence_and_inflight_budget() {
        let (mut producer, mut consumer) = ring(5);
        producer.push(packet(3), Some(&[1.; 6])).unwrap();
        consumer.consume(|_, _, _| {
            assert!(producer.push(packet(3), None).is_err());
        });
        producer.push(packet(4), None).unwrap();
        consumer.consume(|p, a, b| {
            assert_eq!(p.frames, 4);
            assert!(!b.is_empty());
            assert_eq!([a, b].concat(), vec![0.; 8]);
        });
    }

    #[test]
    fn five_second_capacity_is_frames_independent_of_packet_size() {
        let capacity = SAMPLE_RATE as usize * 5;
        let (mut producer, mut consumer) = ring(capacity);
        for _ in 0..500 {
            producer.push(packet(480), None).unwrap();
        }
        assert!(producer.push(packet(1), None).is_err());
        let mut frames = 0;
        while consumer.consume(|p, _, _| frames += p.frames).is_some() {}
        assert_eq!(frames, SAMPLE_RATE * 5);
    }

    #[test]
    fn concurrent_producer_never_reuses_unreleased_pcm_or_descriptors() {
        let (mut producer, mut consumer) = ring(128);
        let writer = std::thread::spawn(move || {
            for index in 0..10_000_u64 {
                let packet = Packet {
                    frames: (index % 17 + 1) as u32,
                    device_position: index,
                    qpc_100ns: index + 1,
                    flags: 0,
                };
                let samples = vec![index as f32; packet.frames as usize * CHANNELS];
                while producer.push(packet, Some(&samples)).is_err() {
                    std::thread::yield_now();
                }
            }
        });
        for index in 0..10_000_u64 {
            while consumer
                .consume(|packet, first, second| {
                    assert_eq!(packet.device_position, index);
                    assert_eq!(packet.qpc_100ns, index + 1);
                    assert_eq!(
                        first.len() + second.len(),
                        packet.frames as usize * CHANNELS
                    );
                    assert!(first
                        .iter()
                        .chain(second)
                        .all(|sample| *sample == index as f32));
                    std::thread::yield_now();
                })
                .is_none()
            {
                std::thread::yield_now();
            }
        }
        writer.join().unwrap();
    }

    #[test]
    fn qpc_is_already_100ns_and_device_positions_are_not_file_offsets() {
        assert_eq!(qpc_seconds(12_345_000_000), 1234.5);
        let mut clock = SourceClock::default();
        clock
            .validate(
                Packet {
                    frames: 480,
                    device_position: 9_000,
                    qpc_100ns: 12_345_000_000,
                    flags: 0,
                },
                SAMPLE_RATE,
            )
            .unwrap();
        clock
            .validate(
                Packet {
                    frames: 480,
                    device_position: 9_480,
                    qpc_100ns: 12_345_100_000,
                    flags: 0,
                },
                SAMPLE_RATE,
            )
            .unwrap();
        assert!(clock
            .validate(
                Packet {
                    frames: 480,
                    device_position: 9_961,
                    qpc_100ns: 12_345_200_000,
                    flags: 0,
                },
                SAMPLE_RATE
            )
            .is_err());
    }

    #[test]
    fn virtual_zero_positions_use_real_qpc_without_rewriting_packets() {
        let mut clock = SourceClock::default();
        let (mut producer, mut consumer) = ring(1440);
        for index in 0..3 {
            let packet = Packet {
                frames: 480,
                device_position: 0,
                qpc_100ns: 10_000_000 + index * 100_000,
                flags: 2,
            };
            clock.validate(packet, SAMPLE_RATE).unwrap();
            producer.push(packet, None).unwrap();
            consumer
                .consume(|actual, first, second| {
                    assert_eq!(actual.device_position, 0);
                    assert_eq!(actual.qpc_100ns, packet.qpc_100ns);
                    assert_eq!(actual.flags, packet.flags);
                    assert!(first.iter().chain(second).all(|sample| *sample == 0.0));
                })
                .unwrap();
        }
    }

    #[test]
    fn virtual_positions_do_not_hide_clock_failures_and_retain_rejected_values() {
        let first = Packet {
            frames: 480,
            device_position: 0,
            qpc_100ns: 10_000_000,
            flags: 0,
        };
        for (qpc_100ns, flags) in [
            (10_000_000, 0),
            (9_000_000, 0),
            (11_000_000, 0),
            (10_100_000, DATA_DISCONTINUITY),
            (10_100_000, TIMESTAMP_ERROR),
        ] {
            let mut clock = SourceClock::default();
            clock.validate(first, SAMPLE_RATE).unwrap();
            let rejected = Packet {
                qpc_100ns,
                flags,
                ..first
            };
            let error = clock.validate(rejected, SAMPLE_RATE).unwrap_err();
            assert!(error.contains("previous=Some(Packet"));
            assert!(error.contains(&format!("rejected={rejected:?}")));
            assert_eq!(clock.previous.unwrap().qpc_100ns, first.qpc_100ns);
            clock
                .validate(
                    Packet {
                        qpc_100ns: 10_100_000,
                        ..first
                    },
                    SAMPLE_RATE,
                )
                .unwrap();
        }
    }

    #[test]
    fn qpc_jump_boundary_uses_the_integer_counter_difference() {
        let first = Packet {
            frames: 480,
            device_position: 0,
            qpc_100ns: 10_000_000,
            flags: 0,
        };
        for (elapsed, accepted) in [(600_000, true), (600_001, false)] {
            let mut clock = SourceClock::default();
            clock.validate(first, SAMPLE_RATE).unwrap();
            assert_eq!(
                clock
                    .validate(
                        Packet {
                            qpc_100ns: first.qpc_100ns + elapsed,
                            ..first
                        },
                        SAMPLE_RATE
                    )
                    .is_ok(),
                accepted
            );
        }
    }

    #[test]
    fn nonzero_positions_still_require_frame_continuity() {
        let first = Packet {
            frames: 480,
            device_position: 9000,
            qpc_100ns: 10_000_000,
            flags: 0,
        };
        for device_position in [0, 9000, 9479, 9481] {
            let mut clock = SourceClock::default();
            clock.validate(first, SAMPLE_RATE).unwrap();
            let packet = Packet {
                device_position,
                qpc_100ns: 10_100_000,
                ..first
            };
            let error = clock.validate(packet, SAMPLE_RATE).unwrap_err();
            assert!(error.contains("device position"));
            assert!(error.contains(&format!("rejected={packet:?}")));
        }
    }

    #[test]
    fn rejects_timestamp_stall_and_jump() {
        let first = Packet {
            frames: 480,
            device_position: 0,
            qpc_100ns: 10_000_000,
            flags: 0,
        };
        for qpc in [10_000_000, 11_000_000] {
            let mut clock = SourceClock::default();
            clock.validate(first, SAMPLE_RATE).unwrap();
            assert!(clock
                .validate(
                    Packet {
                        device_position: 480,
                        qpc_100ns: qpc,
                        ..first
                    },
                    SAMPLE_RATE
                )
                .is_err());
        }
    }

    #[test]
    fn accepts_only_initial_discontinuity_and_retains_raw_flags() {
        let first = Packet {
            frames: 480,
            device_position: 0,
            qpc_100ns: 10_000_000,
            flags: DATA_DISCONTINUITY | 2,
        };
        let mut clock = SourceClock::default();
        clock.validate(first, SAMPLE_RATE).unwrap();
        let second = Packet {
            device_position: 480,
            qpc_100ns: 10_100_000,
            flags: 0,
            ..first
        };
        clock.validate(second, SAMPLE_RATE).unwrap();
        assert!(clock
            .validate(
                Packet {
                    device_position: 960,
                    qpc_100ns: 10_200_000,
                    flags: DATA_DISCONTINUITY,
                    ..first
                },
                SAMPLE_RATE
            )
            .is_err());
        let (mut producer, mut consumer) = ring(480);
        producer.push(first, None).unwrap();
        assert_eq!(
            consumer.consume(|packet, _, _| packet.flags),
            Some(first.flags)
        );
    }

    #[test]
    fn timestamp_error_is_fatal_for_initial_and_later_packets() {
        let first = Packet {
            frames: 480,
            device_position: 0,
            qpc_100ns: 10_000_000,
            flags: 0,
        };
        for flags in [TIMESTAMP_ERROR, TIMESTAMP_ERROR | DATA_DISCONTINUITY] {
            let mut clock = SourceClock::default();
            assert!(clock
                .validate(Packet { flags, ..first }, SAMPLE_RATE)
                .is_err());
            clock.validate(first, SAMPLE_RATE).unwrap();
            assert!(clock
                .validate(
                    Packet {
                        device_position: 480,
                        qpc_100ns: 10_100_000,
                        flags,
                        ..first
                    },
                    SAMPLE_RATE
                )
                .is_err());
        }
    }
}
