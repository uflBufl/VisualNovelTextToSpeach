use crate::{
    pixels,
    protocol::{Failure, Request, Target},
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Condvar, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    core::{factory, Interface},
    Foundation::TypedEventHandler,
    Graphics::{
        Capture::{
            Direct3D11CaptureFrame, Direct3D11CaptureFramePool, GraphicsCaptureItem,
            GraphicsCaptureSession,
        },
        DirectX::{Direct3D11::IDirect3DDevice, DirectXPixelFormat},
        SizeInt32,
    },
    Win32::{
        Foundation::{CloseHandle, FILETIME, HANDLE, HMODULE, HWND, POINT, RECT},
        Graphics::{
            Direct3D::{D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL_11_0},
            Direct3D11::*,
            Dwm::{DwmGetWindowAttribute, DWMWA_EXTENDED_FRAME_BOUNDS},
            Dxgi::{Common::*, IDXGIDevice},
            Gdi::{
                ClientToScreen, GetMonitorInfoW, MonitorFromPoint, HMONITOR, MONITORINFO,
                MONITOR_DEFAULTTOPRIMARY,
            },
        },
        System::{
            Performance::{QueryPerformanceCounter, QueryPerformanceFrequency},
            Threading::{GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
            WinRT::{
                Direct3D11::{CreateDirect3D11DeviceFromDXGIDevice, IDirect3DDxgiInterfaceAccess},
                Graphics::Capture::IGraphicsCaptureItemInterop,
                RoInitialize, RoUninitialize, RO_INIT_MULTITHREADED,
            },
        },
        UI::{
            HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2},
            WindowsAndMessaging::{GetClientRect, GetWindowThreadProcessId, IsIconic, IsWindow},
        },
    },
};

impl From<windows::core::Error> for Failure {
    fn from(error: windows::core::Error) -> Self {
        Self::new(
            "native_error",
            format!(
                "{} (HRESULT 0x{:08X})",
                error.message(),
                error.code().0 as u32
            ),
        )
    }
}
fn clock() -> Result<f64, Failure> {
    unsafe {
        let (mut ticks, mut frequency) = (0, 0);
        QueryPerformanceCounter(&mut ticks)?;
        QueryPerformanceFrequency(&mut frequency)?;
        Ok(ticks as f64 / frequency as f64)
    }
}
#[derive(Clone)]
struct Frame {
    metadata: Value,
    pixels: Arc<Vec<u8>>,
}
#[derive(Default)]
struct State {
    latest: Option<Frame>,
    error: Option<Failure>,
}
impl State {
    fn replace_live(
        &mut self,
        latest: Option<Frame>,
        error: Option<Failure>,
        closed: &AtomicBool,
        stop: &AtomicBool,
    ) -> Result<(), Failure> {
        // Call only while holding the state lock: terminal callbacks must never be cleared by publication.
        ensure_live(closed, stop)?;
        self.latest = latest;
        self.error = error;
        Ok(())
    }
}
fn ensure_live(closed: &AtomicBool, stop: &AtomicBool) -> Result<(), Failure> {
    if closed.load(Ordering::Acquire) {
        return Err(Failure::new("target_closed", "GraphicsCaptureItem closed"));
    }
    if stop.load(Ordering::Acquire) {
        return Err(Failure::new("closed", "Capture source stopped"));
    }
    Ok(())
}
type Shared = Arc<(Mutex<State>, Condvar)>;

