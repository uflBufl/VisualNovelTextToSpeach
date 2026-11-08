#[cfg(any(target_os = "macos", windows))]
use std::ffi::c_void;
#[cfg(any(target_os = "macos", windows))]
use std::io::Write;

/// Owns residency only; the allocation remains owned by the callback state.
pub(super) struct ResidentBuffer {
    #[cfg(any(target_os = "macos", windows))]
    address: *const u8,
    #[cfg(any(target_os = "macos", windows))]
    bytes: usize,
    #[cfg(windows)]
    previous_working_set: Option<(usize, usize)>,
}

// Moving or sharing the lease does not access its allocation. Only Drop releases it.
unsafe impl Send for ResidentBuffer {}
unsafe impl Sync for ResidentBuffer {}

impl ResidentBuffer {
    /// The allocation must stay mapped at this address until the guard is dropped.
    /// Drop must follow native stream closure, with no overlapping residency leases.
    pub(super) unsafe fn lock(address: *const u8, bytes: usize) -> Result<Self, String> {
        if bytes != 0 && address.is_null() {
            return Err("cannot pin a null sample buffer".into());
        }
        (address as usize)
            .checked_add(bytes)
            .ok_or("sample buffer address overflow")?;
        #[cfg(any(target_os = "macos", windows))]
        {
            let (address, bytes) = page_range(address as usize, bytes, native::page_size()?)?;
            let address = address as *const u8;
            #[cfg(target_os = "macos")]
            if bytes != 0 && native::mlock(address.cast(), bytes) != 0 {
                return Err(format!(
                    "mlock sample buffer: {}",
                    std::io::Error::last_os_error()
                ));
            }
            #[cfg(windows)]
            let previous_working_set = if bytes == 0 {
                None
            } else {
                native::lock(address, bytes)?
            };
            Ok(Self {
                address,
                bytes,
                #[cfg(windows)]
                previous_working_set,
            })
        }
        #[cfg(not(any(target_os = "macos", windows)))]
        Ok(Self {})
    }
}

impl Drop for ResidentBuffer {
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        if self.bytes != 0 && unsafe { native::munlock(self.address.cast(), self.bytes) } != 0 {
            let _ = writeln!(
                std::io::stderr(),
                "munlock sample buffer: {}",
                std::io::Error::last_os_error()
            );
        }
        #[cfg(windows)]
        {
            if self.bytes != 0 {
                if let Err(error) = unsafe { native::unlock(self.address, self.bytes) } {
                    let _ = writeln!(std::io::stderr(), "VirtualUnlock sample buffer: {error}");
                }
            }
            if let Some(limits) = self.previous_working_set {
                if let Err(error) = unsafe { native::set_working_set(limits) } {
                    let _ = writeln!(
                        std::io::stderr(),
                        "restore sample buffer working set: {error}"
                    );
                }
            }
        }
    }
}

#[cfg(any(target_os = "macos", windows, test))]
fn page_range(address: usize, bytes: usize, page: usize) -> Result<(usize, usize), String> {
    if !page.is_power_of_two() {
        return Err("invalid native page size".into());
    }
    if bytes == 0 {
        return Ok((address, 0));
    }
    let start = address & !(page - 1);
    let end = address
        .checked_add(bytes)
        .and_then(|end| end.checked_add(page - 1))
        .map(|end| end & !(page - 1))
        .ok_or("sample buffer page range overflow")?;
    Ok((start, end - start))
}

#[cfg(target_os = "macos")]
mod native {
    use super::c_void;
    use std::ffi::c_int;

    extern "C" {
        pub(super) fn mlock(address: *const c_void, bytes: usize) -> c_int;
        pub(super) fn munlock(address: *const c_void, bytes: usize) -> c_int;
        fn getpagesize() -> c_int;
    }

    pub(super) fn page_size() -> Result<usize, String> {
        let page = unsafe { getpagesize() };
        usize::try_from(page).map_err(|_| "invalid native page size".into())
    }
}

