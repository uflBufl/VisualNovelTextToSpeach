use std::{
    ffi::{c_void, CStr},
    os::windows::ffi::OsStrExt,
    path::Path,
};
use windows::{
    core::{Interface, HRESULT, PCSTR, PCWSTR},
    Win32::{
        Foundation::{FreeLibrary, E_POINTER, HMODULE},
        Media::MediaFoundation::*,
        System::{
            LibraryLoader::{GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32},
            WinRT::{RoInitialize, RoUninitialize, RO_INIT_MULTITHREADED},
        },
    },
};

pub struct VideoWriter {
    sink: Option<IMFSinkWriter>,
    foundation: MediaFoundation,
    stream: u32,
    source_width: u32,
    source_height: u32,
    width: u32,
    height: u32,
    first_time: Option<f64>,
    last_time: Option<f64>,
    finished: bool,
}
impl VideoWriter {
    pub fn new(path: &Path, width: u32, height: u32) -> Result<Self, String> {
        let foundation = MediaFoundation::new()?;
        unsafe { Self::create(path, width, height, foundation).map_err(|e| e.to_string()) }
    }
    unsafe fn create(
        path: &Path,
        source_width: u32,
        source_height: u32,
        foundation: MediaFoundation,
    ) -> windows::core::Result<Self> {
        let (width, height) = (source_width & !1, source_height & !1);
        if width < 2 || height < 2 || width > 16384 || height > 16384 {
            return Err(windows::core::Error::from_hresult(
                windows::Win32::Foundation::E_INVALIDARG,
            ));
        }
        let attributes: IMFAttributes =
            created(|output| (foundation.create_attributes)(output, 1))?;
        attributes.SetUINT32(&MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, 1)?;
        let filename: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let sink: IMFSinkWriter = created(|output| {
            (foundation.create_writer)(
                PCWSTR(filename.as_ptr()),
                std::ptr::null_mut(),
                attributes.as_raw(),
                output,
            )
        })?;
        let output: IMFMediaType = created(|output| (foundation.create_media_type)(output))?;
        output.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
        output.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_H264)?;
        output.SetUINT32(&MF_MT_AVG_BITRATE, 8_000_000)?;
        output.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
        output.SetUINT64(&MF_MT_FRAME_SIZE, ((width as u64) << 32) | height as u64)?;
        output.SetUINT64(&MF_MT_FRAME_RATE, (30u64 << 32) | 1)?;
        output.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1u64 << 32) | 1)?;
        let stream = sink.AddStream(&output)?;
        let input: IMFMediaType = created(|output| (foundation.create_media_type)(output))?;
        input.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
        input.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_RGB32)?;
        input.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
        input.SetUINT64(&MF_MT_FRAME_SIZE, ((width as u64) << 32) | height as u64)?;
        input.SetUINT64(&MF_MT_FRAME_RATE, (30u64 << 32) | 1)?;
        input.SetUINT64(&MF_MT_PIXEL_ASPECT_RATIO, (1u64 << 32) | 1)?;
        // WGC emits top-down BGRA; an explicit positive stride preserves row order.
        input.SetUINT32(&MF_MT_DEFAULT_STRIDE, width * 4)?;
        sink.SetInputMediaType(stream, &input, None)?;
        sink.BeginWriting()?;
        Ok(Self {
            sink: Some(sink),
            foundation,
            stream,
            source_width,
            source_height,
            width,
            height,
            first_time: None,
            last_time: None,
            finished: false,
        })
    }
    pub fn write(
        &mut self,
        width: u32,
        height: u32,
        pixels: &[u8],
        source_time: f64,
    ) -> Result<(), String> {
        if (width, height) != (self.source_width, self.source_height) {
            return Err("Selected window resized; MP4 dimensions cannot change".into());
        }
        let expected = (width as usize)
            .checked_mul(height as usize)
            .and_then(|v| v.checked_mul(4))
            .ok_or("Video extent overflow")?;
        if pixels.len() != expected
            || !source_time.is_finite()
            || self.last_time.is_some_and(|last| source_time <= last)
        {
            return Err("Invalid video extent or nonmonotonic source timestamp".into());
        }
        let first = *self.first_time.get_or_insert(source_time);
        unsafe {
            let result = (|| -> windows::core::Result<()> {
                let length = self.width * self.height * 4;
                let buffer: IMFMediaBuffer =
                    created(|output| (self.foundation.create_buffer)(length, output))?;
                let mut destination = std::ptr::null_mut();
                buffer.Lock(&mut destination, None, None)?;
                for row in 0..self.height as usize {
                    std::ptr::copy_nonoverlapping(
                        pixels.as_ptr().add(row * width as usize * 4),
                        destination.add(row * self.width as usize * 4),
                        self.width as usize * 4,
                    );
                }
                buffer.Unlock()?;
                buffer.SetCurrentLength(length)?;
                let sample: IMFSample = created(|output| (self.foundation.create_sample)(output))?;
                sample.AddBuffer(&buffer)?;
                sample.SetSampleTime(((source_time - first) * 10_000_000.0).round() as i64)?;
                sample.SetSampleDuration(10_000_000 / 30)?;
                self.sink
                    .as_ref()
                    .unwrap()
                    .WriteSample(self.stream, &sample)
            })();
            result.map_err(|e| e.to_string())?;
        }
        self.last_time = Some(source_time);
        Ok(())
    }
    pub fn finish(&mut self) -> Result<(), String> {
        if !self.finished {
            self.finished = true;
            unsafe {
                self.sink
                    .as_ref()
                    .unwrap()
                    .Finalize()
                    .map_err(|e| e.to_string())?;
            }
        }
        Ok(())
    }
}
impl Drop for VideoWriter {
    fn drop(&mut self) {
        unsafe {
            if !self.finished {
                let _ = self.sink.as_ref().unwrap().Finalize();
            }
            self.sink.take();
        }
    }
}

