use super::{MAX_HEADER_BYTES, MAX_PCM_BYTES};
use serde::Deserialize;
use std::io::{self, BufRead, BufReader, Read};
use std::sync::mpsc::{self, Receiver};

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(super) enum Latency {
    Named(String),
    Seconds(f64),
}

impl Latency {
    pub(super) fn validate(&self) -> Result<(), String> {
        match self {
            Self::Named(name) if name == "low" || name == "high" => Ok(()),
            Self::Seconds(value) if value.is_finite() && *value > 0.0 => Ok(()),
            _ => Err("latency must be low, high, or positive finite seconds".into()),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Command {
    Load {
        id: u64,
        sample_rate: u32,
        channels: u8,
        frames: u64,
        latency: Latency,
    },
    Start {
        id: u64,
    },
    Stop {
        id: u64,
    },
    Pause {
        id: u64,
    },
    Resume {
        id: u64,
    },
    QueryDevice,
    CheckFormat {
        sample_rate: u32,
        channels: u8,
    },
    Status {
        id: u64,
    },
    Seek {
        id: u64,
        position_frames: u64,
    },
    OpenStream {
        id: u64,
        sample_rate: u32,
        channels: u8,
        latency: Latency,
        capacity_frames: u64,
    },
    Append {
        id: u64,
        frames: u64,
    },
    End {
        id: u64,
    },
    Close {
        id: u64,
    },
    Shutdown,
}

#[derive(Debug)]
pub(super) struct Clip {
    pub(super) id: u64,
    pub(super) sample_rate: u32,
    pub(super) channels: u8,
    pub(super) latency: Latency,
    pub(super) pcm: Vec<f32>,
    pub(super) position: u64,
}

#[derive(Debug)]
pub(super) enum Input {
    Load(Clip),
    Append { id: u64, frames: u64, pcm: Vec<f32> },
    Control(Command),
    End,
    Fatal(String),
}

pub(super) fn pcm_samples(
    id: u64,
    rate: u32,
    channels: u8,
    frames: u64,
    latency: &Latency,
) -> Result<usize, String> {
    latency.validate()?;
    if id == 0 || rate == 0 || frames == 0 || !matches!(channels, 1 | 2) {
        return Err("load requires positive id, sample_rate, frames and 1 or 2 channels".into());
    }
    let bytes = frames
        .checked_mul(u64::from(channels))
        .and_then(|n| n.checked_mul(4));
    match bytes {
        Some(n) if n <= MAX_PCM_BYTES as u64 => Ok(n as usize / 4),
        _ => Err("PCM exceeds 128 MiB limit".into()),
    }
}

fn read_pcm(reader: &mut impl BufRead, count: usize) -> Result<Vec<f32>, String> {
    let mut pcm = vec![0.0_f32; count];
    // Every f32 bit pattern is valid; decode after the exact bounded read.
    let bytes = unsafe { std::slice::from_raw_parts_mut(pcm.as_mut_ptr().cast::<u8>(), count * 4) };
    reader
        .read_exact(bytes)
        .map_err(|e| format!("truncated PCM payload: {e}"))?;
    for sample in &mut pcm {
        *sample = f32::from_bits(u32::from_le(sample.to_bits()));
        if !sample.is_finite() {
            return Err("PCM must contain only finite float32 samples".into());
        }
    }
    Ok(pcm)
}

pub(super) fn read_input(
    reader: &mut impl BufRead,
    format: &mut Option<(u64, u32, u8, u64)>,
) -> Result<Option<Input>, String> {
    let mut header = Vec::new();
    reader
        .take(MAX_HEADER_BYTES + 1)
        .read_until(b'\n', &mut header)
        .map_err(|e| e.to_string())?;
    if header.is_empty() {
        return Ok(None);
    }
    if header.len() as u64 > MAX_HEADER_BYTES || header.last() != Some(&b'\n') {
        return Err("JSON header is truncated or exceeds 4096 bytes".into());
    }
    let command: Command =
        serde_json::from_slice(&header).map_err(|e| format!("invalid JSON command: {e}"))?;
    match command {
        Command::Load {
            id,
            sample_rate,
            channels,
            frames,
            latency,
        } => {
            let count = pcm_samples(id, sample_rate, channels, frames, &latency)?;
            let pcm = read_pcm(reader, count)?;
            Ok(Some(Input::Load(Clip {
                id,
                sample_rate,
                channels,
                latency,
                pcm,
                position: 0,
            })))
        }
        Command::OpenStream {
            id,
            sample_rate,
            channels,
            ref latency,
            capacity_frames,
        } => {
            pcm_samples(id, sample_rate, channels, capacity_frames, latency)?;
            if capacity_frames > u64::from(sample_rate) * 2 {
                return Err("stream capacity exceeds two seconds".into());
            }
            *format = Some((id, sample_rate, channels, capacity_frames));
            Ok(Some(Input::Control(command)))
        }
        Command::Append { id, frames } => {
            let (stream_id, rate, channels, capacity) =
                format.ok_or("append requires open_stream")?;
            if id != stream_id || frames > capacity {
                return Err("append id is stale or payload exceeds stream capacity".into());
            }
            let count = pcm_samples(id, rate, channels, frames, &Latency::Named("low".into()))?;
            Ok(Some(Input::Append {
                id,
                frames,
                pcm: read_pcm(reader, count)?,
            }))
        }
        _ => Ok(Some(Input::Control(command))),
    }
}

pub(super) fn input_channel() -> Receiver<Input> {
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut reader = BufReader::new(io::stdin());
        let mut format = None;
        loop {
            let input = match read_input(&mut reader, &mut format) {
                Ok(Some(input)) => input,
                Ok(None) => Input::End,
                Err(error) => Input::Fatal(error),
            };
            let end = matches!(
                input,
                Input::End | Input::Fatal(_) | Input::Control(Command::Shutdown)
            );
            if sender.send(input).is_err() || end {
                break;
            }
        }
    });
    receiver
}
