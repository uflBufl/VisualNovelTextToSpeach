use crate::protocol::{Failure, MAX_PIXELS};

pub fn dimensions(width: i32, height: i32) -> Result<(usize, usize), Failure> {
    if width <= 0 || height <= 0 {
        return Err(Failure::new(
            "invalid_dimensions",
            "Capture dimensions must be positive",
        ));
    }
    let (width, height) = (width as usize, height as usize);
    if width
        .checked_mul(height)
        .is_none_or(|pixels| pixels > MAX_PIXELS)
    {
        return Err(Failure::new(
            "frame_too_large",
            "Capture exceeds 32 megapixels / 128 MiB BGRA8",
        ));
    }
    Ok((width, height))
}

pub fn client_rect(
    surface: [i32; 4],
    client: [i32; 4],
    width: i32,
    height: i32,
) -> Result<[i32; 4], Failure> {
    let x = client[0] - surface[0];
    let y = client[1] - surface[1];
    if x < 0
        || y < 0
        || client[2] <= 0
        || client[3] <= 0
        || x.checked_add(client[2]).is_none_or(|right| right > width)
        || y.checked_add(client[3])
            .is_none_or(|bottom| bottom > height)
    {
        return Err(Failure::new(
            "layout_changed",
            "Physical client rectangle is outside captured surface",
        ));
    }
    Ok([x, y, client[2], client[3]])
}

/// scRGB linear Rec.709 (1.0 = 80 nit) to deterministic SDR sRGB.
/// Fixed exposure plus a continuous peak-preserving shoulder avoids per-frame exposure pumping.
pub fn convert_rgba16f(
    input: &[u8],
    pitch: usize,
    width: usize,
    height: usize,
) -> Result<Vec<u8>, Failure> {
    let (width, height) = dimensions(
        i32::try_from(width).unwrap_or(-1),
        i32::try_from(height).unwrap_or(-1),
    )?;
    if pitch < width * 8
        || pitch
            .checked_mul(height)
            .is_none_or(|size| input.len() < size)
    {
        return Err(Failure::new(
            "invalid_stride",
            "Mapped float texture has invalid RowPitch or length",
        ));
    }
    let mut output = vec![0; width * height * 4];
    for y in 0..height {
        for x in 0..width {
            let offset = y * pitch + x * 8;
            let mut rgb = [0.0_f32; 3];
            for (channel, value) in rgb.iter_mut().enumerate() {
                let index = offset + channel * 2;
                *value = half(u16::from_le_bytes([input[index], input[index + 1]]));
                if !value.is_finite() {
                    *value = 0.0;
                }
                *value = value.max(0.0);
            }
            let peak = rgb.into_iter().fold(0.0_f32, f32::max);
            // Fixed global Reinhard mapping: 80-nit scRGB white maps to 0.8 linear SDR.
            let scale = 1.0 / (peak + 0.25);
            let destination = (y * width + x) * 4;
            for channel in 0..3 {
                let linear = (rgb[2 - channel] * scale).clamp(0.0, 1.0);
                let srgb = if linear <= 0.0031308 {
                    12.92 * linear
                } else {
                    1.055 * linear.powf(1.0 / 2.4) - 0.055
                };
                output[destination + channel] = (srgb * 255.0).round() as u8;
            }
            output[destination + 3] = 255;
        }
    }
    Ok(output)
}
fn half(bits: u16) -> f32 {
    let sign = if bits & 0x8000 == 0 { 1.0 } else { -1.0 };
    let exponent = (bits >> 10) & 31;
    let fraction = (bits & 1023) as f32;
    sign * match exponent {
        0 => fraction * 2.0_f32.powi(-24),
        31 => {
            if fraction == 0.0 {
                f32::INFINITY
            } else {
                f32::NAN
            }
        }
        _ => (1.0 + fraction / 1024.0) * 2.0_f32.powi(exponent as i32 - 15),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn copies_only_content_and_respects_padded_row_pitch() {
        let mut data = vec![255; 32];
        for row in 0..2 {
            data[row * 16..row * 16 + 8].copy_from_slice(&[0, 0x3c, 0, 0, 0, 0, 0, 0x3c]);
        }
        assert_eq!(
            convert_rgba16f(&data, 16, 1, 2).unwrap(),
            vec![0, 0, 231, 255, 0, 0, 231, 255]
        );
        assert!(convert_rgba16f(&data, 7, 1, 2).is_err());
        assert!(convert_rgba16f(&data[..20], 16, 1, 2).is_err());
    }
    #[test]
    fn bounds_crop_and_dimensions() {
        assert_eq!(
            client_rect([-10, -20, 100, 100], [-5, 0, 90, 70], 100, 100).unwrap(),
            [5, 20, 90, 70]
        );
        assert!(client_rect([0, 0, 100, 100], [-1, 0, 80, 80], 100, 100).is_err());
        assert!(dimensions(0, 1).is_err());
        assert!(dimensions(i32::MAX, i32::MAX).is_err());
    }
    #[test]
    fn hdr_highlights_are_compressed_not_channel_clipped() {
        let result = convert_rgba16f(&[0, 0x49, 0, 0x45, 0, 0x41, 0, 0x3c], 8, 1, 1).unwrap();
        assert!(result[2] < 255 && result[2] > result[1] && result[1] > result[0]);
        assert_eq!(half(0x3c00), 1.0);
        assert_eq!(half(1), 2.0_f32.powi(-24));
    }
}
