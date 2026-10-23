use libloading::Library;
use serde::Deserialize;
use serde_json::{json, Value};
use std::ffi::{c_char, c_int, c_ulong, c_void, CStr, OsString};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

const MAX_PCM_BYTES: usize = 128 * 1024 * 1024;
const MAX_HEADER_BYTES: u64 = 4096;
const PA_COMPLETE: c_int = 1;
const PA_ABORT: c_int = 2;
const PA_OUTPUT_UNDERFLOW: c_ulong = 4;
const PA_STREAM_IS_STOPPED: c_int = -9983;

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Latency {
    Named(String),
    Seconds(f64),
}

impl Latency {
    fn validate(&self) -> Result<(), String> {
        match self {
            Self::Named(name) if name == "low" || name == "high" => Ok(()),
            Self::Seconds(value) if value.is_finite() && *value > 0.0 => Ok(()),
            _ => Err("latency must be low, high, or positive finite seconds".into()),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
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
    Shutdown,
}

#[derive(Debug)]
struct Clip {
    id: u64,
    sample_rate: u32,
    channels: u8,
    latency: Latency,
    pcm: Vec<f32>,
}

#[derive(Debug)]
enum Input {
    Load(Clip),
    Control(Command),
    End,
    Fatal(String),
}

fn pcm_samples(
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

fn read_input(reader: &mut impl BufRead) -> Result<Option<Input>, String> {
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
    if let Command::Load {
        id,
        sample_rate,
        channels,
        frames,
        latency,
    } = command
    {
        let count = pcm_samples(id, sample_rate, channels, frames, &latency)?;
        let mut pcm = vec![0.0_f32; count];
        // Every f32 bit pattern is valid; decode endianness after the exact bounded read.
        let bytes =
            unsafe { std::slice::from_raw_parts_mut(pcm.as_mut_ptr().cast::<u8>(), count * 4) };
        reader
            .read_exact(bytes)
            .map_err(|e| format!("truncated PCM payload: {e}"))?;
        for sample in &mut pcm {
            *sample = f32::from_bits(u32::from_le(sample.to_bits()));
            if !sample.is_finite() {
                return Err("PCM must contain only finite float32 samples".into());
            }
        }
        Ok(Some(Input::Load(Clip {
            id,
            sample_rate,
            channels,
            latency,
            pcm,
        })))
    } else {
        Ok(Some(Input::Control(command)))
    }
}

fn input_channel() -> Receiver<Input> {
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut reader = BufReader::new(io::stdin());
        loop {
            let input = match read_input(&mut reader) {
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

#[repr(C)]
struct DeviceInfo {
    struct_version: c_int,
    name: *const c_char,
    host_api: c_int,
    max_input_channels: c_int,
    max_output_channels: c_int,
    default_low_input_latency: f64,
    default_low_output_latency: f64,
    default_high_input_latency: f64,
    default_high_output_latency: f64,
    default_sample_rate: f64,
}

#[repr(C)]
struct HostApiInfo {
    struct_version: c_int,
    api_type: c_int,
    name: *const c_char,
    device_count: c_int,
    default_input_device: c_int,
    default_output_device: c_int,
}

#[repr(C)]
struct StreamInfo {
    struct_version: c_int,
    input_latency: f64,
    output_latency: f64,
    sample_rate: f64,
}

impl StreamInfo {
    fn validate(&self) -> Result<(), String> {
        if !self.output_latency.is_finite()
            || self.output_latency < 0.0
            || !self.sample_rate.is_finite()
            || self.sample_rate <= 0.0
        {
            return Err("invalid native output latency or sample rate".into());
        }
        Ok(())
    }
}

#[repr(C)]
struct StreamParameters {
    device: c_int,
    channel_count: c_int,
    sample_format: c_ulong,
    suggested_latency: f64,
    host_api_specific_stream_info: *mut c_void,
}

#[repr(C)]
struct CallbackTimeInfo {
    input_buffer_adc_time: f64,
    current_time: f64,
    output_buffer_dac_time: f64,
}

type Callback = unsafe extern "C" fn(
    *const c_void,
    *mut c_void,
    c_ulong,
    *const CallbackTimeInfo,
    c_ulong,
    *mut c_void,
) -> c_int;
type StreamAction = unsafe extern "C" fn(*mut c_void) -> c_int;
type OpenStream = unsafe extern "C" fn(
    *mut *mut c_void,
    *const StreamParameters,
    *const StreamParameters,
    f64,
    c_ulong,
    c_ulong,
    Option<Callback>,
    *mut c_void,
) -> c_int;

struct PortAudio {
    _library: Library,
    initialized: bool,
    terminate: unsafe extern "C" fn() -> c_int,
    error_text: unsafe extern "C" fn(c_int) -> *const c_char,
    default_output: unsafe extern "C" fn() -> c_int,
    device_info: unsafe extern "C" fn(c_int) -> *const DeviceInfo,
    host_api_info: unsafe extern "C" fn(c_int) -> *const HostApiInfo,
    stream_info: unsafe extern "C" fn(*mut c_void) -> *const StreamInfo,
    open: OpenStream,
    start: StreamAction,
    abort: StreamAction,
    close: StreamAction,
    is_active: StreamAction,
}

impl PortAudio {
    fn load(path: OsString) -> Result<Self, String> {
        unsafe {
            let library = Library::new(path).map_err(|e| e.to_string())?;
            let initialize = *library
                .get::<unsafe extern "C" fn() -> c_int>(b"Pa_Initialize\0")
                .map_err(|e| e.to_string())?;
            let mut pa = Self {
                initialized: false,
                terminate: *library.get(b"Pa_Terminate\0").map_err(|e| e.to_string())?,
                error_text: *library
                    .get(b"Pa_GetErrorText\0")
                    .map_err(|e| e.to_string())?,
                default_output: *library
                    .get(b"Pa_GetDefaultOutputDevice\0")
                    .map_err(|e| e.to_string())?,
                device_info: *library
                    .get(b"Pa_GetDeviceInfo\0")
                    .map_err(|e| e.to_string())?,
                host_api_info: *library
                    .get(b"Pa_GetHostApiInfo\0")
                    .map_err(|e| e.to_string())?,
                stream_info: *library
                    .get(b"Pa_GetStreamInfo\0")
                    .map_err(|e| e.to_string())?,
                open: *library.get(b"Pa_OpenStream\0").map_err(|e| e.to_string())?,
                start: *library
                    .get(b"Pa_StartStream\0")
                    .map_err(|e| e.to_string())?,
                abort: *library
                    .get(b"Pa_AbortStream\0")
                    .map_err(|e| e.to_string())?,
                close: *library
                    .get(b"Pa_CloseStream\0")
                    .map_err(|e| e.to_string())?,
                is_active: *library
                    .get(b"Pa_IsStreamActive\0")
                    .map_err(|e| e.to_string())?,
                _library: library,
            };
            let result = initialize();
            if result != 0 {
                let message = pa.error(result);
                return Err(message);
            }
            pa.initialized = true;
            Ok(pa)
        }
    }

    fn error(&self, code: c_int) -> String {
        let text = unsafe { (self.error_text)(code) };
        if text.is_null() {
            format!("PortAudio error {code}")
        } else {
            format!(
                "PortAudio error {code}: {}",
                unsafe { CStr::from_ptr(text) }.to_string_lossy()
            )
        }
    }
}

impl Drop for PortAudio {
    fn drop(&mut self) {
        if !self.initialized {
            return;
        }
        let result = unsafe { (self.terminate)() };
        if result != 0 {
            eprintln!("{}", self.error(result));
        }
    }
}

struct CallbackState {
    pcm: Vec<f32>,
    channels: usize,
    position: AtomicU64,
    cancelled: AtomicBool,
    paused: AtomicBool,
    pause_applied: AtomicBool,
    callbacks: AtomicU64,
    underflows: AtomicU64,
    failed: AtomicBool,
    first_written: AtomicU64,
    first_current_time: AtomicU64,
    first_dac_time: AtomicU64,
}

impl CallbackState {
    fn new(pcm: Vec<f32>, channels: u8) -> Self {
        Self {
            pcm,
            channels: usize::from(channels),
            position: AtomicU64::new(0),
            cancelled: AtomicBool::new(false),
            paused: AtomicBool::new(false),
            pause_applied: AtomicBool::new(false),
            callbacks: AtomicU64::new(0),
            underflows: AtomicU64::new(0),
            failed: AtomicBool::new(false),
            first_written: AtomicU64::new(u64::MAX),
            first_current_time: AtomicU64::new(0),
            first_dac_time: AtomicU64::new(0),
        }
    }

    fn render(&self, output: &mut [f32], flags: c_ulong) -> c_int {
        self.callbacks.store(
            self.callbacks.load(Ordering::Relaxed).saturating_add(1),
            Ordering::Relaxed,
        );
        if flags & PA_OUTPUT_UNDERFLOW != 0 {
            self.underflows.store(
                self.underflows.load(Ordering::Relaxed).saturating_add(1),
                Ordering::Relaxed,
            );
        }
        output.fill(0.0);
        if self.cancelled.load(Ordering::Acquire) {
            return PA_ABORT;
        }
        if self.paused.load(Ordering::Acquire) {
            self.pause_applied.store(true, Ordering::Release);
            return 0;
        }
        self.pause_applied.store(false, Ordering::Release);
        let position = self.position.load(Ordering::Relaxed) as usize;
        let start = position * self.channels;
        let count = output.len().min(self.pcm.len() - start);
        output[..count].copy_from_slice(&self.pcm[start..start + count]);
        self.position
            .store((position + count / self.channels) as u64, Ordering::Release);
        if start + count == self.pcm.len() {
            PA_COMPLETE
        } else {
            0
        }
    }
}

unsafe extern "C" fn audio_callback(
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
    let result = state.render(
        std::slice::from_raw_parts_mut(output.cast::<f32>(), samples),
        flags,
    );
    if state.first_written.load(Ordering::Relaxed) == u64::MAX {
        if !time.is_null() {
            state
                .first_current_time
                .store((*time).current_time.to_bits(), Ordering::Relaxed);
            state
                .first_dac_time
                .store((*time).output_buffer_dac_time.to_bits(), Ordering::Relaxed);
        }
        state.first_written.store(
            state.position.load(Ordering::Relaxed) - before,
            Ordering::Release,
        );
    }
    result
}

struct Active<'a> {
    pa: &'a PortAudio,
    stream: *mut c_void,
    state: Box<CallbackState>,
    id: u64,
    sample_rate: u32,
    channels: u8,
    started: Instant,
    first_emitted: bool,
    pause_reported: bool,
    device_name: Option<String>,
    host_api: Option<String>,
    output_latency_seconds: f64,
    actual_sample_rate: f64,
}

impl<'a> Active<'a> {
    fn start(pa: &'a PortAudio, clip: Clip) -> Result<Self, String> {
        let device = unsafe { (pa.default_output)() };
        if device < 0 {
            return Err("no default output device".into());
        }
        let info = unsafe { (pa.device_info)(device).as_ref() }.ok_or("no output device info")?;
        if info.max_output_channels < c_int::from(clip.channels) {
            return Err("default output device has too few channels".into());
        }
        let device_name = if info.name.is_null() {
            None
        } else {
            Some(
                unsafe { CStr::from_ptr(info.name) }
                    .to_string_lossy()
                    .into_owned(),
            )
        };
        let host_api = unsafe { (pa.host_api_info)(info.host_api).as_ref() }
            .filter(|api| !api.name.is_null())
            .map(|api| {
                unsafe { CStr::from_ptr(api.name) }
                    .to_string_lossy()
                    .into_owned()
            });
        let latency = match clip.latency {
            Latency::Named(ref name) if name == "low" => info.default_low_output_latency,
            Latency::Named(_) => info.default_high_output_latency,
            Latency::Seconds(value) => value,
        };
        let parameters = StreamParameters {
            device,
            channel_count: c_int::from(clip.channels),
            sample_format: 1,
            suggested_latency: latency,
            host_api_specific_stream_info: ptr::null_mut(),
        };
        let mut active = Self {
            pa,
            stream: ptr::null_mut(),
            state: Box::new(CallbackState::new(clip.pcm, clip.channels)),
            id: clip.id,
            sample_rate: clip.sample_rate,
            channels: clip.channels,
            started: Instant::now(),
            first_emitted: false,
            pause_reported: false,
            device_name,
            host_api,
            output_latency_seconds: 0.0,
            actual_sample_rate: f64::from(clip.sample_rate),
        };
        let result = unsafe {
            (pa.open)(
                &mut active.stream,
                ptr::null(),
                &parameters,
                f64::from(active.sample_rate),
                0,
                0,
                Some(audio_callback),
                (&mut *active.state as *mut CallbackState).cast(),
            )
        };
        if result != 0 {
            return Err(pa.error(result));
        }
        let stream_info =
            unsafe { (pa.stream_info)(active.stream).as_ref() }.ok_or("no native stream info")?;
        stream_info.validate()?;
        active.output_latency_seconds = stream_info.output_latency;
        active.actual_sample_rate = stream_info.sample_rate;
        active.started = Instant::now();
        let result = unsafe { (pa.start)(active.stream) };
        if result != 0 {
            return Err(pa.error(result));
        }
        Ok(active)
    }

    fn started_event(&self) -> io::Result<()> {
        emit(json!({"event":"started", "id":self.id,
            "device_name":self.device_name, "host_api":self.host_api,
            "output_latency_seconds":self.output_latency_seconds,
            "actual_sample_rate":self.actual_sample_rate}))
    }

    fn first_callback(&mut self) -> io::Result<()> {
        let written = self.state.first_written.load(Ordering::Acquire);
        if !self.first_emitted && written != u64::MAX {
            emit(
                json!({"event":"first_callback", "id":self.id, "frames_written":written,
                "elapsed_ms":self.started.elapsed().as_secs_f64() * 1000.0,
                "callback_current_time":f64::from_bits(self.state.first_current_time.load(Ordering::Relaxed)),
                "output_dac_time":f64::from_bits(self.state.first_dac_time.load(Ordering::Relaxed))}),
            )?;
            self.first_emitted = true;
        }
        Ok(())
    }

    fn pause_event(&mut self) -> io::Result<()> {
        let paused = self.state.pause_applied.load(Ordering::Acquire);
        if paused != self.pause_reported {
            emit(json!({"event":if paused { "paused" } else { "resumed" }, "id":self.id}))?;
            self.pause_reported = paused;
        }
        Ok(())
    }

    fn close(&mut self) -> Result<(), String> {
        if self.stream.is_null() {
            return Ok(());
        }
        self.state.cancelled.store(true, Ordering::Release);
        let abort = unsafe { (self.pa.abort)(self.stream) };
        let close = unsafe { (self.pa.close)(self.stream) };
        if close != 0 {
            // Do not release callback storage or unload code while native callbacks may still run.
            eprintln!("fatal stream close failure: {}", self.pa.error(close));
            std::process::exit(2);
        }
        self.stream = ptr::null_mut();
        if abort == 0 || abort == PA_STREAM_IS_STOPPED {
            Ok(())
        } else {
            Err(self.pa.error(abort))
        }
    }

    fn finish(mut self, outcome: &str, error: Option<String>) -> io::Result<()> {
        let was_cancelled = self.state.cancelled.load(Ordering::Acquire);
        let close_error = self.close().err();
        self.first_callback()?;
        let error = error.or(close_error);
        let outcome = if error.is_some() {
            "failed"
        } else if was_cancelled {
            "interrupted"
        } else {
            outcome
        };
        let mut event = json!({"event":"finished", "id":self.id, "outcome":outcome,
            "frames_played":self.state.position.load(Ordering::Acquire),
            "underflow_count":self.state.underflows.load(Ordering::Relaxed),
            "callback_count":self.state.callbacks.load(Ordering::Relaxed),
            "sample_rate":self.sample_rate, "channels":self.channels,
            "device_name":self.device_name, "host_api":self.host_api,
            "output_latency_seconds":self.output_latency_seconds,
            "actual_sample_rate":self.actual_sample_rate,
            "elapsed_ms":self.started.elapsed().as_secs_f64() * 1000.0});
        if let Some(error) = error {
            event["error"] = json!(error);
        }
        emit(event)
    }
}

impl Drop for Active<'_> {
    fn drop(&mut self) {
        if let Err(error) = self.close() {
            eprintln!("{error}");
        }
    }
}

fn emit(value: Value) -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, &value)?;
    writeln!(stdout)?;
    stdout.flush()
}

fn finish_unstarted(
    id: u64,
    sample_rate: u32,
    channels: u8,
    outcome: &str,
    error: Option<String>,
) -> io::Result<()> {
    let mut event = json!({"event":"finished", "id":id, "outcome":outcome,
        "frames_played":0, "underflow_count":0, "callback_count":0,
        "sample_rate":sample_rate, "channels":channels, "elapsed_ms":0.0});
    if let Some(error) = error {
        event["error"] = json!(error);
    }
    emit(event)
}

fn run(pa: &PortAudio) -> io::Result<()> {
    emit(
        json!({"event":"ready", "protocol":1, "pid":std::process::id(), "max_pcm_bytes":MAX_PCM_BYTES}),
    )?;
    let input = input_channel();
    let mut loaded: Option<Clip> = None;
    let mut active: Option<Active<'_>> = None;
    let mut last_id = 0;
    loop {
        if let Some(playing) = active.as_mut() {
            playing.first_callback()?;
            playing.pause_event()?;
            let status = unsafe { (pa.is_active)(playing.stream) };
            if status <= 0 {
                let failed = playing.state.failed.load(Ordering::Acquire);
                let error = if status < 0 {
                    Some(pa.error(status))
                } else if failed {
                    Some("native callback rejected output buffer".into())
                } else {
                    None
                };
                active.take().unwrap().finish("complete", error)?;
            }
        }
        let message = match input.recv_timeout(Duration::from_millis(5)) {
            Ok(message) => message,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => Input::End,
        };
        match message {
            Input::Load(clip) => {
                if active.is_some() || loaded.is_some() || clip.id <= last_id {
                    emit(json!({"event":"error", "id":clip.id, "fatal":false,
                        "error":"load requires idle worker and a strictly increasing id"}))?;
                } else {
                    last_id = clip.id;
                    emit(json!({"event":"loaded", "id":clip.id}))?;
                    loaded = Some(clip);
                }
            }
            Input::Control(Command::Start { id }) => {
                if loaded.as_ref().is_some_and(|clip| clip.id == id) {
                    let clip = loaded.take().unwrap();
                    let rate = clip.sample_rate;
                    let channels = clip.channels;
                    match Active::start(pa, clip) {
                        Ok(playing) => {
                            playing.started_event()?;
                            active = Some(playing);
                        }
                        Err(error) => {
                            finish_unstarted(id, rate, channels, "failed", Some(error))?;
                        }
                    }
                }
            }
            Input::Control(Command::Stop { id }) => {
                if active.as_ref().is_some_and(|playing| playing.id == id) {
                    active.take().unwrap().finish("interrupted", None)?;
                } else if loaded.as_ref().is_some_and(|clip| clip.id == id) {
                    let clip = loaded.take().unwrap();
                    finish_unstarted(
                        clip.id,
                        clip.sample_rate,
                        clip.channels,
                        "interrupted",
                        None,
                    )?;
                }
            }
            Input::Control(Command::Pause { id }) => {
                if let Some(playing) = active.as_ref().filter(|playing| playing.id == id) {
                    playing.state.paused.store(true, Ordering::Release);
                }
            }
            Input::Control(Command::Resume { id }) => {
                if let Some(playing) = active.as_ref().filter(|playing| playing.id == id) {
                    playing.state.paused.store(false, Ordering::Release);
                }
            }
            Input::End | Input::Fatal(_) | Input::Control(Command::Shutdown) => {
                let error = if let Input::Fatal(error) = message {
                    Some(error)
                } else {
                    None
                };
                if let Some(playing) = active.take() {
                    playing.finish(
                        if error.is_some() {
                            "failed"
                        } else {
                            "interrupted"
                        },
                        error.clone(),
                    )?;
                }
                if let Some(clip) = loaded.take() {
                    finish_unstarted(
                        clip.id,
                        clip.sample_rate,
                        clip.channels,
                        if error.is_some() {
                            "failed"
                        } else {
                            "interrupted"
                        },
                        error.clone(),
                    )?;
                }
                if let Some(error) = error {
                    emit(json!({"event":"error", "fatal":true, "error":error}))?;
                    return Err(io::Error::new(io::ErrorKind::InvalidData, error));
                }
                return Ok(());
            }
            Input::Control(Command::Load { .. }) => unreachable!(),
        }
    }
}

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let path = match (arguments.next(), arguments.next(), arguments.next()) {
        (Some(flag), Some(path), None) if flag == "--portaudio" => path,
        _ => {
            eprintln!("usage: vntts-audio-worker --portaudio PATH");
            std::process::exit(2);
        }
    };
    let result = PortAudio::load(path)
        .map_err(io::Error::other)
        .and_then(|pa| run(&pa));
    if let Err(error) = result {
        eprintln!("audio worker: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn load_header(frames: u64) -> Vec<u8> {
        format!("{{\"op\":\"load\",\"id\":1,\"sample_rate\":24000,\"channels\":1,\"frames\":{frames},\"latency\":\"low\"}}\n").into_bytes()
    }

    #[test]
    fn validates_size_before_payload_allocation() {
        for (id, rate, channels, frames) in [
            (0, 24000, 1, 1),
            (1, 0, 1, 1),
            (1, 24000, 3, 1),
            (1, 24000, 1, 0),
            (1, 24000, 2, u64::MAX),
        ] {
            assert!(pcm_samples(id, rate, channels, frames, &Latency::Seconds(0.01)).is_err());
        }
        assert_eq!(
            pcm_samples(
                1,
                24000,
                2,
                (MAX_PCM_BYTES / 8) as u64,
                &Latency::Named("high".into())
            )
            .unwrap(),
            MAX_PCM_BYTES / 4
        );
        assert!(read_input(&mut Cursor::new(load_header(u64::MAX))).is_err());
        assert!(Latency::Seconds(f64::NAN).validate().is_err());
    }

    #[test]
    fn reads_exact_little_endian_payload_then_next_header() {
        let mut bytes = load_header(2);
        bytes.extend_from_slice(&0.25_f32.to_le_bytes());
        bytes.extend_from_slice(&(-0.5_f32).to_le_bytes());
        bytes.extend_from_slice(b"{\"op\":\"stop\",\"id\":1}\n");
        let mut reader = Cursor::new(bytes);
        let Some(Input::Load(clip)) = read_input(&mut reader).unwrap() else {
            panic!("missing load");
        };
        assert_eq!(clip.pcm, [0.25, -0.5]);
        assert!(matches!(
            read_input(&mut reader).unwrap(),
            Some(Input::Control(Command::Stop { id: 1 }))
        ));
    }

    #[test]
    fn malformed_or_truncated_input_fails_closed() {
        for bytes in [
            b"{}\n".to_vec(),
            b"{\"op\":\"shutdown\"}".to_vec(),
            vec![b' '; MAX_HEADER_BYTES as usize + 2],
            load_header(1),
        ] {
            assert!(read_input(&mut Cursor::new(bytes)).is_err());
        }
        let mut bytes = load_header(1);
        bytes.extend_from_slice(&f32::INFINITY.to_le_bytes());
        assert!(read_input(&mut Cursor::new(bytes)).is_err());
    }

    #[test]
    fn callback_preserves_pause_position_and_zero_fills_tail() {
        let state = CallbackState::new(vec![0.1, 0.2, 0.3, 0.4, 0.5, 0.6], 2);
        let mut output = [9.0; 4];
        assert_eq!(state.render(&mut output, PA_OUTPUT_UNDERFLOW), 0);
        assert_eq!(output, [0.1, 0.2, 0.3, 0.4]);
        assert_eq!(state.position.load(Ordering::Relaxed), 2);
        state.paused.store(true, Ordering::Release);
        assert_eq!(state.render(&mut output, 0), 0);
        assert_eq!(output, [0.0; 4]);
        assert_eq!(state.position.load(Ordering::Relaxed), 2);
        state.paused.store(false, Ordering::Release);
        assert_eq!(state.render(&mut output, 0), PA_COMPLETE);
        assert_eq!(output, [0.5, 0.6, 0.0, 0.0]);
        assert_eq!(state.position.load(Ordering::Relaxed), 3);
        assert_eq!(state.callbacks.load(Ordering::Relaxed), 3);
        assert_eq!(state.underflows.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn validates_native_stream_telemetry() {
        let mut info = StreamInfo {
            struct_version: 1,
            input_latency: 0.0,
            output_latency: 0.01,
            sample_rate: 24000.0,
        };
        assert!(info.validate().is_ok());
        for latency in [-0.01, f64::NAN, f64::INFINITY] {
            info.output_latency = latency;
            assert!(info.validate().is_err());
        }
        info.output_latency = 0.0;
        for rate in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            info.sample_rate = rate;
            assert!(info.validate().is_err());
        }
    }

    #[test]
    fn cancellation_zero_fills_without_consuming_pcm() {
        let state = CallbackState::new(vec![0.5; 4], 1);
        state.cancelled.store(true, Ordering::Release);
        let mut output = [9.0; 2];
        assert_eq!(state.render(&mut output, 0), PA_ABORT);
        assert_eq!(output, [0.0; 2]);
        assert_eq!(state.position.load(Ordering::Acquire), 0);
    }

    #[test]
    fn ffi_callback_records_first_buffer_once() {
        let mut state = CallbackState::new(vec![0.5; 4], 1);
        let mut output = [0.0; 2];
        let time = CallbackTimeInfo {
            input_buffer_adc_time: 0.0,
            current_time: 1.0,
            output_buffer_dac_time: 1.1,
        };
        let user = (&mut state as *mut CallbackState).cast();
        unsafe {
            assert_eq!(
                audio_callback(ptr::null(), output.as_mut_ptr().cast(), 2, &time, 0, user),
                0
            );
        }
        unsafe {
            assert_eq!(
                audio_callback(ptr::null(), output.as_mut_ptr().cast(), 2, &time, 0, user),
                PA_COMPLETE
            );
        }
        assert_eq!(state.first_written.load(Ordering::Acquire), 2);
        assert_eq!(
            f64::from_bits(state.first_dac_time.load(Ordering::Relaxed)),
            1.1
        );
    }
}
