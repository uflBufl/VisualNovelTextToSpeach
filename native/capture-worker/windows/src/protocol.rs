use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead};
use std::sync::Arc;

pub const MAX_LINE: usize = 64 * 1024;
pub const MAX_PIXELS: usize = 32 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct Failure {
    pub code: &'static str,
    pub message: String,
}
impl Failure {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub version: u32,
    pub id: u64,
    pub op: String,
    pub session_id: String,
    pub target: Option<Target>,
    pub max_fps: Option<u32>,
    pub timeout_ms: Option<u64>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Target {
    Window { window_id: i64, pid: u32 },
    Display { display_id: i64 },
}
impl Request {
    pub fn parse(line: &[u8]) -> Result<Self, Failure> {
        let invalid = |message| Failure::new("invalid_request", message);
        let request: Self =
            serde_json::from_slice(line).map_err(|error| invalid(error.to_string()))?;
        if request.version != 1 {
            return Err(Failure::new(
                "unsupported_version",
                "Protocol version must be 1",
            ));
        }
        if request.id == 0 || !valid_session(&request.session_id) {
            return Err(invalid(
                "Positive request id and UUID session_id are required".into(),
            ));
        }
        if request.max_fps.is_some_and(|fps| !(1..=60).contains(&fps)) {
            return Err(invalid("max_fps must be between 1 and 60".into()));
        }
        if request
            .timeout_ms
            .is_some_and(|timeout| timeout == 0 || timeout > 10_000)
        {
            return Err(invalid("timeout_ms must be between 1 and 10000".into()));
        }
        match request.op.as_str() {
            "open" if request.target.is_some() && request.timeout_ms.is_none() => {}
            "capture" if request.target.is_none() && request.max_fps.is_none() => {}
            "close"
                if request.target.is_none()
                    && request.max_fps.is_none()
                    && request.timeout_ms.is_none() => {}
            _ => {
                return Err(invalid(
                    "Unknown operation or fields invalid for operation".into(),
                ))
            }
        }
        match &request.target {
            Some(Target::Window { window_id, pid }) if *window_id <= 0 || *pid == 0 => {
                return Err(invalid("Invalid HWND or PID".into()))
            }
            Some(Target::Display { display_id }) if *display_id < 0 => {
                return Err(invalid("Invalid HMONITOR".into()))
            }
            _ => {}
        }
        Ok(request)
    }
}
fn valid_session(id: &str) -> bool {
    id.len() == 36
        && id.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

pub struct Response {
    pub header: Value,
    pub pixels: Arc<Vec<u8>>,
}
impl Response {
    pub fn success(id: u64, result: Value, pixels: Arc<Vec<u8>>) -> Self {
        Self {
            header: json!({"version":1,"id":id,"ok":true,"payload_bytes":pixels.len(),"result":result}),
            pixels,
        }
    }
    pub fn failure(id: u64, error: Failure) -> Self {
        Self {
            header: json!({"version":1,"id":id,"ok":false,"payload_bytes":0,"error":{"code":error.code,"message":error.message}}),
            pixels: Arc::new(Vec::new()),
        }
    }
}

pub fn read_line(input: &mut impl BufRead) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    loop {
        let buffer = input.fill_buf()?;
        if buffer.is_empty() {
            return if line.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unterminated request",
                ))
            };
        }
        let length = buffer
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|index| index + 1)
            .unwrap_or(buffer.len());
        // An oversized or unterminated request terminates the stream; never allocate or drain unbounded input.
        if line.len() + length > MAX_LINE {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "request exceeds 64 KiB",
            ));
        }
        line.extend_from_slice(&buffer[..length]);
        input.consume(length);
        if line.last() == Some(&b'\n') {
            return Ok(Some(line));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_unbounded_lines_and_invalid_contracts() {
        assert!(read_line(&mut &vec![b'x'; MAX_LINE + 1][..]).is_err());
        assert!(read_line(&mut &b"{}"[..]).is_err());
        let base = json!({"version":1,"id":1,"session_id":"12345678-1234-1234-1234-123456789abc","op":"capture"});
        assert!(Request::parse(base.to_string().as_bytes()).is_ok());
        for (field, value) in [
            ("version", json!(2)),
            ("id", json!(0)),
            ("timeout_ms", json!(10001)),
            ("session_id", json!("other")),
            ("op", json!("fallback")),
        ] {
            let mut bad = base.clone();
            bad[field] = value;
            assert!(Request::parse(bad.to_string().as_bytes()).is_err());
        }
    }
    #[test]
    fn frame_rate_requires_an_integer_between_one_and_sixty() {
        let base = json!({"version":1,"id":1,"session_id":"12345678-1234-1234-1234-123456789abc","op":"open","target":{"kind":"display","display_id":0}});
        for fps in [json!(1), json!(10), json!(60)] {
            let mut request = base.clone();
            request["max_fps"] = fps;
            assert!(Request::parse(request.to_string().as_bytes()).is_ok());
        }
        for fps in [
            json!(0),
            json!(61),
            json!(-1),
            json!(1.5),
            json!(1.0),
            json!(true),
        ] {
            let mut request = base.clone();
            request["max_fps"] = fps;
            assert!(Request::parse(request.to_string().as_bytes()).is_err());
        }
    }
    #[test]
    fn framing_has_exact_byte_count() {
        let result = Response::success(7, json!({}), Arc::new(vec![0, 10, 255]));
        assert_eq!(result.header["payload_bytes"], 3);
        assert_eq!(result.header["id"], 7);
        assert_eq!(result.pixels.as_slice(), &[0, 10, 255]);
    }
}