#[derive(Default)]
pub struct Worker {
    session: Option<Source>,
    pub must_exit: bool,
}
struct Source {
    id: String,
    target: Target,
    process_created: u64,
    shared: Shared,
    stop: Arc<AtomicBool>,
    closed: Arc<AtomicBool>,
    done: mpsc::Receiver<()>,
}
impl Worker {
    pub fn execute(&mut self, request: Request) -> Result<(Value, Arc<Vec<u8>>), Failure> {
        match request.op.as_str() {
            "open" => {
                if !self.close() {
                    return Err(Failure::new(
                        "shutdown_timeout",
                        "Previous capture source did not stop; worker must exit",
                    ));
                }
                let source = match Source::start(
                    request.session_id.clone(),
                    request.target.unwrap(),
                    request.max_fps.unwrap_or(10),
                ) {
                    Ok(source) => source,
                    Err(error) => {
                        self.must_exit = error.code == "startup_timeout";
                        return Err(error);
                    }
                };
                self.session = Some(source);
                Ok((
                    json!({"backend":"windows_graphics_capture","session_id":request.session_id,"clock_now":clock()?}),
                    Arc::new(Vec::new()),
                ))
            }
            "capture" => {
                let source = self
                    .session
                    .as_ref()
                    .filter(|source| source.id == request.session_id)
                    .ok_or_else(|| {
                        Failure::new("invalid_session", "Session is closed or does not match")
                    })?;
                let frame =
                    source.frame(Duration::from_millis(request.timeout_ms.unwrap_or(2000)))?;
                Ok((frame.metadata, frame.pixels))
            }
            "close" => {
                if self
                    .session
                    .as_ref()
                    .is_some_and(|source| source.id != request.session_id)
                {
                    return Err(Failure::new("invalid_session", "Session does not match"));
                }
                if !self.close() {
                    return Err(Failure::new(
                        "shutdown_timeout",
                        "Capture shutdown exceeded 2 seconds; worker must exit",
                    ));
                }
                Ok((json!({}), Arc::new(Vec::new())))
            }
            _ => unreachable!(),
        }
    }
    pub fn close(&mut self) -> bool {
        if let Some(source) = self.session.take() {
            source.stop.store(true, Ordering::Release);
            let mut state = source.shared.0.lock().unwrap();
            state.latest = None;
            state.error = Some(Failure::new("closed", "Session was closed"));
            source.shared.1.notify_all();
            drop(state);
            if source.done.recv_timeout(Duration::from_secs(2)).is_err() {
                self.must_exit = true;
                return false;
            }
        }
        true
    }
}
impl Source {
    fn start(id: String, target: Target, fps: u32) -> Result<Self, Failure> {
        let shared: Shared = Default::default();
        let stop = Arc::new(AtomicBool::new(false));
        let closed = Arc::new(AtomicBool::new(false));
        let thread_closed = closed.clone();
        let (ready_tx, ready_rx) = mpsc::channel();
        let (done_tx, done) = mpsc::channel();
        let (thread_shared, thread_stop, thread_id, thread_target) =
            (shared.clone(), stop.clone(), id.clone(), target.clone());
        thread::spawn(move || {
            let result = unsafe {
                run_source(
                    &thread_id,
                    thread_target,
                    fps,
                    &thread_shared,
                    &thread_stop,
                    thread_closed,
                    ready_tx,
                )
            };
            if let Err(error) = result {
                let mut state = thread_shared.0.lock().unwrap();
                state.latest = None;
                state.error = Some(error);
                thread_shared.1.notify_all();
            }
            let _ = done_tx.send(());
        });
        match ready_rx.recv_timeout(Duration::from_secs(10)) {
            Ok(Ok(process_created)) => Ok(Self {
                id,
                target,
                process_created,
                shared,
                stop,
                closed,
                done,
            }),
            Ok(Err(error)) => Err(error),
            Err(_) => {
                // A stuck GPU/COM call cannot safely be killed as a thread. Terminate this helper after reporting.
                stop.store(true, Ordering::Release);
                Err(Failure::new(
                    "startup_timeout",
                    "Native capture startup exceeded 10 seconds; restart helper",
                ))
            }
        }
    }
    fn frame(&self, timeout: Duration) -> Result<Frame, Failure> {
        let deadline = Instant::now() + timeout;
        let mut state = self.shared.0.lock().unwrap();
        loop {
            ensure_live(&self.closed, &self.stop)?;
            if let Some(error) = &state.error {
                return Err(error.clone());
            }
            if let Some(frame) = &state.latest {
                let frame = frame.clone();
                // Validate cached static content at request time as well as on native frame arrival.
                let geometry =
                    unsafe { validate_cached(&self.target, self.process_created, &frame.metadata) };
                if let Err(error) = geometry {
                    state.latest = None;
                    return Err(error);
                }
                ensure_live(&self.closed, &self.stop)?;
                return Ok(frame);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(Failure::new(
                    "capture_timeout",
                    "No valid native frame arrived before the deadline",
                ));
            }
            state = self.shared.1.wait_timeout(state, remaining).unwrap().0;
        }
    }
}

