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

pub fn fit_bgra(
    input: &[u8],
    width: u32,
    height: u32,
    output: &mut [u8],
    output_width: u32,
    output_height: u32,
) -> Result<(), Failure> {
    let (width, height) = dimensions(
        i32::try_from(width).unwrap_or(-1),
        i32::try_from(height).unwrap_or(-1),
    )?;
    let (output_width, output_height) = dimensions(
        i32::try_from(output_width).unwrap_or(-1),
        i32::try_from(output_height).unwrap_or(-1),
    )?;
    if input.len() != width * height * 4 || output.len() != output_width * output_height * 4 {
        return Err(Failure::new(
            "invalid_extent",
            "Video buffer has invalid length",
        ));
    }
    if (width, height) == (output_width, output_height) {
        output.copy_from_slice(input);
        return Ok(());
    }
    let (fitted_width, fitted_height) = if width * output_height > height * output_width {
        (output_width, (height * output_width / width).max(1))
    } else {
        ((width * output_height / height).max(1), output_height)
    };
    // Clear the entire canvas on every resize so previous window content cannot remain in bars.
    for pixel in output.chunks_exact_mut(4) {
        pixel.copy_from_slice(&[0, 0, 0, 255]);
    }
    let left = (output_width - fitted_width) / 2;
    let top = (output_height - fitted_height) / 2;
    let axis = |index: usize, source: usize, fitted: usize| {
        let position = ((index as f64 + 0.5) * source as f64 / fitted as f64 - 0.5)
            .clamp(0.0, (source - 1) as f64);
        let low = position as usize;
        (
            low,
            (low + 1).min(source - 1),
            (position - low as f64) as f32,
        )
    };
    let columns: Vec<_> = (0..fitted_width)
        .map(|x| axis(x, width, fitted_width))
        .collect();
    for y in 0..fitted_height {
        let (y0, y1, y_weight) = axis(y, height, fitted_height);
        let output_row = ((y + top) * output_width + left) * 4;
        for (x, &(x0, x1, x_weight)) in columns.iter().enumerate() {
            let destination = output_row + x * 4;
            for channel in 0..4 {
                let upper_left = input[(y0 * width + x0) * 4 + channel] as f32;
                let upper_right = input[(y0 * width + x1) * 4 + channel] as f32;
                let lower_left = input[(y1 * width + x0) * 4 + channel] as f32;
                let lower_right = input[(y1 * width + x1) * 4 + channel] as f32;
                let upper = upper_left + (upper_right - upper_left) * x_weight;
                let lower = lower_left + (lower_right - lower_left) * x_weight;
                output[destination + channel] = (upper + (lower - upper) * y_weight).round() as u8;
            }
        }
    }
    Ok(())
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

    fn gray_pixels(values: &[u8]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| [*value, *value, *value, 255])
            .collect()
    }

    #[test]
    fn video_fit_copies_unchanged_dimensions() {
        let input: Vec<u8> = (0..36).collect();
        let mut output = vec![99; input.len()];
        fit_bgra(&input, 3, 3, &mut output, 3, 3).unwrap();
        assert_eq!(output, input);
    }

    #[test]
    fn video_fit_scales_up_and_down_without_changing_the_canvas() {
        let input = gray_pixels(&[0, 40, 80, 120]);
        let mut enlarged = vec![99; 4 * 4 * 4];
        fit_bgra(&input, 2, 2, &mut enlarged, 4, 4).unwrap();
        assert_eq!(
            enlarged,
            gray_pixels(&[0, 10, 30, 40, 20, 30, 50, 60, 60, 70, 90, 100, 80, 90, 110, 120])
        );
        let mut reduced = vec![99; input.len()];
        fit_bgra(&enlarged, 4, 4, &mut reduced, 2, 2).unwrap();
        assert_eq!(reduced, gray_pixels(&[15, 45, 75, 105]));
    }

    #[test]
    fn video_fit_replaces_previous_content_and_bars_after_aspect_changes() {
        let mut output = vec![99; 4 * 4 * 4];
        fit_bgra(
            &gray_pixels(&[1, 2, 3, 4, 5, 6, 7, 8]),
            2,
            4,
            &mut output,
            4,
            4,
        )
        .unwrap();
        assert_eq!(
            output,
            gray_pixels(&[0, 1, 2, 0, 0, 3, 4, 0, 0, 5, 6, 0, 0, 7, 8, 0])
        );
        fit_bgra(
            &gray_pixels(&[9, 10, 11, 12, 13, 14, 15, 16]),
            4,
            2,
            &mut output,
            4,
            4,
        )
        .unwrap();
        assert_eq!(
            output,
            gray_pixels(&[0, 0, 0, 0, 9, 10, 11, 12, 13, 14, 15, 16, 0, 0, 0, 0])
        );
        fit_bgra(&gray_pixels(&[17; 16]), 4, 4, &mut output, 4, 4).unwrap();
        assert_eq!(output, gray_pixels(&[17; 16]));
    }

    #[test]
    fn video_fit_handles_odd_extents_and_single_pixel_fitted_axes() {
        let mut output = vec![99; 4 * 2 * 4];
        fit_bgra(&gray_pixels(&[1; 15]), 5, 3, &mut output, 4, 2).unwrap();
        assert_eq!(output, gray_pixels(&[1, 1, 1, 0, 1, 1, 1, 0]));
        fit_bgra(&gray_pixels(&[2; 14]), 1, 14, &mut output, 4, 2).unwrap();
        assert_eq!(output, gray_pixels(&[0, 2, 0, 0, 0, 2, 0, 0]));
        let mut square = vec![99; 2 * 2 * 4];
        fit_bgra(
            &gray_pixels(&[0, 10, 20, 30, 40, 50, 60, 70, 80]),
            3,
            3,
            &mut square,
            2,
            2,
        )
        .unwrap();
        assert_eq!(square, gray_pixels(&[10, 25, 55, 70]));
    }

    #[test]
    fn video_fit_rejects_invalid_extents_without_touching_output() {
        let mut output = vec![99; 16];
        for (input, width, height) in [
            (&[1; 16][..], 0, 2),
            (&[1; 15][..], 2, 2),
            (&[1; 16][..], u32::MAX, 2),
        ] {
            assert!(fit_bgra(input, width, height, &mut output, 2, 2).is_err());
            assert_eq!(output, vec![99; 16]);
        }
        assert!(fit_bgra(&[1; 16], 2, 2, &mut output[..15], 2, 2).is_err());
        assert_eq!(output, vec![99; 16]);
    }

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
