use crate::{
    capture::Worker,
    protocol::{Failure, FrameBuffer, Request},
};
use serde_json::Value;
use windows::{
    core::PCWSTR,
    Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::Memory::{
            MapViewOfFile, OpenFileMappingW, UnmapViewOfFile, FILE_MAP_WRITE,
            MEMORY_MAPPED_VIEW_ADDRESS,
        },
    },
};

struct Mapping {
    handle: HANDLE,
    view: MEMORY_MAPPED_VIEW_ADDRESS,
    capacity: usize,
}
impl Mapping {
    fn open(spec: &FrameBuffer) -> Result<Self, Failure> {
        let name: Vec<u16> = spec.name.encode_utf16().chain(Some(0)).collect();
        unsafe {
            let handle = OpenFileMappingW(FILE_MAP_WRITE.0, false, PCWSTR(name.as_ptr()))
                .map_err(|error| Failure::new("frame_buffer_error", error.to_string()))?;
            let view = MapViewOfFile(handle, FILE_MAP_WRITE, 0, 0, spec.capacity);
            if view.Value.is_null() {
                let error = windows::core::Error::from_thread();
                let _ = CloseHandle(handle);
                return Err(Failure::new("frame_buffer_error", error.to_string()));
            }
            Ok(Self {
                handle,
                view,
                capacity: spec.capacity,
            })
        }
    }
    fn write(&mut self, pixels: &[u8]) -> Result<(), Failure> {
        if pixels.len() > self.capacity {
            return Err(Failure::new(
                "frame_buffer_error",
                "Captured frame exceeds shared frame buffer capacity",
            ));
        }
        unsafe {
            std::ptr::copy_nonoverlapping(pixels.as_ptr(), self.view.Value.cast(), pixels.len());
        }
        Ok(())
    }
}
impl Drop for Mapping {
    fn drop(&mut self) {
        unsafe {
            let _ = UnmapViewOfFile(self.view);
            let _ = CloseHandle(self.handle);
        }
    }
}

#[derive(Default)]
pub struct CaptureTransport {
    worker: Worker,
    buffer: Option<Mapping>,
}
impl CaptureTransport {
    pub fn execute(&mut self, request: Request) -> Result<(Value, usize), Failure> {
        let next_buffer = if request.op == "open" {
            Some(Mapping::open(request.frame_buffer.as_ref().ok_or_else(
                || Failure::new("invalid_request", "open requires frame_buffer"),
            )?)?)
        } else {
            None
        };
        let closing = request.op == "close";
        let (metadata, pixels) = self.worker.execute(request)?;
        if let Some(buffer) = next_buffer {
            self.buffer = Some(buffer);
        }
        let frame_bytes = pixels.len();
        if frame_bytes > 0 {
            self.buffer
                .as_mut()
                .ok_or_else(|| Failure::new("frame_buffer_error", "Frame buffer is closed"))?
                .write(&pixels)?;
        }
        if closing {
            self.buffer = None;
        }
        Ok((metadata, frame_bytes))
    }

    pub fn must_exit(&self) -> bool {
        self.worker.must_exit
    }
    pub fn close(&mut self) -> bool {
        // Only this transport writes the mapping, after the native producer returns owned pixels.
        let stopped = self.worker.close();
        self.buffer = None;
        stopped
    }
}
impl Drop for CaptureTransport {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::{
        Foundation::INVALID_HANDLE_VALUE,
        System::Memory::{CreateFileMappingW, FILE_MAP_READ, PAGE_READWRITE},
    };

    #[test]
    fn named_mapping_writes_exact_pixels_and_rejects_overflow_before_writing() {
        let name = format!("vntts_frame_buffer_test_{}", std::process::id());
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        unsafe {
            let owner = CreateFileMappingW(
                INVALID_HANDLE_VALUE,
                None,
                PAGE_READWRITE,
                0,
                16,
                PCWSTR(wide.as_ptr()),
            )
            .unwrap();
            let owner = Mapping {
                handle: owner,
                view: MapViewOfFile(owner, FILE_MAP_READ, 0, 0, 16),
                capacity: 16,
            };
            assert!(!owner.view.Value.is_null());
            let mut attached = Mapping::open(&FrameBuffer {
                name: name.clone(),
                capacity: 16,
            })
            .unwrap();
            attached.write(&[1, 2, 3, 255]).unwrap();
            let bytes = std::slice::from_raw_parts(owner.view.Value.cast::<u8>(), 16);
            assert_eq!(&bytes[..4], &[1, 2, 3, 255]);
            assert_eq!(&bytes[4..], &[0; 12]);
            assert_eq!(
                attached.write(&[42; 17]).unwrap_err().code,
                "frame_buffer_error"
            );
            assert_eq!(&bytes[..4], &[1, 2, 3, 255]);
            drop(attached);
            drop(owner);
            assert!(Mapping::open(&FrameBuffer { name, capacity: 16 }).is_err());
        }
    }

    #[test]
    fn missing_named_mapping_is_a_typed_error() {
        let error = Mapping::open(&FrameBuffer {
            name: format!("vntts_missing_frame_buffer_{}", std::process::id()),
            capacity: 1,
        });
        assert_eq!(error.err().unwrap().code, "frame_buffer_error");
    }
}