struct Native {
    item: GraphicsCaptureItem,
    pool: Direct3D11CaptureFramePool,
    session: GraphicsCaptureSession,
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    winrt_device: IDirect3DDevice,
    size: SizeInt32,
    closed: Arc<AtomicBool>,
    closed_token: Option<i64>,
    target: Target,
    process: Option<HANDLE>,
    process_created: u64,
}
impl Drop for Native {
    fn drop(&mut self) {
        if let Some(token) = self.closed_token {
            let _ = self.item.RemoveClosed(token);
        }
        let _ = self.session.Close();
        let _ = self.pool.Close();
        if let Some(handle) = self.process {
            unsafe {
                let _ = CloseHandle(handle);
            }
        }
    }
}
struct Apartment;
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe {
            RoUninitialize();
        }
    }
}

unsafe fn run_source(
    id: &str,
    target: Target,
    fps: u32,
    shared: &Shared,
    stop: &AtomicBool,
    closed: Arc<AtomicBool>,
    ready: mpsc::Sender<Result<u64, Failure>>,
) -> Result<(), Failure> {
    let initialized = RoInitialize(RO_INIT_MULTITHREADED).map_err(Failure::from);
    if let Err(error) = initialized {
        let _ = ready.send(Err(error.clone()));
        return Err(error);
    }
    let _apartment = Apartment;
    let mut native = match Native::open(target, shared.clone(), closed) {
        Ok(native) => native,
        Err(error) => {
            let _ = ready.send(Err(error.clone()));
            return Err(error);
        }
    };
    let _ = ready.send(Ok(native.process_created));
    let (mut sequence, mut epoch) = (0u64, 1u64);
    let mut layout = None;
    let mut valid_after = clock()?;
    let mut next_copy = Instant::now();
    while !stop.load(Ordering::Acquire) {
        if native.closed.load(Ordering::Acquire) {
            return Err(Failure::new("target_closed", "GraphicsCaptureItem closed"));
        }
        match native.geometry() {
            Ok(current) => {
                if layout.as_ref() != Some(&current) {
                    epoch += 1;
                    valid_after = clock()?;
                    let mut state = shared.0.lock().unwrap();
                    state.replace_live(None, None, &native.closed, stop)?;
                    layout = Some(current);
                }
            }
            Err(error) if error.code == "minimized" => {
                layout = None;
                valid_after = clock()?;
                let mut state = shared.0.lock().unwrap();
                state.replace_live(None, Some(error), &native.closed, stop)?;
                shared.1.notify_all();
                drop(state);
                thread::sleep(Duration::from_millis(10));
                continue;
            }
            Err(error) => return Err(error),
        }
        // Drain at most the bounded pool capacity. Never retain a native frame after Close.
        let mut latest: Option<Direct3D11CaptureFrame> = None;
        for _ in 0..2 {
            let frame = match native.pool.TryGetNextFrame() {
                Ok(frame) => frame,
                Err(error) if error.code().0 as u32 == 0x80004003 => break,
                Err(error) => return Err(error.into()),
            };
            if let Some(previous) = latest.replace(frame) {
                let _ = previous.Close();
            }
        }
        if let Some(frame) = latest {
            let size = frame.ContentSize()?;
            pixels::dimensions(size.Width, size.Height)?;
            if size != native.size {
                frame.Close()?;
                shared.0.lock().unwrap().latest = None;
                epoch += 1;
                valid_after = clock()?;
                native.pool.Recreate(
                    &native.winrt_device,
                    DirectXPixelFormat::R16G16B16A16Float,
                    2,
                    size,
                )?;
                native.size = size;
                continue;
            }
            let source_time = frame.SystemRelativeTime()?.Duration as f64 / 10_000_000.0;
            if source_time >= valid_after && Instant::now() >= next_copy {
                let before = native.geometry()?;
                let result = native.copy(&frame);
                let close_result = frame.Close();
                let bytes = result?;
                close_result?;
                let after = native.geometry()?;
                if before != after || native.closed.load(Ordering::Acquire) {
                    shared.0.lock().unwrap().latest = None;
                    layout = None;
                    continue;
                }
                let (surface, client) = before;
                if surface[2] != size.Width || surface[3] != size.Height {
                    return Err(Failure::new("unsupported_layout", "WGC extent does not match physical DWM/monitor bounds; crop mapping is unverified"));
                }
                let content = pixels::client_rect(surface, client, size.Width, size.Height)?;
                sequence += 1;
                let (scope, window_id, pid) = match native.target {
                    Target::Window { window_id, pid } => {
                        ("selected_window", Some(window_id), Some(pid))
                    }
                    Target::Display { .. } => ("display", None, None),
                };
                let metadata = json!({"backend":"windows_graphics_capture","scope":scope,"session_id":id,"window_id":window_id,"pid":pid,"width":size.Width,"height":size.Height,"row_stride":size.Width*4,"pixel_format":"BGRA8","content_rect":content,"desktop_bounds":surface,"frame_sequence":sequence,"source_time":source_time,"acquired_at":clock()?,"layout_epoch":epoch});
                let mut state = shared.0.lock().unwrap();
                state.replace_live(
                    Some(Frame {
                        metadata,
                        pixels: Arc::new(bytes),
                    }),
                    None,
                    &native.closed,
                    stop,
                )?;
                shared.1.notify_all();
                next_copy = Instant::now() + Duration::from_secs_f64(1.0 / fps as f64);
            } else {
                frame.Close()?;
            }
        }
        if let Err(error) = native.device.GetDeviceRemovedReason() {
            return Err(Failure::new(
                "device_lost",
                format!(
                    "D3D11 device removed: {} (HRESULT 0x{:08X}); session invalidated",
                    error.message(),
                    error.code().0 as u32
                ),
            ));
        }
        thread::sleep(Duration::from_millis(5));
    }
    Ok(())
}

