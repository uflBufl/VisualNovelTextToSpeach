use libloading::Library;
use serde_json::{json, Value};
use std::ffi::{c_char, c_int, c_ulong, c_void, CStr, OsString};
use std::ptr;

#[repr(C)]
pub(super) struct DeviceInfo {
    pub(super) struct_version: c_int,
    pub(super) name: *const c_char,
    pub(super) host_api: c_int,
    pub(super) max_input_channels: c_int,
    pub(super) max_output_channels: c_int,
    pub(super) default_low_input_latency: f64,
    pub(super) default_low_output_latency: f64,
    pub(super) default_high_input_latency: f64,
    pub(super) default_high_output_latency: f64,
    pub(super) default_sample_rate: f64,
}

#[repr(C)]
pub(super) struct HostApiInfo {
    pub(super) struct_version: c_int,
    pub(super) api_type: c_int,
    pub(super) name: *const c_char,
    pub(super) device_count: c_int,
    pub(super) default_input_device: c_int,
    pub(super) default_output_device: c_int,
}

#[repr(C)]
pub(super) struct StreamInfo {
    pub(super) struct_version: c_int,
    pub(super) input_latency: f64,
    pub(super) output_latency: f64,
    pub(super) sample_rate: f64,
}

impl StreamInfo {
    pub(super) fn validate(&self) -> Result<(), String> {
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
pub(super) struct StreamParameters {
    pub(super) device: c_int,
    pub(super) channel_count: c_int,
    pub(super) sample_format: c_ulong,
    pub(super) suggested_latency: f64,
    pub(super) host_api_specific_stream_info: *mut c_void,
}

#[repr(C)]
pub(super) struct CallbackTimeInfo {
    pub(super) input_buffer_adc_time: f64,
    pub(super) current_time: f64,
    pub(super) output_buffer_dac_time: f64,
}

pub(super) type Callback = unsafe extern "C" fn(
    *const c_void,
    *mut c_void,
    c_ulong,
    *const CallbackTimeInfo,
    c_ulong,
    *mut c_void,
) -> c_int;
pub(super) type StreamAction = unsafe extern "C" fn(*mut c_void) -> c_int;
pub(super) type OpenStream = unsafe extern "C" fn(
    *mut *mut c_void,
    *const StreamParameters,
    *const StreamParameters,
    f64,
    c_ulong,
    c_ulong,
    Option<Callback>,
    *mut c_void,
) -> c_int;

pub(super) struct PortAudio {
    pub(super) _library: Library,
    pub(super) initialized: bool,
    pub(super) terminate: unsafe extern "C" fn() -> c_int,
    pub(super) error_text: unsafe extern "C" fn(c_int) -> *const c_char,
    pub(super) default_output: unsafe extern "C" fn() -> c_int,
    pub(super) device_info: unsafe extern "C" fn(c_int) -> *const DeviceInfo,
    pub(super) host_api_info: unsafe extern "C" fn(c_int) -> *const HostApiInfo,
    pub(super) stream_info: unsafe extern "C" fn(*mut c_void) -> *const StreamInfo,
    pub(super) open: OpenStream,
    pub(super) start: StreamAction,
    pub(super) abort: StreamAction,
    pub(super) close: StreamAction,
    pub(super) is_active: StreamAction,
    pub(super) format_supported:
        unsafe extern "C" fn(*const StreamParameters, *const StreamParameters, f64) -> c_int,
    pub(super) stream_time: unsafe extern "C" fn(*mut c_void) -> f64,
}

impl PortAudio {
    pub(super) fn load(path: OsString) -> Result<Self, String> {
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
                format_supported: *library
                    .get(b"Pa_IsFormatSupported\0")
                    .map_err(|e| e.to_string())?,
                stream_time: *library
                    .get(b"Pa_GetStreamTime\0")
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

    fn query_device_info(
        &self,
        phase: &mut dyn FnMut(&'static str) -> Result<(), String>,
    ) -> Result<&DeviceInfo, String> {
        phase("default_output_enter")?;
        let device = unsafe { (self.default_output)() };
        phase("default_output_return")?;
        if device < 0 {
            return Err("no default output device".into());
        }
        phase("device_info_enter")?;
        let info = unsafe { (self.device_info)(device).as_ref() };
        phase("device_info_return")?;
        info.ok_or("no output device info".into())
    }

    pub(super) fn device_event(
        &self,
        phase: &mut dyn FnMut(&'static str) -> Result<(), String>,
    ) -> Result<Value, String> {
        let info = self.query_device_info(phase)?;
        phase("host_api_info_enter")?;
        let host_api = unsafe { (self.host_api_info)(info.host_api).as_ref() };
        phase("host_api_info_return")?;
        let host_api = host_api.filter(|api| !api.name.is_null()).map(|api| {
            unsafe { CStr::from_ptr(api.name) }
                .to_string_lossy()
                .into_owned()
        });
        let name = if info.name.is_null() {
            None
        } else {
            Some(
                unsafe { CStr::from_ptr(info.name) }
                    .to_string_lossy()
                    .into_owned(),
            )
        };
        Ok(
            json!({"event":"device", "name":name, "default_samplerate":info.default_sample_rate,
            "max_output_channels":info.max_output_channels, "hostapi":info.host_api, "host_api":host_api}),
        )
    }

    pub(super) fn check_format(
        &self,
        sample_rate: u32,
        channels: u8,
        phase: &mut dyn FnMut(&'static str) -> Result<(), String>,
    ) -> Result<Value, String> {
        if sample_rate == 0 || !matches!(channels, 1 | 2) {
            return Err("format requires positive sample_rate and 1 or 2 channels".into());
        }
        let info = self.query_device_info(phase)?;
        phase("default_output_parameters_enter")?;
        let device = unsafe { (self.default_output)() };
        phase("default_output_parameters_return")?;
        let parameters = StreamParameters {
            device,
            channel_count: c_int::from(channels),
            sample_format: 1,
            suggested_latency: info.default_low_output_latency,
            host_api_specific_stream_info: ptr::null_mut(),
        };
        phase("format_supported_enter")?;
        let code =
            unsafe { (self.format_supported)(ptr::null(), &parameters, f64::from(sample_rate)) };
        phase("format_supported_return")?;
        Ok(json!({"event":"format", "supported":code == 0, "code":code,
            "error": if code == 0 { None } else { Some(self.error(code)) }}))
    }

    pub(super) fn error(&self, code: c_int) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    thread_local! {
        static CALLS: RefCell<Vec<&'static str>> = const { RefCell::new(Vec::new()) };
        static DEFAULT: Cell<c_int> = const { Cell::new(0) };
        static MISSING_INFO: Cell<bool> = const { Cell::new(false) };
        static FORMAT_CODE: Cell<c_int> = const { Cell::new(0) };
        static INFO: DeviceInfo = const { DeviceInfo {
            struct_version: 1, name: ptr::null(), host_api: 0,
            max_input_channels: 0, max_output_channels: 2,
            default_low_input_latency: 0.0, default_low_output_latency: 0.01,
            default_high_input_latency: 0.0, default_high_output_latency: 0.1,
            default_sample_rate: 24000.0,
        } };
    }

    fn record(phase: &'static str) -> Result<(), String> {
        CALLS.with_borrow_mut(|calls| calls.push(phase));
        Ok(())
    }

    unsafe extern "C" fn default_output() -> c_int {
        record("default_output_call").unwrap();
        DEFAULT.get()
    }

    unsafe extern "C" fn device_info(_: c_int) -> *const DeviceInfo {
        record("device_info_call").unwrap();
        if MISSING_INFO.get() {
            ptr::null()
        } else {
            INFO.with(|info| info as *const _)
        }
    }

    unsafe extern "C" fn host_api_info(_: c_int) -> *const HostApiInfo {
        record("host_api_info_call").unwrap();
        ptr::null()
    }

    unsafe extern "C" fn format_supported(
        _: *const StreamParameters,
        _: *const StreamParameters,
        _: f64,
    ) -> c_int {
        record("format_supported_call").unwrap();
        FORMAT_CODE.get()
    }

    unsafe extern "C" fn terminate() -> c_int {
        0
    }
    unsafe extern "C" fn error_text(_: c_int) -> *const c_char {
        ptr::null()
    }
    unsafe extern "C" fn stream_info(_: *mut c_void) -> *const StreamInfo {
        ptr::null()
    }
    unsafe extern "C" fn stream_action(_: *mut c_void) -> c_int {
        0
    }
    unsafe extern "C" fn stream_time(_: *mut c_void) -> f64 {
        0.0
    }
    unsafe extern "C" fn open(
        _: *mut *mut c_void,
        _: *const StreamParameters,
        _: *const StreamParameters,
        _: f64,
        _: c_ulong,
        _: c_ulong,
        _: Option<Callback>,
        _: *mut c_void,
    ) -> c_int {
        0
    }

    fn fake() -> PortAudio {
        CALLS.with_borrow_mut(Vec::clear);
        DEFAULT.set(0);
        MISSING_INFO.set(false);
        FORMAT_CODE.set(0);
        #[cfg(unix)]
        let library = libloading::os::unix::Library::this().into();
        #[cfg(windows)]
        let library = libloading::os::windows::Library::this().unwrap().into();
        PortAudio {
            _library: library,
            initialized: false,
            terminate,
            error_text,
            default_output,
            device_info,
            host_api_info,
            stream_info,
            open,
            start: stream_action,
            abort: stream_action,
            close: stream_action,
            is_active: stream_action,
            format_supported,
            stream_time,
        }
    }

    const DEVICE_CALLS: [&str; 6] = [
        "default_output_enter",
        "default_output_call",
        "default_output_return",
        "device_info_enter",
        "device_info_call",
        "device_info_return",
    ];

    #[test]
    fn query_device_boundaries_surround_native_calls_and_keep_missing_host_optional() {
        let pa = fake();
        let event = pa.device_event(&mut record).unwrap();
        assert_eq!(event["event"], "device");
        assert_eq!(event["host_api"], Value::Null);
        let mut expected = DEVICE_CALLS.to_vec();
        expected.extend([
            "host_api_info_enter",
            "host_api_info_call",
            "host_api_info_return",
        ]);
        CALLS.with_borrow(|calls| assert_eq!(calls, &expected));
    }

    #[test]
    fn format_boundaries_cover_supported_and_unsupported_results() {
        for code in [0, -9997] {
            let pa = fake();
            FORMAT_CODE.set(code);
            let event = pa.check_format(24000, 2, &mut record).unwrap();
            assert_eq!(event["supported"], code == 0);
            assert_eq!(event["code"], code);
            let mut expected = DEVICE_CALLS.to_vec();
            expected.extend([
                "default_output_parameters_enter",
                "default_output_call",
                "default_output_parameters_return",
                "format_supported_enter",
                "format_supported_call",
                "format_supported_return",
            ]);
            CALLS.with_borrow(|calls| assert_eq!(calls, &expected));
        }
    }

    #[test]
    fn device_errors_retain_return_boundary_and_skip_later_calls() {
        for missing_info in [false, true] {
            let pa = fake();
            DEFAULT.set(if missing_info { 0 } else { -1 });
            MISSING_INFO.set(missing_info);
            assert!(pa.device_event(&mut record).is_err());
            let expected = &DEVICE_CALLS[..if missing_info { 6 } else { 3 }];
            CALLS.with_borrow(|calls| assert_eq!(calls, expected));
            CALLS.with_borrow_mut(Vec::clear);
            assert!(pa.check_format(24000, 2, &mut record).is_err());
            CALLS.with_borrow(|calls| assert_eq!(calls, expected));
        }
        let pa = fake();
        for (rate, channels) in [(0, 1), (24000, 3)] {
            assert!(pa.check_format(rate, channels, &mut record).is_err());
            CALLS.with_borrow(|calls| assert!(calls.is_empty()));
        }
    }
}