#[cfg(any(windows, test))]
fn increased_working_set(
    (minimum, maximum): (usize, usize),
    bytes: usize,
) -> Result<(usize, usize), String> {
    // Keep the existing minimum as headroom for the OS's lock quota overhead.
    let minimum = minimum
        .checked_add(bytes)
        .ok_or("sample buffer working set overflow")?;
    Ok((minimum, maximum.max(minimum)))
}

#[cfg(windows)]
mod native {
    use super::{c_void, increased_working_set};
    use std::io;

    const ERROR_WORKING_SET_QUOTA: i32 = 1453;
    const ERROR_NOT_LOCKED: i32 = 158;

    #[repr(C)]
    struct SystemInfo {
        architecture: u32,
        page_size: u32,
        minimum_address: *mut c_void,
        maximum_address: *mut c_void,
        processor_mask: usize,
        processor_count: u32,
        processor_type: u32,
        allocation_granularity: u32,
        processor_level: u16,
        processor_revision: u16,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn VirtualLock(address: *const c_void, bytes: usize) -> i32;
        fn VirtualUnlock(address: *const c_void, bytes: usize) -> i32;
        fn GetCurrentProcess() -> *mut c_void;
        fn GetProcessWorkingSetSize(
            process: *mut c_void,
            minimum: *mut usize,
            maximum: *mut usize,
        ) -> i32;
        fn SetProcessWorkingSetSize(process: *mut c_void, minimum: usize, maximum: usize) -> i32;
        fn GetSystemInfo(info: *mut SystemInfo);
    }

    pub(super) fn page_size() -> Result<usize, String> {
        let mut info = std::mem::MaybeUninit::<SystemInfo>::uninit();
        unsafe {
            GetSystemInfo(info.as_mut_ptr());
            Ok(info.assume_init().page_size as usize)
        }
    }

    pub(super) unsafe fn working_set() -> Result<(usize, usize), String> {
        let (mut minimum, mut maximum) = (0, 0);
        if GetProcessWorkingSetSize(GetCurrentProcess(), &mut minimum, &mut maximum) == 0 {
            return Err(format!(
                "GetProcessWorkingSetSize: {}",
                io::Error::last_os_error()
            ));
        }
        Ok((minimum, maximum))
    }

