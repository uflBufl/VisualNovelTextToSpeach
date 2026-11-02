use crate::{
    audio_buffer::{self, Packet, Producer, SourceClock, CHANNELS, SAMPLE_RATE},
    caf::{write_packet, Caf},
    recording::{Options, Session},
};
use serde_json::{json, Value};
use std::{
    fs::OpenOptions,
    io::{BufWriter, Write},
    mem::{size_of, ManuallyDrop},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    core::{implement, Interface, Ref, HRESULT},
    Win32::{
        Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        Media::Audio::*,
        System::{
            Com::{
                CoInitializeEx, CoUninitialize,
                StructuredStorage::{
                    PROPVARIANT, PROPVARIANT_0, PROPVARIANT_0_0, PROPVARIANT_0_0_0,
                },
                BLOB, COINIT_MULTITHREADED,
            },
            Threading::{CreateEventW, WaitForSingleObject},
            Variant::VT_BLOB,
        },
    },
};

fn native(error: windows::core::Error) -> String {
    format!(
        "{} (HRESULT 0x{:08X})",
        error.message(),
        error.code().0 as u32
    )
}

#[implement(IActivateAudioInterfaceCompletionHandler)]
struct Completion {
    ready: mpsc::SyncSender<()>,
    // Keep activation storage alive even when a stopped/timed-out caller returns.
    _parameters: Box<AUDIOCLIENT_ACTIVATION_PARAMS>,
}
impl IActivateAudioInterfaceCompletionHandler_Impl for Completion_Impl {
    fn ActivateCompleted(
        &self,
        _: Ref<IActivateAudioInterfaceAsyncOperation>,
    ) -> windows::core::Result<()> {
        let _ = self.ready.try_send(());
        Ok(())
    }
}

struct Com;
impl Drop for Com {
    fn drop(&mut self) {
        unsafe {
            CoUninitialize();
        }
    }
}
struct Event(HANDLE);
impl Drop for Event {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

fn activate(pid: u32, stop: &AtomicBool) -> Result<IAudioClient, String> {
    let parameters = Box::new(AUDIOCLIENT_ACTIVATION_PARAMS {
        ActivationType: AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        Anonymous: AUDIOCLIENT_ACTIVATION_PARAMS_0 {
            ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
                TargetProcessId: pid,
                ProcessLoopbackMode: PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
            },
        },
    });
    let blob = BLOB {
        cbSize: size_of::<AUDIOCLIENT_ACTIVATION_PARAMS>() as u32,
        pBlobData: (&*parameters as *const AUDIOCLIENT_ACTIVATION_PARAMS)
            .cast_mut()
            .cast(),
    };
    let variant = PROPVARIANT {
        Anonymous: PROPVARIANT_0 {
            Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                vt: VT_BLOB,
                Anonymous: PROPVARIANT_0_0_0 { blob },
                ..Default::default()
            }),
        },
    };
    let (send, receive) = mpsc::sync_channel(1);
    // windows::core::implement supplies IAgileObject and free-threaded marshalling.
    let completion: IActivateAudioInterfaceCompletionHandler = Completion {
        ready: send,
        _parameters: parameters,
    }
    .into();
    let operation = unsafe {
        ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            &IAudioClient::IID,
            Some(&variant),
            &completion,
        )
    }
    .map_err(|error| {
        format!(
            "Process loopback unavailable (requires Windows 10 build 19041+): {}",
            native(error)
        )
    })?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match receive.recv_timeout(Duration::from_millis(20)) {
            Ok(()) => break,
            Err(mpsc::RecvTimeoutError::Timeout)
                if !stop.load(Ordering::Acquire) && Instant::now() < deadline =>
            {
                ()
            }
            _ => return Err("Process loopback activation stopped or exceeded 10 seconds".into()),
        }
    }
    unsafe {
        let mut result = HRESULT(0);
        let mut interface = None;
        operation
            .GetActivateResult(&mut result, &mut interface)
            .map_err(native)?;
        result
            .ok()
            .map_err(|error| format!("Process loopback activation failed: {}", native(error)))?;
        interface
            .ok_or("Process loopback activation returned no interface")?
            .cast()
            .map_err(native)
    }
}