impl Native {
    unsafe fn open(
        target: Target,
        shared: Shared,
        closed: Arc<AtomicBool>,
    ) -> Result<Self, Failure> {
        static DPI: std::sync::OnceLock<Result<(), Failure>> = std::sync::OnceLock::new();
        DPI.get_or_init(|| {
            SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
                .map_err(Failure::from)
        })
        .clone()?;
        if !GraphicsCaptureSession::IsSupported()? {
            return Err(Failure::new(
                "unsupported_capture",
                "Windows Graphics Capture is unavailable",
            ));
        }
        let interop: IGraphicsCaptureItemInterop =
            factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()?;
        let item = match target {
            Target::Window { window_id, .. } => interop
                .CreateForWindow::<GraphicsCaptureItem>(HWND(window_id as usize as *mut _))?,
            Target::Display { display_id } => {
                interop.CreateForMonitor::<GraphicsCaptureItem>(monitor(display_id))?
            }
        };
        let size = item.Size()?;
        pixels::dimensions(size.Width, size.Height)?;
        let (mut device, mut context) = (None, None);
        D3D11CreateDevice(
            None,
            D3D_DRIVER_TYPE_HARDWARE,
            HMODULE::default(),
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            Some(&[D3D_FEATURE_LEVEL_11_0]),
            D3D11_SDK_VERSION,
            Some(&mut device),
            None,
            Some(&mut context),
        )?;
        let device =
            device.ok_or_else(|| Failure::new("native_error", "D3D11 device was not returned"))?;
        let context = context
            .ok_or_else(|| Failure::new("native_error", "D3D11 context was not returned"))?;
        let dxgi: IDXGIDevice = device.cast()?;
        let winrt_device: IDirect3DDevice = CreateDirect3D11DeviceFromDXGIDevice(&dxgi)?.cast()?;
        let pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &winrt_device,
            DirectXPixelFormat::R16G16B16A16Float,
            2,
            size,
        )?;
        let session = pool.CreateCaptureSession(&item)?;
        session.SetIsCursorCaptureEnabled(false)?;
        // Keep the OS default capture border; never request borderless capture access.
        let (process, process_created) = match target {
            Target::Window { pid, .. } => {
                let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid)?;
                match creation_time(handle) {
                    Ok(created) => (Some(handle), created),
                    Err(error) => {
                        let _ = CloseHandle(handle);
                        return Err(error);
                    }
                }
            }
            _ => (None, 0),
        };
        let mut native = Self {
            item,
            pool,
            session,
            device,
            context,
            winrt_device,
            size,
            closed,
            closed_token: None,
            target,
            process,
            process_created,
        };
        let callback_closed = native.closed.clone();
        native.closed_token = Some(native.item.Closed(&TypedEventHandler::new(move |_, _| {
            callback_closed.store(true, Ordering::Release);
            let mut state = shared.0.lock().unwrap();
            state.latest = None;
            state.error = Some(Failure::new("target_closed", "GraphicsCaptureItem closed"));
            shared.1.notify_all();
            Ok(())
        }))?);
        native.geometry()?;
        native.session.StartCapture()?;
        Ok(native)
    }
    unsafe fn geometry(&self) -> Result<([i32; 4], [i32; 4]), Failure> {
        geometry(&self.target, self.process, self.process_created)
    }
    unsafe fn copy(
        &self,
        frame: &windows::Graphics::Capture::Direct3D11CaptureFrame,
    ) -> Result<Vec<u8>, Failure> {
        let access: IDirect3DDxgiInterfaceAccess = frame.Surface()?.cast()?;
        let texture: ID3D11Texture2D = access.GetInterface()?;
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        texture.GetDesc(&mut desc);
        if desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT
            || desc.Width < self.size.Width as u32
            || desc.Height < self.size.Height as u32
        {
            return Err(Failure::new(
                "invalid_texture",
                "Unexpected native capture texture format or extent",
            ));
        }
        desc.Width = self.size.Width as u32;
        desc.Height = self.size.Height as u32;
        desc.Usage = D3D11_USAGE_STAGING;
        desc.BindFlags = 0;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ.0 as u32;
        desc.MiscFlags = 0;
        let mut staging = None;
        self.device
            .CreateTexture2D(&desc, None, Some(&mut staging))?;
        let staging = staging.ok_or_else(|| Failure::new("native_error", "No staging texture"))?;
        let area = D3D11_BOX {
            left: 0,
            top: 0,
            front: 0,
            right: desc.Width,
            bottom: desc.Height,
            back: 1,
        };
        self.context
            .CopySubresourceRegion(&staging, 0, 0, 0, 0, &texture, 0, Some(&area));
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        self.context
            .Map(&staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))?;
        let length = (mapped.RowPitch as usize).checked_mul(desc.Height as usize);
        let result = match length {
            Some(length)
                if !mapped.pData.is_null()
                    && mapped.RowPitch >= desc.Width * 8
                    && length <= 1024 * 1024 * 1024 =>
            {
                let bytes = std::slice::from_raw_parts(mapped.pData as *const u8, length);
                pixels::convert_rgba16f(
                    bytes,
                    mapped.RowPitch as usize,
                    desc.Width as usize,
                    desc.Height as usize,
                )
            }
            _ => Err(Failure::new(
                "invalid_stride",
                "Mapped native RowPitch exceeds safe extent",
            )),
        };
        self.context.Unmap(&staging, 0);
        result
    }
}
unsafe fn creation_time(process: HANDLE) -> Result<u64, Failure> {
    let (mut created, mut exit, mut kernel, mut user) = (
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
    );
    GetProcessTimes(process, &mut created, &mut exit, &mut kernel, &mut user)?;
    Ok(((created.dwHighDateTime as u64) << 32) | created.dwLowDateTime as u64)
}
unsafe fn monitor(id: i64) -> HMONITOR {
    if id == 0 {
        MonitorFromPoint(POINT::default(), MONITOR_DEFAULTTOPRIMARY)
    } else {
        HMONITOR(id as usize as *mut _)
    }
}

