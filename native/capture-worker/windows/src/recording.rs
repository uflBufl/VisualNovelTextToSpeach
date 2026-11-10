use std::{ffi::OsString, path::PathBuf};

#[derive(Debug)]
pub struct Options {
    pub pid: u32,
    pub executable: Option<PathBuf>,
    pub window_id: Option<i64>,
    pub seconds: f64,
    pub output_dir: PathBuf,
}

impl Options {
    fn parse(args: &[OsString]) -> Result<Self, String> {
        let video = args.first().is_some_and(|v| v == "--video-only");
        if !((video && args.len() == 5)
            || (args.first().is_some_and(|v| v == "--capture")
                && args.len() == 6
                && args[5] == "--allow-audio-capture"))
        {
            return Err("Usage: --video-only PID HWND SECONDS NEW_ABSOLUTE_DIR | --capture PID ABSOLUTE_EXE SECONDS NEW_ABSOLUTE_DIR --allow-audio-capture | --preflight | --selfcheck".into());
        }
        let parse = |index: usize| args[index].to_str().ok_or("Numeric argument is not UTF-8");
        let pid = parse(1)?.parse::<u32>().map_err(|_| "Invalid PID")?;
        let seconds = parse(3)?.parse::<f64>().map_err(|_| "Invalid duration")?;
        if pid == 0 || !seconds.is_finite() || !(1.0..=3600.0).contains(&seconds) {
            return Err("Positive PID and duration in 1..3600 seconds required".into());
        }
        let (window_id, executable) = if video {
            let window = parse(2)?.parse::<i64>().map_err(|_| "Invalid HWND")?;
            if window <= 0 || (window as u64) > usize::MAX as u64 {
                return Err("Invalid HWND".into());
            }
            (Some(window), None)
        } else {
            let path = PathBuf::from(&args[2]);
            if !path.is_absolute() {
                return Err("Executable path must be absolute".into());
            }
            (None, Some(path))
        };
        let output_dir = PathBuf::from(&args[4]);
        if !output_dir.is_absolute()
            || output_dir.exists()
            || !output_dir.parent().is_some_and(|p| p.is_dir())
        {
            return Err("Output must be new and absolute with an existing parent".into());
        }
        Ok(Self {
            pid,
            executable,
            window_id,
            seconds,
            output_dir,
        })
    }
}

pub fn run(args: Vec<OsString>) -> Result<(), String> {
    if args.len() == 1 && args[0] == "--selfcheck" {
        crate::audio_buffer::selfcheck()?;
        crate::caf::selfcheck()?;
        println!("selfcheck passed: bounded process audio buffer and CAF; no capture or playback");
        return Ok(());
    }
    #[cfg(windows)]
    {
        if args.len() == 1 && args[0] == "--preflight" {
            let build = native::platform_build()?;
            println!(
                "{}",
                serde_json::json!({"status":"available_unqualified","windows_build":build,"video_mechanism":"Windows Graphics Capture selected HWND","audio_mechanism":"WASAPI ActivateAudioInterfaceAsync process loopback","audio_scope":"selected_process_tree","microphone":false,"global_fallback":false,"runtime_activation_required":true})
            );
            return Ok(());
        }
        let options = Options::parse(&args)?;
        let mut session = Session::create(&options)?;
        let video = options.window_id.is_some();
        let result = if video {
            record_video(&options, &mut session)
        } else {
            crate::process_audio::record(&options, &mut session)
        };
        session.finish(
            result,
            if video {
                "succeeded"
            } else {
                "captured_unqualified"
            },
        )
    }
    #[cfg(not(windows))]
    {
        if args.len() != 1 || args[0] != "--preflight" {
            let _ = Options::parse(&args)?;
        }
        Err("Windows 10 build 19041 or later is required".into())
    }
}

#[cfg(windows)]
pub use native::{clock, Session};

fn skipped_frames(previous: u64, current: u64) -> Result<u64, &'static str> {
    current
        .checked_sub(previous)
        .filter(|delta| *delta > 0)
        .map(|delta| delta - 1)
        .ok_or("Video frame sequence did not advance")
}

#[cfg(windows)]
mod native {
    use super::Options;
    use serde_json::{json, Value};
    use std::{
        fs,
        io::{Read, Write},
        os::windows::ffi::OsStrExt,
        path::Path,
        sync::{
            atomic::{AtomicBool, AtomicU8, Ordering},
            Arc,
        },
        thread,
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };
    use windows::{
        core::PCWSTR,
        Win32::{
            Foundation::{CloseHandle, FILETIME, HANDLE},
            Storage::FileSystem::{
                GetDiskFreeSpaceExW, MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
            },
            System::{
                Diagnostics::ToolHelp::{
                    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
                    TH32CS_SNAPPROCESS,
                },
                Performance::{QueryPerformanceCounter, QueryPerformanceFrequency},
                Threading::{
                    GetCurrentProcessId, GetProcessTimes, OpenProcess, QueryFullProcessImageNameW,
                    WaitForSingleObject, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
                    PROCESS_SYNCHRONIZE,
                },
            },
        },
    };

