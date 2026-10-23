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

    pub(super) fn device(&self) -> Result<&DeviceInfo, String> {
        let device = unsafe { (self.default_output)() };
        if device < 0 {
            return Err("no default output device".into());
        }
        unsafe { (self.device_info)(device).as_ref() }.ok_or("no output device info".into())
    }

    pub(super) fn device_event(&self) -> Result<Value, String> {
        let info = self.device()?;
        let host_api = unsafe { (self.host_api_info)(info.host_api).as_ref() }
            .filter(|api| !api.name.is_null())
            .map(|api| {
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

    pub(super) fn check_format(&self, sample_rate: u32, channels: u8) -> Result<Value, String> {
        if sample_rate == 0 || !matches!(channels, 1 | 2) {
            return Err("format requires positive sample_rate and 1 or 2 channels".into());
        }
        let info = self.device()?;
        let parameters = StreamParameters {
            device: unsafe { (self.default_output)() },
            channel_count: c_int::from(channels),
            sample_format: 1,
            suggested_latency: info.default_low_output_latency,
            host_api_specific_stream_info: ptr::null_mut(),
        };
        let code =
            unsafe { (self.format_supported)(ptr::null(), &parameters, f64::from(sample_rate)) };
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
