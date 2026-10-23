use serde_json::{json, Value};
use std::cell::Cell;
use std::ffi::{c_int, c_ulong, c_void, CStr};
use std::io::{self, Write};
use std::ptr;
use std::sync::atomic::Ordering;

mod audio;
mod portaudio;
mod protocol;
mod timing;
use audio::{audio_callback, CallbackState};
#[cfg(test)]
use portaudio::StreamInfo;
use portaudio::{CallbackTimeInfo, PortAudio, StreamParameters};
use protocol::{input_channel, Clip, Command, Input, Latency};
#[cfg(test)]
use protocol::{pcm_samples, read_input};
use std::sync::mpsc::RecvTimeoutError;
use std::time::{Duration, Instant};

const MAX_PCM_BYTES: usize = 128 * 1024 * 1024;
const MAX_HEADER_BYTES: u64 = 4096;
const PA_COMPLETE: c_int = 1;
const PA_ABORT: c_int = 2;
const PA_OUTPUT_UNDERFLOW: c_ulong = 4;
const PA_STREAM_IS_STOPPED: c_int = -9983;

struct AudiblePosition {
    position: Cell<u64>,
    seek_generation: Cell<u64>,
}

impl AudiblePosition {
    fn new(position: u64) -> Self {
        Self {
            position: Cell::new(position),
            seek_generation: Cell::new(0),
        }
    }

    fn seek(&self, generation: u64, position: u64) {
        if generation != self.seek_generation.get() {
            self.position.set(position);
            self.seek_generation.set(generation);
        }
    }

    fn observe(&self, submitted: u64, estimate: u64) -> u64 {
        // A new DAC buffer may be in the future after pause or producer silence.
        let audible = self.position.get().max(estimate).min(submitted);
        self.position.set(audible);
        audible
    }
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
    running: bool,
    seek_reported: u64,
    audible: AudiblePosition,
}