    struct Process(HANDLE);
    // Windows process handles support concurrent, read-only waits and queries.
    unsafe impl Send for Process {}
    unsafe impl Sync for Process {}
    impl Drop for Process {
        fn drop(&mut self) {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
    impl Process {
        fn open(pid: u32) -> Result<Self, String> {
            unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                    false,
                    pid,
                )
                .map(Self)
                .map_err(|e| e.to_string())
            }
        }
        fn running(&self) -> bool {
            unsafe { WaitForSingleObject(self.0, 0) == windows::Win32::Foundation::WAIT_TIMEOUT }
        }
        fn identity(&self, pid: u32) -> Result<Value, String> {
            unsafe {
                let (mut created, mut exit, mut kernel, mut user) = (
                    FILETIME::default(),
                    FILETIME::default(),
                    FILETIME::default(),
                    FILETIME::default(),
                );
                GetProcessTimes(self.0, &mut created, &mut exit, &mut kernel, &mut user)
                    .map_err(|e| e.to_string())?;
                let mut name = vec![0u16; 32768];
                let mut count = name.len() as u32;
                QueryFullProcessImageNameW(
                    self.0,
                    PROCESS_NAME_WIN32,
                    windows::core::PWSTR(name.as_mut_ptr()),
                    &mut count,
                )
                .map_err(|e| e.to_string())?;
                let executable =
                    String::from_utf16(&name[..count as usize]).map_err(|e| e.to_string())?;
                Ok(
                    json!({"pid":pid,"executable":executable,"creation_time":((created.dwHighDateTime as u64)<<32)|created.dwLowDateTime as u64}),
                )
            }
        }
    }
    fn parent_pid() -> Result<u32, String> {
        unsafe {
            let snapshot = Process(
                CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).map_err(|e| e.to_string())?,
            );
            let mut entry = PROCESSENTRY32W::default();
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            Process32FirstW(snapshot.0, &mut entry).map_err(|e| e.to_string())?;
            loop {
                if entry.th32ProcessID == GetCurrentProcessId() {
                    return Ok(entry.th32ParentProcessID);
                }
                if Process32NextW(snapshot.0, &mut entry).is_err() {
                    break;
                }
            }
            Err("Cannot identify controlling parent process".into())
        }
    }
    fn wide(path: &Path) -> Vec<u16> {
        path.as_os_str().encode_wide().chain(Some(0)).collect()
    }
    pub fn platform_build() -> Result<u32, String> {
        use windows::Win32::System::SystemInformation::OSVERSIONINFOW;
        #[link(name = "ntdll")]
        extern "system" {
            fn RtlGetVersion(version: *mut OSVERSIONINFOW) -> i32;
        }
        let mut version = OSVERSIONINFOW::default();
        version.dwOSVersionInfoSize = std::mem::size_of::<OSVERSIONINFOW>() as u32;
        if unsafe { RtlGetVersion(&mut version) } < 0
            || version.dwMajorVersion < 10
            || version.dwBuildNumber < 19041
        {
            return Err("Windows 10 build 19041 or later is required".into());
        }
        Ok(version.dwBuildNumber)
    }
    fn disk_available(path: &Path) -> bool {
        unsafe {
            let mut bytes = 0;
            GetDiskFreeSpaceExW(PCWSTR(wide(path).as_ptr()), Some(&mut bytes), None, None).is_ok()
                && bytes >= 512 * 1024 * 1024
        }
    }
    pub fn clock() -> Result<f64, String> {
        unsafe {
            let (mut ticks, mut frequency) = (0, 0);
            QueryPerformanceCounter(&mut ticks).map_err(|e| e.to_string())?;
            QueryPerformanceFrequency(&mut frequency).map_err(|e| e.to_string())?;
            if frequency <= 0 {
                return Err("Invalid QPC frequency".into());
            }
            Ok(ticks as f64 / frequency as f64)
        }
    }
    pub struct Session {
        pub metadata: Value,
        output_dir: std::path::PathBuf,
        target: Arc<Process>,
        stop: Arc<AtomicBool>,
        safety: Arc<AtomicU8>,
        done: Arc<AtomicBool>,
        begin: Instant,
        seconds: f64,
    }
    impl Session {
        pub fn create(options: &Options) -> Result<Self, String> {
            platform_build()?;
            let target = Arc::new(Process::open(options.pid)?);
            let identity = target.identity(options.pid)?;
            if let Some(expected) = &options.executable {
                let actual = std::path::PathBuf::from(identity["executable"].as_str().unwrap());
                let expected =
                    fs::canonicalize(expected).map_err(|e| format!("Target executable: {e}"))?;
                let actual =
                    fs::canonicalize(actual).map_err(|e| format!("Target executable: {e}"))?;
                if !expected
                    .as_os_str()
                    .to_string_lossy()
                    .eq_ignore_ascii_case(&actual.as_os_str().to_string_lossy())
                {
                    return Err("Selected process executable does not match".into());
                }
            }
            let parent_pid = parent_pid()?;
            if parent_pid <= 1 {
                return Err("Controlling parent process is unavailable".into());
            }
            let parent = Arc::new(Process::open(parent_pid)?);
            if !target.running() || !parent.running() {
                return Err("Source or controlling parent already exited".into());
            }
            fs::create_dir(&options.output_dir).map_err(|e| e.to_string())?;
            let session = Self {
                metadata: json!({"status":"in_progress","target":identity,"game_pid":options.pid,"game_window_id":options.window_id,"requested_seconds":options.seconds,"controlling_parent_pid":parent_pid,"recorder_pid":std::process::id(),"recorder_executable":std::env::current_exe().map_err(|e|e.to_string())?,"microphone":false,"global_fallback":false,"started_monotonic_s":clock()?,"clock_anchor":{"monotonic_s":clock()?,"unix_s":SystemTime::now().duration_since(UNIX_EPOCH).map_err(|e|e.to_string())?.as_secs_f64()},"errors":[]}),
                output_dir: options.output_dir.clone(),
                target: target.clone(),
                stop: Arc::new(AtomicBool::new(false)),
                safety: Arc::new(AtomicU8::new(0)),
                done: Arc::new(AtomicBool::new(false)),
                begin: Instant::now(),
                seconds: options.seconds,
            };
            session.save()?;
            let stop = session.stop.clone();
            thread::spawn(move || {
                let mut input = std::io::stdin().lock();
                let mut byte = [0u8; 1];
                loop {
                    match input.read(&mut byte) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            if byte[0] == b'\n' {
                                break;
                            }
                        }
                    }
                }
                stop.store(true, Ordering::Release);
            });
            let (stop, safety, done, root) = (
                session.stop.clone(),
                session.safety.clone(),
                session.done.clone(),
                session.output_dir.clone(),
            );
            let seconds = options.seconds;
            thread::spawn(move || {
                let started = Instant::now();
                let mut stopping = None;
                while !done.load(Ordering::Acquire) {
                    let reason = if !parent.running() {
                        1
                    } else if !target.running() {
                        2
                    } else if !disk_available(&root) {
                        3
                    } else {
                        0
                    };
                    if reason != 0 {
                        let _ =
                            safety.compare_exchange(0, reason, Ordering::AcqRel, Ordering::Acquire);
                        stop.store(true, Ordering::Release);
                    }
                    if stop.load(Ordering::Acquire) || started.elapsed().as_secs_f64() >= seconds {
                        stopping.get_or_insert_with(Instant::now);
                    }
                    if started.elapsed().as_secs_f64() > seconds + 15.0
                        || stopping.is_some_and(|s: Instant| s.elapsed() > Duration::from_secs(10))
                    {
                        eprintln!("Native recorder watchdog expired; session remains incomplete");
                        std::process::exit(2);
                    }
                    thread::sleep(Duration::from_millis(250));
                }
            });
            Ok(session)
        }
        pub fn save(&self) -> Result<(), String> {
            let temporary = self.output_dir.join("session.json.tmp");
            let mut file = fs::File::create(&temporary).map_err(|e| e.to_string())?;
            file.write_all(&serde_json::to_vec_pretty(&self.metadata).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
            file.sync_all().map_err(|e| e.to_string())?;
            drop(file);
            for attempt in 0..20 {
                let result = unsafe {
                    MoveFileExW(
                        PCWSTR(wide(&temporary).as_ptr()),
                        PCWSTR(wide(&self.output_dir.join("session.json")).as_ptr()),
                        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                    )
                };
                match result {
                    Ok(()) => return Ok(()),
                    // Python's brief metadata reads can hold a Windows handle without delete sharing.
                    Err(error) if attempt < 19 && error.code().0 as u32 == 0x80070020 => {
                        thread::sleep(Duration::from_millis(10))
                    }
                    Err(error) => return Err(error.to_string()),
                }
            }
            unreachable!()
        }
        pub fn started(&mut self) -> Result<(), String> {
            self.metadata["capture_started_monotonic_s"] = json!(clock()?);
            self.save()
        }
        pub fn stopped(&mut self) -> Result<(), String> {
            self.metadata["capture_stopped_monotonic_s"] = json!(clock()?);
            self.save()
        }
        pub fn poll(&self) -> Result<Option<&'static str>, String> {
            let failure = match self.safety.load(Ordering::Acquire) {
                1 => Some("controlling_parent_exited"),
                2 => Some("selected_process_exited"),
                3 => Some("disk_space_unavailable_or_low"),
                _ => None,
            };
            if let Some(reason) = failure {
                return Err(reason.into());
            }
            if !self.target.running() {
                return Err("selected_process_exited".into());
            }
            Ok(if self.stop.load(Ordering::Acquire) {
                Some("requested_stop")
            } else if self.begin.elapsed().as_secs_f64() >= self.seconds {
                Some("duration")
            } else {
                None
            })
        }
        pub fn finish(
            &mut self,
            result: Result<Value, String>,
            success_status: &str,
        ) -> Result<(), String> {
            let reason = self.poll();
            let result = match (&reason, result) {
                (_, Err(e)) => Err(e),
                (Err(e), _) => Err(e.clone()),
                (_, Ok(summary)) => Ok(summary),
            };
            if self.metadata.get("capture_stopped_monotonic_s").is_none() {
                self.metadata["capture_stopped_monotonic_s"] = json!(clock()?);
            }
            self.metadata["stop_reason"] = json!(reason
                .as_ref()
                .map(|r| r.unwrap_or("duration"))
                .unwrap_or_else(|e| e.as_str()));
            self.metadata["stop_requested"] = json!(matches!(reason, Ok(Some("requested_stop"))));
            self.metadata["elapsed_seconds"] = json!(self.begin.elapsed().as_secs_f64());
            match &result {
                Ok(summary) => {
                    if let Some(fields) = summary.as_object() {
                        for (k, v) in fields {
                            self.metadata[k] = v.clone();
                        }
                    }
                    self.metadata["status"] = json!(success_status);
                    self.metadata["native_status"] = json!("succeeded");
                }
                Err(error) => {
                    self.metadata["status"] = json!("failed");
                    self.metadata["native_status"] = json!("failed");
                    self.metadata["errors"] = json!([error]);
                }
            }
            self.save()?;
            self.done.store(true, Ordering::Release);
            result.map(|_| ())
        }
    }
    impl Drop for Session {
        fn drop(&mut self) {
            self.done.store(true, Ordering::Release);
        }
    }
}

