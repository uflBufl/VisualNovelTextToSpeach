use windows::{
    core::Interface,
    Graphics::DirectX::DirectXPixelFormat,
    Win32::Graphics::{
        Dxgi::{
            Common::{DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709, DXGI_COLOR_SPACE_TYPE},
            CreateDXGIFactory1, IDXGIFactory1, IDXGIOutput6,
        },
        Gdi::HMONITOR,
    },
};

pub(crate) struct DisplayColor {
    factory: Option<IDXGIFactory1>,
    monitor: HMONITOR,
    format: DirectXPixelFormat,
}

impl DisplayColor {
    pub(crate) fn new(monitor: HMONITOR) -> Self {
        let factory = unsafe { CreateDXGIFactory1().ok() };
        let format = capture_format(output_color_space(factory.as_ref(), monitor));
        Self {
            factory,
            monitor,
            format,
        }
    }

    pub(crate) fn format(&self) -> DirectXPixelFormat {
        self.format
    }

    pub(crate) fn refresh(&mut self, monitor: HMONITOR) -> DirectXPixelFormat {
        let stale = self
            .factory
            .as_ref()
            .is_some_and(|factory| !unsafe { factory.IsCurrent() }.as_bool());
        if self.monitor != monitor || stale {
            // Refresh the factory after display changes, including HDR toggles.
            if stale || self.factory.is_none() {
                self.factory = unsafe { CreateDXGIFactory1().ok() };
            }
            self.monitor = monitor;
            self.format = capture_format(output_color_space(self.factory.as_ref(), monitor));
        }
        self.format
    }
}

fn capture_format(space: Option<DXGI_COLOR_SPACE_TYPE>) -> DirectXPixelFormat {
    if space == Some(DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709) {
        DirectXPixelFormat::B8G8R8A8UIntNormalized
    } else {
        // Preserve FP16 capture when HDR status cannot be established.
        DirectXPixelFormat::R16G16B16A16Float
    }
}

fn output_color_space(
    factory: Option<&IDXGIFactory1>,
    monitor: HMONITOR,
) -> Option<DXGI_COLOR_SPACE_TYPE> {
    let factory = factory?;
    if monitor.0.is_null() {
        return None;
    }
    unsafe {
        for adapter_index in 0.. {
            let Ok(adapter) = factory.EnumAdapters1(adapter_index) else {
                break;
            };
            for output_index in 0.. {
                let Ok(output) = adapter.EnumOutputs(output_index) else {
                    break;
                };
                let Ok(desc) = output
                    .cast::<IDXGIOutput6>()
                    .and_then(|output| output.GetDesc1())
                else {
                    continue;
                };
                if desc.Monitor == monitor {
                    return Some(desc.ColorSpace);
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::Graphics::Dxgi::Common::DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;

    #[test]
    fn only_confirmed_sdr_uses_direct_bgra() {
        assert_eq!(
            capture_format(Some(DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709)),
            DirectXPixelFormat::B8G8R8A8UIntNormalized
        );
        for mode in [Some(DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020), None] {
            assert_eq!(capture_format(mode), DirectXPixelFormat::R16G16B16A16Float);
        }
    }
}