    pub(super) unsafe fn set_working_set((minimum, maximum): (usize, usize)) -> Result<(), String> {
        if SetProcessWorkingSetSize(GetCurrentProcess(), minimum, maximum) == 0 {
            return Err(format!(
                "SetProcessWorkingSetSize: {}",
                io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    pub(super) unsafe fn unlock(address: *const u8, bytes: usize) -> Result<(), io::Error> {
        if VirtualUnlock(address.cast(), bytes) == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    unsafe fn rollback_lock(address: *const u8, bytes: usize, error: &mut String) {
        if let Err(rollback) = unlock(address, bytes) {
            // A failed acquisition can leave a mix of locked and unlocked pages.
            if rollback.raw_os_error() != Some(ERROR_NOT_LOCKED) {
                error.push_str(&format!("; VirtualUnlock rollback: {rollback}"));
            }
        }
    }

    pub(super) unsafe fn lock(
        address: *const u8,
        bytes: usize,
    ) -> Result<Option<(usize, usize)>, String> {
        if VirtualLock(address.cast(), bytes) != 0 {
            return Ok(None);
        }
        let first_error = io::Error::last_os_error();
        let mut error = format!("VirtualLock sample buffer: {first_error}");
        rollback_lock(address, bytes, &mut error);
        if first_error.raw_os_error() != Some(ERROR_WORKING_SET_QUOTA) {
            return Err(error);
        }
        let previous = working_set().map_err(|failure| format!("{error}; {failure}"))?;
        let enlarged = increased_working_set(previous, bytes)
            .map_err(|failure| format!("{error}; {failure}"))?;
        set_working_set(enlarged).map_err(|failure| format!("{error}; {failure}"))?;
        if VirtualLock(address.cast(), bytes) != 0 {
            return Ok(Some(previous));
        }
        error.push_str(&format!(
            "; retry VirtualLock: {}",
            io::Error::last_os_error()
        ));
        rollback_lock(address, bytes, &mut error);
        if let Err(restore) = set_working_set(previous) {
            error.push_str(&format!("; restore working set: {restore}"));
        }
        Err(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_range_covers_both_edges_and_rejects_overflow() {
        assert_eq!(page_range(4095, 2, 4096).unwrap(), (0, 8192));
        assert_eq!(page_range(4096, 4096, 4096).unwrap(), (4096, 4096));
        assert_eq!(page_range(7, 0, 4096).unwrap(), (7, 0));
        assert!(page_range(usize::MAX - 1, 1, 4096).is_err());
        assert!(page_range(0, 1, 0).is_err());
    }

    #[test]
    fn working_set_growth_preserves_headroom_and_maximum() {
        assert_eq!(
            increased_working_set((4096, 16384), 8192).unwrap(),
            (12288, 16384)
        );
        assert_eq!(
            increased_working_set((4096, 8192), 8192).unwrap(),
            (12288, 12288)
        );
        assert!(increased_working_set((usize::MAX, usize::MAX), 1).is_err());
    }

    #[test]
    fn empty_buffer_needs_no_residency() {
        drop(unsafe { ResidentBuffer::lock(std::ptr::null(), 0) }.unwrap());
        assert!(unsafe { ResidentBuffer::lock(std::ptr::null(), 1) }.is_err());
        assert!(unsafe { ResidentBuffer::lock(usize::MAX as *const u8, 1) }.is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn native_lock_accepts_unaligned_allocation_and_releases_it() {
        let mut samples = vec![0u8; 8193];
        let lease = unsafe { ResidentBuffer::lock(samples.as_ptr().add(1), samples.len() - 1) }
            .expect("native sample buffer residency");
        samples[1] = 17;
        drop(lease);
        assert_eq!(samples[1], 17);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn native_lock_limit_error_is_reported() {
        const CHILD: &str = "VNTTS_TEST_MEMLOCK_LIMIT";
        if std::env::var_os(CHILD).is_some() {
            extern "C" {
                fn setrlimit(resource: i32, limits: *const u64) -> i32;
            }
            // Only the isolated child lowers RLIMIT_MEMLOCK; parallel tests keep their limit.
            assert_eq!(unsafe { setrlimit(6, [0u64, 0].as_ptr()) }, 0);
            let samples = vec![0u8; 32768];
            let error = unsafe { ResidentBuffer::lock(samples.as_ptr(), samples.len()) }
                .err()
                .expect("zero memlock limit must reject residency");
            assert!(error.contains("mlock sample buffer:"), "{error}");
            assert!(error.contains("os error 35"), "{error}");
            return;
        }
        assert!(std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "resident::tests::native_lock_limit_error_is_reported",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .status()
            .unwrap()
            .success());
    }

    #[cfg(windows)]
    #[test]
    fn native_large_lock_restores_working_set() {
        let before = unsafe { native::working_set() }.unwrap();
        let samples = vec![0u8; before.0 + 1024 * 1024];
        let lease = unsafe { ResidentBuffer::lock(samples.as_ptr().add(1), samples.len() - 1) }
            .expect("working set quota increase for large sample buffer");
        assert!(lease.previous_working_set.is_some());
        drop(lease);
        assert_eq!(unsafe { native::working_set() }.unwrap(), before);
        #[link(name = "kernel32")]
        extern "system" {
            fn VirtualAlloc(
                address: *mut c_void,
                bytes: usize,
                kind: u32,
                protect: u32,
            ) -> *mut c_void;
            fn VirtualFree(address: *mut c_void, bytes: usize, kind: u32) -> i32;
        }
        // Committed PAGE_NOACCESS pages are mapped but cannot be locked.
        let inaccessible = unsafe { VirtualAlloc(std::ptr::null_mut(), 4096, 0x3000, 0x01) };
        assert!(!inaccessible.is_null());
        let error = unsafe { ResidentBuffer::lock(inaccessible.cast(), 4096) }.err();
        assert_ne!(unsafe { VirtualFree(inaccessible, 0, 0x8000) }, 0);
        let error = error.expect("inaccessible pages must reject residency");
        assert!(error.contains("VirtualLock sample buffer:"), "{error}");
        assert!(error.contains("os error"), "{error}");
        assert_eq!(unsafe { native::working_set() }.unwrap(), before);
    }
}