unsafe fn geometry(
    target: &Target,
    process: Option<HANDLE>,
    process_created: u64,
) -> Result<([i32; 4], [i32; 4]), Failure> {
    match *target {
        Target::Window { window_id, pid } => {
            let window = HWND(window_id as usize as *mut _);
            let mut current_pid = 0;
            if !IsWindow(Some(window)).as_bool()
                || GetWindowThreadProcessId(window, Some(&mut current_pid)) == 0
                || current_pid != pid
                || creation_time(process.unwrap())? != process_created
            {
                return Err(Failure::new(
                    "identity_changed",
                    "Selected HWND/process identity no longer matches",
                ));
            }
            if IsIconic(window).as_bool() {
                return Err(Failure::new(
                    "minimized",
                    "Selected window is minimized; cached pixels invalidated",
                ));
            }
            let mut surface = RECT::default();
            DwmGetWindowAttribute(
                window,
                DWMWA_EXTENDED_FRAME_BOUNDS,
                &mut surface as *mut _ as *mut _,
                std::mem::size_of::<RECT>() as u32,
            )?;
            let mut client = RECT::default();
            GetClientRect(window, &mut client)?;
            let mut origin = POINT::default();
            ClientToScreen(window, &mut origin).ok()?;
            Ok((
                [
                    surface.left,
                    surface.top,
                    surface.right - surface.left,
                    surface.bottom - surface.top,
                ],
                [
                    origin.x,
                    origin.y,
                    client.right - client.left,
                    client.bottom - client.top,
                ],
            ))
        }
        Target::Display { display_id } => {
            let mut info = MONITORINFO {
                cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                ..Default::default()
            };
            GetMonitorInfoW(monitor(display_id), &mut info).ok()?;
            let bounds = [
                info.rcMonitor.left,
                info.rcMonitor.top,
                info.rcMonitor.right - info.rcMonitor.left,
                info.rcMonitor.bottom - info.rcMonitor.top,
            ];
            Ok((bounds, bounds))
        }
    }
}