fn acquire(
    pid: u32,
    mut producer: Producer,
    stop: &AtomicBool,
    ready: &mpsc::SyncSender<Result<f64, String>>,
    stopped_at: &AtomicU64,
) -> Result<(), String> {
    unsafe {
        CoInitializeEx(None, COINIT_MULTITHREADED)
            .ok()
            .map_err(native)?;
    }
    let _com = Com;
    let event = Event(unsafe { CreateEventW(None, false, false, None).map_err(native)? });
    let client = activate(pid, stop)?;
    let format = WAVEFORMATEX {
        wFormatTag: 3, // WAVE_FORMAT_IEEE_FLOAT
        nChannels: CHANNELS as u16,
        nSamplesPerSec: SAMPLE_RATE,
        nAvgBytesPerSec: SAMPLE_RATE * CHANNELS as u32 * 4,
        nBlockAlign: CHANNELS as u16 * 4,
        wBitsPerSample: 32,
        cbSize: 0,
    };
    let capture: IAudioCaptureClient = unsafe {
        client
            .Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK
                    | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                    | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                0,
                0,
                &format,
                None,
            )
            .map_err(native)?;
        client.SetEventHandle(event.0).map_err(native)?;
        client.GetService().map_err(native)?
    };
    let mut source_clock = SourceClock::default();
    unsafe {
        client.Start().map_err(native)?;
    }
    let result = (|| {
        let _ = ready.try_send(Ok(crate::recording::clock()?));
        while !stop.load(Ordering::Acquire) {
            match unsafe { WaitForSingleObject(event.0, 20) } {
                WAIT_OBJECT_0 => packets(&capture, &mut producer, &mut source_clock)?,
                WAIT_TIMEOUT => (),
                _ => return Err("WASAPI packet event wait failed".into()),
            }
        }
        Ok(())
    })();
    let stopped = unsafe { client.Stop().map_err(native) };
    let stop_clock = crate::recording::clock().map(|value| {
        stopped_at.store(value.to_bits(), Ordering::Release);
    });
    // Stop source production before draining its final packets into the bounded ring.
    let drained = if result.is_ok() && stopped.is_ok() {
        packets(&capture, &mut producer, &mut source_clock)
    } else {
        Ok(())
    };
    result.and(stopped).and(stop_clock).and(drained)
}

fn packets(
    capture: &IAudioCaptureClient,
    producer: &mut Producer,
    clock: &mut SourceClock,
) -> Result<(), String> {
    while unsafe { capture.GetNextPacketSize().map_err(native)? } > 0 {
        let (mut data, mut frames, mut flags, mut position, mut qpc) =
            (std::ptr::null_mut(), 0, 0, 0, 0);
        unsafe {
            capture
                .GetBuffer(
                    &mut data,
                    &mut frames,
                    &mut flags,
                    Some(&mut position),
                    Some(&mut qpc),
                )
                .map_err(native)?;
        }
        // GetBuffer can report AUDCLNT_S_BUFFER_EMPTY without a packet to release.
        if frames == 0 {
            return Ok(());
        }
        let copied = (|| {
            if frames > SAMPLE_RATE * 5 {
                return Err("Process audio packet exceeds the five-second buffer".into());
            }
            let packet = Packet {
                frames,
                device_position: position,
                qpc_100ns: qpc,
                flags,
            };
            clock.validate(packet, SAMPLE_RATE).map_err(str::to_owned)?;
            let silent = flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0;
            if !silent && data.is_null() {
                return Err("WASAPI returned null process PCM".into());
            }
            let samples = if silent {
                None
            } else {
                Some(unsafe {
                    std::slice::from_raw_parts(data.cast::<f32>(), frames as usize * CHANNELS)
                })
            };
            producer.push(packet, samples).map_err(str::to_owned)
        })();
        // Every acquired packet is released even when validation or publication fails.
        let released = unsafe { capture.ReleaseBuffer(frames).map_err(native) };
        copied.and(released)?;
    }
    Ok(())
}