#[cfg(windows)]
fn record_video(options: &Options, session: &mut Session) -> Result<serde_json::Value, String> {
    use crate::{
        capture::Worker,
        media_writer::VideoWriter,
        protocol::{Request, Target},
    };
    use serde_json::json;
    use std::{
        fs::OpenOptions,
        io::{BufWriter, Write},
        thread,
        time::Duration,
    };
    let id = "00000000-0000-0000-0000-000000000001";
    let request = |op: &str, target| Request {
        version: 2,
        id: 1,
        op: op.into(),
        session_id: id.into(),
        target,
        frame_buffer: None,
        max_fps: if op == "open" { Some(30) } else { None },
        timeout_ms: if op == "capture" { Some(500) } else { None },
    };
    session.metadata["mechanism"] = json!("Windows Graphics Capture selected HWND");
    session.metadata["scope"] = json!("selected_window");
    session.metadata["video_only"] = json!(true);
    session.metadata["audio_scope"] = json!("none");
    session.save()?;
    let mut worker = Worker::default();
    let result = (|| {
        let (width, height) = crate::capture::selected_window_size(
            options.window_id.unwrap(),
            options.pid,
            session.metadata["target"]["creation_time"]
                .as_u64()
                .ok_or("Missing source identity")?,
        )
        .map_err(|e| e.message)?;
        // Initialize the encoder before acquisition so its startup cannot discard WGC frames.
        let mut writer = VideoWriter::new(&options.output_dir.join("game.mp4"), width, height)?;
        session.metadata["video_size"] = json!([width & !1, height & !1]);
        session.save()?;
        let mut timeline = BufWriter::new(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(options.output_dir.join("game.jsonl"))
                .map_err(|e| e.to_string())?,
        );
        worker
            .execute(request(
                "open",
                Some(Target::Window {
                    window_id: options.window_id.unwrap(),
                    pid: options.pid,
                }),
            ))
            .map_err(|e| e.message)?;
        let (mut count, mut last_sequence, mut first_time, mut last_time) =
            (0u64, 0u64, 0f64, 0f64);
        let mut drops = 0u64;
        session.started()?;
        while session.poll()?.is_none() {
            let (metadata, pixels) = match worker.execute(request("capture", None)) {
                Ok(frame) => frame,
                Err(error) if error.code == "capture_timeout" => continue,
                Err(error) => return Err(error.message),
            };
            let sequence = metadata["frame_sequence"]
                .as_u64()
                .ok_or("Missing frame sequence")?;
            if sequence == last_sequence {
                thread::sleep(Duration::from_millis(5));
                continue;
            }
            let skipped = skipped_frames(last_sequence, sequence)?;
            drops += skipped;
            session.metadata["video_drops"] = json!(drops);
            let time = metadata["source_time"]
                .as_f64()
                .filter(|v| v.is_finite())
                .ok_or("Invalid source timestamp")?;
            let width = metadata["width"].as_u64().ok_or("Invalid video width")? as u32;
            let height = metadata["height"].as_u64().ok_or("Invalid video height")? as u32;
            if count == 0 {
                first_time = time;
            }
            writer.write(width, height, &pixels, time)?;
            count += 1;
            last_sequence = sequence;
            last_time = time;
            session.metadata["video_frames"] = json!(count);
            writeln!(timeline,"{}",json!({"kind":"video","source":"game","pts_s":time,"pts_value":(time*10_000_000.0).round() as i64,"pts_timescale":10_000_000,"host_clock_s":metadata["acquired_at"],"callback_monotonic_s":clock()?,"frame_sequence":sequence,"frame_status":0,"written":true,"dropped_since_previous":skipped,"width":width&!1,"height":height&!1})).map_err(|e|e.to_string())?;
        }
        session.stopped()?;
        writer.finish()?;
        writeln!(timeline,"{}",json!({"kind":"finished","source":"game","video_frames":count,"video_drops":drops,"callback_monotonic_s":clock()?})).map_err(|e|e.to_string())?;
        timeline.flush().map_err(|e| e.to_string())?;
        session.metadata["video_frames"] = json!(count);
        if count == 0 {
            return Err("No selected-window video frames captured".into());
        }
        if drops != 0 {
            return Err(format!(
                "Selected-window video skipped {drops} acquired frames"
            ));
        }
        let span = last_time - first_time;
        let elapsed = session.metadata["capture_stopped_monotonic_s"]
            .as_f64()
            .unwrap()
            - session.metadata["capture_started_monotonic_s"]
                .as_f64()
                .unwrap();
        let covered = elapsed <= 5.0 || span >= elapsed - 5.0;
        session.metadata["video_span_seconds"] = json!(span);
        session.metadata["video_change_coverage"] = json!(if covered {
            "covered"
        } else {
            "static_or_incomplete"
        });
        if !covered {
            return Err("Video change coverage unverified; static or incomplete recording".into());
        }
        Ok(
            json!({"video_frames":count,"video_span_seconds":span,"video_change_coverage":"covered"}),
        )
    })();
    if !worker.close() && result.is_ok() {
        return Err("Capture shutdown exceeded two seconds".into());
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counts_initial_and_intermediate_video_losses() {
        assert_eq!(skipped_frames(0, 1), Ok(0));
        assert_eq!(skipped_frames(0, 4), Ok(3));
        assert_eq!(skipped_frames(4, 7), Ok(2));
        assert!(skipped_frames(7, 7).is_err());
        assert!(skipped_frames(7, 6).is_err());
    }
    #[test]
    fn rejects_invalid_recording_contract() {
        let root = std::env::temp_dir().join(format!("vntts-cli-{}", std::process::id()));
        let base = vec![
            "--video-only".into(),
            "7".into(),
            "4294967297".into(),
            "1".into(),
            root.into_os_string(),
        ];
        assert!(Options::parse(&base).is_ok());
        for (index, value) in [
            (1, "0"),
            (2, "-1"),
            (3, "NaN"),
            (3, "0"),
            (3, "3601"),
            (4, "relative"),
        ] {
            let mut bad = base.clone();
            bad[index] = value.into();
            assert!(Options::parse(&bad).is_err());
        }
        assert!(Options::parse(&base[..4]).is_err());
        let mut audio = base;
        audio[0] = "--capture".into();
        audio[2] = std::env::current_exe().unwrap().into_os_string();
        assert!(Options::parse(&audio).is_err());
        audio.push("--allow-audio-capture".into());
        assert!(Options::parse(&audio).is_ok());
    }
}