impl<'a> Active<'a> {
    fn open(pa: &'a PortAudio, clip: Clip, capacity: Option<u64>) -> Result<Self, String> {
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
            state: Box::new(match capacity {
                Some(capacity) => CallbackState::stream(capacity, clip.channels),
                None => CallbackState::new(clip.pcm, clip.channels),
            }),
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
            running: false,
            seek_reported: 0,
            audible: AudiblePosition::new(clip.position),
        };
        active
            .state
            .position
            .store(clip.position, Ordering::Relaxed);
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
        active.state.timing.sample_rate = stream_info.sample_rate;
        Ok(active)
    }

    fn start(&mut self) -> Result<(), String> {
        if self.running {
            return Ok(());
        }
        self.started = Instant::now();
        let result = unsafe { (self.pa.start)(self.stream) };
        if result != 0 {
            return Err(self.pa.error(result));
        }
        self.running = true;
        Ok(())
    }

    fn metadata(&self, event: &str) -> Value {
        json!({"event":event, "id":self.id, "device_name":self.device_name, "host_api":self.host_api,
            "sample_rate":self.sample_rate, "channels":self.channels,
            "output_latency_seconds":self.output_latency_seconds, "actual_sample_rate":self.actual_sample_rate})
    }

    fn status(&self, finished: bool, error: Option<String>) -> Value {
        let position = self.state.position.load(Ordering::Acquire);
        let mut audible = self.state.timeline_position.load(Ordering::Relaxed);
        let written = self.state.timeline_written.load(Ordering::Acquire);
        if written > 0 && !self.stream.is_null() {
            let now = unsafe { (self.pa.stream_time)(self.stream) };
            let dac = f64::from_bits(self.state.timeline_dac.load(Ordering::Relaxed));
            if now.is_finite() && dac.is_finite() {
                if now >= dac {
                    audible += ((now - dac) * self.actual_sample_rate).min(written as f64) as u64;
                } else {
                    audible =
                        audible.saturating_sub(((dac - now) * self.actual_sample_rate) as u64);
                }
            }
        }
        let paused = self.state.paused.load(Ordering::Acquire);
        json!({"event":"status", "id":self.id, "position_frames":position,
            "audible_position_frames":self.audible.observe(position, audible), "frames_played":position,
            "total_frames":self.state.total.load(Ordering::Acquire),
            "started":self.state.first_written.load(Ordering::Acquire) != u64::MAX,
            "playing":self.running && !finished && !paused, "paused":paused && !finished, "finished":finished,
            "underflow_count":self.state.underflows.load(Ordering::Relaxed),
            "producer_starvation_count":self.state.starvations.load(Ordering::Relaxed),
            "sample_rate":self.sample_rate, "channels":self.channels,
            "actual_sample_rate":self.actual_sample_rate, "output_latency_seconds":self.output_latency_seconds,
            "error":error})
    }

    fn seek_event(&mut self) -> io::Result<()> {
        let applied = self.state.seek_applied.load(Ordering::Acquire);
        if applied != self.seek_reported {
            let position = self.state.seek_position.load(Ordering::Relaxed);
            self.audible.seek(applied, position);
            emit(json!({"event":"seeked", "id":self.id,
                "position_frames":position}))?;
            self.seek_reported = applied;
            self.first_emitted = false;
        }
        Ok(())
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

    fn finish(mut self, outcome: &str, error: Option<String>) -> io::Result<Value> {
        let was_cancelled = self.state.cancelled.load(Ordering::Acquire);
        let seek_generation = self.state.seek_applied.load(Ordering::Acquire);
        let audible = self.status(false, None)["audible_position_frames"].clone();
        let close_error = self.close().err();
        self.seek_event()?;
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
        if let Some(ref error) = error {
            event["error"] = json!(error);
        }
        self.state.timing.add_to(&mut event);
        event["producer_starvation_count"] = json!(self.state.starvations.load(Ordering::Relaxed));
        let mut status = self.status(true, error.clone());
        status["outcome"] = json!(outcome);
        if seek_generation == self.state.seek_applied.load(Ordering::Acquire) {
            status["audible_position_frames"] = json!(audible
                .as_u64()
                .unwrap_or(0)
                .min(self.state.position.load(Ordering::Acquire)));
        }
        if outcome == "complete" {
            status["audible_position_frames"] = status["position_frames"].clone();
        }
        for field in [
            "total_frames",
            "position_frames",
            "audible_position_frames",
            "started",
            "playing",
            "paused",
            "finished",
            "error",
        ] {
            event[field] = status[field].clone();
        }
        emit(event)?;
        Ok(status)
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

fn unstarted_status(clip: &Clip, finished: bool, error: Option<String>) -> Value {
    let mut status = json!({"event":"status", "id":clip.id, "position_frames":clip.position,
        "audible_position_frames":clip.position, "frames_played":clip.position,
        "total_frames":clip.pcm.len() / usize::from(clip.channels), "started":false,
        "playing":false, "paused":false, "finished":finished, "underflow_count":0,
        "producer_starvation_count":0, "sample_rate":clip.sample_rate, "channels":clip.channels,
        "actual_sample_rate":clip.sample_rate, "output_latency_seconds":0.0, "error":error});
    if finished {
        status["outcome"] = json!(if error.is_some() {
            "failed"
        } else {
            "interrupted"
        });
    }
    status
}

fn finish_unstarted(clip: &Clip, error: Option<String>) -> io::Result<Value> {
    finish_status(unstarted_status(clip, true, error))
}

fn finish_status(status: Value) -> io::Result<Value> {
    let mut event = status.clone();
    event["event"] = json!("finished");
    event["callback_count"] = json!(0);
    event["elapsed_ms"] = json!(0.0);
    timing::CallbackTiming::default().add_to(&mut event);
    emit(event)?;
    Ok(status)
}

fn reject(id: u64, error: &str) -> io::Result<()> {
    emit(json!({"event":"error", "id":id, "fatal":false, "error":error}))
}

fn terminal_ack(terminal: &Option<Value>, id: u64, event: &str) -> Option<Value> {
    terminal
        .as_ref()
        .filter(|status| status["id"] == id)
        .map(|status| {
            let mut reply = status.clone();
            reply["event"] = json!(event);
            reply
        })
}

struct PendingAppend {
    id: u64,
    frames: u64,
    pcm: Vec<f32>,
    offset: usize,
}

fn admit_pending(
    active: &mut Active<'_>,
    pending: &mut Option<PendingAppend>,
    end: bool,
) -> io::Result<()> {
    if let Some(append) = pending.as_mut() {
        append.offset += active.state.append(&append.pcm[append.offset..]);
        if !active.running && append.offset > 0 {
            active.start().map_err(io::Error::other)?;
            emit(active.metadata("started"))?;
        }
        if append.offset == append.pcm.len() {
            emit(
                json!({"event":"appended", "id":append.id, "frames":append.frames,
                "underflow_count":active.state.underflows.load(Ordering::Relaxed),
                "producer_starvation_count":active.state.starvations.load(Ordering::Relaxed)}),
            )?;
            *pending = None;
        }
    }
    if end && pending.is_none() {
        active.state.eos.store(true, Ordering::Release);
    }
    Ok(())
}

fn run(pa: &PortAudio) -> io::Result<()> {
    emit(
        json!({"event":"ready", "protocol":2, "pid":std::process::id(), "max_pcm_bytes":MAX_PCM_BYTES}),
    )?;
    let input = input_channel();
    let mut loaded: Option<Clip> = None;
    let mut active: Option<Active<'_>> = None;
    let mut terminal: Option<Value> = None;
    let mut pending: Option<PendingAppend> = None;
    let mut end = false;
    let mut last_id = 0;
    loop {
        if let Some(playing) = active.as_mut() {
            if playing.state.streaming() {
                if let Err(error) = admit_pending(playing, &mut pending, end) {
                    terminal = Some(
                        active
                            .take()
                            .unwrap()
                            .finish("failed", Some(error.to_string()))?,
                    );
                    pending = None;
                    continue;
                }
            }
        }
        if let Some(playing) = active.as_mut() {
            playing.seek_event()?;
            playing.first_callback()?;
            playing.pause_event()?;
            let status = if playing.running {
                unsafe { (pa.is_active)(playing.stream) }
            } else {
                1
            };
            if status <= 0 {
                let error = if status < 0 {
                    Some(pa.error(status))
                } else if playing.state.failed.load(Ordering::Acquire) {
                    Some("native callback rejected output buffer".into())
                } else if playing.state.streaming() && !playing.state.eos.load(Ordering::Acquire) {
                    Some("native stream stopped before end-of-stream".into())
                } else if playing.state.position.load(Ordering::Acquire)
                    != playing.state.total.load(Ordering::Acquire)
                {
                    Some("native stream stopped before all PCM was submitted".into())
                } else {
                    None
                };
                terminal = Some(active.take().unwrap().finish("complete", error)?);
                pending = None;
            }
        }
        let message = match input.recv_timeout(Duration::from_millis(2)) {
            Ok(message) => message,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => Input::End,
        };
        match message {
            Input::Load(clip) => {
                if active.is_some() || loaded.is_some() || clip.id <= last_id {
                    reject(
                        clip.id,
                        "load requires idle worker and a strictly increasing id",
                    )?;
                } else {
                    last_id = clip.id;
                    terminal = None;
                    end = false;
                    emit(json!({"event":"loaded", "id":clip.id}))?;
                    loaded = Some(clip);
                }
            }
            Input::Control(Command::OpenStream {
                id,
                sample_rate,
                channels,
                latency,
                capacity_frames,
            }) => {
                if active.is_some() || loaded.is_some() || id <= last_id {
                    reject(
                        id,
                        "open_stream requires idle worker and a strictly increasing id",
                    )?;
                } else {
                    last_id = id;
                    terminal = None;
                    end = false;
                    let clip = Clip {
                        id,
                        sample_rate,
                        channels,
                        latency,
                        pcm: Vec::new(),
                        position: 0,
                    };
                    match Active::open(pa, clip, Some(capacity_frames)) {
                        Ok(playing) => {
                            emit(playing.metadata("opened"))?;
                            active = Some(playing);
                        }
                        Err(error) => {
                            let clip = Clip {
                                id,
                                sample_rate,
                                channels,
                                latency: Latency::Named("low".into()),
                                pcm: Vec::new(),
                                position: 0,
                            };
                            terminal = Some(finish_unstarted(&clip, Some(error))?);
                        }
                    }
                }
            }
            Input::Append { id, frames, pcm } => {
                if !active
                    .as_ref()
                    .is_some_and(|a| a.id == id && a.state.streaming())
                    || end
                {
                    reject(id, "append requires an open current stream before end")?;
                } else if pending.is_some() {
                    reject(id, "only one append may await admission")?;
                } else {
                    pending = Some(PendingAppend {
                        id,
                        frames,
                        pcm,
                        offset: 0,
                    });
                }
            }
            Input::Control(Command::End { id }) => {
                if let Some(playing) = active
                    .as_ref()
                    .filter(|a| a.id == id && a.state.streaming())
                {
                    end = true;
                    emit(json!({"event":"ended", "id":id}))?;
                    if !playing.running && pending.is_none() {
                        terminal = Some(active.take().unwrap().finish("complete", None)?);
                    }
                } else {
                    reject(id, "end requires the current open stream")?;
                }
            }
            Input::Control(Command::Start { id }) => {
                if loaded.as_ref().is_some_and(|clip| clip.id == id) {
                    let clip = loaded.take().unwrap();
                    let mut failed_status = unstarted_status(&clip, true, None);
                    match Active::open(pa, clip, None).and_then(|mut playing| {
                        playing.start()?;
                        Ok(playing)
                    }) {
                        Ok(playing) => {
                            emit(playing.metadata("started"))?;
                            active = Some(playing);
                        }
                        Err(error) => {
                            failed_status["error"] = json!(error);
                            failed_status["outcome"] = json!("failed");
                            terminal = Some(finish_status(failed_status)?);
                        }
                    }
                } else {
                    reject(id, "start requires the current loaded clip")?;
                }
            }
            Input::Control(Command::Stop { id } | Command::Close { id }) => {
                if active.as_ref().is_some_and(|a| a.id == id) {
                    terminal = Some(active.take().unwrap().finish("interrupted", None)?);
                    pending = None;
                } else if loaded.as_ref().is_some_and(|clip| clip.id == id) {
                    terminal = Some(finish_unstarted(&loaded.take().unwrap(), None)?);
                } else if !terminal.as_ref().is_some_and(|status| status["id"] == id) {
                    reject(id, "stop requires the current clip")?;
                }
            }
            Input::Control(Command::Pause { id }) => {
                if let Some(playing) = active.as_ref().filter(|a| a.id == id) {
                    playing.state.paused.store(true, Ordering::Release);
                    if !playing.running {
                        emit(json!({"event":"paused", "id":id}))?;
                    }
                } else if let Some(reply) = terminal_ack(&terminal, id, "paused") {
                    emit(reply)?;
                } else {
                    reject(id, "pause requires the current clip")?;
                }
            }
            Input::Control(Command::Resume { id }) => {
                if let Some(playing) = active.as_ref().filter(|a| a.id == id) {
                    playing.state.paused.store(false, Ordering::Release);
                } else if let Some(reply) = terminal_ack(&terminal, id, "resumed") {
                    emit(reply)?;
                } else {
                    reject(id, "resume requires the current clip")?;
                }
            }
            Input::Control(Command::Seek {
                id,
                position_frames,
            }) => {
                if let Some(clip) = loaded.as_mut().filter(|clip| clip.id == id) {
                    clip.position =
                        position_frames.min((clip.pcm.len() / usize::from(clip.channels)) as u64);
                    emit(json!({"event":"seeked", "id":id, "position_frames":clip.position}))?;
                } else if let Some(playing) = active
                    .as_ref()
                    .filter(|a| a.id == id && !a.state.streaming())
                {
                    if playing.state.seek_request.load(Ordering::Acquire)
                        != playing.state.seek_applied.load(Ordering::Acquire)
                        || playing.seek_reported
                            != playing.state.seek_applied.load(Ordering::Acquire)
                    {
                        reject(id, "previous seek is awaiting callback")?;
                    } else {
                        playing.state.seek_position.store(
                            position_frames.min(playing.state.total.load(Ordering::Relaxed)),
                            Ordering::Relaxed,
                        );
                        playing.state.seek_request.fetch_add(1, Ordering::Release);
                    }
                } else if let Some(reply) = terminal_ack(&terminal, id, "seeked") {
                    emit(reply)?;
                } else {
                    reject(id, "seek requires the current prepared clip")?;
                }
            }
            Input::Control(Command::Status { id }) => {
                if let Some(playing) = active.as_ref().filter(|a| a.id == id) {
                    emit(playing.status(false, None))?;
                } else if let Some(clip) = loaded.as_ref().filter(|clip| clip.id == id) {
                    emit(unstarted_status(clip, false, None))?;
                } else if let Some(status) = terminal.as_ref().filter(|v| v["id"] == id) {
                    emit(status.clone())?;
                } else {
                    reject(id, "status id is stale or unknown")?;
                }
            }
            Input::Control(Command::QueryDevice) => match pa.device_event() {
                Ok(event) => emit(event)?,
                Err(error) => emit(json!({"event":"error", "fatal":false, "error":error}))?,
            },
            Input::Control(Command::CheckFormat {
                sample_rate,
                channels,
            }) => match pa.check_format(sample_rate, channels) {
                Ok(event) => emit(event)?,
                Err(error) => emit(json!({"event":"error", "fatal":false, "error":error}))?,
            },
            Input::End | Input::Fatal(_) | Input::Control(Command::Shutdown) => {
                let error = if let Input::Fatal(error) = message {
                    Some(error)
                } else {
                    None
                };
                if let Some(playing) = active.take() {
                    playing.finish("interrupted", error.clone())?;
                }
                if let Some(clip) = loaded.take() {
                    finish_unstarted(&clip, error.clone())?;
                }
                if let Some(error) = error {
                    emit(json!({"event":"error", "fatal":true, "error":error}))?;
                    return Err(io::Error::new(io::ErrorKind::InvalidData, error));
                }
                return Ok(());
            }
            Input::Control(Command::Load { .. } | Command::Append { .. }) => unreachable!(),
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
    fn audible_position_never_regresses_on_resume_or_starvation_but_resets_on_seek() {
        let audible = AudiblePosition::new(0);
        assert_eq!(audible.observe(100, 100), 100);
        assert_eq!(audible.observe(120, 95), 100);
        assert_eq!(audible.observe(120, 110), 110);
        assert_eq!(audible.observe(140, 105), 110);
        audible.seek(1, 40);
        assert_eq!(audible.observe(40, 0), 40);
        assert_eq!(audible.observe(60, 55), 55);
        audible.seek(1, 10);
        assert_eq!(audible.observe(60, 50), 55);
        audible.seek(2, 100);
        assert_eq!(audible.observe(100, 90), 100);
        assert_eq!(audible.observe(110, 999), 110);
        assert_eq!(AudiblePosition::new(20).observe(20, 0), 20);
    }

    #[test]
    fn terminal_controls_acknowledge_completion_without_changing_retained_state() {
        let terminal = Some(json!({"event":"status", "id":4, "finished":true,
            "started":true, "playing":false, "paused":false, "position_frames":40}));
        for event in ["paused", "resumed", "seeked"] {
            let ack = terminal_ack(&terminal, 4, event).unwrap();
            assert_eq!(ack["event"], event);
            assert_eq!(ack["finished"], true);
            assert_eq!(ack["position_frames"], 40);
            assert_eq!(terminal.as_ref().unwrap()["event"], "status");
        }
        assert!(terminal_ack(&terminal, 3, "paused").is_none());
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
        assert!(read_input(&mut Cursor::new(load_header(u64::MAX)), &mut None).is_err());
        assert!(Latency::Seconds(f64::NAN).validate().is_err());
    }

    #[test]
    fn reads_exact_little_endian_payload_then_next_header() {
        let mut bytes = load_header(2);
        bytes.extend_from_slice(&0.25_f32.to_le_bytes());
        bytes.extend_from_slice(&(-0.5_f32).to_le_bytes());
        bytes.extend_from_slice(b"{\"op\":\"stop\",\"id\":1}\n");
        let mut reader = Cursor::new(bytes);
        let Some(Input::Load(clip)) = read_input(&mut reader, &mut None).unwrap() else {
            panic!("missing load");
        };
        assert_eq!(clip.pcm, [0.25, -0.5]);
        assert!(matches!(
            read_input(&mut reader, &mut None).unwrap(),
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
            assert!(read_input(&mut Cursor::new(bytes), &mut None).is_err());
        }
        let mut bytes = load_header(1);
        bytes.extend_from_slice(&f32::INFINITY.to_le_bytes());
        assert!(read_input(&mut Cursor::new(bytes), &mut None).is_err());
    }

    #[test]
    fn streaming_framing_preserves_control_after_payload_and_rejects_late_format() {
        let mut bytes = b"{\"op\":\"open_stream\",\"id\":7,\"sample_rate\":24000,\"channels\":2,\"latency\":\"low\",\"capacity_frames\":3}\n{\"op\":\"append\",\"id\":7,\"frames\":2}\n".to_vec();
        for sample in [1.0_f32, 2.0, 3.0, 4.0] {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        bytes.extend_from_slice(
            b"{\"op\":\"stop\",\"id\":7}\n{\"op\":\"append\",\"id\":6,\"frames\":1}\n",
        );
        let mut input = Cursor::new(bytes);
        let mut format = None;
        assert!(matches!(
            read_input(&mut input, &mut format).unwrap(),
            Some(Input::Control(Command::OpenStream { id: 7, .. }))
        ));
        let Some(Input::Append { id, frames, pcm }) = read_input(&mut input, &mut format).unwrap()
        else {
            panic!("missing append");
        };
        assert_eq!((id, frames), (7, 2));
        assert_eq!(pcm, [1.0, 2.0, 3.0, 4.0]);
        assert!(matches!(
            read_input(&mut input, &mut format).unwrap(),
            Some(Input::Control(Command::Stop { id: 7 }))
        ));
        assert!(read_input(&mut input, &mut format).is_err());
    }

    #[test]
    fn streaming_rejects_unbounded_capacity_and_append_before_reading_pcm() {
        let mut format = None;
        let bytes = b"{\"op\":\"open_stream\",\"id\":1,\"sample_rate\":24000,\"channels\":1,\"latency\":\"low\",\"capacity_frames\":48001}\n";
        assert!(read_input(&mut Cursor::new(bytes), &mut format).is_err());
        let bytes = b"{\"op\":\"append\",\"id\":1,\"frames\":5}\n";
        assert!(read_input(&mut Cursor::new(bytes), &mut Some((1, 24000, 1, 4))).is_err());
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