unsafe fn created<T: Interface>(
    create: impl FnOnce(*mut *mut c_void) -> HRESULT,
) -> windows::core::Result<T> {
    let mut raw = std::ptr::null_mut();
    create(&mut raw).ok()?;
    if raw.is_null() {
        return Err(windows::core::Error::from_hresult(E_POINTER));
    }
    Ok(T::from_raw(raw))
}

struct Library(HMODULE);
impl Library {
    fn load(name: &str) -> Result<Self, String> {
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        unsafe { LoadLibraryExW(PCWSTR(wide.as_ptr()), None, LOAD_LIBRARY_SEARCH_SYSTEM32) }
            .map(Self)
            .map_err(|e| format!("Windows video recording requires Media Foundation ({name}): {e}"))
    }
    unsafe fn symbol<T: Copy>(&self, name: &CStr) -> Result<T, String> {
        let address = GetProcAddress(self.0, PCSTR(name.as_ptr().cast())).ok_or_else(|| {
            format!(
                "Media Foundation recording function unavailable: {}",
                name.to_string_lossy()
            )
        })?;
        assert_eq!(std::mem::size_of::<T>(), std::mem::size_of_val(&address));
        Ok(std::mem::transmute_copy(&address))
    }
}
impl Drop for Library {
    fn drop(&mut self) {
        unsafe {
            let _ = FreeLibrary(self.0);
        }
    }
}
type CreateObject = unsafe extern "system" fn(*mut *mut c_void) -> HRESULT;
struct MediaFoundation {
    startup: unsafe extern "system" fn(u32, u32) -> HRESULT,
    shutdown: unsafe extern "system" fn() -> HRESULT,
    create_attributes: unsafe extern "system" fn(*mut *mut c_void, u32) -> HRESULT,
    create_media_type: CreateObject,
    create_buffer: unsafe extern "system" fn(u32, *mut *mut c_void) -> HRESULT,
    create_sample: CreateObject,
    create_writer:
        unsafe extern "system" fn(PCWSTR, *mut c_void, *mut c_void, *mut *mut c_void) -> HRESULT,
    started: bool,
    initialized: bool,
    _platform: Library,
    _readwrite: Library,
}
impl MediaFoundation {
    fn new() -> Result<Self, String> {
        unsafe {
            // Optional media DLLs must never become loader prerequisites for ordinary OCR capture.
            let platform = Library::load("mfplat.dll")?;
            let readwrite = Library::load("mfreadwrite.dll")?;
            let mut api = Self {
                startup: platform.symbol(c"MFStartup")?,
                shutdown: platform.symbol(c"MFShutdown")?,
                create_attributes: platform.symbol(c"MFCreateAttributes")?,
                create_media_type: platform.symbol(c"MFCreateMediaType")?,
                create_buffer: platform.symbol(c"MFCreateMemoryBuffer")?,
                create_sample: platform.symbol(c"MFCreateSample")?,
                create_writer: readwrite.symbol(c"MFCreateSinkWriterFromURL")?,
                started: false,
                initialized: false,
                _platform: platform,
                _readwrite: readwrite,
            };
            RoInitialize(RO_INIT_MULTITHREADED).map_err(|e| e.to_string())?;
            api.initialized = true;
            (api.startup)(MF_VERSION, MFSTARTUP_FULL)
                .ok()
                .map_err(|e| e.to_string())?;
            api.started = true;
            Ok(api)
        }
    }
}
impl Drop for MediaFoundation {
    fn drop(&mut self) {
        unsafe {
            if self.started {
                let _ = (self.shutdown)();
            }
            if self.initialized {
                RoUninitialize();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_media_component_is_a_recording_error() {
        let error = match Library::load("vntts-missing-media-72f066b5.dll") {
            Ok(_) => panic!("Unexpected test library"),
            Err(error) => error,
        };
        assert!(error.contains("video recording requires Media Foundation"));
        let kernel = Library::load("kernel32.dll").unwrap();
        assert!(
            unsafe { kernel.symbol::<CreateObject>(c"VnttsMissingMediaFunction72f066b5") }.is_err()
        );
    }
}