unsafe fn validate_cached(
    target: &Target,
    process_created: u64,
    metadata: &Value,
) -> Result<(), Failure> {
    let process = match target {
        Target::Window { pid, .. } => {
            Some(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, *pid)?)
        }
        _ => None,
    };
    let result = geometry(target, process, process_created);
    if let Some(handle) = process {
        let _ = CloseHandle(handle);
    }
    let (surface, client) = result?;
    let width = metadata["width"].as_i64().unwrap_or(0) as i32;
    let height = metadata["height"].as_i64().unwrap_or(0) as i32;
    if metadata["desktop_bounds"] != json!(surface)
        || metadata["content_rect"] != json!(pixels::client_rect(surface, client, width, height)?)
    {
        return Err(Failure::new(
            "layout_changed",
            "Cached frame no longer matches target layout",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn stopped_source() -> Source {
        let (done_tx, done) = mpsc::channel();
        done_tx.send(()).unwrap();
        Source {
            id: "12345678-1234-1234-1234-123456789abc".into(),
            target: Target::Display { display_id: 0 },
            process_created: 0,
            shared: Default::default(),
            stop: Arc::new(AtomicBool::new(false)),
            closed: Arc::new(AtomicBool::new(false)),
            done,
        }
    }
    #[test]
    fn close_invalidates_owned_pixels_before_source_shutdown() {
        let source = stopped_source();
        let shared = source.shared.clone();
        let stop = source.stop.clone();
        shared.0.lock().unwrap().latest = Some(Frame {
            metadata: json!({}),
            pixels: Arc::new(vec![1, 2, 3, 255]),
        });
        let mut worker = Worker {
            session: Some(source),
            must_exit: false,
        };
        assert!(worker.close());
        assert!(stop.load(Ordering::Acquire));
        let state = shared.0.lock().unwrap();
        assert!(state.latest.is_none());
        assert_eq!(state.error.as_ref().unwrap().code, "closed");
    }
    #[test]
    fn closed_signal_blocks_cached_pixels_before_callback_takes_lock() {
        let source = stopped_source();
        source.shared.0.lock().unwrap().latest = Some(Frame {
            metadata: json!({}),
            pixels: Arc::new(vec![1, 2, 3, 255]),
        });
        source.closed.store(true, Ordering::Release);
        match source.frame(Duration::from_millis(1)) {
            Err(error) => assert_eq!(error.code, "target_closed"),
            _ => panic!("closed cached pixels returned"),
        }
    }
    #[test]
    fn terminal_state_cannot_be_replaced_by_layout_reset_or_publication() {
        for stopped in [false, true] {
            let closed = AtomicBool::new(!stopped);
            let stop = AtomicBool::new(stopped);
            let mut state = State {
                latest: None,
                error: Some(Failure::new("target_closed", "terminal")),
            };
            assert!(state.replace_live(None, None, &closed, &stop).is_err());
            assert!(state
                .replace_live(
                    Some(Frame {
                        metadata: json!({}),
                        pixels: Arc::new(vec![1, 2, 3, 255])
                    }),
                    None,
                    &closed,
                    &stop
                )
                .is_err());
            assert!(state.latest.is_none());
            assert_eq!(state.error.unwrap().message, "terminal");
        }
    }
    #[test]
    fn capture_failure_cannot_return_cached_pixels() {
        let source = stopped_source();
        let mut state = source.shared.0.lock().unwrap();
        state.latest = Some(Frame {
            metadata: json!({}),
            pixels: Arc::new(vec![1, 2, 3, 255]),
        });
        state.error = Some(Failure::new("device_lost", "removed"));
        drop(state);
        match source.frame(Duration::from_millis(1)) {
            Err(error) => assert_eq!(error.code, "device_lost"),
            _ => panic!("invalid cached pixels returned"),
        }
    }
    #[test]
    fn mismatched_session_cannot_close_an_owned_source() {
        let mut worker = Worker {
            session: Some(stopped_source()),
            must_exit: false,
        };
        let request = Request::parse(br#"{"version":1,"id":1,"op":"close","session_id":"00000000-0000-0000-0000-000000000000"}"#).unwrap();
        assert_eq!(worker.execute(request).unwrap_err().code, "invalid_session");
        assert!(worker.session.is_some());
        worker.close();
    }
}