pub fn record(options: &Options, session: &mut Session) -> Result<Value, String> {
    for (key, value) in json!({
        "mechanism":"WASAPI ActivateAudioInterfaceAsync process loopback",
        "scope":"selected_process_tree", "microphone":false, "global_fallback":false,
        "sample_rate":SAMPLE_RATE, "channels":CHANNELS, "sample_format":"float32_le",
        "audio_buffer_capacity_frames":SAMPLE_RATE * 5,
        "frames":0,"peak":0.0
    })
    .as_object()
    .unwrap()
    {
        session.metadata[key] = value.clone();
    }
    session.save()?;
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(options.output_dir.join("process.caf"))
        .map_err(|e| e.to_string())?;
    let mut caf = Caf::new(BufWriter::with_capacity(64 * 1024, file), SAMPLE_RATE)
        .map_err(|e| e.to_string())?;
    let log = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(options.output_dir.join("audio.jsonl"))
        .map_err(|e| e.to_string())?;
    let mut journal = BufWriter::new(log);
    let (producer, mut consumer) = audio_buffer::ring(SAMPLE_RATE as usize * 5);
    let stop = Arc::new(AtomicBool::new(false));
    let thread_stop = stop.clone();
    let stopped_at = Arc::new(AtomicU64::new(0));
    let thread_stopped_at = stopped_at.clone();
    let (ready_send, ready_receive) = mpsc::sync_channel(1);
    let pid = options.pid;
    let worker = thread::Builder::new()
        .name("process-audio-acquisition".into())
        .spawn(move || {
            let result = acquire(pid, producer, &thread_stop, &ready_send, &thread_stopped_at);
            if let Err(error) = &result {
                let _ = ready_send.try_send(Err(error.clone()));
            }
            result
        })
        .map_err(|e| e.to_string())?;
    let (mut frames, mut peak, mut callbacks) = (0_u64, 0_f32, 0_u64);
    let mut first_packet_flags = None;
    let mut failure = None;
    let mut writer_failed = false;
    let mut stopping = None;
    let mut reason = "requested_stop";
    let mut started = false;
    loop {
        if stopping.is_none() {
            match session.poll() {
                Ok(Some(value)) => {
                    reason = value;
                    stopping = Some(Instant::now());
                }
                Err(error) => {
                    failure = Some(error);
                    stopping = Some(Instant::now());
                }
                Ok(None) => (),
            }
            if stopping.is_some() {
                stop.store(true, Ordering::Release);
            }
        }
        if !started && stopping.is_none() {
            match ready_receive.try_recv() {
                Ok(Ok(source_start)) => {
                    session.metadata["capture_started_monotonic_s"] = json!(source_start);
                    if let Err(error) = session.save() {
                        failure = Some(error);
                        stopping = Some(Instant::now());
                        stop.store(true, Ordering::Release);
                    }
                    started = true;
                }
                Ok(Err(error)) => {
                    failure = Some(error);
                    stopping = Some(Instant::now());
                    stop.store(true, Ordering::Release);
                }
                Err(_) => (),
            }
        }
        if !writer_failed {
            if let Some(result) = write_packet(
                &mut consumer,
                &mut caf,
                &mut journal,
                &mut frames,
                &mut peak,
                &mut callbacks,
                &mut first_packet_flags,
            ) {
                if let Err(error) = result {
                    writer_failed = true;
                    failure = Some(error);
                    stopping = Some(Instant::now());
                    stop.store(true, Ordering::Release);
                }
                continue;
            }
        }
        if worker.is_finished() {
            break;
        }
        if stopping.is_some_and(|at| at.elapsed() > Duration::from_secs(2)) {
            failure
                .get_or_insert("Process audio acquisition did not stop within two seconds".into());
            break;
        }
        thread::sleep(Duration::from_millis(2));
    }
    stop.store(true, Ordering::Release);
    if worker.is_finished() {
        match worker.join() {
            Ok(Ok(())) => (),
            Ok(Err(error)) => {
                failure.get_or_insert(error);
            }
            Err(_) => {
                failure.get_or_insert("Process audio acquisition panicked".into());
            }
        }
    }
    if !writer_failed {
        while let Some(result) = write_packet(
            &mut consumer,
            &mut caf,
            &mut journal,
            &mut frames,
            &mut peak,
            &mut callbacks,
            &mut first_packet_flags,
        ) {
            if let Err(error) = result {
                failure = Some(error);
                break;
            }
        }
    }
    let summary = json!({"frames":frames,"peak":peak,"callbacks":callbacks,"stop_reason":reason,
        "first_packet_flags":first_packet_flags});
    for (key, value) in summary.as_object().unwrap() {
        session.metadata[key] = value.clone();
    }
    let source_stop = stopped_at.load(Ordering::Acquire);
    if source_stop != 0 {
        session.metadata["capture_stopped_monotonic_s"] = json!(f64::from_bits(source_stop));
    }
    if let Err(error) = caf.finalize().and_then(|_| journal.flush()) {
        failure.get_or_insert(error.to_string());
    }
    if frames == 0 {
        failure.get_or_insert("No process audio frames were captured".into());
    } else if peak == 0.0 {
        failure.get_or_insert("No nonzero process audio PCM was captured".into());
    }
    if let Some(error) = failure {
        Err(error)
    } else {
        Ok(summary)
    }
}
