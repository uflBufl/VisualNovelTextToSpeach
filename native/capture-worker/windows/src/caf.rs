use crate::audio_buffer::{self, Consumer};
use serde_json::json;
use std::io::{self, Seek, SeekFrom, Write};

pub struct Caf<W> {
    output: W,
    samples: u64,
}
impl<W: Write + Seek> Caf<W> {
    pub fn new(mut output: W, rate: u32) -> io::Result<Self> {
        if !(8000..=192000).contains(&rate) {
            return Err(io::Error::other("Invalid CAF sample rate"));
        }
        output.write_all(b"caff\0\x01\0\0desc")?;
        output.write_all(&32_i64.to_be_bytes())?;
        output.write_all(&(rate as f64).to_be_bytes())?;
        output.write_all(b"lpcm")?;
        for value in [3_u32, 8, 1, 2, 32] {
            output.write_all(&value.to_be_bytes())?;
        }
        output.write_all(b"data")?;
        // CAF -1 permits lossless recovery if the helper is terminated before finalize.
        output.write_all(&(-1_i64).to_be_bytes())?;
        output.write_all(&0_u32.to_be_bytes())?;
        output.flush()?;
        Ok(Self { output, samples: 0 })
    }
    pub fn write(&mut self, samples: &[f32]) -> io::Result<()> {
        if samples.len() % 2 != 0 {
            return Err(io::Error::other("CAF stereo packet is incomplete"));
        }
        // The source platform is little-endian; CAF flags declare little-endian float32.
        #[cfg(target_endian = "little")]
        self.output.write_all(unsafe {
            std::slice::from_raw_parts(samples.as_ptr().cast::<u8>(), samples.len() * 4)
        })?;
        #[cfg(target_endian = "big")]
        for sample in samples {
            self.output.write_all(&sample.to_le_bytes())?;
        }
        self.samples += samples.len() as u64;
        Ok(())
    }
    pub fn finalize(&mut self) -> io::Result<()> {
        self.output.flush()?;
        self.output.seek(SeekFrom::Start(56))?;
        self.output
            .write_all(&(self.samples as i64 * 4 + 4).to_be_bytes())?;
        self.output.flush()?;
        self.output.seek(SeekFrom::End(0))?;
        Ok(())
    }
    pub fn flush(&mut self) -> io::Result<()> {
        self.output.flush()
    }
}

pub fn write_packet<W: Write + Seek>(
    consumer: &mut Consumer,
    caf: &mut Caf<W>,
    journal: &mut impl Write,
    frames: &mut u64,
    peak: &mut f32,
    callbacks: &mut u64,
    first_packet_flags: &mut Option<u32>,
) -> Option<Result<(), String>> {
    consumer.consume(|packet, first, second| {
        let mut packet_peak = 0_f32;
        for value in first.iter().chain(second) {
            if !value.is_finite() {
                return Err("Process PCM contains non-finite samples".into());
            }
            packet_peak = packet_peak.max(value.abs());
        }
        caf.write(first)
            .and_then(|_| caf.write(second))
            .and_then(|_| caf.flush())
            .map_err(|e| e.to_string())?;
        let file_frame_start = *frames;
        // PCM remains in CAF even if the following journal write fails.
        *frames += packet.frames as u64;
        *peak = peak.max(packet_peak);
        *callbacks += 1;
        first_packet_flags.get_or_insert(packet.flags);
        serde_json::to_writer(
            &mut *journal,
            &json!({
                "kind":"audio", "frame_count":packet.frames, "file_frame_start":file_frame_start,
                "sample_time":packet.device_position,
                "monotonic_s":audio_buffer::qpc_seconds(packet.qpc_100ns),
                "qpc_100ns":packet.qpc_100ns, "flags":packet.flags
            }),
        )
        .map_err(|e| e.to_string())?;
        journal
            .write_all(b"\n")
            .and_then(|_| journal.flush())
            .map_err(|e| e.to_string())?;
        Ok(())
    })
}

pub fn selfcheck() -> Result<(), String> {
    let mut writer =
        Caf::new(std::io::Cursor::new(Vec::new()), 48000).map_err(|e| e.to_string())?;
    writer.write(&[0.25, -0.5]).map_err(|e| e.to_string())?;
    if writer.output.get_ref().get(56..64) != Some((-1_i64).to_be_bytes().as_slice()) {
        return Err("CAF recovery header check failed".into());
    }
    writer.finalize().map_err(|e| e.to_string())?;
    let bytes = writer.output.get_ref();
    if bytes.len() != 76
        || bytes.get(56..64) != Some(12_i64.to_be_bytes().as_slice())
        || bytes.get(68..72) != Some(0.25_f32.to_le_bytes().as_slice())
    {
        return Err("CAF float32 finalization check failed".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    #[test]
    fn interrupted_and_finalized_caf_match_recovery_contract() {
        let mut writer = Caf::new(Cursor::new(Vec::new()), 48000).unwrap();
        writer.write(&[0.25, -0.5, 0., 0.]).unwrap();
        let bytes = writer.output.get_ref();
        assert_eq!(&bytes[..8], b"caff\0\x01\0\0");
        assert_eq!(
            f64::from_be_bytes(bytes[20..28].try_into().unwrap()),
            48000.
        );
        assert_eq!(&bytes[28..32], b"lpcm");
        assert_eq!(
            &bytes[32..52],
            &[0, 0, 0, 3, 0, 0, 0, 8, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 32]
        );
        assert_eq!(&bytes[52..56], b"data");
        assert_eq!(&bytes[56..64], &(-1_i64).to_be_bytes());
        assert_eq!(&bytes[68..72], &0.25_f32.to_le_bytes());
        writer.finalize().unwrap();
        assert_eq!(&writer.output.get_ref()[56..64], &20_i64.to_be_bytes());
        assert_eq!(writer.output.get_ref().len(), 84);
    }

    #[test]
    fn rejects_invalid_header_and_partial_stereo_frames() {
        assert!(Caf::new(Cursor::new(Vec::new()), 0).is_err());
        let mut writer = Caf::new(Cursor::new(Vec::new()), 48000).unwrap();
        assert!(writer.write(&[1.]).is_err());
        assert_eq!(writer.samples, 0);
    }

    #[test]
    fn journal_failure_preserves_committed_pcm_accounting() {
        struct BrokenJournal {
            fail_flush: bool,
            bytes: Vec<u8>,
        }
        impl Write for BrokenJournal {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                if self.fail_flush {
                    self.bytes.extend_from_slice(bytes);
                    Ok(bytes.len())
                } else {
                    Err(io::Error::other("journal write failed"))
                }
            }
            fn flush(&mut self) -> io::Result<()> {
                Err(io::Error::other("journal flush failed"))
            }
        }
        for fail_flush in [false, true] {
            let (mut producer, mut consumer) = audio_buffer::ring(4);
            let mut caf = Caf::new(Cursor::new(Vec::new()), 48000).unwrap();
            let (mut frames, mut peak, mut callbacks, mut flags) = (0, 0., 0, None);
            let mut journal = Vec::new();
            producer
                .push(
                    audio_buffer::Packet {
                        frames: 2,
                        flags: 1,
                        ..Default::default()
                    },
                    Some(&[0.25; 4]),
                )
                .unwrap();
            write_packet(
                &mut consumer,
                &mut caf,
                &mut journal,
                &mut frames,
                &mut peak,
                &mut callbacks,
                &mut flags,
            )
            .unwrap()
            .unwrap();
            let event: serde_json::Value = serde_json::from_slice(&journal).unwrap();
            assert_eq!(event["file_frame_start"], 0);
            producer
                .push(
                    audio_buffer::Packet {
                        frames: 2,
                        ..Default::default()
                    },
                    Some(&[0.75; 4]),
                )
                .unwrap();
            let mut broken = BrokenJournal {
                fail_flush,
                bytes: Vec::new(),
            };
            let result = write_packet(
                &mut consumer,
                &mut caf,
                &mut broken,
                &mut frames,
                &mut peak,
                &mut callbacks,
                &mut flags,
            )
            .unwrap();
            assert!(result.is_err());
            if fail_flush {
                let event: serde_json::Value = serde_json::from_slice(&broken.bytes).unwrap();
                assert_eq!(event["file_frame_start"], 2);
            }
            caf.finalize().unwrap();
            assert_eq!((frames, peak, callbacks, flags), (4, 0.75, 2, Some(1)));
            assert_eq!(caf.output.get_ref().len(), 68 + frames as usize * 8);
            assert_eq!(
                &caf.output.get_ref()[56..64],
                &(4_i64 + frames as i64 * 8).to_be_bytes()
            );
        }
    }
}
